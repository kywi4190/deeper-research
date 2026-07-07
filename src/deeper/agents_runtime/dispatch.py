"""Agent dispatch: the single chokepoint for all subagent invocations (§8).

Every invocation — live or mock — flows through _BaseDispatcher.run_agent:
semaphore-limited concurrency, schema-retry with LLM-facing validation
feedback, and a SpendEntry persisted to state.json per attempt. Subclasses
implement only _invoke (the network call, or a fixture read); everything that
makes the chokepoint trustworthy is shared code, so mock mode exercises the
real pipeline.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple, Protocol

from deeper.config import RunConfig
from deeper.schemas import SpendEntry
from deeper.workspace import Workspace

from .contracts import (
    AgentContract,
    AgentOutputInvalid,
    AgentResult,
    ArtifactParseError,
    assemble_prompt,
    load_role,
    parse_artifacts,
)
from .hooks import build_hooks
from .ledger import SpendLedger

# Tool allowlists per role kind (design §6/§11): research agents get the web +
# file tools (Write additionally fenced to the contract's subtree by hooks);
# no research agent ever gets Bash or the ability to spawn subagents.
RESEARCH_TOOLS = ["WebSearch", "WebFetch", "Read", "Write"]
NON_RESEARCH_TOOLS = ["Read"]
DISALLOWED_TOOLS = ["Bash", "Task", "Agent"]

_RETRY_TEMPLATE = (
    "{prompt}\n"
    "# PREVIOUS ATTEMPT (INVALID — shown so you can correct it)\n"
    "{raw}\n\n"
    "# VALIDATION ERRORS\n"
    "{feedback}\n\n"
    "Resubmit ALL required artifacts, complete and corrected — not just the "
    "fields named above.\n"
)


class _Invocation(NamedTuple):
    """What one raw model invocation returned, before validation."""

    text: str
    usd: float
    input_tokens: int
    output_tokens: int
    num_turns: int
    duration_ms: int
    session_id: str | None


class SpendCapExceeded(Exception):
    """The run's ledger crossed config.max_spend_usd — the dispatcher refuses
    new invocations and the orchestrator pauses the run (design §11 runaway-cost
    mitigation). Raise the cap (config.yaml or `deeper resume --max-spend-usd`)
    to continue; completed work is never lost."""

    def __init__(self, contract: AgentContract, total_usd: float, cap_usd: float) -> None:
        super().__init__(
            f"spend cap crossed before dispatching '{contract.role}' "
            f"({contract.stage.value}): ${total_usd:.4f} spent >= cap ${cap_usd:.2f}"
        )
        self.contract = contract
        self.total_usd = total_usd
        self.cap_usd = cap_usd


class AgentDispatchFailed(Exception):
    """The invocation itself failed (SDK/network/CLI error, missing mock
    fixture) — an infrastructure problem, not invalid agent content. The
    orchestrator pauses the run for human attention instead of crashing."""

    def __init__(self, contract: AgentContract, cause: BaseException) -> None:
        super().__init__(
            f"agent '{contract.role}' ({contract.stage.value}) dispatch failed: "
            f"{type(cause).__name__}: {cause}"
        )
        self.contract = contract
        self.cause = cause


class Dispatcher(Protocol):
    """The interface every stage codes against."""

    async def run_agent(self, contract: AgentContract) -> AgentResult: ...

    async def run_interview(
        self,
        contract: AgentContract,
        *,
        ask_user: Callable[[str], str] | None,
        max_questions: int,
    ) -> AgentResult: ...


class _BaseDispatcher:
    """Shared retry/concurrency/accounting spine; subclasses supply _invoke."""

    def __init__(self, workspace: Workspace, config: RunConfig) -> None:
        self.workspace = workspace
        self.config = config
        self.ledger = SpendLedger(workspace)
        # Created lazily on first use: an asyncio.Semaphore binds to the running
        # event loop, and this object may outlive one (pytest creates a loop per
        # test). One dispatcher per run keeps the limit global in practice —
        # the design's "module-level semaphore" intent, loop-safely.
        self._sem: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.config.concurrency)
        return self._sem

    async def _guarded_invoke(self, prompt: str, contract: AgentContract, attempt: int):
        """Every raw invocation passes here: the spend guard runs first (an
        in-flight batch may have crossed the cap since the stage started), then
        the semaphore, then _invoke with infrastructure failures wrapped so the
        orchestrator can pause instead of crash."""
        total = self.ledger.total_usd()
        if total >= self.config.max_spend_usd:
            raise SpendCapExceeded(contract, total, self.config.max_spend_usd)
        async with self._semaphore():
            try:
                return await self._invoke(prompt, contract, attempt)
            except Exception as err:
                raise AgentDispatchFailed(contract, err) from err

    async def run_agent(self, contract: AgentContract) -> AgentResult:
        prompt = assemble_prompt(contract)  # raises on drift before any spend
        retry_key = f"{contract.stage.value}:{contract.role}:{contract.context or '-'}"
        max_attempts = self.config.caps.max_schema_retries + 1
        raw, feedback = "", ""
        for attempt in range(max_attempts):
            full = (
                prompt
                if attempt == 0
                else _RETRY_TEMPLATE.format(prompt=prompt, raw=raw, feedback=feedback)
            )
            inv = await self._guarded_invoke(full, contract, attempt)
            # Every attempt is paid for, so every attempt is ledgered — before
            # validation, which may still reject the output.
            self.ledger.record(
                SpendEntry(
                    stage=contract.stage,
                    role=contract.role,
                    context=contract.context,
                    usd=inv.usd,
                    input_tokens=inv.input_tokens,
                    output_tokens=inv.output_tokens,
                    at=datetime.now(UTC),
                )
            )
            if attempt > 0:
                self.ledger.bump_retry(retry_key)
            try:
                artifacts = parse_artifacts(inv.text, contract.output_schemas)
            except ArtifactParseError as err:
                raw, feedback = inv.text, err.report
                continue
            return AgentResult(
                role=contract.role,
                artifacts=artifacts,
                raw_text=inv.text,
                usd=inv.usd,
                input_tokens=inv.input_tokens,
                output_tokens=inv.output_tokens,
                num_turns=inv.num_turns,
                duration_ms=inv.duration_ms,
                session_id=inv.session_id,
                retries_used=attempt,
            )
        raise AgentOutputInvalid(contract, errors=feedback, raw_output=raw)

    async def run_interview(
        self,
        contract: AgentContract,
        *,
        ask_user: Callable[[str], str] | None,
        max_questions: int,
    ) -> AgentResult:
        """The S0 conversational loop — the only multi-turn dispatch in the
        system (design §5/S0: the interviewer is "the only conversational
        agent"). Each turn re-invokes the agent with the transcript so far; a
        reply without artifact markers is a question for the human (`ask_user`),
        a reply with markers is the final artifact emission and flows through
        the same parse/validate/retry discipline as `run_agent`. When
        `ask_user` is None (non-interactive session) or the question budget is
        spent, the turn is marked final and the agent must emit."""
        prompt = assemble_prompt(contract)
        retry_key = f"{contract.stage.value}:{contract.role}:{contract.context or '-'}"
        transcript: list[tuple[str, str]] = []
        retries = 0
        raw, feedback = "", ""
        while True:
            final = ask_user is None or len(transcript) >= max_questions
            full = _interview_prompt(
                prompt,
                transcript,
                max_questions=max_questions,
                final=final,
                raw=raw,
                feedback=feedback,
            )
            inv = await self._guarded_invoke(full, contract, retries)
            self.ledger.record(
                SpendEntry(
                    stage=contract.stage,
                    role=contract.role,
                    context=contract.context,
                    usd=inv.usd,
                    input_tokens=inv.input_tokens,
                    output_tokens=inv.output_tokens,
                    at=datetime.now(UTC),
                )
            )
            if "### artifact:" in inv.text:
                try:
                    artifacts = parse_artifacts(inv.text, contract.output_schemas)
                except ArtifactParseError as err:
                    retries += 1
                    if retries > self.config.caps.max_schema_retries:
                        raise AgentOutputInvalid(
                            contract, errors=err.report, raw_output=inv.text
                        ) from err
                    self.ledger.bump_retry(retry_key)
                    raw, feedback = inv.text, err.report
                    continue
                return AgentResult(
                    role=contract.role,
                    artifacts=artifacts,
                    raw_text=inv.text,
                    usd=inv.usd,
                    input_tokens=inv.input_tokens,
                    output_tokens=inv.output_tokens,
                    num_turns=inv.num_turns,
                    duration_ms=inv.duration_ms,
                    session_id=inv.session_id,
                    retries_used=retries,
                )
            question = inv.text.strip()
            if final or not question:
                # Asked past the budget (or replied with nothing): a contract
                # violation, disciplined exactly like a schema failure.
                problem = (
                    "The question budget is spent — do not ask anything more; "
                    "emit all three artifacts now."
                    if final
                    else "Your reply was empty. Ask one question as plain text, "
                    "or emit all three artifacts."
                )
                retries += 1
                if retries > self.config.caps.max_schema_retries:
                    raise AgentOutputInvalid(contract, errors=problem, raw_output=inv.text)
                self.ledger.bump_retry(retry_key)
                raw, feedback = inv.text, problem
                continue
            raw, feedback = "", ""
            assert ask_user is not None  # final would be True otherwise
            answer = ask_user(question)
            transcript.append((question, answer))

    async def _invoke(self, prompt: str, contract: AgentContract, attempt: int) -> _Invocation:
        raise NotImplementedError


def _interview_prompt(
    prompt: str,
    transcript: list[tuple[str, str]],
    *,
    max_questions: int,
    final: bool,
    raw: str,
    feedback: str,
) -> str:
    """One interview turn's full prompt: role prompt + transcript + turn directive."""
    parts = [prompt.rstrip()]
    if transcript:
        lines = []
        for i, (question, answer) in enumerate(transcript, start=1):
            lines.append(f"Q{i} (you): {question}")
            lines.append(f"A{i} (user): {answer}")
        parts.append("# INTERVIEW SO FAR\n" + "\n".join(lines))
    else:
        parts.append("# INTERVIEW SO FAR\n(no questions asked yet)")
    if final:
        parts.append(
            "# TURN\nThis is your FINAL TURN: the question budget is spent or the "
            "session is non-interactive. Do not ask anything more — emit all three "
            "artifacts now, resolving any ambiguity per your finalizing instructions."
        )
    else:
        parts.append(
            f"# TURN\nYou have used {len(transcript)} of {max_questions} questions. "
            "Either ask exactly ONE next question as plain text (no artifact "
            "markers), or — if all three artifacts are already complete and "
            "unambiguous — emit them now."
        )
    if feedback:
        parts.append(
            "# PREVIOUS ATTEMPT (INVALID — shown so you can correct it)\n"
            f"{raw}\n\n# VALIDATION ERRORS\n{feedback}\n\n"
            "Resubmit ALL required artifacts, complete and corrected."
        )
    return "\n\n".join(parts) + "\n"


class LiveDispatcher(_BaseDispatcher):
    """Dispatch through claude_agent_sdk.query() with per-role tool restriction.

    Restriction is layered (verified against current SDK semantics):
    allowed_tools only *auto-approves*; the actual fence is
    permission_mode="dontAsk" (anything that would prompt is denied) plus
    disallowed_tools and the deny-hooks from build_hooks. setting_sources=[]
    keeps this machine's user/project settings and CLAUDE.md out of subagent
    context; cwd pins relative tool paths inside the run workspace.
    """

    async def _invoke(self, prompt: str, contract: AgentContract, attempt: int) -> _Invocation:
        from claude_agent_sdk import ClaudeAgentOptions, query

        meta, _ = load_role(contract.role)
        research = bool(meta.get("research"))
        spec = self.config.size_classes[contract.size_class]
        options = ClaudeAgentOptions(
            model=spec.model,
            allowed_tools=list(RESEARCH_TOOLS if research else NON_RESEARCH_TOOLS),
            disallowed_tools=list(DISALLOWED_TOOLS),
            permission_mode="dontAsk",
            setting_sources=[],
            cwd=str(self.workspace.root),
            max_turns=2 * spec.max_searches + 6,
            hooks=build_hooks(contract, self.workspace),
        )
        chunks: list[str] = []
        usd = 0.0
        input_tokens = output_tokens = num_turns = 0
        duration_ms = 0
        session_id: str | None = None
        started = time.monotonic()
        async for message in query(prompt=prompt, options=options):
            for block in getattr(message, "content", None) or []:
                text = getattr(block, "text", None)
                if text:
                    chunks.append(text)
            if type(message).__name__ == "ResultMessage":
                usd = getattr(message, "total_cost_usd", None) or 0.0
                usage = getattr(message, "usage", None) or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                num_turns = getattr(message, "num_turns", 0) or 0
                duration_ms = getattr(message, "duration_ms", 0) or 0
                session_id = getattr(message, "session_id", None)
        if not duration_ms:
            duration_ms = int((time.monotonic() - started) * 1000)
        return _Invocation(
            text="\n".join(chunks),
            usd=usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            num_turns=num_turns,
            duration_ms=duration_ms,
            session_id=session_id,
        )


def create_dispatcher(workspace: Workspace, config: RunConfig, **mock_kwargs) -> Dispatcher:
    """The config-selected dispatcher: mode 'mock' (offline, fixture-backed) or
    'live' (claude_agent_sdk). Extra kwargs go to MockDispatcher only."""
    if config.mode == "live":
        if mock_kwargs:
            raise ValueError(f"mock-only arguments {sorted(mock_kwargs)} with mode='live'")
        return LiveDispatcher(workspace, config)
    from .mock import MockDispatcher  # local import: keeps modules decoupled

    return MockDispatcher(workspace, config, **mock_kwargs)
