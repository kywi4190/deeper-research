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
