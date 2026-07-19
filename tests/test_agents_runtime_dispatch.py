"""Dispatch spine: schema-retry, semaphore, spend ledger, mode switch."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from deeper.agents_runtime import (
    AgentContract,
    AgentOutputInvalid,
    BillingAuthError,
    ContractError,
    LiveDispatcher,
    MockDispatcher,
    SpendLedger,
    create_dispatcher,
)
from deeper.config import SizeClass, profile_config
from deeper.schemas import SpendEntry, Stage
from deeper.workspace import Workspace

SCOUT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "mock_agents" / "scout" / "option-card-set.yaml"
)

VALID_TEXT = (
    "### artifact: option-card-set\n```yaml\n" + SCOUT_FIXTURE.read_text(encoding="utf-8") + "```\n"
)
INVALID_TEXT = (  # empty cards violates OptionCardSet's min_length=1
    "### artifact: option-card-set\n```yaml\nangle_id: interpretability-research\ncards: []\n```\n"
)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.create(tmp_path / "run", profile_config("quick"))


def scout_contract(**overrides) -> AgentContract:
    fields = dict(
        role="scout",
        stage=Stage.S3,
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="You have 3 units.",
        context="interpretability-research",
    )
    fields.update(overrides)
    return AgentContract(**fields)


class CapturingMock(MockDispatcher):
    """Records every prompt _invoke received, for retry-feedback assertions."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.prompts: list[str] = []

    async def _invoke(self, prompt, contract, attempt):
        self.prompts.append(prompt)
        return await super()._invoke(prompt, contract, attempt)


async def test_mock_dispatch_happy_path(ws: Workspace) -> None:
    result = await MockDispatcher(ws, ws.load_config()).run_agent(scout_contract())
    assert result.retries_used == 0
    assert result.artifacts["option-card-set"].angle_id == "interpretability-research"
    state = ws.load_state()
    assert len(state.spend) == 1
    assert state.spend[0].role == "scout"
    assert state.spend[0].context == "interpretability-research"


async def test_retry_invalid_then_valid(ws: Workspace) -> None:
    dispatcher = CapturingMock(
        ws, ws.load_config(), scripted_responses={"scout": [INVALID_TEXT, VALID_TEXT]}
    )
    result = await dispatcher.run_agent(scout_contract())
    assert result.retries_used == 1
    # The retry prompt carries the previous output and the formatted errors.
    assert "PREVIOUS ATTEMPT" in dispatcher.prompts[1]
    assert "failed validation against the OptionCardSet schema" in dispatcher.prompts[1]
    # Both attempts were paid for, so both are ledgered.
    state = ws.load_state()
    assert len(state.spend) == 2
    assert state.retry_counts == {"S3:scout:interpretability-research": 1}


async def test_every_attempt_lands_a_structured_jsonl_line(ws: Workspace) -> None:
    """§11 ops: logs/agents.jsonl carries one machine-readable line per raw
    invocation attempt — contract hash, duration, spend, outcome."""
    import json

    dispatcher = MockDispatcher(
        ws, ws.load_config(), scripted_responses={"scout": [INVALID_TEXT, VALID_TEXT]}
    )
    await dispatcher.run_agent(scout_contract())
    lines = [
        json.loads(raw)
        for raw in ws.path("logs/agents.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [line["outcome"] for line in lines] == ["invalid", "ok"]
    assert [line["attempt"] for line in lines] == [0, 1]
    first, second = lines
    assert first["stage"] == "S3" and first["role"] == "scout"
    assert first["context"] == "interpretability-research"
    # Both attempts ran the same contract: the hash groups them.
    assert first["contract_hash"] == second["contract_hash"]
    assert len(first["contract_hash"]) == 16
    for key in ("usd", "input_tokens", "output_tokens", "duration_ms", "at"):
        assert key in first


async def test_dispatch_error_lands_a_jsonl_line_too(ws: Workspace, monkeypatch) -> None:
    import json

    from deeper.agents_runtime import AgentDispatchFailed

    dispatcher = MockDispatcher(ws, ws.load_config())
    dispatcher.DISPATCH_RETRY_BACKOFF_S = ()  # no backoff sleeps in tests

    async def boom(prompt, contract, attempt):
        raise RuntimeError("stream died")

    monkeypatch.setattr(dispatcher, "_invoke", boom)
    with pytest.raises(AgentDispatchFailed):
        await dispatcher.run_agent(scout_contract())
    lines = [
        json.loads(raw)
        for raw in ws.path("logs/agents.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert lines and all(line["outcome"] == "dispatch-error" for line in lines)
    assert "stream died" in lines[-1]["error"]


async def test_retry_exhaustion_raises_and_persists(ws: Workspace) -> None:
    config = ws.load_config()
    attempts = config.caps.max_schema_retries + 1
    dispatcher = MockDispatcher(ws, config, scripted_responses={"scout": [INVALID_TEXT] * attempts})
    with pytest.raises(AgentOutputInvalid) as err:
        await dispatcher.run_agent(scout_contract())
    assert "OptionCardSet" in err.value.errors
    assert err.value.raw_output == INVALID_TEXT
    state = ws.load_state()
    assert len(state.spend) == attempts
    assert state.retry_counts["S3:scout:interpretability-research"] == attempts - 1


async def test_validate_callback_feeds_coherence_errors_into_the_retry_loop(ws: Workspace) -> None:
    """Stage-owned coherence checks (sampling assignments, cross-artifact
    linkage) run INSIDE the retry loop via run_agent's validate callback, so a
    semantically wrong reply gets corrective feedback like a schema failure
    does — the M2 live run's verifier dropped one sampled claim and the run
    paused on its FIRST miss, feedback never sent (M2 finding 5)."""
    dispatcher = CapturingMock(
        ws, ws.load_config(), scripted_responses={"scout": [VALID_TEXT, VALID_TEXT]}
    )
    calls: list[int] = []

    def check(artifacts) -> str | None:
        calls.append(1)
        if len(calls) == 1:
            return "- the card set is missing option 'x' from its assignment"
        return None

    result = await dispatcher.run_agent(scout_contract(), validate=check)
    assert result.retries_used == 1
    assert "PREVIOUS ATTEMPT" in dispatcher.prompts[1]
    assert "missing option 'x'" in dispatcher.prompts[1]
    # Both attempts were paid for, so both are ledgered.
    state = ws.load_state()
    assert len(state.spend) == 2
    assert state.retry_counts == {"S3:scout:interpretability-research": 1}


async def test_validate_callback_exhaustion_raises_with_true_attempt_count(ws: Workspace) -> None:
    config = ws.load_config()
    attempts = config.caps.max_schema_retries + 1
    dispatcher = MockDispatcher(ws, config, scripted_responses={"scout": [VALID_TEXT] * attempts})
    with pytest.raises(AgentOutputInvalid) as err:
        await dispatcher.run_agent(scout_contract(), validate=lambda artifacts: "- never coherent")
    assert "never coherent" in err.value.errors
    assert err.value.attempts == attempts
    assert len(ws.load_state().spend) == attempts


async def test_semaphore_limits_concurrency(ws: Workspace) -> None:
    config = ws.load_config().model_copy(update={"concurrency": 2})

    class SlowMock(MockDispatcher):
        in_flight = 0
        max_in_flight = 0

        async def _invoke(self, prompt, contract, attempt):
            SlowMock.in_flight += 1
            SlowMock.max_in_flight = max(SlowMock.max_in_flight, SlowMock.in_flight)
            await asyncio.sleep(0.01)
            SlowMock.in_flight -= 1
            return await super()._invoke(prompt, contract, attempt)

    dispatcher = SlowMock(ws, config)
    await asyncio.gather(*(dispatcher.run_agent(scout_contract()) for _ in range(6)))
    assert SlowMock.max_in_flight == 2


async def test_spend_so_far_by_stage(ws: Workspace) -> None:
    ledger = SpendLedger(ws)
    from datetime import UTC, datetime

    for stage, usd in ((Stage.S1, 0.10), (Stage.S1, 0.25), (Stage.S3, 1.0)):
        ledger.record(
            SpendEntry(
                stage=stage,
                role="scout",
                usd=usd,
                input_tokens=10,
                output_tokens=20,
                at=datetime.now(UTC),
            )
        )
    assert ledger.spend_so_far(Stage.S1) == pytest.approx(0.35)
    assert ledger.spend_so_far(Stage.S3) == pytest.approx(1.0)
    assert ledger.spend_so_far(Stage.S5) == 0.0
    assert ledger.total_usd() == pytest.approx(1.35)


async def test_ledger_persists_after_each_invocation(ws: Workspace) -> None:
    dispatcher = MockDispatcher(ws, ws.load_config())
    for expected in (1, 2, 3):
        await dispatcher.run_agent(scout_contract())
        assert len(ws.load_state().spend) == expected  # re-read from disk each time


def test_create_dispatcher_mode_switch(ws: Workspace) -> None:
    config = ws.load_config()
    assert isinstance(create_dispatcher(ws, config), MockDispatcher)
    live = config.model_copy(update={"mode": "live"})
    assert isinstance(create_dispatcher(ws, live), LiveDispatcher)
    with pytest.raises(ValueError, match="mock-only"):
        create_dispatcher(ws, live, scripted_responses={})


def test_mock_mode_never_imports_sdk(tmp_path: Path) -> None:
    """The zero-network guarantee: a full mock dispatch in a fresh interpreter
    must finish without claude_agent_sdk ever entering sys.modules."""
    script = textwrap.dedent(
        """
        import asyncio, sys
        from deeper.agents_runtime import AgentContract, MockDispatcher
        from deeper.config import SizeClass, profile_config
        from deeper.schemas import Stage
        from deeper.workspace import Workspace

        ws = Workspace.create(sys.argv[1] + "/run", profile_config("quick"))
        contract = AgentContract(
            role="scout", stage=Stage.S3, output_schemas=("option-card-set",),
            size_class=SizeClass.M, budget_line="b",
        )
        asyncio.run(MockDispatcher(ws, ws.load_config()).run_agent(contract))
        assert "claude_agent_sdk" not in sys.modules, "mock mode imported the SDK"
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


async def test_assembly_failure_spends_nothing(ws: Workspace) -> None:
    dispatcher = CapturingMock(ws, ws.load_config())
    with pytest.raises(ContractError):
        await dispatcher.run_agent(scout_contract(output_schemas=("rubric",)))
    assert dispatcher.prompts == []
    assert ws.load_state().spend == []


def test_live_options_enforce_size_class_budgets(ws: Workspace) -> None:
    """Size-class budgets are process-enforced, not prompt goodwill: the model,
    the turn budget, and the output-token ceiling (as
    CLAUDE_CODE_MAX_OUTPUT_TOKENS in the subagent env — the CLI's default 32k
    ceiling truncates long single-reply artifacts) all come from the config."""
    from deeper.config import SizeClass

    config = ws.load_config().model_copy(update={"mode": "live"})
    dispatcher = LiveDispatcher(ws, config)
    contract = AgentContract(
        role="scout",
        stage=Stage.S3,
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="b",
    )
    options = dispatcher._live_options(contract)
    spec = config.size_classes[SizeClass.M]
    assert options.model == spec.model
    assert options.max_turns == 2 * spec.max_searches + 6
    assert options.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(spec.max_output_tokens)
    assert options.permission_mode == "dontAsk"
    assert "Bash" in options.disallowed_tools
    assert options.cwd == str(ws.root)
    # The SDK's default 1MB stdout-message buffer kills a dispatch whenever one
    # streamed JSON message (a big WebFetch tool result) exceeds it — the M2
    # live run's verifier died twice on the same fetch (finding 7).
    from deeper.agents_runtime.dispatch import MAX_SDK_MESSAGE_BYTES

    assert options.max_buffer_size == MAX_SDK_MESSAGE_BYTES
    assert MAX_SDK_MESSAGE_BYTES >= 16 * 1024 * 1024


def test_subscription_billing_blanks_the_api_key(ws: Workspace, monkeypatch) -> None:
    """billing: subscription (the default) must guarantee plan usage even when
    a metered key sits in the environment: ClaudeAgentOptions.env merges OVER
    the inherited process env, so the dispatcher hands the SDK an empty-string
    override — the CLI treats that as unset and uses the stored login."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-metered-key-that-must-not-be-billed")
    config = ws.load_config().model_copy(update={"mode": "live"})
    assert config.billing == "subscription"  # the default
    options = LiveDispatcher(ws, config)._live_options(scout_contract())
    assert options.env["ANTHROPIC_API_KEY"] == ""


def test_api_billing_passes_the_key_through(ws: Workspace, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-explicitly-metered")
    config = ws.load_config().model_copy(update={"mode": "live", "billing": "api"})
    options = LiveDispatcher(ws, config)._live_options(scout_contract())
    assert options.env["ANTHROPIC_API_KEY"] == "sk-explicitly-metered"


def test_api_billing_without_a_key_fails_fast(ws: Workspace, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = ws.load_config().model_copy(update={"mode": "live", "billing": "api"})
    with pytest.raises(BillingAuthError, match="billing: api"):
        LiveDispatcher(ws, config)


def test_subscription_billing_refuses_a_foreign_auth_source(ws: Workspace, monkeypatch) -> None:
    """The runtime belt to the env-override suspenders: if the CLI's init
    message reports it authenticated with anything but the stored login, the
    dispatch fails immediately (no backoff retries — it is deterministic)."""

    class SystemMessage:
        subtype = "init"
        data = {"apiKeySource": "ANTHROPIC_API_KEY"}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    config = ws.load_config().model_copy(update={"mode": "live"})
    with pytest.raises(BillingAuthError, match="apiKeySource"):
        LiveDispatcher(ws, config)._check_auth_source(SystemMessage())

    # A login-authenticated init passes, and billing: api accepts any source.
    class LoginInit(SystemMessage):
        data = {"apiKeySource": "none"}

    LoginInit.__name__ = "SystemMessage"
    LiveDispatcher(ws, config)._check_auth_source(LoginInit())  # no raise
    api_config = ws.load_config().model_copy(update={"mode": "live", "billing": "api"})
    LiveDispatcher(ws, api_config)._check_auth_source(SystemMessage())  # no raise


def test_usage_limit_notice_recognizes_known_shapes() -> None:
    """M1 finding 11's detector, over the three families the CLI is known to
    emit (the third — "hit your session limit" — confirmed live, M2 finding 8);
    deliberately broad — but not so broad ordinary prose trips it."""
    from deeper.agents_runtime import usage_limit_notice

    hit, resets = usage_limit_notice("Claude AI usage limit reached|1751986800")
    assert hit and resets is not None  # epoch marker -> local ISO reset time
    assert resets[:2] == "20"  # a formatted timestamp, not the raw epoch

    hit, resets = usage_limit_notice("5-hour limit reached ∙ resets 3am")
    assert hit and resets == "3am"

    hit, resets = usage_limit_notice("You've reached your usage limit for this session.")
    assert hit and resets is None  # detected, no reset time to echo

    # The third confirmed live shape (M2 finding 8): "hit your ... limit",
    # embedded in the enriched LiveDispatchError text, reset time in prose.
    hit, resets = usage_limit_notice(
        "Claude Code returned an error result: success [CLI result "
        "subtype='success'; is_error=true; result: You've hit your session "
        "limit · resets 2pm (America/Denver)]"
    )
    assert hit and resets == "2pm (America/Denver)"

    for benign in (
        "the vendor imposes rate limits on bulk exports",
        "### artifact: option-card-set (usage notes: none)",
        "the budget limit reached by S3 was 16 units",
    ):
        hit, _ = usage_limit_notice(benign)
        assert not hit, f"false positive on: {benign}"


async def test_live_invoke_enriches_sdk_failures_with_cli_result_detail(
    ws: Workspace, monkeypatch
) -> None:
    """Finding 3: when the SDK raises an opaque wrapper, the exception that
    reaches the transcript carries the CLI's own result subtype/text (the M1
    run's 'error result: success' was really output-token exhaustion)."""
    import claude_agent_sdk

    from deeper.agents_runtime import LiveDispatchError

    class ResultMessage:
        subtype = "error_during_execution"
        result = "response exceeded the 32000 output token maximum"
        is_error = True

    async def fake_query(*, prompt, options):
        yield ResultMessage()
        raise RuntimeError("Claude Code returned an error result: success")

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    config = ws.load_config().model_copy(update={"mode": "live"})
    dispatcher = LiveDispatcher(ws, config)
    with pytest.raises(LiveDispatchError) as excinfo:
        await dispatcher._invoke("p", scout_contract(), 0)
    message = str(excinfo.value)
    assert "error result: success" in message  # the SDK's wrapper survives
    assert "error_during_execution" in message  # ...enriched with the subtype
    assert "32000 output token" in message  # ...and the CLI's real diagnosis


def test_live_options_wire_the_stderr_callback(ws: Workspace) -> None:
    """The CLI subprocess's stderr must be piped to the host (options.stderr),
    never inherited by the operator's terminal (M2 finding 9: minified-JS hook
    dumps and SDK reader errors spewing mid-run)."""
    config = ws.load_config().model_copy(update={"mode": "live"})
    dispatcher = LiveDispatcher(ws, config)

    def sink(line: str) -> None:  # pragma: no cover — wiring test
        pass

    assert dispatcher._live_options(scout_contract(), on_stderr=sink).stderr is sink
    assert dispatcher._live_options(scout_contract()).stderr is None


async def test_live_invoke_failure_carries_and_persists_the_stderr_tail(
    ws: Workspace, monkeypatch
) -> None:
    """When a dispatch dies, the subprocess's captured stderr is the diagnosis
    that used to spew into the terminal — it must land in logs/stderr/ and the
    enriched failure text must carry the tail + the file's relpath."""
    import claude_agent_sdk

    from deeper.agents_runtime import LiveDispatchError

    async def fake_query(*, prompt, options):
        options.stderr("Error in hook callback hook_0: minified js " + "x" * 5000)
        options.stderr("error: Stream closed")
        raise RuntimeError("boom")
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    config = ws.load_config().model_copy(update={"mode": "live"})
    with pytest.raises(LiveDispatchError) as excinfo:
        await LiveDispatcher(ws, config)._invoke("p", scout_contract(), 0)
    message = str(excinfo.value)
    assert "error: Stream closed" in message  # the tail rides the failure text
    assert "logs/stderr/" in message  # ...naming the full capture
    logs = list(ws.path("logs/stderr").glob("*-S3-scout-interpretability-research-attempt0.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "error: Stream closed" in text
    assert len(text) < 5000  # long lines are stored truncated (2000-char cap)


async def test_live_invoke_error_result_also_carries_the_stderr_tail(
    ws: Workspace, monkeypatch
) -> None:
    import claude_agent_sdk

    from deeper.agents_runtime import LiveDispatchError

    class ResultMessage:
        subtype = "error_during_execution"
        result = "something died"
        is_error = True

    async def fake_query(*, prompt, options):
        options.stderr("Fatal error in message reader: whatever")
        yield ResultMessage()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    config = ws.load_config().model_copy(update={"mode": "live"})
    with pytest.raises(LiveDispatchError) as excinfo:
        await LiveDispatcher(ws, config)._invoke("p", scout_contract(), 0)
    assert "Fatal error in message reader" in str(excinfo.value)
    assert list(ws.path("logs/stderr").glob("*.log"))


async def test_live_invoke_success_discards_the_stderr_buffer(ws: Workspace, monkeypatch) -> None:
    import claude_agent_sdk

    class ResultMessage:
        subtype = "success"
        result = "ok"
        is_error = False

    async def fake_query(*, prompt, options):
        options.stderr("harmless chatter")
        yield ResultMessage()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    config = ws.load_config().model_copy(update={"mode": "live"})
    await LiveDispatcher(ws, config)._invoke("p", scout_contract(), 0)
    assert not ws.path("logs/stderr").exists()  # nothing persisted on success


async def test_live_invoke_closes_the_generator_when_the_loop_body_raises(
    ws: Workspace, monkeypatch
) -> None:
    """PEP 533 gap: when the async-for BODY raises (here the billing guard),
    the abandoned query() generator's cleanup — the SDK's transport close and
    graceful subprocess shutdown — only runs at GC, on loop-shutdown luck.
    _invoke must aclose() it deterministically before the exception leaves."""
    import claude_agent_sdk

    from deeper.agents_runtime import BillingAuthError

    closed = asyncio.Event()

    class SystemMessage:  # the billing belt trips on a real apiKeySource
        subtype = "init"
        data = {"apiKeySource": "apiKey"}

    async def fake_query(*, prompt, options):
        try:
            yield SystemMessage()
            await asyncio.sleep(60)  # pragma: no cover — the guard raises first
        finally:
            closed.set()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    config = ws.load_config().model_copy(update={"mode": "live"})
    with pytest.raises(BillingAuthError):
        await LiveDispatcher(ws, config)._invoke("p", scout_contract(), 0)
    assert closed.is_set()  # deterministic teardown, not GC luck


async def test_live_invoke_surfaces_unraised_error_results(ws: Workspace, monkeypatch) -> None:
    """An is_error ResultMessage the SDK does NOT raise for must fail the
    dispatch (transient class) instead of burning schema retries on garbage."""
    import claude_agent_sdk

    from deeper.agents_runtime import LiveDispatchError

    class ResultMessage:
        subtype = "error_max_turns"
        result = "stopped: maximum turns exceeded"
        is_error = True

    async def fake_query(*, prompt, options):
        yield ResultMessage()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    config = ws.load_config().model_copy(update={"mode": "live"})
    with pytest.raises(LiveDispatchError, match="error_max_turns"):
        await LiveDispatcher(ws, config)._invoke("p", scout_contract(), 0)
