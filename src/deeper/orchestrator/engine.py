"""The deterministic spine: an explicit state machine over the pipeline
(design P8, §8). Code decides sequencing, budgets, and stop rules; LLM agents
— reached only through stage `execute()` and the dispatch layer — decide
content.

The machine's nodes are S0–S8 plus the three gates, PAUSED_ATTENTION, and
DONE. Nothing new is persisted for them: `RunState` already encodes every node
as the (stage, status, pending_gate) triple, and `node_of` derives the node
from it. Crash safety is therefore free — state is files, every transition is
a git commit, and `run()` always starts by re-deriving where it is.

One `run()` step per node:
- stage node → skip if `is_complete` (idempotent re-entry), else validate
  inputs → execute → validate declared outputs → commit → advance;
- gate node → template written on entry (never overwritten); resume validates
  the decision file and either advances, loops back, or re-pauses with the
  validation message;
- AgentOutputInvalid (schema retries exhausted, §6) → PAUSED_ATTENTION;
- NotImplementedYet → clean report, state untouched, resumable later.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import (
    AgentDispatchFailed,
    AgentOutputInvalid,
    SpendCapExceeded,
    UsageLimitReached,
    create_dispatcher,
)
from deeper.config import RunConfig
from deeper.schemas import (
    FrameCheck,
    GateCDecision,
    GateName,
    GateStatus,
    RunState,
    RunStatus,
    Stage,
)
from deeper.stages import STAGES, NotImplementedYet, StageBase, StageContext, StageInterrupted
from deeper.stages.s7_tournament import FRAME_CHECK_PATH
from deeper.workspace import Workspace, WorkspaceError

from .gate_c_loops import (
    apply_preference_feedback,
    run_evidence_challenges,
    validate_gate_c_decision,
)
from .gates import (
    _TEMPLATE_C_CAPPED,
    GATE_AFTER_STAGE,
    GATE_SPECS,
    GateSpec,
    read_decision,
    write_template_if_absent,
)
from .redivergence import run_mini_loop
from .rerun import invalidate


class EngineError(Exception):
    """A machine invariant broke — a code bug or a hand-mangled workspace,
    never an agent content problem (those pause the run instead)."""


class Node(StrEnum):
    """The state machine's nodes. Stage nodes share Stage's values; gate nodes
    share GateName's; the two terminals share RunStatus's."""

    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"
    S7 = "S7"
    S8 = "S8"
    GATE_A = "gate-a"
    GATE_B = "gate-b"
    GATE_C = "gate-c"
    PAUSED_ATTENTION = "paused-attention"
    DONE = "done"


GATE_NODES = {Node.GATE_A, Node.GATE_B, Node.GATE_C}

# The forward path S(n) -> S(n+1) for stages not followed by a gate; gates own
# their exits (GateOutcome.next_stage), and GATE_AFTER_STAGE marks gate entries.
NEXT_STAGE: dict[Stage, Stage] = {
    Stage.S0: Stage.S1,
    Stage.S2: Stage.S3,
    Stage.S3: Stage.S4,
    Stage.S5: Stage.S6,
    Stage.S6: Stage.S7,
}


def node_of(state: RunState) -> Node:
    """Derive the machine node from the persisted (stage, status, pending_gate)."""
    if state.status is RunStatus.DONE:
        return Node.DONE
    if state.status is RunStatus.PAUSED_ATTENTION:
        return Node.PAUSED_ATTENTION
    if state.status is RunStatus.GATE_PENDING:
        assert state.pending_gate is not None  # RunState validator guarantees this
        return Node(state.pending_gate.value)
    return Node(state.stage.value)


class Engine:
    """Runs one workspace's state machine until it pauses or completes."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        stages: dict[Stage, type[StageBase]] | None = None,
        emit: Callable[[str], None] = print,
        ask_user: Callable[[str], str] | None = None,
        mock_kwargs: dict | None = None,
    ) -> None:
        self.workspace = workspace
        self.config: RunConfig = workspace.load_config()
        self.stages = stages if stages is not None else STAGES
        self.emit = emit
        self.ask_user = ask_user
        self.dispatcher = create_dispatcher(workspace, self.config, **(mock_kwargs or {}))

    # -- public entry points -----------------------------------------------------

    async def run(self) -> Node:
        """Advance until a pause node (gate / attention / not-built-yet) or DONE.
        Safe to call any number of times from any interruption point."""
        while True:
            state = self.workspace.load_state()
            node = node_of(state)
            if node is Node.DONE:
                self.emit(f"run {state.run_id} is complete — see report/decision-report.md")
                return node
            if node is Node.PAUSED_ATTENTION:
                self.emit(
                    f"run {state.run_id} is paused for human attention (stage "
                    f"{state.stage.value}); inspect logs/ and the last commit, then "
                    f"`deeper resume {self.workspace.root}` to re-enter the stage"
                )
                return node
            if node in GATE_NODES:
                if not await self._process_gate(GATE_SPECS[GateName(node.value)]):
                    return node_of(self.workspace.load_state())
                continue
            if not await self._process_stage(Stage(node.value)):
                return node_of(self.workspace.load_state())

    async def resume(self) -> Node:
        """`deeper resume`: clear a human-attention pause (the human has acted),
        then run. From any other node this is exactly `run()`."""
        state = self.workspace.load_state()
        if state.status is RunStatus.PAUSED_ATTENTION:
            state.status = RunStatus.RUNNING
            state.updated_at = datetime.now(UTC)
            self.workspace.save_state(state)
            self.workspace.commit(f"resumed after human attention (stage {state.stage.value})")
            self.emit(f"re-entering stage {state.stage.value} after human attention")
        return await self.run()

    # -- stage nodes ---------------------------------------------------------------

    async def _process_stage(self, stage: Stage) -> bool:
        """Run one stage; True when the machine advanced past it."""
        if stage not in self.stages:
            raise EngineError(f"no stage registered for {stage.value}")
        instance = self.stages[stage]()
        ctx = StageContext(
            workspace=self.workspace,
            config=self.config,
            dispatcher=self.dispatcher,
            emit=self.emit,
            ask_user=self.ask_user,
        )
        if instance.is_complete(ctx):
            self.emit(f"{stage.value}: outputs already valid — skipping (idempotent re-entry)")
        else:
            instance.validate_inputs(ctx)
            try:
                await instance.execute(ctx)
            except (NotImplementedYet, StageInterrupted) as err:
                self.emit(str(err))
                return False
            except (
                AgentOutputInvalid,
                AgentDispatchFailed,
                SpendCapExceeded,
                UsageLimitReached,
            ) as err:
                self._pause_on_agent_error(stage, err)
                return False
            instance.evaluate_stop_rules(ctx)
            self._check_outputs(stage, instance, ctx)
        self._advance_past(stage)
        return True

    def _check_outputs(self, stage: Stage, instance: StageBase, ctx: StageContext) -> None:
        problems = []
        for relpath, model in instance.outputs(ctx):
            try:
                ctx.workspace.read_artifact(relpath, model)
            except (WorkspaceError, ValidationError, yaml.YAMLError) as err:
                problems.append(f"- {relpath}: {err}")
        if problems:
            raise EngineError(
                f"stage {stage.value} finished but its declared outputs are missing "
                "or invalid — this is a stage bug:\n" + "\n".join(problems)
            )

    def _pause_on_agent_error(
        self,
        stage: Stage,
        err: AgentOutputInvalid | AgentDispatchFailed | SpendCapExceeded | UsageLimitReached,
    ) -> None:
        """Route any agent failure — from a stage or a Gate-C loop — into the
        one resumable pause path."""
        if isinstance(err, UsageLimitReached):
            # Finding 11's recorded scope: the pause message with the reset
            # time IS the whole feature — resuming stays an explicit human
            # action (never an automatic sleep/wait), like every other pause.
            reset_line = (
                f"The limit resets at: {err.resets_at}.\n"
                if err.resets_at
                else "The CLI's notice did not carry a machine-readable reset time — "
                "subscription limits reset on a rolling window (typically hours).\n"
            )
            self._pause_attention(
                stage,
                role=err.contract.role,
                reason="Claude plan usage limit reached",
                detail=(
                    "Your Claude plan's session usage limit was reached — this is not "
                    "an error in the run, and nothing is lost: completed work is saved "
                    f"and re-entry skips it.\n{reset_line}"
                    f"When the limit has reset, run: deeper resume {self.workspace.root}\n"
                    "(The run does NOT resume automatically.)"
                ),
                transcript=(
                    f"# {stage.value} '{err.contract.role}' — Claude plan usage limit "
                    f"reached\n\n{err}\n\n## The notice as received\n\n{err.notice}\n"
                ),
            )
        elif isinstance(err, AgentOutputInvalid):
            self._pause_attention(
                stage,
                role=err.contract.role,
                reason=f"agent '{err.contract.role}' output invalid after retries",
                detail=(
                    f"agent '{err.contract.role}' failed schema validation "
                    f"{self.config.caps.max_schema_retries + 1} times.\n"
                    f"Last validation errors:\n{err.errors}\n"
                    "Fix the cause (prompt, fixture, or schema), then resume."
                ),
                transcript=(
                    f"# {stage.value} '{err.contract.role}' — schema retries exhausted\n\n"
                    f"## Validation errors\n\n{err.errors}\n\n"
                    f"## Last raw output\n\n{err.raw_output}\n"
                ),
            )
        elif isinstance(err, AgentDispatchFailed):
            tb = "".join(
                traceback.format_exception(type(err.cause), err.cause, err.cause.__traceback__)
            )
            self._pause_attention(
                stage,
                role=err.contract.role,
                reason=f"agent '{err.contract.role}' dispatch failed",
                detail=f"{err}\nInspect the saved transcript, fix the cause, then resume.",
                transcript=(
                    f"# {stage.value} '{err.contract.role}' — dispatch failed\n\n"
                    f"{err}\n\n## Traceback\n\n```\n{tb}```\n"
                ),
            )
        else:
            self._pause_attention(
                stage,
                role=err.contract.role,
                reason=f"spend cap ${err.cap_usd:.2f} crossed",
                detail=(
                    f"{err}\nCompleted work is saved. To continue, raise the cap "
                    f"(`deeper resume {self.workspace.root} --max-spend-usd <usd>`, "
                    "or edit max_spend_usd in config.yaml) — or accept the partial run."
                ),
                transcript=(
                    f"# {stage.value} — spend cap crossed\n\n{err}\n\n"
                    f"Spend by stage:\n"
                    + "\n".join(
                        f"- {s}: ${usd:.4f}"
                        for s, usd in sorted(self.workspace.load_state().spend_by_stage().items())
                    )
                    + "\n"
                ),
            )

    def _pause_attention(
        self, stage: Stage, *, role: str, reason: str, detail: str, transcript: str
    ) -> None:
        """Pause the run for a human: transcript saved to logs/ first so it
        rides the pause commit, then one committed state transition. Every
        agent-failure path funnels here — a failed live run must always be a
        resumable pause, never a crash."""
        log_relpath = self._write_attention_log(stage, role, transcript)
        state = self.workspace.load_state()
        state.status = RunStatus.PAUSED_ATTENTION
        state.pending_gate = None
        state.updated_at = datetime.now(UTC)
        self.workspace.save_state(state)
        self.workspace.commit(f"{stage.value} paused: {reason}")
        self.emit(
            f"{stage.value}: {reason} — run paused for human attention.\n"
            f"{detail}\n"
            f"Transcript saved to {log_relpath}.\n"
            f"Then: deeper resume {self.workspace.root}"
        )

    def _write_attention_log(self, stage: Stage, role: str, transcript: str) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        relpath = f"logs/attention-{stamp}-{stage.value}-{role}.md"
        target = self.workspace.path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(transcript, encoding="utf-8", newline="\n")
        return relpath

    def _advance_past(self, completed: Stage) -> None:
        """One committed transition: into the following gate, the next stage,
        or DONE."""
        state = self.workspace.load_state()
        state.updated_at = datetime.now(UTC)
        gate = GATE_AFTER_STAGE.get(completed)
        if gate is not None:
            spec = GATE_SPECS[gate]
            write_template_if_absent(self.workspace, spec, self._gate_template_override(spec))
            state.status = RunStatus.GATE_PENDING
            state.pending_gate = gate
            state.gates[gate] = GateStatus.PENDING
            self.workspace.save_state(state)
            self.workspace.commit(f"{completed.value} complete; {gate.value} pending")
            return
        if completed is Stage.S8:
            state.status = RunStatus.DONE
            state.pending_gate = None
            self.workspace.save_state(state)
            self.workspace.commit("run complete")
            return
        state.stage = NEXT_STAGE[completed]
        state.status = RunStatus.RUNNING
        self.workspace.save_state(state)
        self.workspace.commit(f"{completed.value} complete")

    # -- gate nodes ------------------------------------------------------------------

    def _gate_template_override(self, spec: GateSpec) -> str | None:
        """The capped Gate-C template once the §12 iteration cap is spent —
        the gate then only offers approve, with a note explaining why."""
        if spec.name is not GateName.C:
            return None
        state = self.workspace.load_state()
        if state.gate_c_iterations >= self.config.caps.max_gate_c_loops:
            return _TEMPLATE_C_CAPPED
        return None

    async def _process_gate(self, spec: GateSpec) -> bool:
        """Validate + interpret the gate file; True when the machine advanced."""
        if write_template_if_absent(self.workspace, spec, self._gate_template_override(spec)):
            # Entry normally writes it; this covers a hand-deleted file.
            self.workspace.commit(f"{spec.name.value} template rewritten")
        decision, problem = read_decision(self.workspace, spec)
        if problem is not None or decision is None:
            self.emit(problem or f"{spec.relpath} could not be read")
            self._emit_gate_instructions(spec)
            return False
        outcome = spec.interpret(decision)
        for message in outcome.messages:
            self.emit(message)
        if outcome.loop_decision is not None:
            return await self._process_gate_c_loop(spec, outcome.loop_decision)
        if not outcome.advanced:
            self._emit_gate_instructions(spec)
            return False
        if outcome.apply is not None:
            # Decision-mandated workspace edits (Gate A actions, hint recording)
            # run before the transition: their writes ride the gate commit, and
            # a problem (e.g. an unknown angle id) re-pauses the gate untouched.
            messages, problem = outcome.apply(self.workspace)
            if problem is not None:
                self.emit(problem)
                self._emit_gate_instructions(spec)
                return False
            for message in messages:
                self.emit(message)
        state = self.workspace.load_state()
        assert outcome.gate_status is not None and outcome.next_stage is not None
        state.gates[spec.name] = outcome.gate_status
        state.status = RunStatus.RUNNING
        state.pending_gate = None
        state.stage = outcome.next_stage
        state.updated_at = datetime.now(UTC)
        self.workspace.save_state(state)
        self.workspace.commit(f"{spec.name.value}: {outcome.commit_label}")
        if outcome.invalidate_to is not None:
            # Loop edge (e.g. Gate A rerun_hint): re-enter via the same git-tracked
            # invalidation `deeper rerun` uses, so stale outputs never look complete.
            invalidate(self.workspace, outcome.invalidate_to)
        return True

    async def _process_gate_c_loop(self, spec: GateSpec, decision: GateCDecision) -> bool:
        """Execute one Gate-C loop iteration (design §5 Gate C, §12 caps).

        Cap and referential checks first — a refused decision consumes no
        iteration and dispatches nothing. Applied loops archive the decision
        to gates/gate-c.N.yaml, bump the per-run counters, and either re-open
        the gate with a fresh template (feedback/challenges) or invalidate S7
        so the tournament reruns over the merged finalists (re-divergence).
        Returns True only on the S7-rerun path — `run()` then continues.
        """
        state = self.workspace.load_state()
        caps = self.config.caps
        if state.gate_c_iterations >= caps.max_gate_c_loops:
            self.emit(
                f"gate-c: the {caps.max_gate_c_loops} feedback loop(s) this run "
                "allows are used (design §12: hard cap, then decide) — the "
                "remaining action is `approved: true`."
            )
            self._emit_gate_instructions(spec)
            return False
        problems = validate_gate_c_decision(self.workspace, decision, state, self.config)
        if problems:
            self.emit(
                "gate-c decision cannot be applied:\n- "
                + "\n- ".join(problems)
                + f"\nFix {spec.relpath}, then `deeper resume` again."
            )
            self._emit_gate_instructions(spec)
            return False

        n = state.gate_c_iterations + 1
        ctx = StageContext(
            workspace=self.workspace,
            config=self.config,
            dispatcher=self.dispatcher,
            emit=self.emit,
            ask_user=self.ask_user,
        )
        rerun_s7 = False
        try:
            if decision.evidence_challenges:
                await run_evidence_challenges(ctx, list(decision.evidence_challenges), iteration=n)
            if decision.preference_feedback:
                await apply_preference_feedback(ctx, list(decision.preference_feedback))
            if decision.accept_redivergence:
                frame_check = self.workspace.read_artifact(FRAME_CHECK_PATH, FrameCheck)
                assert frame_check.proposal is not None  # validated above
                rerun_s7 = await run_mini_loop(ctx, frame_check.proposal)
        except (
            AgentOutputInvalid,
            AgentDispatchFailed,
            SpendCapExceeded,
            UsageLimitReached,
        ) as err:
            self._pause_on_agent_error(self.workspace.load_state().stage, err)
            return False

        parts = []
        if decision.evidence_challenges:
            parts.append(f"{len(decision.evidence_challenges)} evidence challenge(s)")
        if decision.preference_feedback:
            parts.append(f"{len(decision.preference_feedback)} preference reaction(s)")
        if decision.accept_redivergence:
            parts.append("re-divergence accepted")
        label = ", ".join(parts)
        archive = f"gates/gate-c.{n}.yaml"
        self.workspace.path(spec.relpath).replace(self.workspace.path(archive))
        # Reload: the loop's dispatches appended spend entries to state.json.
        state = self.workspace.load_state()
        state.gate_c_iterations = n
        if decision.accept_redivergence:
            state.redivergence_runs += 1
        state.updated_at = datetime.now(UTC)
        self.workspace.save_state(state)
        if rerun_s7:
            self.workspace.commit(f"gate-c loop {n}: {label}")
            # The same git-tracked invalidation `deeper rerun` uses: S7's stale
            # outputs must not look complete over the merged scoreboard.
            invalidate(self.workspace, Stage.S7)
            self.emit(
                f"gate-c: loop {n} of {caps.max_gate_c_loops} applied ({label}; "
                f"decision archived to {archive}) — S7 reruns, then Gate C reopens"
            )
            return True
        write_template_if_absent(self.workspace, spec, self._gate_template_override(spec))
        self.workspace.commit(f"gate-c loop {n}: {label}")
        self.emit(
            f"gate-c: loop {n} of {caps.max_gate_c_loops} applied ({label}; decision "
            f"archived to {archive}) — review the updated artifacts, then decide again"
        )
        self._emit_gate_instructions(spec)
        return False

    def _emit_gate_instructions(self, spec: GateSpec) -> None:
        total = self.workspace.load_state().total_usd()
        self.emit(
            f"[{spec.name.value}] pending — human review required.\n"
            f"  Review: {', '.join(spec.review_paths)}\n"
            f"  Edit:   {spec.relpath}\n"
            f"  Then:   deeper resume {self.workspace.root}\n"
            f"  Agent spend so far: ${total:.4f}"
        )
