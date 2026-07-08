"""Unit tests for the S5 shortlist rule (design §5/S5) — every clause, on
hand-built screening records: kill-first, top-k-by-UCB with the dark-horse
margin and the absolute floor, the per-angle cap, the hard finalist cap, and
both sides of the breadth guardrail."""

from __future__ import annotations

import pytest

from deeper.schemas import (
    Criterion,
    CriterionScore,
    KillRiskCheck,
    KillRiskOutcome,
    OptionCard,
    OptionCardSet,
    OptionScreening,
    PreferenceSlot,
    Rubric,
    ScreeningResult,
    ShortlistCause,
    ShortlistOutcome,
    SourceRef,
    UncertaintyBand,
)
from deeper.schemas.common import EvidenceItem
from deeper.stages.shortlist import (
    build_shortlist,
    confirmed_kill,
    recompute_aggregates,
    verify_screening,
)

CRITERIA = [f"crit-{i}" for i in range(1, 6)]


def rubric(slot_weight: float = 0.2) -> Rubric:
    return Rubric(
        criteria=[
            Criterion(
                id=cid,
                name=cid,
                definition="what it measures",
                measurement_method="what evidence moves it",
                levels={i: f"level {i}" for i in range(1, 6)},
                weight=0.2,
                justification="traceable to the destination model",
            )
            for cid in CRITERIA
        ],
        preference_slot=PreferenceSlot(weight=slot_weight),
    )


def screened(
    option_id: str,
    angle_id: str,
    *,
    score: float = 3.0,
    hi: float = 4.0,
    lo: float = 2.0,
    pref: tuple[float, float, float] | None = (3.0, 2.5, 3.5),
    kill: KillRiskOutcome | None = None,
) -> OptionScreening:
    """One screening record with uniform criterion scores; aggregates computed
    the way recompute_aggregates would (slot weight 0.2)."""
    pref_score = None
    point, ucb = score, hi
    if pref is not None:
        p, plo, phi = pref
        pref_score = CriterionScore(
            criterion_id="preference-slot",
            score=p,
            band=UncertaintyBand(lo=plo, hi=phi),
            evidence_pointer="preferences",
        )
        point = 0.8 * score + 0.2 * p
        ucb = 0.8 * hi + 0.2 * phi
    checks = []
    if kill is not None:
        checks.append(KillRiskCheck(fact=f"{option_id} kill fact", outcome=kill))
    return OptionScreening(
        option_id=option_id,
        angle_id=angle_id,
        criterion_scores=[
            CriterionScore(
                criterion_id=cid,
                score=score,
                band=UncertaintyBand(lo=lo, hi=hi),
                evidence_pointer="the card",
            )
            for cid in CRITERIA
        ],
        preference_score=pref_score,
        kill_risk_checks=checks,
        weighted_point=round(point, 4),
        weighted_ucb=round(ucb, 4),
    )


def shortlist(
    options, priors, *, threshold=3.5, top_k=3, max_per_angle=3, margin=0.25, max_finalists=7
):
    return build_shortlist(
        ScreeningResult(options=options),
        priors,
        threshold=threshold,
        top_k=top_k,
        max_per_angle=max_per_angle,
        margin=margin,
        max_finalists=max_finalists,
    )


def decision_of(sl, option_id):
    return next(d for d in sl.decisions if d.option_id == option_id)


# -- clause (a): kill-risks first -------------------------------------------------


def test_confirmed_kill_eliminates_regardless_of_score():
    options = [
        screened("killed-star", "a", score=4.5, hi=5.0, kill=KillRiskOutcome.CONFIRMED),
        screened("survivor", "b", score=3.5, hi=4.2),
    ]
    sl = shortlist(options, {"a": 0.9, "b": 0.8})
    d = decision_of(sl, "killed-star")
    assert d.decision is ShortlistOutcome.CUT
    assert d.cause is ShortlistCause.KILL_RISK_CONFIRMED
    assert "killed-star kill fact" in d.reason  # the fact is in the audit trail
    assert "killed-star" not in sl.finalist_ids
    # The kill wins even though its UCB towers over every finalist.
    assert options[0].weighted_ucb > max(
        o.weighted_ucb for o in options if o.option_id in sl.finalist_ids
    )


def test_cleared_and_unresolved_kill_risks_do_not_eliminate():
    options = [
        screened("cleared", "a", score=3.8, hi=4.5, kill=KillRiskOutcome.CLEARED),
        screened("unresolved", "b", score=3.8, hi=4.5, kill=KillRiskOutcome.UNRESOLVED),
    ]
    sl = shortlist(options, {"a": 0.9, "b": 0.8})
    assert set(sl.finalist_ids) == {"cleared", "unresolved"}
    assert confirmed_kill(options[0]) is None


# -- clause (b): advance on the UPPER confidence bound -----------------------------


def test_advance_compares_ucb_not_point_estimate():
    # Dark horse: point estimate below threshold, wide band lifts the UCB above.
    dark = screened("dark-horse", "a", score=3.0, lo=2.0, hi=4.3)
    safe = screened("safe-bet", "b", score=3.8, lo=3.6, hi=4.1)
    dud = screened("dud", "c", score=3.4, lo=3.2, hi=3.45)  # point AND ucb below
    sl = shortlist([dark, safe, dud], {"a": 0.9, "b": 0.8, "c": 0.7})
    assert dark.weighted_point < 3.5 <= dark.weighted_ucb
    d = decision_of(sl, "dark-horse")
    assert d.decision is ShortlistOutcome.ADVANCED
    assert d.cause is ShortlistCause.UCB_ABOVE_THRESHOLD
    assert "wide uncertainty band" in d.reason
    cut = decision_of(sl, "dud")
    assert cut.decision is ShortlistOutcome.CUT
    assert cut.cause is ShortlistCause.BELOW_THRESHOLD


def test_concentration_cuts_above_floor_but_beyond_margin():
    # The M1 live-run failure shape: everything clears the absolute floor. The
    # relative rule advances the top k plus within-margin dark horses only.
    options = [
        screened("first", "a", score=4.2, hi=4.8),  # ucb 4.54
        screened("second", "b", score=4.0, hi=4.6),  # ucb 4.38
        screened("third", "c", score=3.9, hi=4.5),  # ucb 4.30 -> cutoff
        screened("margin-rider", "d", score=3.4, hi=4.3),  # ucb 4.14 >= 4.30 - 0.25
        screened("laggard", "e", score=3.5, hi=4.0),  # ucb 3.90: floor yes, margin no
    ]
    priors = {"a": 0.9, "b": 0.8, "c": 0.7, "d": 0.6, "e": 0.5}
    sl = shortlist(options, priors, top_k=3, margin=0.25)
    assert set(sl.finalist_ids) == {"first", "second", "third", "margin-rider"}
    rider = decision_of(sl, "margin-rider")
    assert rider.decision is ShortlistOutcome.ADVANCED
    assert rider.cause is ShortlistCause.UCB_ABOVE_THRESHOLD
    assert "margin" in rider.reason  # advanced as a dark-horse contender
    cut = decision_of(sl, "laggard")
    assert cut.decision is ShortlistOutcome.CUT
    assert cut.cause is ShortlistCause.BELOW_CUTOFF
    assert "nothing here eliminates the option on the merits" in cut.reason


def test_absolute_floor_holds_even_inside_the_top_k():
    # Rank alone never advances an option the evidence cannot support.
    options = [
        screened("good", "a", score=4.0, hi=4.5),
        screened("meh", "b", score=3.0, hi=3.4),  # rank #2 by UCB, ucb 3.42 < 3.5
    ]
    sl = shortlist(options, {"a": 0.9, "b": 0.8}, top_k=3)
    d = decision_of(sl, "meh")
    assert d.decision is ShortlistOutcome.CUT
    assert d.cause is ShortlistCause.BELOW_THRESHOLD


def test_hard_finalist_cap_bounds_the_margin():
    # Nine near-tied options across nine angles: the margin alone would
    # advance all of them; the §12 hard cap seats only max_finalists.
    options = [screened(f"o-{i}", f"ang-{i}", score=4.0, hi=4.5 - i * 0.01) for i in range(9)]
    priors = {f"ang-{i}": 0.9 - i * 0.05 for i in range(9)}
    sl = shortlist(options, priors, top_k=3, max_finalists=7)
    assert len(sl.finalist_ids) == 7
    overflow = decision_of(sl, "o-7")
    assert overflow.decision is ShortlistOutcome.CUT
    assert overflow.cause is ShortlistCause.BELOW_CUTOFF
    assert "hard finalist cap" in overflow.reason


# -- clause (c1): max finalists per angle -----------------------------------------


def test_angle_cap_cuts_fourth_option_from_one_angle():
    a = [screened(f"a-{i}", "a", score=4.0, hi=4.5 - i * 0.1) for i in range(4)]
    other = screened("b-1", "b", score=3.8, hi=4.4)
    sl = shortlist([*a, other], {"a": 0.9, "b": 0.8}, max_per_angle=3)
    d = decision_of(sl, "a-3")  # lowest UCB of the four
    assert d.decision is ShortlistOutcome.CUT
    assert d.cause is ShortlistCause.ANGLE_CAP
    assert "a-0" in d.reason  # names the finalists holding the slots
    assert [x for x in sl.finalist_ids if x.startswith("a-")] == ["a-0", "a-1", "a-2"]


# -- clause (c2): breadth guardrail ------------------------------------------------


def test_guardrail_adds_top_half_unrepresented_angles_only():
    # Top-3 finalists all come from angles a+b; angles c,d,e,f exist with priors
    # ordering c,d into the top half (of 6 angles -> top 3 = a,b,c... use
    # explicit priors so a,b,c are the top half and d-f the bottom).
    priors = {"a": 0.9, "b": 0.8, "c": 0.7, "d": 0.4, "e": 0.3, "f": 0.2}
    options = [
        screened("a-1", "a", score=4.2, hi=4.8),
        screened("a-2", "a", score=4.0, hi=4.6),
        screened("b-1", "b", score=3.9, hi=4.5),
        screened("c-best", "c", score=3.0, hi=3.3),  # below threshold
        screened("c-worse", "c", score=2.8, hi=3.1),  # below, lower UCB
        screened("d-best", "d", score=3.2, hi=3.4),  # below; bottom-half angle
    ]
    sl = shortlist(options, priors, threshold=3.5, top_k=3)
    added = decision_of(sl, "c-best")
    assert added.decision is ShortlistOutcome.ADVANCED
    assert added.cause is ShortlistCause.DIVERSITY_GUARDRAIL_ADD
    assert "top-half" in added.reason
    # The angle's lower-UCB option stays cut; bottom-half angle d gets nothing.
    assert decision_of(sl, "c-worse").decision is ShortlistOutcome.CUT
    assert decision_of(sl, "d-best").decision is ShortlistOutcome.CUT
    assert decision_of(sl, "d-best").cause is ShortlistCause.BELOW_THRESHOLD


def test_guardrail_does_not_fire_when_top_k_spans_three_angles():
    priors = {"a": 0.9, "b": 0.8, "c": 0.7, "d": 0.6}
    options = [
        screened("a-1", "a", score=4.2, hi=4.8),
        screened("b-1", "b", score=4.0, hi=4.6),
        screened("c-1", "c", score=3.9, hi=4.5),
        screened("d-1", "d", score=3.0, hi=3.3),  # top-half-ish, below threshold
    ]
    sl = shortlist(options, priors, threshold=3.5, top_k=3)
    assert decision_of(sl, "d-1").decision is ShortlistOutcome.CUT
    assert sorted(sl.finalist_ids) == ["a-1", "b-1", "c-1"]


def test_guardrail_rescues_an_empty_finalist_set():
    priors = {"a": 0.9, "b": 0.2}
    options = [screened("a-1", "a", score=3.0, hi=3.2)]
    sl = shortlist(options, priors, threshold=3.5)
    assert sl.finalist_ids == ["a-1"]
    assert decision_of(sl, "a-1").cause is ShortlistCause.DIVERSITY_GUARDRAIL_ADD


def test_everything_killed_raises_instead_of_empty_shortlist():
    options = [screened("only", "a", score=4.0, hi=4.5, kill=KillRiskOutcome.CONFIRMED)]
    with pytest.raises(ValueError, match="eliminated every option"):
        shortlist(options, {"a": 0.9})


def test_every_option_gets_exactly_one_decision_with_a_reason():
    options = [
        screened("x", "a", score=4.0, hi=4.5),
        screened("y", "a", score=2.0, hi=2.5),
        screened("z", "b", score=4.0, hi=4.4, kill=KillRiskOutcome.CONFIRMED),
    ]
    sl = shortlist(options, {"a": 0.9, "b": 0.8})
    assert [d.option_id for d in sl.decisions] == ["x", "y", "z"]  # scores.yaml order
    assert all(len(d.reason) > 40 for d in sl.decisions)  # a paragraph, not a token


# -- the screening arithmetic code owns ---------------------------------------------


def test_recompute_aggregates_corrects_drifted_numbers():
    option = screened("opt", "a", score=3.0, hi=4.0).model_copy(
        update={"weighted_point": 4.9, "weighted_ucb": 4.9}  # screener got it wrong
    )
    result, drift = recompute_aggregates(ScreeningResult(options=[option]), rubric())
    assert len(drift) == 1 and "corrected opt aggregates" in drift[0]
    assert result.options[0].weighted_point == pytest.approx(3.0)
    assert result.options[0].weighted_ucb == pytest.approx(3.9)  # 0.8*4.0 + 0.2*3.5


def test_recompute_aggregates_without_preference_score_is_destination_only():
    option = screened("opt", "a", score=3.0, hi=4.0, pref=None)
    result, _ = recompute_aggregates(ScreeningResult(options=[option]), rubric())
    assert result.options[0].weighted_point == pytest.approx(3.0)
    assert result.options[0].weighted_ucb == pytest.approx(4.0)


def _card_set(angle_id: str, ids: list[str]) -> OptionCardSet:
    return OptionCardSet(
        angle_id=angle_id,
        cards=[
            OptionCard(
                id=i,
                name=i,
                angle_id=angle_id,
                description="a concrete option",
                mechanism="how it works, twice over",
                preliminary_evidence=[
                    EvidenceItem(
                        text="it exists",
                        source=SourceRef(url="https://example.org", tier="T2"),
                    )
                ],
                uncertainties=["the unknown"],
            )
            for i in ids
        ],
    )


def test_verify_screening_reports_every_incoherence():
    cards = [_card_set("a", ["known"]), _card_set("b", ["unscored"])]
    ghost = screened("ghost", "a")  # no card
    misfiled = screened("known", "b")  # card lives in angle a
    problems = verify_screening(ScreeningResult(options=[ghost, misfiled]), rubric(), cards)
    text = "\n".join(problems)
    assert "'ghost' does not match any option card" in text
    assert "carries angle_id 'b'" in text
    assert "never scored: ['unscored']" in text


def test_verify_screening_demands_full_criterion_coverage_and_preference_score():
    cards = [_card_set("a", ["opt"])]
    partial = screened("opt", "a", pref=None)
    partial = partial.model_copy(update={"criterion_scores": partial.criterion_scores[:3]})
    problems = verify_screening(ScreeningResult(options=[partial]), rubric(), cards)
    text = "\n".join(problems)
    assert "must score every rubric criterion" in text
    assert "missing its preference-slot score" in text


def test_verify_screening_passes_on_coherent_input():
    cards = [_card_set("a", ["opt"])]
    assert verify_screening(ScreeningResult(options=[screened("opt", "a")]), rubric(), cards) == []
