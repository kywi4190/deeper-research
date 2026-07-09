"""S6 deep-dive tests: the three-clause stopping rule truth table, the
effective load-bearing cross-check, deterministic verifier sampling, and the
canonical mock scenario's three termination paths (converged / BUDGET-CAPPED /
verifier-contradiction → one targeted revision), plus round idempotency on
mid-stage resume."""

from __future__ import annotations

import pytest

from deeper.agents_runtime import AgentOutputInvalid
from deeper.contradictions import LEDGER_PATH
from deeper.schemas import (
    Claim,
    Confidence,
    ContradictionLedger,
    DeepDiveRoundLog,
    DeepDiveStatus,
    Dossier,
    DossierSection,
    ScreeningResult,
    SourceRef,
    SourceTier,
    VerificationReport,
)
from deeper.stages.depth import (
    derive_open_questions,
    effective_load_bearing,
    low_conf_load_bearing,
    should_stop,
    verifier_sample,
)
from deeper.stages.s3_scouting import ScoutingStage
from deeper.stages.s4_rubric import RubricStage
from deeper.stages.s5_screening import ScreeningStage
from deeper.stages.s6_deepdive import DeepDiveStage

from .helpers import (
    FIXTURES,
    RecordingMockDispatcher,
    make_ctx,
    make_workspace,
    write_s0_artifacts,
    write_s1_s2_artifacts,
)

CONVERGER = "sae-feature-atlas"  # stabilizes on round 2
CAPPED = "contamination-robust-benchmark"  # still moving when the cap hits
CONTRADICTED = "backdoor-probe-study"  # verifier contradicts a load-bearing claim


# -- the stopping rule: all clause combinations --------------------------------------


@pytest.mark.parametrize(
    ("delta", "low_conf_lb", "rounds_used", "expected"),
    [
        # (a) stable AND (b) no low-conf load-bearing -> converged, even at the cap.
        (0.1, False, 1, (True, False)),
        (0.1, False, 2, (True, False)),
        # stable but a low-conf load-bearing claim remains -> keep going / cap.
        (0.1, True, 1, (False, False)),
        (0.1, True, 2, (True, True)),
        # still moving -> keep going / cap, whatever clause (b) says.
        (0.2, False, 1, (False, False)),
        (0.2, False, 2, (True, True)),
        (0.2, True, 1, (False, False)),
        (0.2, True, 2, (True, True)),
    ],
)
def test_stopping_rule_truth_table(delta, low_conf_lb, rounds_used, expected):
    assert should_stop(delta, low_conf_lb, rounds_used, cap=2, delta_stop=0.15) == expected


def test_stopping_rule_boundary_is_strictly_less_than():
    # The design says "moved < 0.15": a delta of exactly 0.15 is NOT stable.
    assert should_stop(0.15, False, 1, cap=3, delta_stop=0.15) == (False, False)
    assert should_stop(0.1499, False, 1, cap=3, delta_stop=0.15) == (True, False)


# -- effective load-bearing + sampling over a synthetic dossier ----------------------


def _claim(cid: str, *, lb: bool, conf: Confidence = Confidence.MED) -> Claim:
    return Claim(
        id=cid,
        text=f"claim {cid}",
        confidence=conf,
        source=SourceRef(url="https://example.org", tier=SourceTier.T2),
        load_bearing=lb,
    )


def _dossier(claims: list[Claim], section_claims: dict[str, list[str]]) -> Dossier:
    filler = DossierSection(content="filler", claim_ids=[])
    return Dossier(
        option_id="synthetic-option",
        criterion_sections={
            cid: DossierSection(content=f"section {cid}", claim_ids=ids)
            for cid, ids in section_claims.items()
        },
        failure_modes=filler,
        cost_of_adoption=filler,
        second_order_effects=filler,
        strongest_criticism=filler,
        comparable_cases=filler,
        claims=claims,
        rounds_completed=1,
    )


def test_rescore_diff_promotes_untagged_claims_with_a_warning():
    dossier = _dossier(
        [_claim("c-tagged", lb=True), _claim("c-untagged", lb=False)],
        {"crit-a": ["c-untagged"], "crit-b": ["c-untagged"]},
    )
    # crit-a moved a full point: its section's untagged claim is promoted.
    effective, warnings = effective_load_bearing(dossier, {"crit-a": 1.0, "crit-b": 0.5})
    assert effective == {"c-tagged", "c-untagged"}
    assert len(warnings) == 1 and "c-untagged" in warnings[0] and "crit-a" in warnings[0]
    # No >= 1-point move: only the analyst's own tags count, no warnings.
    effective, warnings = effective_load_bearing(dossier, {"crit-a": 0.99, "crit-b": 0.0})
    assert effective == {"c-tagged"}
    assert warnings == []


def test_low_conf_load_bearing_intersects_confidence_with_the_effective_set():
    dossier = _dossier(
        [
            _claim("c-low-lb", lb=True, conf=Confidence.LOW),
            _claim("c-low-free", lb=False, conf=Confidence.LOW),
            _claim("c-high-lb", lb=True, conf=Confidence.HIGH),
        ],
        {"crit-a": []},
    )
    assert low_conf_load_bearing(dossier, {"c-low-lb", "c-high-lb"}) == ["c-low-lb"]


def test_verifier_sample_takes_all_load_bearing_plus_a_seeded_fifth():
    load_bearing = [_claim(f"c-lb-{i}", lb=True) for i in range(3)]
    rest = [_claim(f"c-rest-{i}", lb=False) for i in range(10)]
    dossier = _dossier([*load_bearing, *rest], {"crit-a": []})
    effective = {c.id for c in load_bearing}
    sampled, n_lb, n_other = verifier_sample(dossier, effective)
    assert n_lb == 3 and set(sampled[:3]) == effective  # ALL load-bearing claims
    assert n_other == 2  # ceil(0.2 * 10)
    assert set(sampled[3:]) <= {c.id for c in rest}
    # Deterministic: the "random" 20% is a seeded draw keyed on the option id.
    assert verifier_sample(dossier, effective) == (sampled, n_lb, n_other)


def test_verifier_sample_edge_cases():
    all_lb = [_claim(f"c-{i}", lb=True) for i in range(4)]
    dossier = _dossier(all_lb, {"crit-a": []})
    assert verifier_sample(dossier, {c.id for c in all_lb}) == (
        sorted(c.id for c in all_lb),
        4,
        0,
    )
    one_rest = _dossier([_claim("c-lb", lb=True), _claim("c-r", lb=False)], {"crit-a": []})
    sampled, n_lb, n_other = verifier_sample(one_rest, {"c-lb"})
    assert sampled == ["c-lb", "c-r"] and (n_lb, n_other) == (1, 1)  # ceil(0.2) = 1


def test_derive_open_questions_names_the_blocking_claims():
    dossier = _dossier([_claim("c-low-lb", lb=True, conf=Confidence.LOW)], {"crit-a": []})
    questions = derive_open_questions(dossier, ["c-low-lb"])
    assert len(questions) == 1 and "c-low-lb" in questions[0] and "claim c-low-lb" in questions[0]
    fallback = derive_open_questions(dossier, [])
    assert len(fallback) == 1 and "budget cap" in fallback[0]


# -- the mock scenario: three termination paths end to end ---------------------------


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
    await ScreeningStage().execute(ctx)
    stage = DeepDiveStage()
    stage.validate_inputs(ctx)
    await stage.execute(ctx)
    return ws, ctx, stage, dispatcher, emitted


def dispatches(dispatcher, role: str, context: str) -> int:
    return sum(1 for r, c, _ in dispatcher.invocations if r == role and c == context)


async def test_converger_stops_on_stability_in_two_rounds(run):
    ws, _, _, dispatcher, emitted = run
    log = ws.read_artifact(f"dossiers/{CONVERGER}-rounds.yaml", DeepDiveRoundLog)
    assert log.status is DeepDiveStatus.CONVERGED
    assert [r.round for r in log.rounds] == [1, 2]
    assert log.rounds[0].delta >= 0.15  # round 1 moved: the loop had to continue
    assert log.rounds[1].delta < 0.15  # round 2 stabilized
    assert log.rounds[0].low_confidence_load_bearing == ["c-sae-compute"]
    assert log.rounds[1].low_confidence_load_bearing == []
    dossier = ws.read_artifact(f"dossiers/{CONVERGER}.md", Dossier)
    assert dossier.rounds_completed == 2 and not dossier.budget_capped
    assert dispatches(dispatcher, "analyst", f"{CONVERGER}-r1") == 1
    assert dispatches(dispatcher, "analyst", f"{CONVERGER}-r2") == 1
    assert any("converged after 2 round(s)" in m and CONVERGER in m for m in emitted)


async def test_budget_cap_stamps_the_dossier_with_open_questions(run):
    ws, _, _, _, emitted = run
    log = ws.read_artifact(f"dossiers/{CAPPED}-rounds.yaml", DeepDiveRoundLog)
    assert log.status is DeepDiveStatus.BUDGET_CAPPED
    assert len(log.rounds) == 2  # the quick profile's deep_dive_unit_cap
    assert log.rounds[1].delta >= 0.15  # still moving when the budget ran out
    dossier = ws.read_artifact(f"dossiers/{CAPPED}.md", Dossier)
    assert dossier.budget_capped and dossier.open_questions
    assert any("BUDGET-CAPPED" in m and CAPPED in m for m in emitted)


async def test_contradicted_claim_fires_exactly_one_revision_and_moves_the_score(run):
    ws, _, _, dispatcher, emitted = run
    log = ws.read_artifact(f"dossiers/{CONTRADICTED}-rounds.yaml", DeepDiveRoundLog)
    assert log.status is DeepDiveStatus.CONVERGED  # round 1, rode the S5 fallback
    assert log.verification is not None
    assert log.verification.contradicted_claim_ids == ["c-back-transfer"]
    assert log.verification.revision_completed
    # Exactly ONE targeted revision and one final re-score were dispatched.
    assert dispatches(dispatcher, "analyst", f"{CONTRADICTED}-vrev") == 1
    assert dispatches(dispatcher, "screener", f"{CONTRADICTED}-final") == 1
    # The corrected evidence moved the score.
    assert log.final_screening is not None
    assert log.final_screening.weighted_point != log.rounds[-1].screening.weighted_point
    dossier = ws.read_artifact(f"dossiers/{CONTRADICTED}.md", Dossier)
    transfer = next(c for c in dossier.claims if c.id == "c-back-transfer")
    assert "0.62" in transfer.text  # the revision corrected the claim itself
    assert any("one targeted analyst revision" in m for m in emitted)


async def test_contradiction_lands_in_the_ledger(run):
    ws, _, _, _, _ = run
    ledger = ws.read_artifact(LEDGER_PATH, ContradictionLedger)
    entry = next(e for e in ledger.entries if e.id == f"{CONTRADICTED}-c-back-transfer")
    assert entry.detected_by == "verifier"
    assert entry.statement_a.artifact == f"dossiers/{CONTRADICTED}.md"
    assert entry.statement_b.artifact == f"dossiers/{CONTRADICTED}-verification.md"
    assert "0.62" in entry.statement_b.statement  # the verifier's evidence quote


async def test_every_load_bearing_claim_of_every_dossier_was_verified(run):
    ws, _, _, _, _ = run
    shortlist_ids = {
        o.option_id for o in ws.read_artifact("dossiers/scores.yaml", ScreeningResult).options
    }
    for option_id in shortlist_ids:
        dossier = ws.read_artifact(f"dossiers/{option_id}.md", Dossier)
        report = ws.read_artifact(f"dossiers/{option_id}-verification.md", VerificationReport)
        adjudicated = {r.claim_id for r in report.results}
        load_bearing = {c.id for c in dossier.claims if c.load_bearing}
        assert load_bearing <= adjudicated, f"{option_id}: unverified load-bearing claims"
        assert report.sampled_load_bearing_count >= len(load_bearing)


async def test_final_scores_merge_matches_the_logs(run):
    ws, ctx, stage, _, _ = run
    scores = ws.read_artifact("dossiers/scores.yaml", ScreeningResult)
    by_id = {o.option_id: o for o in scores.options}
    log = ws.read_artifact(f"dossiers/{CONTRADICTED}-rounds.yaml", DeepDiveRoundLog)
    assert log.final_screening is not None
    # The post-revision re-score, not the pre-verification one, is what S7 gets.
    assert by_id[CONTRADICTED].weighted_point == log.final_screening.weighted_point
    conv = ws.read_artifact(f"dossiers/{CONVERGER}-rounds.yaml", DeepDiveRoundLog)
    assert by_id[CONVERGER].weighted_point == conv.rounds[-1].screening.weighted_point
    assert stage.is_complete(ctx)


# -- idempotency: resume mid-S6 never re-dispatches settled work ---------------------


async def test_settled_stage_reentry_dispatches_nothing(run):
    ws, _, stage, _, _ = run
    fresh = RecordingMockDispatcher(ws, ws.load_config())
    ctx = make_ctx(ws, dispatcher=fresh)
    await stage.execute(ctx)  # a resume replay over a fully settled stage
    assert fresh.invocations == []


async def test_resume_replays_only_the_unsettled_tail(run):
    ws, _, stage, _, _ = run
    # Simulate a crash after round 2's dossier write but before its log append:
    # truncate the log to round 1 (in-progress, unverified); the round-2 dossier
    # stays on disk with rounds_completed=2.
    log = ws.read_artifact(f"dossiers/{CONVERGER}-rounds.yaml", DeepDiveRoundLog)
    ws.write_artifact(
        f"dossiers/{CONVERGER}-rounds.yaml",
        log.model_copy(
            update={
                "rounds": log.rounds[:1],
                "status": DeepDiveStatus.IN_PROGRESS,
                "verification": None,
            }
        ),
    )
    fresh = RecordingMockDispatcher(ws, ws.load_config())
    ctx = make_ctx(ws, dispatcher=fresh)
    await stage.execute(ctx)
    # The round-2 research is on disk: only the re-score and the verification
    # are owed — the analyst is never re-dispatched, nor any other finalist.
    assert dispatches(fresh, "analyst", f"{CONVERGER}-r2") == 0
    assert dispatches(fresh, "screener", f"{CONVERGER}-r2") == 1
    assert dispatches(fresh, "verifier", CONVERGER) == 1
    assert {c for r, c, _ in fresh.invocations} == {f"{CONVERGER}-r2", CONVERGER}
    restored = ws.read_artifact(f"dossiers/{CONVERGER}-rounds.yaml", DeepDiveRoundLog)
    assert restored.status is DeepDiveStatus.CONVERGED and len(restored.rounds) == 2


# -- cross-artifact integrity is enforced, not assumed -------------------------------


async def test_wrong_option_dossier_is_rejected(tmp_path):
    ws = make_workspace(tmp_path)
    write_s0_artifacts(ws)
    write_s1_s2_artifacts(ws)
    emitted: list[str] = []
    # Serve the sae-feature-atlas fallback dossier to the CAPPED option's
    # analyst: schema-valid, wrong option_id — the stage must reject it.
    wrong = (FIXTURES / "analyst" / "dossier.yaml").read_text(encoding="utf-8")
    scripted = {f"analyst:{CAPPED}-r1": [f"### artifact: dossier\n```yaml\n{wrong}\n```"]}
    ctx = make_ctx(ws, emitted=emitted, scripted_responses=scripted)
    await ScoutingStage().execute(ctx)
    await RubricStage().execute(ctx)
    await ScreeningStage().execute(ctx)
    with pytest.raises(AgentOutputInvalid, match="does not cohere"):
        await DeepDiveStage().execute(ctx)
