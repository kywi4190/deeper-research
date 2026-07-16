"""S7 tournament tests: the code-computed dual scoreboards over the mock
scenario's engineered rank inversion, the priority docket (every inversion gets
a steelman), the three adversarial roles' artifacts, the frame-check gap
surfaced-but-never-executed, and the judge's updates landing in the ledger with
causes and applied in code."""

from __future__ import annotations

import pytest

from deeper.agents_runtime import AgentOutputInvalid
from deeper.schemas import (
    FrameCheck,
    FrameCheckVerdict,
    Prosecution,
    RedivergenceKind,
    Rubric,
    ScoreUpdate,
    ScoreUpdateLog,
    ScreeningResult,
    Steelman,
    SteelmanTrigger,
)
from deeper.sensitivity import dual_scoreboards, steelman_docket
from deeper.stages.s3_scouting import ScoutingStage
from deeper.stages.s4_rubric import RubricStage
from deeper.stages.s5_screening import ScreeningStage
from deeper.stages.s6_deepdive import DeepDiveStage
from deeper.stages.s7_tournament import (
    FRAME_CHECK_PATH,
    UPDATE_LOG_PATH,
    UPDATED_SCORES_PATH,
    TournamentStage,
    apply_score_updates,
    prosecution_path,
    steelman_path,
)

from .helpers import (
    FIXTURES,
    RecordingMockDispatcher,
    make_ctx,
    make_workspace,
    write_s0_artifacts,
    write_s1_s2_artifacts,
)

# The engineered inversion (screener r2 fixtures): contamination wins the
# destination-only board (4.6 vs 4.5), sae wins preference-adjusted.
DEST_WINNER = "contamination-robust-benchmark"
ADJ_WINNER = "sae-feature-atlas"
THIRD = "backdoor-probe-study"
# The engineered frame gap: the applied-domain critique named a clinical-
# imaging collaboration that no reflow top-up ever scouted.
GAP_ANGLE = "applied-domain-collaboration"


async def walk_to_s7(tmp_path, **mock_kwargs):
    """Materialize a complete pre-S7 workspace by running the real stages over
    the mock scenario, then hand back a ready TournamentStage context."""
    ws = make_workspace(tmp_path)
    write_s0_artifacts(ws)
    write_s1_s2_artifacts(ws)
    # The stage-level walk never runs Gate A; S7's removals log reads the
    # decision file, so write the approved-no-actions decision it would leave.
    ws.path("gates/gate-a.yaml").write_text("approved: true\n", encoding="utf-8")
    emitted: list[str] = []
    dispatcher = RecordingMockDispatcher(ws, ws.load_config(), **mock_kwargs)
    ctx = make_ctx(ws, dispatcher=dispatcher, emitted=emitted)
    await ScoutingStage().execute(ctx)
    await RubricStage().execute(ctx)
    await ScreeningStage().execute(ctx)
    await DeepDiveStage().execute(ctx)
    return ws, ctx, dispatcher, emitted


@pytest.fixture()
async def run(tmp_path):
    ws, ctx, dispatcher, emitted = await walk_to_s7(tmp_path)
    stage = TournamentStage()
    stage.validate_inputs(ctx)
    await stage.execute(ctx)
    return ws, ctx, stage, dispatcher, emitted


def dispatches(dispatcher, role: str, context: str | None = None) -> int:
    return sum(
        1 for r, c, _ in dispatcher.invocations if r == role and (context is None or c == context)
    )


# -- scoreboards + docket over the engineered scenario -------------------------------


async def test_engineered_inversion_shows_on_the_dual_scoreboards(run):
    ws, _, _, _, emitted = run
    scores = ws.read_artifact("dossiers/scores.yaml", ScreeningResult)
    rubric = ws.read_artifact("rubric.yaml", Rubric)
    dest, adjusted = dual_scoreboards(scores, rubric)
    assert dest[0].option_id == DEST_WINNER  # 4.6 destination-only
    assert adjusted[0].option_id == ADJ_WINNER  # the slot lifts sae past it
    assert dest[0].score == pytest.approx(4.6)
    assert adjusted[0].score == pytest.approx(0.8 * 4.5 + 0.2 * 4.5)
    docket = steelman_docket(dest, adjusted)
    assert docket == [(DEST_WINNER, "rank-inversion")]
    assert any("rank inversion" in m and DEST_WINNER in m for m in emitted)


async def test_top3_prosecutions_written_and_coherent(run):
    ws, _, _, dispatcher, _ = run
    for option_id in (ADJ_WINNER, DEST_WINNER, THIRD):
        prosecution = ws.read_artifact(prosecution_path(option_id), Prosecution)
        assert prosecution.option_id == option_id
        assert prosecution.regret_path  # the mandatory modal-failure story
        assert len(prosecution.new_evidence) <= 3
        assert dispatches(dispatcher, "prosecutor", option_id) == 1
    assert dispatches(dispatcher, "prosecutor") == 3  # top-3 only


async def test_steelman_keyed_to_the_inversion(run):
    ws, _, _, dispatcher, _ = run
    steelman = ws.read_artifact(steelman_path(DEST_WINNER), Steelman)
    assert steelman.option_id == DEST_WINNER
    assert steelman.trigger is SteelmanTrigger.RANK_INVERSION
    # The docket is exactly one entry: no steelman for the winner or the rest.
    assert dispatches(dispatcher, "steelman") == 1
    assert not ws.path(steelman_path(ADJ_WINNER)).exists()


async def test_frame_check_gap_surfaced_never_executed(run):
    ws, _, _, dispatcher, emitted = run
    frame_check = ws.read_artifact(FRAME_CHECK_PATH, FrameCheck)
    assert frame_check.verdict is FrameCheckVerdict.GAP_FOUND
    assert frame_check.missed_options_check.consequential
    proposal = frame_check.proposal
    assert proposal is not None
    assert proposal.kind is RedivergenceKind.SCOUT_TASK
    assert proposal.target_angle_id == GAP_ANGLE
    assert any("NOT auto-executed" in m for m in emitted)
    # Surfaced only: the gap angle got its one S3 scout and nothing more.
    assert dispatches(dispatcher, "scout", GAP_ANGLE) == 1


async def test_judge_updates_land_in_the_ledger_with_causes(run):
    ws, _, _, _, emitted = run
    log = ws.read_artifact(UPDATE_LOG_PATH, ScoreUpdateLog)
    assert len(log.updates) == 2
    for update in log.updates:
        assert update.cause  # every change logged with its cause
        assert update.source_artifact.startswith("tournament/")
    by_key = {(u.option_id, u.criterion_id): u for u in log.updates}
    assert by_key[(ADJ_WINNER, "momentum-by-deadline")].new_score == 4.25
    assert by_key[(DEST_WINNER, "feasibility")].new_score == 4.2
    assert any("judge" in m and "momentum-by-deadline" in m for m in emitted)


async def test_judge_updates_are_applied_in_code(run):
    ws, _, _, _, _ = run
    updated = ws.read_artifact(UPDATED_SCORES_PATH, ScreeningResult)
    records = {o.option_id: o for o in updated.options}
    sae = records[ADJ_WINNER]
    momentum = next(c for c in sae.criterion_scores if c.criterion_id == "momentum-by-deadline")
    assert momentum.score == 4.25
    # Aggregates recomputed from the updated criterion scores (weights: the
    # rubric fixture's momentum 0.1, slot 0.2): dest 4.475, combined 4.48.
    assert sae.weighted_point == pytest.approx(0.8 * 4.475 + 0.2 * 4.5)
    cont = records[DEST_WINNER]
    feasibility = next(c for c in cont.criterion_scores if c.criterion_id == "feasibility")
    assert feasibility.score == 4.2
    assert feasibility.band.lo == 4.2  # band widened to keep the score inside
    assert cont.weighted_point == pytest.approx(0.8 * (4.6 - 0.4 * 0.2) + 0.2 * 4.0)
    # Untouched options carry over unchanged.
    assert records[THIRD].weighted_point == pytest.approx(0.8 * 3.0 + 0.2 * 4.0)


async def test_stage_is_complete_and_reentry_dispatches_nothing(run):
    ws, ctx, stage, dispatcher, _ = run
    assert stage.is_complete(ctx)
    before = len(dispatcher.invocations)
    await stage.execute(ctx)  # crash-safe re-entry: everything on disk
    assert len(dispatcher.invocations) == before


async def test_partial_reentry_dispatches_only_whats_missing(run):
    ws, ctx, stage, dispatcher, _ = run
    ws.path(prosecution_path(THIRD)).unlink()
    ws.path(UPDATED_SCORES_PATH).unlink()
    before = len(dispatcher.invocations)
    await stage.execute(ctx)
    new = dispatcher.invocations[before:]
    # Only the deleted prosecution is re-dispatched; the judge's persisted
    # ledger is replayed, not re-asked.
    assert [(r, c) for r, c, _ in new] == [("prosecutor", THIRD)]
    assert stage.is_complete(ctx)


# -- integrity checks: incoherent adversarial output pauses, never persists -----------


async def test_judge_incoherence_is_retried_with_feedback(tmp_path):
    """One incoherent score-update log costs a feedback retry, not a paused
    run — the judge's coherence check rides run_agent's validate loop and the
    retry (fixture fallback) completes the stage."""
    bad_log = (
        "### artifact: score-update-log\n```yaml\n"
        "updates:\n"
        "  - option_id: sae-feature-atlas\n"
        "    criterion_id: momentum-by-deadline\n"
        "    old_score: 5.0\n"
        "    new_score: 4.0\n"
        "    cause: mismatched ledger amendment\n"
        "    source_artifact: tournament/sae-feature-atlas-prosecution.md\n"
        "notes: null\n```\n"
    )
    ws, ctx, _, _ = await walk_to_s7(tmp_path, scripted_responses={"judge": [bad_log]})
    await TournamentStage().execute(ctx)
    assert ws.path(UPDATE_LOG_PATH).exists()
    assert ws.load_state().retry_counts["S7:judge:-"] == 1


async def test_judge_old_score_mismatch_is_rejected(tmp_path):
    bad_log = (
        "### artifact: score-update-log\n```yaml\n"
        "updates:\n"
        "  - option_id: sae-feature-atlas\n"
        "    criterion_id: momentum-by-deadline\n"
        "    old_score: 5.0\n"
        "    new_score: 4.0\n"
        "    cause: mismatched ledger amendment\n"
        "    source_artifact: tournament/sae-feature-atlas-prosecution.md\n"
        "notes: null\n```\n"
    )
    ws, ctx, _, _ = await walk_to_s7(tmp_path, scripted_responses={"judge": [bad_log] * 3})
    with pytest.raises(AgentOutputInvalid, match="current score is 4.5"):
        await TournamentStage().execute(ctx)
    assert not ws.path(UPDATE_LOG_PATH).exists()  # nothing incoherent persisted


async def test_judge_may_not_touch_unknown_criteria(tmp_path):
    bad_log = (
        "### artifact: score-update-log\n```yaml\n"
        "updates:\n"
        "  - option_id: sae-feature-atlas\n"
        "    criterion_id: preference-slot\n"
        "    old_score: 4.5\n"
        "    new_score: 3.0\n"
        "    cause: preferences are not evidence\n"
        "    source_artifact: tournament/sae-feature-atlas-prosecution.md\n"
        "notes: null\n```\n"
    )
    ws, ctx, _, _ = await walk_to_s7(tmp_path, scripted_responses={"judge": [bad_log] * 3})
    with pytest.raises(AgentOutputInvalid, match="never the judge's to touch"):
        await TournamentStage().execute(ctx)


async def test_steelman_trigger_mismatch_is_rejected(tmp_path):
    wrong_trigger = (
        "### artifact: steelman\n```yaml\n"
        "option_id: contamination-robust-benchmark\n"
        "trigger: runner-up\n"
        "case: argued under the wrong docket reason\n"
        "supporting_claim_ids: []\n"
        "notes: null\n```\n"
    )
    ws, ctx, _, _ = await walk_to_s7(tmp_path, scripted_responses={"steelman": [wrong_trigger] * 3})
    with pytest.raises(AgentOutputInvalid, match="trigger must be 'rank-inversion'"):
        await TournamentStage().execute(ctx)


async def test_prosecutor_unknown_claim_ids_are_rejected(tmp_path):
    bad = (
        "### artifact: prosecution\n```yaml\n"
        "option_id: sae-feature-atlas\n"
        "case: rests on a claim the dossier does not contain\n"
        "regret_path: an unanchored regret story\n"
        "supporting_claim_ids: [c-invented]\n"
        "new_evidence: []\n"
        "notes: null\n```\n"
    )
    ws, ctx, _, _ = await walk_to_s7(
        tmp_path, scripted_responses={"prosecutor:sae-feature-atlas": [bad] * 3}
    )
    with pytest.raises(AgentOutputInvalid, match="c-invented"):
        await TournamentStage().execute(ctx)


# -- apply_score_updates: pure ledger arithmetic --------------------------------------


def test_apply_score_updates_widens_bands_and_recomputes_aggregates():
    rubric = Rubric.from_yaml_file(FIXTURES / "rubric-builder" / "rubric.yaml")
    screening = ScreeningResult.from_yaml_file(FIXTURES / "screener" / "screening-result.yaml")
    update = ScoreUpdate(
        option_id="sae-feature-atlas",
        criterion_id="feasibility",
        old_score=4.0,
        new_score=3.0,  # below the stored band's lo of 3.5
        cause="test",
        source_artifact="tournament/sae-feature-atlas-prosecution.md",
    )
    updated = apply_score_updates(screening, [update], rubric)
    record = next(o for o in updated.options if o.option_id == "sae-feature-atlas")
    feasibility = next(c for c in record.criterion_scores if c.criterion_id == "feasibility")
    assert feasibility.score == 3.0
    assert feasibility.band.lo == 3.0 and feasibility.band.hi == 4.5
    # dest = 4.0 - 1.0 * weight(feasibility 0.2) = 3.8; slot 0.2, pref 4.5.
    assert record.weighted_point == pytest.approx(0.8 * 3.8 + 0.2 * 4.5)
    untouched = next(c for c in record.criterion_scores if c.criterion_id == "letter-strength")
    assert untouched.score == 4.0 and untouched.band.lo == 3.5


def test_apply_score_updates_without_updates_only_recomputes():
    rubric = Rubric.from_yaml_file(FIXTURES / "rubric-builder" / "rubric.yaml")
    screening = ScreeningResult.from_yaml_file(FIXTURES / "screener" / "screening-result.yaml")
    updated = apply_score_updates(screening, [], rubric)
    assert [o.option_id for o in updated.options] == [o.option_id for o in screening.options]
