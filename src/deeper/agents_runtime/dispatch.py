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
import os
import random
import re
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


class LiveDispatchError(Exception):
    """A live invocation died, enriched with the CLI's own result
    subtype/detail when one was streamed before the failure (finding 3: the
    SDK's wrapper text alone can be actively misleading — 'error result:
    success' was output-token exhaustion). Transient class: retried with
    backoff, then wrapped as AgentDispatchFailed."""


def _cli_result_detail(subtype: str | None, result_text: str | None, is_error: bool) -> str:
    parts = []
    if subtype is not None:
        parts.append(f"CLI result subtype={subtype!r}")
    if is_error:
        parts.append("is_error=true")
    if result_text:
        parts.append(f"result: {result_text[:2000]}")
    return "; ".join(parts)


class UsageLimitReached(Exception):
    """The Claude plan's session usage limit was hit (billing: subscription).
    Deterministic until the limit window resets — never backed off, never
    schema-retried (each retry would just rediscover the limit). The
    orchestrator pauses the run; resuming is an explicit human action
    (`deeper resume` at/after the reset time), never automatic."""

    def __init__(self, contract: AgentContract, notice: str, resets_at: str | None) -> None:
        reset_part = f" (resets: {resets_at})" if resets_at else ""
        super().__init__(
            f"the Claude plan's usage limit was reached while dispatching "
            f"'{contract.role}' ({contract.stage.value}){reset_part}"
        )
        self.contract = contract
        self.notice = notice
        self.resets_at = resets_at


# The M1 triage (finding 11) could not pin down the exact shape the SDK/CLI
# surfaces a plan-limit hit in, so detection is deliberately broad over the two
# known families: the API-style marker "usage limit reached|<epoch>" and the
# CLI's prose notices ("...limit reached ∙ resets 3pm"). Matched against BOTH
# exception text and (short, artifact-free) reply text.
_LIMIT_EPOCH_RE = re.compile(r"usage limit reached\|(\d{9,13})", re.IGNORECASE)
_LIMIT_TEXT_RE = re.compile(
    r"usage limit (?:reached|hit|exceeded)"
    r"|(?:\d+\s*-?\s*hour|session|weekly|daily) limit reached"
    r"|reached your (?:usage|session|plan) limit",
    re.IGNORECASE,
)
_LIMIT_RESET_RE = re.compile(r"resets?(?:\s+at)?[:\s]+([^\n|]+)", re.IGNORECASE)


def usage_limit_notice(text: str) -> tuple[bool, str | None]:
    """(is a plan-usage-limit notice, reset time if stated). The epoch form
    carries a machine-readable reset; prose forms carry it as free text."""
    match = _LIMIT_EPOCH_RE.search(text)
    if match:
        resets = (
            datetime.fromtimestamp(int(match.group(1)), tz=UTC)
            .astimezone()
            .isoformat(timespec="minutes")
        )
        return True, resets
    if _LIMIT_TEXT_RE.search(text):
        reset = _LIMIT_RESET_RE.search(text)
        return True, (reset.group(1).strip() or None) if reset else None
    return False, None


class BillingAuthError(Exception):
    """The run's `billing` setting and the actual auth path disagree — a
    deterministic misconfiguration, never retried. Raised fast at dispatcher
    construction (billing: api without ANTHROPIC_API_KEY) or when a live
    subagent reports it authenticated with a source the billing mode forbids
    (billing: subscription must never spend the metered API key)."""


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

    # Transient-infrastructure backoff before a dispatch failure pauses the
    # run: the M1 live run hit repeated one-off SDK stream errors ("Claude
    # Code returned an error result") that succeeded on plain re-dispatch —
    # each human resume re-paid every completed sibling batch.
    DISPATCH_RETRY_BACKOFF_S: tuple[float, ...] = (2.0, 8.0)

    async def _guarded_invoke(self, prompt: str, contract: AgentContract, attempt: int):
        """Every raw invocation passes here: the spend guard runs first (an
        in-flight batch may have crossed the cap since the stage started), then
        the semaphore, then _invoke under the per-invocation timeout — transient
        infrastructure failures are retried with backoff, and only then wrapped
        so the orchestrator can pause instead of crash. Deterministic failures
        (billing mismatch, plan usage limit) skip the backoff entirely: retrying
        them re-spends on the wrong meter or rediscovers the same limit."""
        total = self.ledger.total_usd()
        if total >= self.config.max_spend_usd:
            raise SpendCapExceeded(contract, total, self.config.max_spend_usd)
        async with self._semaphore():
            for backoff in (*self.DISPATCH_RETRY_BACKOFF_S, None):
                try:
                    inv = await asyncio.wait_for(
                        self._invoke(prompt, contract, attempt),
                        timeout=self.config.dispatch_timeout_s,
                    )
                except Exception as err:  # noqa: PERF203 — retry loop is the point
                    # The tokens a failed attempt consumed are unknowable, but
                    # the attempt itself must reach the audit trail (M1 finding
                    # 5b: three full-prompt dispatch deaths, zero ledger trace).
                    self._record_failed_attempt(contract, err)
                    limit, resets_at = usage_limit_notice(str(err))
                    if limit:
                        raise UsageLimitReached(contract, str(err), resets_at) from err
                    if backoff is None or isinstance(err, BillingAuthError):
                        raise AgentDispatchFailed(contract, err) from err
                    await asyncio.sleep(backoff + random.random())
                else:
                    # A plan-limit notice can also arrive as a "successful"
                    # short text reply instead of an exception (M1 finding 11:
                    # it then burns the whole schema-retry loop as a bogus
                    # parse failure). Artifact-bearing replies are never
                    # limit notices.
                    if "### artifact:" not in inv.text and len(inv.text) < 2000:
                        limit, resets_at = usage_limit_notice(inv.text)
                        if limit:
                            raise UsageLimitReached(contract, inv.text.strip(), resets_at)
                    return inv

    def _record_failed_attempt(self, contract: AgentContract, err: BaseException) -> None:
        """Zero-cost marker SpendEntry for a dispatch attempt that died in
        flight — the real token spend is unknowable from here, but the audit
        trail must show the attempt happened."""
        self.ledger.record(
            SpendEntry(
                stage=contract.stage,
                role=contract.role,
                context=contract.context,
                usd=0.0,
                input_tokens=0,
                output_tokens=0,
                failed=f"{type(err).__name__}: {err}"[:500],
                at=datetime.now(UTC),
            )
        )

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
                self._log_failed_attempt(contract, attempt, feedback, raw)
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

    def _log_failed_attempt(
        self, contract: AgentContract, attempt: int, feedback: str, raw: str
    ) -> None:
        """Persist every schema-invalid attempt to logs/retries/ — even when the
        retry then succeeds. Recovered failures are the §10 prompt-iteration
        evidence (schema-failure rates AND causes); before this, the invalid
        output that triggered a successful retry was silently discarded (M1
        finding 5)."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        context = f"-{contract.context}" if contract.context else ""
        relpath = (
            f"logs/retries/{stamp}-{contract.stage.value}-{contract.role}"
            f"{context}-attempt{attempt}.md"
        )
        target = self.workspace.path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"# {contract.stage.value} '{contract.role}'"
            f"{f' ({contract.context})' if contract.context else ''} — invalid attempt "
            f"{attempt} (recovered attempts land here too)\n\n"
            f"## Validation errors\n\n{feedback}\n\n## Raw output\n\n{raw}\n",
            encoding="utf-8",
            newline="\n",
        )

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
                    self._log_failed_attempt(contract, retries, err.report, inv.text)
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
                self._log_failed_attempt(contract, retries, problem, inv.text)
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

    def __init__(self, workspace: Workspace, config: RunConfig) -> None:
        super().__init__(workspace, config)
        if config.billing == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
            raise BillingAuthError(
                "config.yaml sets billing: api but ANTHROPIC_API_KEY is not in the "
                "environment — set the key, or switch to billing: subscription "
                "(the default) to use the Claude Code CLI's stored login"
            )

    def _live_options(self, contract: AgentContract):
        """The full option set for one live subagent process. Size-class
        budgets are enforced here, not just stated in the prompt: the CLI's
        default 32k output ceiling kills long single-reply artifacts (observed
        live: a 52-option screening), so max_output_tokens is passed as
        CLAUDE_CODE_MAX_OUTPUT_TOKENS into the subagent's environment.

        Billing is enforced here too. The SDK builds the subagent env as
        `{**os.environ, **options.env}` — options.env overrides but cannot
        *remove* a key, so under billing: subscription ANTHROPIC_API_KEY is
        overridden to the empty string, which the CLI treats as unset
        (verified against the bundled CLI: init reports apiKeySource "none")
        and falls back to the stored Claude Code login. A non-empty key would
        take precedence over that login and silently meter the API account.
        Under billing: api the key is passed through explicitly (its absence
        already failed fast at construction)."""
        from claude_agent_sdk import ClaudeAgentOptions

        meta, _ = load_role(contract.role)
        research = bool(meta.get("research"))
        spec = self.config.size_classes[contract.size_class]
        env = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(spec.max_output_tokens)}
        if self.config.billing == "subscription":
            env["ANTHROPIC_API_KEY"] = ""
        else:
            env["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "")
        return ClaudeAgentOptions(
            model=spec.model,
            allowed_tools=list(RESEARCH_TOOLS if research else NON_RESEARCH_TOOLS),
            disallowed_tools=list(DISALLOWED_TOOLS),
            permission_mode="dontAsk",
            setting_sources=[],
            cwd=str(self.workspace.root),
            max_turns=2 * spec.max_searches + 6,
            hooks=build_hooks(contract, self.workspace),
            env=env,
        )

    def _check_auth_source(self, message: object) -> None:
        """Belt to _live_options' suspenders: the CLI's init message reports
        which auth source it actually chose. Under billing: subscription any
        real key source (an apiKeyHelper, a future CLI behavior change) must
        stop the run before it meters the wrong account."""
        if self.config.billing != "subscription":
            return
        if type(message).__name__ != "SystemMessage" or getattr(message, "subtype", "") != "init":
            return
        source = (getattr(message, "data", None) or {}).get("apiKeySource")
        if source and source != "none":
            raise BillingAuthError(
                f"billing: subscription, but the live subagent authenticated via "
                f"apiKeySource '{source}' instead of the Claude Code login — refusing "
                "to meter an API account; set billing: api if that is intended"
            )

    async def _invoke(self, prompt: str, contract: AgentContract, attempt: int) -> _Invocation:
        from claude_agent_sdk import query

        options = self._live_options(contract)
        chunks: list[str] = []
        usd = 0.0
        input_tokens = output_tokens = num_turns = 0
        duration_ms = 0
        session_id: str | None = None
        # The CLI's result subtype/text is the real diagnosis when the SDK dies
        # with an opaque wrapper ("error result: success" was output-token
        # exhaustion on the M1 run, finding 3) — capture it as we stream so a
        # raised exception can carry it.
        result_subtype: str | None = None
        result_text: str | None = None
        result_is_error = False
        started = time.monotonic()
        try:
            async for message in query(prompt=prompt, options=options):
                self._check_auth_source(message)
                for block in getattr(message, "content", None) or []:
                    text = getattr(block, "text", None)
                    if text:
                        chunks.append(text)
                if type(message).__name__ == "ResultMessage":
                    result_subtype = getattr(message, "subtype", None)
                    result_text = getattr(message, "result", None)
                    result_is_error = bool(getattr(message, "is_error", False))
                    usd = getattr(message, "total_cost_usd", None) or 0.0
                    usage = getattr(message, "usage", None) or {}
                    input_tokens = int(usage.get("input_tokens") or 0)
                    output_tokens = int(usage.get("output_tokens") or 0)
                    num_turns = getattr(message, "num_turns", 0) or 0
                    duration_ms = getattr(message, "duration_ms", 0) or 0
                    session_id = getattr(message, "session_id", None)
        except BillingAuthError:
            raise
        except Exception as err:
            detail = _cli_result_detail(result_subtype, result_text, result_is_error)
            raise LiveDispatchError(
                f"{type(err).__name__}: {err}" + (f" [{detail}]" if detail else "")
            ) from err
        if result_is_error or (result_subtype not in (None, "success")):
            # An error result the SDK did NOT raise for: surfacing it here (with
            # the CLI's own words) beats letting an empty/garbled reply burn the
            # schema-retry loop as a bogus parse failure.
            raise LiveDispatchError(
                "the CLI reported an error result: "
                + _cli_result_detail(result_subtype, result_text, result_is_error)
            )
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
