"""Rerun invalidation cascade tests (design §8 surgical re-execution)."""

from __future__ import annotations

import pytest

from deeper.config import RunConfig, profile_config
from deeper.orchestrator import RerunError, invalidate
from deeper.schemas import GateADecision, GateName, GateStatus, RunStatus, Stage
from deeper.workspace import Workspace

from .test_orchestrator_engine import FAKE_OUTPUT

DUMMY = GateADecision(approved=False)

# A minimal-but-valid angle map so --angle can resolve names against it.
ANGLE_MAP_YAML = """\
angles:
  - id: alpha-angle
    name: Alpha Angle
    definition: first region
    distinctness_rationale: distinct because alpha
    example_options: [option one]
    relevance_prior: 0.7
    prior_justification: grounded in the brief
    contributing_heuristics: [first-principles]
  - id: beta-angle
    name: Beta Angle
    definition: second region
    distinctness_rationale: distinct because beta
    example_options: [option two]
    relevance_prior: 0.3
    prior_justification: grounded in the destination
    contributing_heuristics: [analogist]
"""


@pytest.fixture()
def seeded(tmp_path) -> Workspace:
    """A workspace with artifacts through S5, gates A approved / B pending."""
    data = profile_config("quick").model_dump(mode="json")
    data["goal"] = "rerun test goal"
    ws = Workspace.create(tmp_path / "run", RunConfig.model_validate(data))
    for stage in (Stage.S0, Stage.S2, Stage.S4, Stage.S5):
        ws.write_artifact(FAKE_OUTPUT[stage], DUMMY)
    ws.path("angles/map.yaml").write_text(ANGLE_MAP_YAML, encoding="utf-8")
    for angle in ("alpha-angle", "beta-angle"):
        target = ws.path(f"options/{angle}/cards.yaml")
        target.parent.mkdir(parents=True, exist_ok=True)
        ws.write_artifact(f"options/{angle}/cards.yaml", DUMMY)
    ws.path("gates/gate-a.yaml").write_text("approved: true\n", encoding="utf-8")
    ws.path("gates/gate-b.yaml").write_text("approved: false\n", encoding="utf-8")
    state = ws.load_state()
    state.stage = Stage.S5
    state.gates[GateName.A] = GateStatus.APPROVED
    state.gates[GateName.B] = GateStatus.PENDING
    ws.save_state(state)
    ws.commit("seeded through S5")
    return ws


def test_rerun_s3_cascades_downstream_only(seeded):
    removed = invalidate(seeded, Stage.S3)

    # Target + downstream gone (including the pending Gate B decision file)…
    assert not seeded.path("options/alpha-angle").exists()
    assert not seeded.path("options/beta-angle").exists()
    assert not seeded.path("rubric.yaml").exists()
    assert not seeded.path("screening/scores.yaml").exists()
    assert not seeded.path("gates/gate-b.yaml").exists()
    assert "options" in removed and "rubric.yaml" in removed

    # …upstream intact, including the approved Gate A decision.
    assert seeded.path("brief.md").is_file()
    assert seeded.path("angles/map.yaml").is_file()
    assert seeded.path("allocation.yaml").is_file()
    assert seeded.path("gates/gate-a.yaml").is_file()

    state = seeded.load_state()
    assert state.stage is Stage.S3
    assert state.status is RunStatus.RUNNING
    assert state.gates[GateName.A] is GateStatus.APPROVED
    assert state.gates[GateName.B] is GateStatus.NOT_REACHED
    assert state.gates[GateName.C] is GateStatus.NOT_REACHED

    # The invalidation is one git-tracked commit; the tree skeleton survives.
    assert any(s.startswith("rerun S3") for s in seeded.history())
    assert seeded.path("options").is_dir()
    assert seeded.path("screening").is_dir()


def test_rerun_s3_angle_scoped_deletes_only_that_angle(seeded):
    invalidate(seeded, Stage.S3, angle="Alpha Angle")  # resolves by name too
    assert not seeded.path("options/alpha-angle").exists()
    assert seeded.path("options/beta-angle/cards.yaml").is_file()
    # Downstream is still fully invalidated — it consumed the angle's cards.
    assert not seeded.path("rubric.yaml").exists()
    assert not seeded.path("screening/scores.yaml").exists()
    assert seeded.load_state().stage is Stage.S3
    assert any("angle: alpha-angle" in s for s in seeded.history())


def test_rerun_s1_resets_gate_a_too(seeded):
    invalidate(seeded, Stage.S1)
    assert not seeded.path("angles/map.yaml").exists()
    assert seeded.path("angles/raw").is_dir()  # skeleton restored
    assert not seeded.path("gates/gate-a.yaml").exists()
    state = seeded.load_state()
    assert state.stage is Stage.S1
    assert state.gates[GateName.A] is GateStatus.NOT_REACHED
    assert seeded.path("brief.md").is_file()  # S0 untouched


def test_rerun_preserves_spend_audit_trail(seeded):
    before = seeded.load_state().spend
    invalidate(seeded, Stage.S1)
    assert seeded.load_state().spend == before


def test_angle_scope_rejected_off_s3(seeded):
    with pytest.raises(RerunError, match="--angle only applies"):
        invalidate(seeded, Stage.S4, angle="alpha-angle")


def test_unknown_angle_rejected_with_known_ids(seeded):
    with pytest.raises(RerunError, match="alpha-angle, beta-angle"):
        invalidate(seeded, Stage.S3, angle="no-such-angle")
