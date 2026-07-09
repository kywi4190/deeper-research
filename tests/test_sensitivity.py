"""The code-computed scoreboard/sensitivity math (deeper.sensitivity).

Every expected number below is hand-computable from a 3-option / 3-criterion
fixture: weights a=0.5, b=0.3, c=0.2 and small integer-ish scores.
"""

from __future__ import annotations

import pytest

from deeper.schemas import (
    CriterionScore,
    OptionScreening,
    Rubric,
    ScreeningResult,
    UncertaintyBand,
)
from deeper.sensitivity import (
    RankedOption,
    combined_point,
    criterion_flip_delta,
    destination_point,
    dual_scoreboards,
    preference_sweep,
    rank_inversions,
    render_scoreboards,
    render_sensitivity,
    scoreboard,
    steelman_docket,
    weight_sensitivity,
)

from .test_mock_fixtures import DEFAULT_FIXTURES_DIR

WEIGHTS = {"a": 0.5, "b": 0.3, "c": 0.2}


def option(option_id: str, scores: dict[str, float], pref: float | None) -> OptionScreening:
    def cs(cid: str, score: float) -> CriterionScore:
        return CriterionScore(
            criterion_id=cid,
            score=score,
            band=UncertaintyBand(lo=max(1.0, score - 0.5), hi=min(5.0, score + 0.5)),
            evidence_pointer="hand-built test fixture",
        )

    return OptionScreening(
        option_id=option_id,
        angle_id="test-angle",
        criterion_scores=[cs(cid, s) for cid, s in scores.items()],
        preference_score=None if pref is None else cs("preference-slot", pref),
        kill_risk_checks=[],
        weighted_point=3.0,  # stored aggregates are deliberately wrong:
        weighted_ucb=3.0,  # sensitivity math must never read them
    )


# o1 leads destination-only (3.2 vs 2.8) but has the weaker preference fit —
# the engineered inversion every S7 docket test builds on.
O1 = option("o1-dest-leader", {"a": 4.0, "b": 2.0, "c": 3.0}, pref=2.0)  # dest 3.2
O2 = option("o2-pref-leader", {"a": 2.0, "b": 4.0, "c": 3.0}, pref=5.0)  # dest 2.8
O3 = option("o3-tail", {"a": 2.5, "b": 2.5, "c": 2.5}, pref=2.5)  # dest 2.5


# -- point arithmetic ---------------------------------------------------------


def test_destination_point_is_weighted_sum():
    assert destination_point(O1, WEIGHTS) == pytest.approx(3.2)
    assert destination_point(O2, WEIGHTS) == pytest.approx(2.8)


def test_combined_point_mixes_in_the_slot_weight():
    # 0.8 * 3.2 + 0.2 * 2.0
    assert combined_point(O1, WEIGHTS, 0.2) == pytest.approx(2.96)
    # 0.8 * 2.8 + 0.2 * 5.0
    assert combined_point(O2, WEIGHTS, 0.2) == pytest.approx(3.24)


def test_combined_point_at_zero_slot_weight_is_destination_only():
    assert combined_point(O1, WEIGHTS, 0.0) == pytest.approx(destination_point(O1, WEIGHTS))


def test_combined_point_without_preference_score_ignores_the_slot():
    bare = option("bare", {"a": 4.0, "b": 2.0, "c": 3.0}, pref=None)
    assert combined_point(bare, WEIGHTS, 0.4) == pytest.approx(3.2)


# -- scoreboards --------------------------------------------------------------


def test_scoreboard_orders_by_score_descending():
    board = scoreboard([O1, O2, O3], WEIGHTS, 0.0)
    assert [row.option_id for row in board] == ["o1-dest-leader", "o2-pref-leader", "o3-tail"]
    assert [row.rank for row in board] == [1, 2, 3]
    assert board[0].score == pytest.approx(3.2)


def test_scoreboard_slot_weight_reorders():
    board = scoreboard([O1, O2, O3], WEIGHTS, 0.2)
    assert [row.option_id for row in board] == ["o2-pref-leader", "o1-dest-leader", "o3-tail"]


def test_scoreboard_ties_share_a_dense_rank_and_break_by_id():
    twin_a = option("twin-a", {"a": 3.0, "b": 3.0, "c": 3.0}, pref=3.0)
    twin_b = option("twin-b", {"a": 3.0, "b": 3.0, "c": 3.0}, pref=3.0)
    board = scoreboard([twin_b, O1, twin_a], WEIGHTS, 0.0)
    assert [(row.option_id, row.rank) for row in board] == [
        ("o1-dest-leader", 1),
        ("twin-a", 2),
        ("twin-b", 2),
    ]


def test_dual_scoreboards_use_rubric_weights_and_slot():
    rubric = Rubric.from_yaml_file(DEFAULT_FIXTURES_DIR / "rubric-builder" / "rubric.yaml")
    screening = ScreeningResult.from_yaml_file(
        DEFAULT_FIXTURES_DIR / "screener" / "screening-result.yaml"
    )
    dest, adjusted = dual_scoreboards(screening, rubric)
    assert {row.option_id for row in dest} == {o.option_id for o in screening.options}
    # sae-feature-atlas scores 4.0 on every criterion: destination point is 4.0
    # exactly, and the slot (weight 0.2, pref 4.5) lifts the adjusted score.
    sae_dest = next(row for row in dest if row.option_id == "sae-feature-atlas")
    sae_adj = next(row for row in adjusted if row.option_id == "sae-feature-atlas")
    assert sae_dest.score == pytest.approx(4.0)
    assert sae_adj.score == pytest.approx(0.8 * 4.0 + 0.2 * 4.5)


# -- inversions and the steelman docket ---------------------------------------


def test_rank_inversion_detected():
    dest = scoreboard([O1, O2, O3], WEIGHTS, 0.0)
    adjusted = scoreboard([O1, O2, O3], WEIGHTS, 0.2)
    assert rank_inversions(dest, adjusted) == [("o1-dest-leader", "o2-pref-leader")]


def test_no_inversions_when_boards_agree():
    dest = scoreboard([O1, O3], WEIGHTS, 0.0)
    adjusted = scoreboard([O1, O3], WEIGHTS, 0.1)  # O1 still leads at 0.1
    assert rank_inversions(dest, adjusted) == []


def test_tie_refined_by_the_other_board_is_not_an_inversion():
    # Destination ties them; preferences order them — a refinement, not a flip.
    twin_a = option("twin-a", {"a": 3.0, "b": 3.0, "c": 3.0}, pref=4.0)
    twin_b = option("twin-b", {"a": 3.0, "b": 3.0, "c": 3.0}, pref=2.0)
    dest = scoreboard([twin_a, twin_b], WEIGHTS, 0.0)
    adjusted = scoreboard([twin_a, twin_b], WEIGHTS, 0.2)
    assert adjusted[0].option_id == "twin-a"  # preferences did reorder
    assert rank_inversions(dest, adjusted) == []


def test_steelman_docket_marks_the_inversion_over_the_runner_up_trigger():
    dest = scoreboard([O1, O2, O3], WEIGHTS, 0.0)
    adjusted = scoreboard([O1, O2, O3], WEIGHTS, 0.2)
    # O1 is both the adjusted runner-up and the demoted half of the inversion:
    # the inversion is the sharper reason and wins.
    assert steelman_docket(dest, adjusted) == [("o1-dest-leader", "rank-inversion")]


def test_steelman_docket_plain_runner_up_without_inversion():
    dest = scoreboard([O1, O3], WEIGHTS, 0.0)
    adjusted = scoreboard([O1, O3], WEIGHTS, 0.1)
    assert steelman_docket(dest, adjusted) == [("o3-tail", "runner-up")]


def test_steelman_docket_includes_demoted_options_below_the_runner_up():
    # o4 sits at adjusted rank 4, demoted from destination rank 2 — it gets a
    # steelman even though it is nowhere near runner-up.
    o4 = option("o4-demoted", {"a": 3.4, "b": 2.4, "c": 2.8}, pref=1.0)  # dest 2.98
    mid = option("mid", {"a": 2.9, "b": 2.9, "c": 2.9}, pref=2.9)  # dest 2.9
    dest = scoreboard([O1, O2, o4, mid], WEIGHTS, 0.0)
    assert [row.option_id for row in dest] == [
        "o1-dest-leader",
        "o4-demoted",
        "mid",
        "o2-pref-leader",
    ]
    adjusted = scoreboard([O1, O2, o4, mid], WEIGHTS, 0.2)
    assert [row.option_id for row in adjusted] == [
        "o2-pref-leader",
        "o1-dest-leader",
        "mid",
        "o4-demoted",
    ]
    docket = steelman_docket(dest, adjusted)
    assert ("o4-demoted", "rank-inversion") in docket
    assert ("o1-dest-leader", "rank-inversion") in docket
    assert all(oid != "o2-pref-leader" for oid, _ in docket)  # never the winner


def test_steelman_docket_never_includes_the_adjusted_winner():
    dest = scoreboard([O1, O2], WEIGHTS, 0.0)
    adjusted = scoreboard([O1, O2], WEIGHTS, 0.2)
    assert all(oid != "o2-pref-leader" for oid, _ in steelman_docket(dest, adjusted))


# -- criterion weight flips ----------------------------------------------------


def test_flip_delta_hand_computed():
    # gap(0) = 0.4; sensitivity g on 'a' = (4 - 2.4) - (2 - 3.6) = 3.2;
    # tie at delta = -0.4 / 3.2 = -0.125 (weight 0.5 -> 0.375).
    delta = criterion_flip_delta(O1, O2, WEIGHTS, 0.0, "a")
    assert delta == pytest.approx(-0.125)


def test_flip_delta_actually_ties_the_pair_under_gate_b_rescaling():
    delta = criterion_flip_delta(O1, O2, WEIGHTS, 0.0, "a")
    new_a = WEIGHTS["a"] + delta
    factor = (1 - new_a) / (1 - WEIGHTS["a"])
    new_weights = {"a": new_a, "b": WEIGHTS["b"] * factor, "c": WEIGHTS["c"] * factor}
    assert sum(new_weights.values()) == pytest.approx(1.0)
    assert destination_point(O1, new_weights) == pytest.approx(destination_point(O2, new_weights))


def test_flip_delta_scales_through_the_slot_weight():
    # Equal preference scores: the slot shrinks gap and sensitivity by the
    # same (1 - w_p), so the flip delta is unchanged.
    a = option("a1", {"a": 4.0, "b": 2.0, "c": 3.0}, pref=3.0)
    b = option("b1", {"a": 2.0, "b": 4.0, "c": 3.0}, pref=3.0)
    assert criterion_flip_delta(a, b, WEIGHTS, 0.2, "a") == pytest.approx(-0.125)


def test_flip_delta_none_when_options_move_together():
    # Identical criterion profiles: no reweighting can separate them.
    a = option("a1", {"a": 4.0, "b": 2.0, "c": 3.0}, pref=4.0)
    b = option("b1", {"a": 4.0, "b": 2.0, "c": 3.0}, pref=2.0)
    assert criterion_flip_delta(a, b, WEIGHTS, 0.2, "a") is None


def test_flip_delta_zero_when_already_tied():
    # Different profiles, same weighted total (3.0) and same criterion-'a'
    # sensitivity direction is irrelevant: the gap is already zero.
    tied_1 = option("t1", {"a": 3.0, "b": 3.0, "c": 3.0}, pref=3.0)
    tied_2 = option("t2", {"a": 3.6, "b": 2.0, "c": 3.0}, pref=3.0)  # dest 3.0
    assert criterion_flip_delta(tied_1, tied_2, WEIGHTS, 0.0, "a") == 0.0


def test_flip_delta_none_when_out_of_range():
    # A huge gap on a light criterion: the required delta exceeds 1 - w_c.
    strong = option("strong", {"a": 5.0, "b": 5.0, "c": 5.0}, pref=5.0)
    weak = option("weak", {"a": 1.0, "b": 1.0, "c": 1.5}, pref=1.0)
    assert criterion_flip_delta(strong, weak, WEIGHTS, 0.0, "c") is None


def test_weight_sensitivity_covers_every_criterion_for_the_top_two():
    flips = weight_sensitivity([O1, O2, O3], WEIGHTS, 0.0)
    assert {f.criterion_id for f in flips} == set(WEIGHTS)
    by_id = {f.criterion_id: f for f in flips}
    assert by_id["a"].flip_delta == pytest.approx(-0.125)
    # Shifting weight onto 'b' (where O2 leads) closes the gap from the other
    # side: gap 0.4, sensitivity g = (2 - 2.6/0.7) - (4 - 1.6/0.7) = -24/7,
    # so delta = 0.4 * 7/24 ~ +0.1167 (not symmetric with 'a': rescaling is
    # relative to 1 - w_k, and w_a != w_b).
    assert by_id["b"].flip_delta == pytest.approx(0.1167, abs=1e-4)


def test_weight_sensitivity_needs_two_options():
    assert weight_sensitivity([O1], WEIGHTS, 0.0) == []


# -- the preference-slot sweep --------------------------------------------------


def test_preference_sweep_covers_0_to_40_inclusive():
    sweep = preference_sweep([O1, O2], WEIGHTS)
    assert [p.slot_weight for p in sweep] == pytest.approx(
        [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4]
    )


def test_preference_sweep_winner_changes_at_the_crossing():
    # gap: (3.2 - 1.2w) vs (2.8 + 2.2w) -> crossing at w = 0.4/3.4 ~ 0.1176.
    sweep = preference_sweep([O1, O2], WEIGHTS)
    winners = [p.winner for p in sweep]
    assert winners[:3] == ["o1-dest-leader"] * 3  # 0, 0.05, 0.1
    assert winners[3:] == ["o2-pref-leader"] * 6  # 0.15 .. 0.4


def test_preference_sweep_rankings_are_complete():
    sweep = preference_sweep([O1, O2, O3], WEIGHTS)
    for point in sweep:
        assert sorted(point.ranking) == ["o1-dest-leader", "o2-pref-leader", "o3-tail"]


# -- rendered views --------------------------------------------------------------


def test_render_scoreboards_names_the_inversion():
    dest = scoreboard([O1, O2], WEIGHTS, 0.0)
    adjusted = scoreboard([O1, O2], WEIGHTS, 0.2)
    text = render_scoreboards(dest, adjusted)
    assert "o1-dest-leader" in text and "o2-pref-leader" in text
    assert "Rank inversions" in text
    assert "reverses them" in text


def test_render_scoreboards_reports_agreement():
    dest = scoreboard([O1, O3], WEIGHTS, 0.0)
    text = render_scoreboards(dest, dest)
    assert "No rank inversions" in text


def test_render_sensitivity_carries_flips_and_sweep():
    flips = weight_sensitivity([O1, O2], WEIGHTS, 0.2)
    sweep = preference_sweep([O1, O2], WEIGHTS)
    text = render_sensitivity(flips, sweep, 0.2)
    assert "Criterion-weight flips" in text
    assert "Preference-slot sweep" in text
    assert "The winner DEPENDS on the preference-slot weight" in text


def test_render_sensitivity_stable_winner():
    flips = weight_sensitivity([O1, O3], WEIGHTS, 0.2)
    sweep = preference_sweep([O1, O3], WEIGHTS)
    text = render_sensitivity(flips, sweep, 0.2)
    assert "stable across the whole preference-slot sweep" in text


def test_ranked_option_is_frozen():
    row = RankedOption(option_id="x", score=3.0, rank=1)
    with pytest.raises(AttributeError):
        row.rank = 2  # type: ignore[misc]
