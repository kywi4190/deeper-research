"""S3 scouting-stage tests over the canonical mock scenario: per-angle
scout→critic pipelines, the one revision round, the redundancy early-stop, and
budget reflow onto critic-flagged misses (design §5/S3)."""

from __future__ import annotations

import pytest

from deeper.schemas import AllocationKind, AllocationTable, CardCritique, OptionCardSet
from deeper.stages.s3_scouting import (
    ScoutingStage,
    cards_path,
    critique_path,
    distinct_cards,
    needs_revision,
    returned_units,
)

from .helpers import (
    RecordingMockDispatcher,
    make_ctx,
    make_workspace,
    write_s0_artifacts,
    write_s1_s2_artifacts,
)

ANGLES = [
    "interpretability-research",
    "evaluation-science",
    "training-efficiency",
    "small-model-science",
    "applied-domain-collaboration",
    "research-tooling",
    "negative-results-science",
    "supervisor-pipeline",
]
SATURATED = {"applied-domain-collaboration", "training-efficiency"}


# -- pure reflow math ---------------------------------------------------------------


def test_distinct_cards_floors_the_redundant_count():
    assert distinct_cards(3, 55) == 2  # floor(1.65) = 1 redundant
    assert distinct_cards(3, 60) == 2
    assert distinct_cards(4, 0) == 4
    assert distinct_cards(4, 100) == 0


def test_returned_units_only_for_early_stopped_angles():
    # Not saturated: nothing returns, no matter how short the set came up.
    assert returned_units(2, 2, 30, stop_pct=40) == 0
    # Saturated: allocated minus units the distinct cards actually consumed.
    assert returned_units(2, 3, 55, stop_pct=40) == 1  # 2 distinct -> 1 unit consumed
    assert returned_units(2, 3, 60, stop_pct=40) == 1
    assert returned_units(1, 6, 90, stop_pct=40) == 0  # never negative


def test_needs_revision_ignores_missed_options():
    clean = CardCritique(angle_id="a", redundancy_pct=0, missed_options=["a miss"])
    assert not needs_revision(clean)  # misses are the reflow pool's business
    flagged = CardCritique(
        angle_id="a",
        redundancy_pct=0,
        completeness_issues=[{"card_id": "x", "issue": "thin evidence"}],
    )
    assert needs_revision(flagged)


# -- the full mock walk ---------------------------------------------------------------


@pytest.fixture()
async def run(tmp_path):
    ws = make_workspace(tmp_path)
    write_s0_artifacts(ws)
    write_s1_s2_artifacts(ws)
    emitted: list[str] = []
    config = ws.load_config()
    dispatcher = RecordingMockDispatcher(ws, config)
    ctx = make_ctx(ws, dispatcher=dispatcher, emitted=emitted)
    stage = ScoutingStage()
    stage.validate_inputs(ctx)
    await stage.execute(ctx)
    return ws, ctx, stage, dispatcher, emitted


async def test_every_allocated_angle_gets_cards_and_critique(run):
    ws, ctx, stage, dispatcher, _ = run
    for angle in ANGLES:
        cards = ws.read_artifact(cards_path(angle), OptionCardSet)
        assert cards.angle_id == angle
        critique = ws.read_artifact(critique_path(angle), CardCritique)
        assert critique.angle_id == angle
    assert stage.is_complete(ctx)


async def test_budget_line_injected_from_allocation(run):
    _, _, _, dispatcher, _ = run
    prompt = next(
        p for role, c, p in dispatcher.invocations if role == "scout" and c == "evaluation-science"
    )
    assert "~2 budget unit(s) ≈ target 4 option cards" in prompt


async def test_one_revision_round_against_the_critique(run):
    ws, _, _, dispatcher, _ = run
    contexts = [c for role, c, _ in dispatcher.invocations if role == "scout"]
    # Only interpretability's critique carries fixable issues -> exactly one -rev pass.
    assert contexts.count("interpretability-research-rev") == 1
    assert [c for c in contexts if c and c.endswith("-rev")] == ["interpretability-research-rev"]
    # The revision landed: the backdoor card gained its second evidence item.
    cards = ws.read_artifact(cards_path("interpretability-research"), OptionCardSet)
    backdoor = next(c for c in cards.cards if c.id == "backdoor-probe-study")
    assert len(backdoor.preliminary_evidence) == 2
    # The revision prompt carried the critique verbatim.
    rev_prompt = next(
        p for role, c, p in dispatcher.invocations if c == "interpretability-research-rev"
    )
    assert "REVISION ROUND" in rev_prompt and "probe-method viability" in rev_prompt


async def test_redundancy_early_stop_skips_revision_and_returns_units(run):
    _, _, _, dispatcher, emitted = run
    contexts = [c for role, c, _ in dispatcher.invocations if role == "scout"]
    for angle in SATURATED:
        # Saturated angles get no revision pass even though their critiques
        # list distinctness issues — early stop trumps revision.
        assert f"{angle}-rev" not in contexts
        assert f"{angle}-topup" not in contexts  # and no reflow either
    assert any("redundancy 60% > 40%" in m and "early stop" in m for m in emitted)
    assert any("redundancy 55% > 40%" in m for m in emitted)


async def test_reflow_targets_missed_options_of_unsaturated_angles(run):
    ws, _, _, dispatcher, emitted = run
    table = ws.read_artifact("options/reflow.yaml", AllocationTable)
    assert table.kind is AllocationKind.REFLOW
    assert {r.angle_id: r.units for r in table.rows} == {
        "interpretability-research": 1,
        "evaluation-science": 1,
    }
    contexts = [c for role, c, _ in dispatcher.invocations if role == "scout"]
    assert contexts.count("interpretability-research-topup") == 1
    assert contexts.count("evaluation-science-topup") == 1
    # The top-up contract names the critique's specific missed options.
    prompt = next(p for _, c, p in dispatcher.invocations if c == "evaluation-science-topup")
    assert "REFLOW TOP-UP" in prompt and "long-horizon agent-behavior" in prompt.lower()
    # And the new cards merged into the angle's set.
    interp = ws.read_artifact(cards_path("interpretability-research"), OptionCardSet)
    assert "feature-steering-study" in [c.id for c in interp.cards]
    evaluation = ws.read_artifact(cards_path("evaluation-science"), OptionCardSet)
    assert "long-horizon-eval-harness" in [c.id for c in evaluation.cards]
    assert any("reflow — 2 returned unit(s) over 2 angle(s)" in m for m in emitted)


async def test_reexecution_is_idempotent(run):
    ws, ctx, stage, dispatcher, _ = run
    before = len(dispatcher.invocations)
    assert stage.is_complete(ctx)
    await stage.execute(ctx)  # engine would skip; even a direct call must not re-spend
    assert len(dispatcher.invocations) == before


async def test_scout_contract_never_contains_preferences(run):
    ws, _, _, dispatcher, _ = run
    prefs_text = ws.path("preferences.yaml").read_text(encoding="utf-8")
    marker = prefs_text.strip().splitlines()[0]  # distinctive content line
    assert marker
    for role, _context, prompt in dispatcher.invocations:
        assert role in {"scout", "card-critic"}
        assert marker not in prompt


async def test_crash_between_cards_and_critique_resumes_mid_angle(tmp_path):
    ws = make_workspace(tmp_path)
    write_s0_artifacts(ws)
    write_s1_s2_artifacts(ws)
    dispatcher = RecordingMockDispatcher(ws, ws.load_config())
    ctx = make_ctx(ws, dispatcher=dispatcher)
    stage = ScoutingStage()
    await stage.execute(ctx)
    # Simulate the crash: one angle's critique lost, its cards intact.
    ws.path(critique_path("small-model-science")).unlink()
    ws.path("options/reflow.yaml").unlink()
    assert not stage.is_complete(ctx)
    before_scouts = sum(
        1 for r, c, _ in dispatcher.invocations if r == "scout" and c == "small-model-science"
    )
    await stage.execute(ctx)
    assert stage.is_complete(ctx)
    # The scout did not re-run (cards were on disk); only the critic did.
    after_scouts = sum(
        1 for r, c, _ in dispatcher.invocations if r == "scout" and c == "small-model-science"
    )
    assert after_scouts == before_scouts
