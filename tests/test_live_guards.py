"""Live-run safety rails: the --max-spend-usd guard and the rule that every
agent failure path lands in PAUSED_ATTENTION with a transcript in logs/ —
a live run must always end in a resumable pause, never a crash."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from deeper.agents_runtime import (
    AgentContract,
    AgentDispatchFailed,
    MockDispatcher,
    SpendCapExceeded,
)
from deeper.config import SizeClass
from deeper.orchestrator import Engine, Node
from deeper.orchestrator.cli import app
from deeper.schemas import RunStatus, SpendEntry, Stage
from deeper.workspace import Workspace

from .helpers import make_workspace

runner = CliRunner()


def burn(ws: Workspace, usd: float, stage: Stage = Stage.S0) -> None:
    """Simulate prior live spend: one ledgered entry of `usd`."""
    state = ws.load_state()
    state.spend.append(
        SpendEntry(
            stage=stage,
            role="interviewer",
            usd=usd,
            input_tokens=1,
            output_tokens=1,
            at=datetime.now(UTC),
        )
    )
    ws.save_state(state)


async def test_dispatcher_refuses_past_the_spend_cap(tmp_path):
    ws = make_workspace(tmp_path)  # quick profile: max_spend_usd 5.0
    burn(ws, 5.0)
    dispatcher = MockDispatcher(ws, ws.load_config())
    contract = AgentContract(
        role="scout",
        stage=Stage.S3,
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="b",
    )
    with pytest.raises(SpendCapExceeded) as excinfo:
        await dispatcher.run_agent(contract)
    assert excinfo.value.total_usd == 5.0
    assert excinfo.value.cap_usd == 5.0


async def test_spend_cap_pauses_run_with_transcript(tmp_path):
    ws = make_workspace(tmp_path)
    burn(ws, 6.0)
    emitted: list[str] = []
    engine = Engine(ws, emit=emitted.append)

    assert await engine.run() is Node.PAUSED_ATTENTION
    state = ws.load_state()
    assert state.status is RunStatus.PAUSED_ATTENTION
    assert state.stage is Stage.S0  # nothing advanced; completed work preserved
    assert any("spend cap" in m and "--max-spend-usd" in m for m in emitted)
    logs = list(ws.path("logs").glob("attention-*-S0-*.md"))
    assert len(logs) == 1
    assert "spend cap crossed" in logs[0].read_text(encoding="utf-8")


def test_resume_with_raised_cap_continues_the_run(tmp_path):
    """`deeper resume --max-spend-usd` rewrites the cap (one commit) and the
    previously-blocked run walks on."""
    ws = make_workspace(tmp_path)
    burn(ws, 6.0)  # over the quick profile's 5.0 default: dispatch would refuse
    result = runner.invoke(app, ["resume", str(ws.root), "--max-spend-usd", "50"])
    assert result.exit_code == 0, result.output
    assert ws.load_config().max_spend_usd == 50.0
    state = ws.load_state()
    assert state.status is RunStatus.GATE_PENDING  # walked S0+S1 to Gate A
    assert any(s.startswith("spend cap 5 -> 50") for s in ws.history())


def test_resume_rejects_invalid_spend_cap(tmp_path):
    ws = make_workspace(tmp_path)
    result = runner.invoke(app, ["resume", str(ws.root), "--max-spend-usd", "-1"])
    assert result.exit_code == 1
    assert "not a valid cap" in result.output


def test_new_records_spend_cap_override(tmp_path):
    result = runner.invoke(
        app,
        [
            "new",
            "spend cap goal",
            "--profile",
            "quick",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--max-spend-usd",
            "2.5",
        ],
    )
    assert result.exit_code == 0, result.output
    run_dir = next((tmp_path / "runs").iterdir())
    assert Workspace.open(run_dir).load_config().max_spend_usd == 2.5


async def test_dispatch_failure_pauses_run_with_traceback_transcript(tmp_path, monkeypatch):
    """An infrastructure failure (here: a missing mock fixture, standing in for
    any SDK/network error) pauses the run with the traceback saved to logs/."""
    monkeypatch.setattr(MockDispatcher, "DISPATCH_RETRY_BACKOFF_S", (0.0,))
    ws = make_workspace(tmp_path)
    emitted: list[str] = []
    engine = Engine(ws, emit=emitted.append, mock_kwargs={"fixtures_dir": tmp_path / "empty"})

    assert await engine.run() is Node.PAUSED_ATTENTION
    state = ws.load_state()
    assert state.status is RunStatus.PAUSED_ATTENTION
    assert state.stage is Stage.S0
    assert any("dispatch failed" in m for m in emitted)
    logs = list(ws.path("logs").glob("attention-*-S0-interviewer.md"))
    assert len(logs) == 1
    transcript = logs[0].read_text(encoding="utf-8")
    assert "MockFixtureMissing" in transcript
    assert "Traceback" in transcript
    # The pause is a commit: the transcript rides the audit trail.
    assert any("S0 paused" in s for s in ws.history())

    # resume() re-enters the stage; with the cause fixed it proceeds normally.
    engine = Engine(ws, emit=emitted.append)  # default fixtures
    assert await engine.resume() is Node.GATE_A


async def test_schema_retry_exhaustion_saves_transcript(tmp_path):
    ws = make_workspace(tmp_path)
    retries = ws.load_config().caps.max_schema_retries
    engine = Engine(
        ws,
        emit=lambda _line: None,
        mock_kwargs={"scripted_responses": {"interviewer": ["not an artifact"] * (retries + 1)}},
    )
    assert await engine.run() is Node.PAUSED_ATTENTION
    logs = list(ws.path("logs").glob("attention-*-S0-interviewer.md"))
    assert len(logs) == 1
    transcript = logs[0].read_text(encoding="utf-8")
    assert "schema retries exhausted" in transcript
    assert "not an artifact" in transcript  # the raw output is preserved


async def test_dispatch_failed_wraps_sdk_errors(tmp_path, monkeypatch):
    """LiveDispatcher failure shape without the network: any exception from
    _invoke becomes AgentDispatchFailed with the cause attached."""
    ws = make_workspace(tmp_path)

    class ExplodingDispatcher(MockDispatcher):
        async def _invoke(self, prompt, contract, attempt):
            raise ConnectionError("api unreachable")

    monkeypatch.setattr(ExplodingDispatcher, "DISPATCH_RETRY_BACKOFF_S", (0.0,))

    dispatcher = ExplodingDispatcher(ws, ws.load_config())
    contract = AgentContract(
        role="scout",
        stage=Stage.S3,
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="b",
    )
    with pytest.raises(AgentDispatchFailed) as excinfo:
        await dispatcher.run_agent(contract)
    assert isinstance(excinfo.value.cause, ConnectionError)


async def test_transient_dispatch_failure_retries_with_backoff(tmp_path, monkeypatch):
    """A one-off infrastructure failure is absorbed by the dispatch backoff
    (observed live: SDK stream errors that succeed on plain re-dispatch);
    only exhaustion of the backoff schedule wraps into AgentDispatchFailed."""
    ws = make_workspace(tmp_path)

    class FlakyDispatcher(MockDispatcher):
        failures_left = 1
        calls = 0

        async def _invoke(self, prompt, contract, attempt):
            type(self).calls += 1
            if type(self).failures_left > 0:
                type(self).failures_left -= 1
                raise ConnectionError("transient stream error")
            return await super()._invoke(prompt, contract, attempt)

    monkeypatch.setattr(FlakyDispatcher, "DISPATCH_RETRY_BACKOFF_S", (0.0, 0.0))
    dispatcher = FlakyDispatcher(ws, ws.load_config())
    contract = AgentContract(
        role="scout",
        stage=Stage.S3,
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="b",
    )
    result = await dispatcher.run_agent(contract)
    assert result.artifacts["option-card-set"] is not None
    assert FlakyDispatcher.calls == 2  # one failure, one clean retry

    FlakyDispatcher.failures_left = 99  # never recovers: schedule exhausts
    with pytest.raises(AgentDispatchFailed):
        await dispatcher.run_agent(contract)
