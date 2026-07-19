"""CLI tests: the real stage registry walked in mock mode via typer's runner."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from deeper.orchestrator import Engine, Node
from deeper.orchestrator.cli import app
from deeper.schemas import AllocationTable, AngleMap, Brief, RunStatus
from deeper.workspace import Workspace

runner = CliRunner()


def new_run(tmp_path, goal="pick a senior research project"):
    result = runner.invoke(
        app, ["new", goal, "--profile", "quick", "--runs-dir", str(tmp_path / "runs")]
    )
    assert result.exit_code == 0, result.output
    runs = list((tmp_path / "runs").iterdir())
    assert len(runs) == 1
    return result, runs[0]


def approve_gate_a(run_dir) -> None:
    (run_dir / "gates" / "gate-a.yaml").write_text("approved: true\n", encoding="utf-8")


def test_new_reaches_gate_a_with_instructions(tmp_path):
    result, run_dir = new_run(tmp_path)
    assert "gate-a" in result.output
    assert "gates/gate-a.yaml" in result.output  # tells the user exactly what to edit
    ws = Workspace.open(run_dir)
    assert ws.load_state().status is RunStatus.GATE_PENDING
    ws.read_artifact("brief.md", Brief)  # S0 mock artifacts validate
    ws.read_artifact("angles/map.yaml", AngleMap)
    assert ws.load_config().goal == "pick a senior research project"


def test_new_twice_same_day_gets_distinct_run_dirs(tmp_path):
    runner.invoke(
        app, ["new", "same goal", "--profile", "quick", "--runs-dir", str(tmp_path / "runs")]
    )
    result = runner.invoke(
        app, ["new", "same goal", "--profile", "quick", "--runs-dir", str(tmp_path / "runs")]
    )
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "runs").iterdir())) == 2


def test_new_unknown_profile_fails_cleanly(tmp_path):
    result = runner.invoke(
        app, ["new", "goal", "--profile", "nope", "--runs-dir", str(tmp_path / "runs")]
    )
    assert result.exit_code == 1
    assert "unknown profile" in result.output


def test_resume_past_gate_a_walks_s2_to_gate_c_then_s8_report(tmp_path):
    _, run_dir = new_run(tmp_path)
    approve_gate_a(run_dir)
    result = runner.invoke(app, ["resume", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "allocation.yaml" in result.output  # S2 table printed at the gate exit
    assert "reflow" in result.output  # S3 redistributed early-stopped units
    assert "gate-b" in result.output  # paused at the values review
    ws = Workspace.open(run_dir)
    ws.read_artifact("allocation.yaml", AllocationTable)
    assert ws.load_state().status is RunStatus.GATE_PENDING

    (run_dir / "gates" / "gate-b.yaml").write_text(
        "approved: true\npreference_slot_weight: 0.25\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["resume", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "preference-slot weight set to 0.25" in result.output
    assert "finalists" in result.output  # S5 shortlist reported
    assert "deep-diving" in result.output  # S6 ran its round loops
    assert "rank inversion" in result.output  # S7's docket over the dual boards
    assert "NOT auto-executed" in result.output  # the re-divergence proposal
    assert "gate-c" in result.output  # paused at the contender review

    (run_dir / "gates" / "gate-c.yaml").write_text("approved: true\n", encoding="utf-8")
    result = runner.invoke(app, ["resume", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "decision report written" in result.output  # S8 synthesized
    assert "is complete" in result.output  # ...and the run reached DONE
    ws = Workspace.open(run_dir)
    assert ws.load_state().status is RunStatus.DONE
    # Resuming again is safe and repeats the same terminal report.
    again = runner.invoke(app, ["resume", str(run_dir)])
    assert again.exit_code == 0
    assert "is complete" in again.output

    # `deeper report`: path + winner + both boards + flag + pass rates + spend.
    result = runner.invoke(app, ["report", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "decision-report.md" in result.output
    assert "winner:" in result.output and "sae-feature-atlas" in result.output
    assert "DISSENT UNREBUTTED" in result.output
    assert "destination-only" in result.output and "preference-adjusted" in result.output
    assert "sensitivity:" in result.output
    assert "pass rate" in result.output
    assert "agent spend: $" in result.output


def test_resume_with_undecided_gate_repauses(tmp_path):
    _, run_dir = new_run(tmp_path)
    result = runner.invoke(app, ["resume", str(run_dir)])
    assert result.exit_code == 0
    assert "no decision yet" in result.output


def test_status_shows_node_gates_and_spend(tmp_path):
    _, run_dir = new_run(tmp_path)
    result = runner.invoke(app, ["status", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "gate-a" in result.output
    assert "pending" in result.output
    assert "total" in result.output  # spend table
    # status is read-only: state unchanged after any number of calls.
    before = (run_dir / "state.json").read_text(encoding="utf-8")
    runner.invoke(app, ["status", str(run_dir)])
    assert (run_dir / "state.json").read_text(encoding="utf-8") == before


def test_status_spend_prints_the_stage_by_role_matrix(tmp_path):
    _, run_dir = new_run(tmp_path)  # mock walk: S0 + S1 spend is ledgered
    result = runner.invoke(app, ["status", str(run_dir), "--spend"])
    assert result.exit_code == 0, result.output
    assert "stage x role" in result.output
    # Rows for the walked stages, columns for the roles they dispatched (long
    # role names wrap at narrow widths, so assert wrap-safe tokens).
    assert "interview" in result.output
    assert "merger" in result.output
    assert "S1" in result.output
    assert "x1" in result.output  # per-cell attempt counts
    # Without the flag the matrix is not printed.
    plain = runner.invoke(app, ["status", str(run_dir)])
    assert "stage x role" not in plain.output


def test_status_missing_run_fails_cleanly(tmp_path):
    result = runner.invoke(app, ["status", str(tmp_path / "nope")])
    assert result.exit_code == 1
    assert "no run found" in result.output


def test_rerun_s1_invalidates_and_rewalks_to_gate_a(tmp_path):
    _, run_dir = new_run(tmp_path)
    approve_gate_a(run_dir)
    runner.invoke(app, ["resume", str(run_dir)])

    result = runner.invoke(app, ["rerun", str(run_dir), "--stage", "S1"])
    assert result.exit_code == 0, result.output
    assert "invalidated" in result.output
    assert "gate-a" in result.output  # walked S1 again and re-paused at the gate
    ws = Workspace.open(run_dir)
    assert ws.load_state().status is RunStatus.GATE_PENDING
    assert not ws.path("allocation.yaml").exists()  # downstream stayed invalid
    assert any(s.startswith("rerun S1") for s in ws.history())


def test_rerun_s3_angle_scoped_via_cli(tmp_path):
    _, run_dir = new_run(tmp_path)
    approve_gate_a(run_dir)
    runner.invoke(app, ["resume", str(run_dir)])
    ws = Workspace.open(run_dir)
    angle_id = ws.read_artifact("angles/map.yaml", AngleMap).angles[0].id

    result = runner.invoke(app, ["rerun", str(run_dir), "--stage", "S3", "--angle", angle_id])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["rerun", str(run_dir), "--stage", "S3", "--angle", "not-an-angle"])
    assert result.exit_code == 1
    assert "unknown angle" in result.output


def test_rerun_unknown_stage_fails_cleanly(tmp_path):
    _, run_dir = new_run(tmp_path)
    result = runner.invoke(app, ["rerun", str(run_dir), "--stage", "S99"])
    assert result.exit_code == 1
    assert "unknown stage" in result.output


def test_report_before_s8_says_no_report_yet(tmp_path):
    _, run_dir = new_run(tmp_path)
    result = runner.invoke(app, ["report", str(run_dir)])
    assert result.exit_code == 0
    assert "no report yet" in result.output
    # A stray markdown alone is not a report — the validated artifact is.
    (run_dir / "report" / "decision-report.md").write_text("# report\n", encoding="utf-8")
    result = runner.invoke(app, ["report", str(run_dir)])
    assert "no report yet" in result.output


async def test_schema_retry_exhaustion_pauses_run_for_attention(tmp_path):
    """The real dispatcher path: an interviewer that never validates exhausts
    caps.max_schema_retries and the engine lands in PAUSED_ATTENTION."""
    from deeper.config import RunConfig, profile_config
    from deeper.schemas import Stage

    data = profile_config("quick").model_dump(mode="json")
    data["goal"] = "retry exhaustion goal"
    ws = Workspace.create(tmp_path / "run", RunConfig.model_validate(data))
    retries = ws.load_config().caps.max_schema_retries
    engine = Engine(
        ws,
        emit=lambda _line: None,
        mock_kwargs={"scripted_responses": {"interviewer": ["not an artifact"] * (retries + 1)}},
    )
    assert await engine.run() is Node.PAUSED_ATTENTION
    state = ws.load_state()
    assert state.status is RunStatus.PAUSED_ATTENTION
    assert state.stage is Stage.S0
    # Every failed attempt was still ledgered (spend + retry counts persist).
    assert len(state.spend) >= retries + 1
    assert state.retry_counts.get("S0:interviewer:-") == retries


@pytest.mark.parametrize("command", [["status"], ["resume"], ["report"]])
def test_commands_resolve_run_names_under_runs_dir(tmp_path, monkeypatch, command):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["new", "cwd resolution goal", "--profile", "quick"])
    assert result.exit_code == 0, result.output
    run_name = next(p.name for p in (tmp_path / "runs").iterdir())
    result = runner.invoke(app, [*command, run_name])
    assert result.exit_code == 0, result.output


# -- multi-line-paste-safe interview input (M2 live-run finding 1) ---------------


def _lines(*items):
    it = iter(items)

    def read_line() -> str:
        value = next(it)
        if value is EOFError:
            raise EOFError
        return value

    return read_line


def _pending(*flags):
    it = iter(flags)
    return lambda: next(it, False)


def test_read_answer_typed_single_line_submits_immediately():
    from deeper.orchestrator.cli import _read_answer

    answer = _read_answer(_lines("  just one line  "), _pending(False), grace_s=0)
    assert answer == "just one line"


def test_read_answer_drains_a_multiline_paste_into_one_answer():
    """The observed live failure: a pasted block sent line 1 as the answer and
    the buffered rest auto-answered the NEXT questions unseen. Buffered lines
    are one answer — interior blank lines (a multi-paragraph paste) included."""
    from deeper.orchestrator.cli import _read_answer

    answer = _read_answer(
        _lines("first paragraph line", "", "second paragraph line"),
        _pending(True, True, False),
        grace_s=0,
    )
    assert answer == "first paragraph line\n\nsecond paragraph line"


def test_read_answer_eof_while_draining_keeps_what_was_read():
    from deeper.orchestrator.cli import _read_answer

    answer = _read_answer(_lines("kept line", EOFError), _pending(True), grace_s=0)
    assert answer == "kept line"


def test_read_answer_eof_on_first_line_propagates():
    """The caller (ask) turns a first-line EOF into a decline — the pre-fix
    behavior, preserved."""
    from deeper.orchestrator.cli import _read_answer

    with pytest.raises(EOFError):
        _read_answer(_lines(EOFError), _pending(False), grace_s=0)


def test_paste_pending_grace_window_polls_until_input_arrives():
    from deeper.orchestrator.cli import _paste_pending

    assert _paste_pending(_pending(False, False, True), grace_s=0.5) is True
    assert _paste_pending(_pending(), grace_s=0) is False
