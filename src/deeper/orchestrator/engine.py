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

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import AgentOutputInvalid, create_dispatcher
from deeper.config import RunConfig
from deeper.schemas import GateName, GateStatus, RunState, RunStatus, Stage
from deeper.stages import STAGES, NotImplementedYet, StageBase, StageContext
from deeper.workspace import Workspace, WorkspaceError

from .gates import GATE_AFTER_STAGE, GATE_SPECS, GateSpec, read_decision, write_template_if_absent
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
        mock_kwargs: dict | None = None,
    ) -> None:
        self.workspace = workspace
        self.config: RunConfig = workspace.load_config()
        self.stages = stages if stages is not None else STAGES
        self.emit = emit
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
                if not self._process_gate(GATE_SPECS[GateName(node.value)]):
                    return node
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
        )
        if instance.is_complete(ctx):
            self.emit(f"{stage.value}: outputs already valid — skipping (idempotent re-entry)")
        else:
            instance.validate_inputs(ctx)
            try:
                await instance.execute(ctx)
            except NotImplementedYet as err:
                self.emit(str(err))
                return False
            except AgentOutputInvalid as err:
                self._pause_attention(stage, err)
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

    def _pause_attention(self, stage: Stage, err: AgentOutputInvalid) -> None:
        state = self.workspace.load_state()
        state.status = RunStatus.PAUSED_ATTENTION
        state.pending_gate = None
        state.updated_at = datetime.now(UTC)
        self.workspace.save_state(state)
        self.workspace.commit(
            f"{stage.value} paused: agent '{err.contract.role}' output invalid after retries"
        )
        self.emit(
            f"{stage.value}: agent '{err.contract.role}' failed schema validation "
            f"{self.config.caps.max_schema_retries + 1} times — run paused for human "
            f"attention.\nLast validation errors:\n{err.errors}\n"
            f"Fix the cause (prompt, fixture, or schema), then "
            f"`deeper resume {self.workspace.root}`."
        )

    def _advance_past(self, completed: Stage) -> None:
        """One committed transition: into the following gate, the next stage,
        or DONE."""
        state = self.workspace.load_state()
        state.updated_at = datetime.now(UTC)
        gate = GATE_AFTER_STAGE.get(completed)
        if gate is not None:
            spec = GATE_SPECS[gate]
            write_template_if_absent(self.workspace, spec)
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

    def _process_gate(self, spec: GateSpec) -> bool:
        """Validate + interpret the gate file; True when the machine advanced."""
        if write_template_if_absent(self.workspace, spec):
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
        if not outcome.advanced:
            self._emit_gate_instructions(spec)
            return False
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

    def _emit_gate_instructions(self, spec: GateSpec) -> None:
        total = self.workspace.load_state().total_usd()
        self.emit(
            f"[{spec.name.value}] pending — human review required.\n"
            f"  Review: {', '.join(spec.review_paths)}\n"
            f"  Edit:   {spec.relpath}\n"
            f"  Then:   deeper resume {self.workspace.root}\n"
            f"  Agent spend so far: ${total:.4f}"
        )
