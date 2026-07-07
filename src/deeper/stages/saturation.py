"""S1 saturation rule — pure math over the merger's dedup map (design §5/S1, §12).

Exactly as specified: for each cartographer invocation, **marginal novelty** =
(new distinct merged angles contributed by that invocation) / (its total raw
angles), where "distinct" comes from the merger's dedup mapping. Cartography is
saturated when the mean novelty across the trailing window (default: the last
two invocations) falls below the threshold (default 0.2); otherwise the
orchestrator spawns up to 2 more cartographers re-running the heuristics that
produced the most novel angles, under a hard cap of 8 invocations total.

Everything here is deterministic and side-effect free; the stage owns dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass

from deeper.schemas import DedupEntry, Heuristic


@dataclass(frozen=True)
class CartographerRun:
    """One cartographer invocation, in dispatch order. `context` distinguishes
    repeat passes of the same heuristic (e.g. "contrarian" vs "contrarian-2");
    dedup entries are attributed to a run by (heuristic, raw angle name)."""

    heuristic: Heuristic
    context: str
    raw_angle_names: tuple[str, ...]


@dataclass(frozen=True)
class RunNovelty:
    """One run's marginal-novelty measurement."""

    run: CartographerRun
    novel_ids: tuple[str, ...]  # merged angle ids this run introduced first

    @property
    def total_angles(self) -> int:
        return len(self.run.raw_angle_names)

    @property
    def novelty(self) -> float:
        return len(self.novel_ids) / self.total_angles if self.total_angles else 0.0


def marginal_novelty(runs: list[CartographerRun], dedup_map: list[DedupEntry]) -> list[RunNovelty]:
    """Per-invocation marginal novelty, in dispatch order. A merged angle is
    "new" for the run that first mapped a raw angle into it; raw angles the
    merger has not (yet) covered contribute nothing but still count in the
    denominator, so an incomplete dedup map depresses novelty instead of
    inflating it."""
    seen: set[str] = set()
    out: list[RunNovelty] = []
    for run in runs:
        names = set(run.raw_angle_names)
        targets = {
            e.merged_into for e in dedup_map if e.heuristic is run.heuristic and e.raw_name in names
        }
        out.append(RunNovelty(run=run, novel_ids=tuple(sorted(targets - seen))))
        seen |= targets
    return out


def is_saturated(novelties: list[RunNovelty], *, threshold: float, window: int) -> bool:
    """Mean marginal novelty over the trailing `window` invocations < threshold."""
    if not novelties:
        return False
    tail = novelties[-max(window, 1) :]
    return sum(n.novelty for n in tail) / len(tail) < threshold


def expansion_heuristics(
    novelties: list[RunNovelty], *, max_cartographers: int, spawn_limit: int = 2
) -> list[Heuristic]:
    """Which heuristics to spawn again: up to `spawn_limit` (design: 2) of the
    heuristics that produced the most novel angles so far, never exceeding the
    hard cap on total invocations. Zero-novelty heuristics are never respawned;
    ties break toward the earlier-dispatched heuristic (deterministic)."""
    budget = min(spawn_limit, max_cartographers - len(novelties))
    if budget <= 0:
        return []
    counts: dict[Heuristic, int] = {}
    first_index: dict[Heuristic, int] = {}
    for i, n in enumerate(novelties):
        counts[n.run.heuristic] = counts.get(n.run.heuristic, 0) + len(n.novel_ids)
        first_index.setdefault(n.run.heuristic, i)
    ranked = sorted(
        (h for h, c in counts.items() if c > 0),
        key=lambda h: (-counts[h], first_index[h]),
    )
    return ranked[:budget]


def uncovered_raw_angles(
    runs: list[CartographerRun], dedup_map: list[DedupEntry]
) -> list[tuple[Heuristic, str]]:
    """Raw angles with no dedup entry — the merger's map is stale (or its dedup
    map incomplete) and must be re-merged before the saturation rule can run."""
    covered = {(e.heuristic, e.raw_name) for e in dedup_map}
    return [
        (run.heuristic, name)
        for run in runs
        for name in run.raw_angle_names
        if (run.heuristic, name) not in covered
    ]
