"""S5 screening-stage tests over the canonical mock scenario, asserting the
design's landmark behaviors end to end: the dark horse advancing on its band
alone, the confirmed kill cutting the top scorer, the angle cap, the breadth
guardrail, and the concentration cutoff (top-k + dark-horse margin) — plus the
P9 quarantine at the contract-assembly level."""

from __future__ import annotations

import pytest

from deeper.agents_runtime import AgentOutputInvalid
from deeper.schemas import (
    ScreeningResult,
    Shortlist,
    ShortlistCause,
    ShortlistOutcome,
)
from deeper.stages.s3_scouting import ScoutingStage
from deeper.stages.s4_rubric import RubricStage
from deeper.stages.s5_screening import ScreeningStage, batch_path

from .helpers import (
    RecordingMockDispatcher,
    make_ctx,
    make_workspace,
    write_s0_artifacts,
    write_s1_s2_artifacts,
)

THRESHOLD = 3.5  # quick profile


@pytest.fixture()
async def run(tmp_path):
    ws = make_workspace(tmp_path)
    write_s0_artifacts(ws)
    write_s1_s2_artifacts(ws)
    emitted: list[str] = []
    dispatcher = RecordingMockDispatcher(ws, ws.load_config())
    ctx = make_ctx(ws, dispatcher=dispatcher, emitted=emitted)
    await ScoutingStage().execute(ctx)
    await RubricStage().execute(ctx)
    stage = ScreeningStage()
    stage.validate_inputs(ctx)
    await stage.execute(ctx)
    return ws, ctx, stage, dispatcher, emitted


def decision_of(shortlist: Shortlist, option_id: str):
    return next(d for d in shortlist.decisions if d.option_id == option_id)


async def test_scores_and_shortlist_written_and_stage_complete(run):
    ws, ctx, stage, _, _ = run
    scores = ws.read_artifact("screening/scores.yaml", ScreeningResult)
    shortlist = ws.read_artifact("screening/shortlist.md", Shortlist)
    assert len(scores.options) == 22  # every card, including the two top-ups
    assert len(shortlist.decisions) == 22  # one auditable decision per option
    assert all(d.reason for d in shortlist.decisions)
    assert stage.is_complete(ctx)


async def test_dark_horse_advances_on_upper_bound_alone(run):
    ws, _, _, _, _ = run
    scores = ws.read_artifact("screening/scores.yaml", ScreeningResult)
    dark = next(o for o in scores.options if o.option_id == "cot-hurts-small-models")
    # The fixture is built so the point estimate fails the floor and only the
    # wide band's upper bound carries it (within the dark-horse margin of the
    # top-k cutoff) — assert exactly that.
    assert dark.weighted_point < THRESHOLD <= dark.weighted_ucb
    shortlist = ws.read_artifact("screening/shortlist.md", Shortlist)
    d = decision_of(shortlist, "cot-hurts-small-models")
    assert d.decision is ShortlistOutcome.ADVANCED
    assert d.cause is ShortlistCause.UCB_ABOVE_THRESHOLD
    assert "wide uncertainty band" in d.reason


async def test_concentration_cutoff_bounds_the_shortlist(run):
    ws, ctx, _, _, _ = run
    shortlist = ws.read_artifact("screening/shortlist.md", Shortlist)
    # The M1 live-run regression guard: the shortlist is bounded by the hard
    # cap no matter how generously the screener banded.
    assert len(shortlist.finalist_ids) <= ctx.config.caps.max_finalists
    # Above the absolute floor but beyond the dark-horse margin of the top-k
    # cutoff: cut for concentration, explicitly not on the merits.
    d = decision_of(shortlist, "long-horizon-eval-harness")
    assert d.decision is ShortlistOutcome.CUT
    assert d.cause is ShortlistCause.BELOW_CUTOFF
    assert "nothing here eliminates the option on the merits" in d.reason


async def test_confirmed_kill_cuts_the_top_scorer(run):
    ws, _, _, _, _ = run
    scores = ws.read_artifact("screening/scores.yaml", ScreeningResult)
    killed = next(o for o in scores.options if o.option_id == "supervisor-submission-extension")
    # Highest point estimate in the whole run — and still out.
    assert killed.weighted_point == max(o.weighted_point for o in scores.options)
    shortlist = ws.read_artifact("screening/shortlist.md", Shortlist)
    d = decision_of(shortlist, "supervisor-submission-extension")
    assert d.decision is ShortlistOutcome.CUT
    assert d.cause is ShortlistCause.KILL_RISK_CONFIRMED
    assert "incoming postdoc" in d.reason  # the confirmed fact, auditable


async def test_angle_cap_limits_interpretability_to_three(run):
    ws, _, _, _, _ = run
    shortlist = ws.read_artifact("screening/shortlist.md", Shortlist)
    scores = ws.read_artifact("screening/scores.yaml", ScreeningResult)
    angle_of = {o.option_id: o.angle_id for o in scores.options}
    interp_finalists = [
        f for f in shortlist.finalist_ids if angle_of[f] == "interpretability-research"
    ]
    assert len(interp_finalists) == 3
    d = decision_of(shortlist, "feature-steering-study")  # 4th-highest UCB in the angle
    assert d.decision is ShortlistOutcome.CUT
    assert d.cause is ShortlistCause.ANGLE_CAP


async def test_breadth_guardrail_adds_unrepresented_top_half_angles(run):
    ws, _, _, _, _ = run
    shortlist = ws.read_artifact("screening/shortlist.md", Shortlist)
    for option_id in ("lab-codebase-takeover", "model-collapse-dynamics"):
        d = decision_of(shortlist, option_id)
        assert d.decision is ShortlistOutcome.ADVANCED
        assert d.cause is ShortlistCause.DIVERSITY_GUARDRAIL_ADD
    # research-tooling is also unrepresented but bottom-half by prior: no add.
    d = decision_of(shortlist, "activation-caching-lib")
    assert d.decision is ShortlistOutcome.CUT
    assert shortlist.finalist_ids == [
        "sae-feature-atlas",
        "contamination-robust-benchmark",
        "backdoor-probe-study",
        "cot-hurts-small-models",
        "activation-patching-toolkit",
        "model-collapse-dynamics",
        "lab-codebase-takeover",
    ]


async def test_screener_contract_contains_preferences_scout_does_not(run):
    ws, _, _, dispatcher, _ = run
    prefs_marker = ws.path("preferences.yaml").read_text(encoding="utf-8").strip().splitlines()[0]
    screener_batches = [
        (context, prompt) for role, context, prompt in dispatcher.invocations if role == "screener"
    ]
    # One batch per allocated angle, each carrying preferences (P9), the rubric,
    # and exactly its own angle's cards; no other role's contract has preferences.
    assert {c for c, _p in screener_batches} == {
        "interpretability-research",
        "evaluation-science",
        "training-efficiency",
        "small-model-science",
        "applied-domain-collaboration",
        "research-tooling",
        "negative-results-science",
        "supervisor-pipeline",
    }
    for _context, prompt in screener_batches:
        assert "### input: preferences" in prompt
        assert prefs_marker in prompt
        assert "### input: rubric" in prompt
        assert "### input: cards" in prompt
    for role, _context, prompt in dispatcher.invocations:
        if role != "screener":
            assert prefs_marker not in prompt, f"{role} contract leaked preferences"


async def test_reexecution_reuses_scores_and_reapplies_pure_code(run):
    ws, ctx, stage, dispatcher, _ = run
    screener_calls = sum(1 for r, _c, _p in dispatcher.invocations if r == "screener")
    assert screener_calls == 8  # one batch per allocated angle
    ws.path("screening/shortlist.md").unlink()  # crash between the two writes
    assert not stage.is_complete(ctx)
    await stage.execute(ctx)
    assert stage.is_complete(ctx)
    assert sum(1 for r, _c, _p in dispatcher.invocations if r == "screener") == 8


async def test_batches_persist_per_angle_and_are_not_repaid(run):
    """Finding 7: every batch lands at screening/batches/{angle}.yaml the
    moment it passes its integrity checks, and a re-screen (scores.yaml lost
    before the merge settled) re-dispatches nothing."""
    ws, ctx, _stage, dispatcher, emitted = run
    batch_files = sorted(ws.path("screening/batches").glob("*.yaml"))
    assert len(batch_files) == 8  # one persisted batch per allocated angle
    ws.path("screening/scores.yaml").unlink()  # the merge itself was lost
    await ScreeningStage().execute(ctx)
    assert sum(1 for r, _c, _p in dispatcher.invocations if r == "screener") == 8
    assert any("already valid — skipping (persisted)" in m for m in emitted)
    assert ws.path("screening/scores.yaml").is_file()


async def test_stale_batch_is_rescreened_not_trusted(run):
    """A persisted batch that no longer coheres (here: hand-mangled) silently
    re-dispatches instead of poisoning the merge."""
    ws, ctx, _stage, dispatcher, _ = run
    target = ws.path(batch_path("interpretability-research"))
    scores = ws.read_artifact(batch_path("interpretability-research"), ScreeningResult)
    mangled = scores.model_copy(update={"options": scores.options[1:]})  # a card unscored
    target.write_text(mangled.dump_yaml(), encoding="utf-8")
    ws.path("screening/scores.yaml").unlink()
    before = sum(1 for r, _c, _p in dispatcher.invocations if r == "screener")
    await ScreeningStage().execute(ctx)
    after = sum(1 for r, _c, _p in dispatcher.invocations if r == "screener")
    assert after == before + 1  # exactly the mangled angle's batch re-ran


async def test_incoherent_screening_pauses_instead_of_shortlisting(tmp_path):
    ws = make_workspace(tmp_path)
    write_s0_artifacts(ws)
    write_s1_s2_artifacts(ws)
    ctx = make_ctx(ws)
    await ScoutingStage().execute(ctx)
    await RubricStage().execute(ctx)
    # A screener that scores a nonexistent option: schema-valid, incoherent.
    fixture = (
        "### artifact: screening-result\n```yaml\n"
        "options:\n"
        "  - option_id: no-such-card\n"
        "    angle_id: interpretability-research\n"
        "    criterion_scores:\n"
        "      - {criterion_id: publication-potential, score: 3.0,\n"
        "         band: {lo: 2.0, hi: 4.0}, evidence_pointer: made up}\n"
        "    weighted_point: 3.0\n"
        "    weighted_ucb: 4.0\n"
        "```\n"
    )
    bad_ctx = make_ctx(ws, scripted_responses={"screener": [fixture] * 3})
    with pytest.raises(AgentOutputInvalid) as excinfo:
        await ScreeningStage().execute(bad_ctx)
    assert "does not cohere" in str(excinfo.value.errors)
    assert not ws.path("screening/scores.yaml").exists()  # nothing persisted
