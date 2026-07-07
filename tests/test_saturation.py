"""Unit tests for the S1 saturation rule's pure math (design §5/S1, §12):
marginal novelty from contrived dedup maps, the threshold/window decision, and
expansion-choice ranking with hard-cap enforcement."""

from __future__ import annotations

from deeper.schemas import DedupEntry, Heuristic
from deeper.stages.saturation import (
    CartographerRun,
    RunNovelty,
    expansion_heuristics,
    is_saturated,
    marginal_novelty,
    uncovered_raw_angles,
)

FP = Heuristic.FIRST_PRINCIPLES
AN = Heuristic.ANALOGIST
CO = Heuristic.CONTRARIAN


def run(heuristic: Heuristic, context: str, *names: str) -> CartographerRun:
    return CartographerRun(heuristic=heuristic, context=context, raw_angle_names=names)


def entry(heuristic: Heuristic, raw: str, into: str) -> DedupEntry:
    return DedupEntry(heuristic=heuristic, raw_name=raw, merged_into=into)


def novelty_of(novel: int, total: int, heuristic: Heuristic = FP, index: int = 0) -> RunNovelty:
    return RunNovelty(
        run=run(heuristic, f"{heuristic.value}-{index}", *(f"raw-{i}" for i in range(total))),
        novel_ids=tuple(f"novel-{i}" for i in range(novel)),
    )


# -- marginal novelty ----------------------------------------------------------


def test_marginal_novelty_is_new_distinct_over_total():
    runs = [run(FP, "first-principles", "A1", "A2"), run(AN, "analogist", "B1", "B2")]
    dedup = [
        entry(FP, "A1", "region-1"),
        entry(FP, "A2", "region-2"),
        entry(AN, "B1", "region-1"),  # corroboration, not novelty
        entry(AN, "B2", "region-3"),
    ]
    first, second = marginal_novelty(runs, dedup)
    assert first.novel_ids == ("region-1", "region-2") and first.novelty == 1.0
    assert second.novel_ids == ("region-3",) and second.novelty == 0.5


def test_multiple_raws_folding_into_one_region_count_once():
    runs = [run(FP, "first-principles", "A1", "A2", "A3")]
    dedup = [entry(FP, name, "one-region") for name in ("A1", "A2", "A3")]
    (only,) = marginal_novelty(runs, dedup)
    assert only.novel_ids == ("one-region",)
    assert only.novelty == 1 / 3


def test_uncovered_raw_angles_depress_novelty_not_inflate_it():
    runs = [run(FP, "first-principles", "A1", "A2", "A3")]
    dedup = [entry(FP, "A2", "region-1")]  # merger missed A1 and A3
    (only,) = marginal_novelty(runs, dedup)
    assert only.novelty == 1 / 3
    assert uncovered_raw_angles(runs, dedup) == [(FP, "A1"), (FP, "A3")]


def test_repeat_passes_of_one_heuristic_are_attributed_by_raw_name():
    runs = [run(CO, "contrarian", "C1"), run(CO, "contrarian-2", "C9")]
    dedup = [entry(CO, "C1", "region-1"), entry(CO, "C9", "region-2")]
    first, second = marginal_novelty(runs, dedup)
    assert first.novel_ids == ("region-1",)
    assert second.novel_ids == ("region-2",)


# -- the saturation decision ----------------------------------------------------


def test_saturated_when_trailing_window_mean_below_threshold():
    novelties = [novelty_of(3, 3), novelty_of(0, 3), novelty_of(1, 3)]
    # trailing two: (0 + 1/3) / 2 = 0.1667 < 0.2 -> saturated
    assert is_saturated(novelties, threshold=0.2, window=2)


def test_not_saturated_at_threshold_or_above():
    # design: "if >= 0.2, spawn" — the boundary itself keeps exploring
    novelties = [novelty_of(0, 5), novelty_of(1, 5), novelty_of(3, 10)]
    # trailing two: (0.2 + 0.3) / 2 = 0.25 -> not saturated
    assert not is_saturated(novelties, threshold=0.2, window=2)
    exactly = [novelty_of(1, 5), novelty_of(1, 5)]  # mean exactly 0.2
    assert not is_saturated(exactly, threshold=0.2, window=2)


def test_window_larger_than_history_uses_everything():
    novelties = [novelty_of(0, 4)]
    assert is_saturated(novelties, threshold=0.2, window=2)
    assert not is_saturated([novelty_of(4, 4)], threshold=0.2, window=2)


# -- expansion choice -----------------------------------------------------------


def test_expansion_picks_top_two_by_novel_angle_count():
    novelties = [
        novelty_of(3, 3, FP, 0),
        novelty_of(2, 3, AN, 1),
        novelty_of(3, 3, CO, 2),
    ]
    # fp and contrarian tie at 3; fp dispatched earlier wins the tie-break
    assert expansion_heuristics(novelties, max_cartographers=8) == [FP, CO]


def test_expansion_never_exceeds_hard_cap():
    novelties = [novelty_of(3, 3, FP, 0), novelty_of(2, 3, AN, 1), novelty_of(3, 3, CO, 2)]
    assert expansion_heuristics(novelties, max_cartographers=4) == [FP]
    assert expansion_heuristics(novelties, max_cartographers=3) == []
    assert expansion_heuristics(novelties, max_cartographers=2) == []


def test_expansion_skips_heuristics_with_zero_novel_angles():
    novelties = [novelty_of(0, 3, FP, 0), novelty_of(1, 3, AN, 1), novelty_of(0, 3, CO, 2)]
    assert expansion_heuristics(novelties, max_cartographers=8) == [AN]


def test_expansion_sums_novelty_across_repeat_passes():
    novelties = [
        novelty_of(1, 3, FP, 0),
        novelty_of(2, 3, AN, 1),
        novelty_of(2, 3, FP, 2),  # a second first-principles pass: 1 + 2 = 3 total
    ]
    assert expansion_heuristics(novelties, max_cartographers=8) == [FP, AN]
