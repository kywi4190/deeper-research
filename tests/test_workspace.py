"""Workspace: §7 tree creation, git audit trail, schema-checked artifact I/O,
state resume, corruption rejection."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from deeper.allocation import allocate
from deeper.config import profile_config
from deeper.schemas import (
    AllocationTable,
    RunStatus,
    SpendEntry,
    Stage,
)
from deeper.workspace import CONFIG_FILE, RUN_DIRS, STATE_FILE, Workspace, WorkspaceError


@pytest.fixture
def ws(tmp_path):
    return Workspace.create(tmp_path / "2026-07-06-test-run", profile_config("quick"))


def sample_table() -> AllocationTable:
    return allocate({"angle-a": 0.8, "angle-b": 0.2}, 10, 2, 1.0, 100.0)


# -- creation ------------------------------------------------------------------


def test_create_builds_design_tree(ws):
    for d in RUN_DIRS:
        assert (ws.root / d).is_dir(), f"missing workspace dir: {d}"
    assert (ws.root / CONFIG_FILE).is_file()
    assert (ws.root / STATE_FILE).is_file()
    assert (ws.root / ".git").is_dir()


def test_create_writes_initial_state_and_config(ws):
    state = ws.load_state()
    assert state.run_id == "2026-07-06-test-run"
    assert state.stage is Stage.S0
    assert state.status is RunStatus.RUNNING
    assert ws.load_config().profile == "quick"


def test_create_makes_initial_commit(ws):
    assert ws.history() == ["run created (profile=quick)"]


def test_create_refuses_nonempty_directory(tmp_path):
    target = tmp_path / "run"
    target.mkdir()
    (target / "junk.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="not empty"):
        Workspace.create(target, profile_config("quick"))


# -- artifact round-trip + git ---------------------------------------------------


def test_artifact_round_trip_with_commit(ws):
    table = sample_table()
    ws.write_artifact("allocation.yaml", table, commit_message="S2 complete: allocation")
    assert ws.read_artifact("allocation.yaml", AllocationTable) == table
    assert ws.history()[0] == "S2 complete: allocation"


def test_every_stage_completion_is_one_commit(ws):
    ws.write_artifact("allocation.yaml", sample_table(), commit_message="S2 complete")
    state = ws.load_state()
    ws.save_state(state.model_copy(update={"stage": Stage.S3}), commit_message="gate decision")
    assert ws.history() == ["gate decision", "S2 complete", "run created (profile=quick)"]


def test_commit_with_no_changes_returns_none(ws):
    assert ws.commit("nothing happened") is None
    assert len(ws.history()) == 1


def test_overwrite_is_atomic_replacement(ws):
    ws.write_artifact("allocation.yaml", sample_table())
    bigger = allocate({"angle-a": 0.8, "angle-b": 0.2}, 12, 2, 1.0, 100.0)
    ws.write_artifact("allocation.yaml", bigger)
    assert ws.read_artifact("allocation.yaml", AllocationTable) == bigger
    assert not list(ws.root.glob("*.tmp")), "temp files must not linger"


def test_json_suffix_writes_json(ws):
    raw = (ws.root / STATE_FILE).read_text(encoding="utf-8")
    assert json.loads(raw)["stage"] == "S0"


def test_mutated_invalid_model_never_reaches_disk(ws):
    table = sample_table()
    table.rows[0].units = 999  # breaks sum-conservation after construction
    with pytest.raises(ValidationError, match="sum exactly"):
        ws.write_artifact("allocation.yaml", table)
    assert not (ws.root / "allocation.yaml").exists()


def test_path_escaping_workspace_is_rejected(ws):
    with pytest.raises(WorkspaceError, match="escapes"):
        ws.write_artifact("../evil.yaml", sample_table())


# -- corruption rejection ----------------------------------------------------------


def test_corrupted_artifact_rejected_on_read(ws):
    (ws.root / "allocation.yaml").write_text("kind: nonsense\nrows: []\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        ws.read_artifact("allocation.yaml", AllocationTable)


def test_missing_artifact_raises(ws):
    with pytest.raises(WorkspaceError, match="not found"):
        ws.read_artifact("rubric.yaml", AllocationTable)


def test_open_rejects_corrupted_state(ws):
    (ws.root / STATE_FILE).write_text('{"run_id": "x"}', encoding="utf-8")
    with pytest.raises(ValidationError):
        Workspace.open(ws.root)


# -- resume -------------------------------------------------------------------------


def test_state_resume_round_trip(ws):
    state = ws.load_state()
    advanced = state.model_copy(
        update={
            "stage": Stage.S2,
            "spend": [
                SpendEntry(
                    stage=Stage.S1,
                    role="merger",
                    usd=0.42,
                    input_tokens=1000,
                    output_tokens=500,
                    at=datetime.now(UTC),
                )
            ],
            "updated_at": datetime.now(UTC),
        }
    )
    ws.save_state(advanced, commit_message="S1 complete")
    resumed = Workspace.open(ws.root)
    assert resumed.load_state() == advanced
    assert resumed.load_state().total_usd() == pytest.approx(0.42)


def test_open_missing_directory_raises(tmp_path):
    with pytest.raises(WorkspaceError, match="does not exist"):
        Workspace.open(tmp_path / "no-such-run")


def test_open_non_workspace_directory_raises(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(WorkspaceError, match="missing"):
        Workspace.open(tmp_path / "plain")
