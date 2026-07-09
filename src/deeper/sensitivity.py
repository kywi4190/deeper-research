"""Code-computed scoreboards and rubric sensitivity (design §5/S7, §5/S8).

Everything here is deterministic arithmetic over screening records — never an
LLM's job (P8). S7 uses it to build the tournament's priority docket (the two
scoreboards and their rank inversions, which is exactly where
preference-overfitting would hide) and to hand the frame-checker its
rubric-fragility evidence; S8 reuses the same functions for the report's
sensitivity analysis.

The two scoreboards (design §5/S7):

- **destination-only** — the preference-slot weight forced to 0: every option
  scored purely on the destination-derived criteria.
- **preference-adjusted** — the slot weight as configured at Gate B.

A *rank inversion* is a pair of options whose strict order flips between the
boards: the destination model says X beats Y, the preference-adjusted board
says Y beats X. A tie on one board refined by the other is not an inversion —
nothing was contradicted, only resolved.

The flip analysis answers §5/S8-3's question analytically: for the top two
options and each criterion, the signed weight change (criterion pinned, the
others rescaled proportionally — Gate B's own semantics) that ties them; None
when no in-range change can. The preference sweep tables the ranking as the
slot weight runs 0 → 40%.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from deeper.schemas import OptionScreening, Rubric, ScreeningResult

# Score-comparison tolerance: board scores are rounded to 4 decimals (matching
# the stored-aggregate convention), so anything closer than this is a tie.
EPSILON = 1e-6

# The report's preference-slot sweep range and resolution (design §5/S8-3:
# "how the ranking shifts as the preference-slot weight sweeps 0% -> 40%").
SWEEP_MAX_WEIGHT = 0.4
SWEEP_STEP = 0.05


@dataclass(frozen=True)
class RankedOption:
    """One scoreboard row. Ranks are dense (1, 2, 2, 3): epsilon-tied scores
    share a rank, so a rank difference always reflects a real score difference."""

    option_id: str
    score: float
    rank: int


@dataclass(frozen=True)
class CriterionFlip:
    """The signed weight change on one criterion that ties ranks 1 and 2
    (criterion pinned, others rescaled — Gate B semantics); None when no
    in-range change can flip them."""

    criterion_id: str
    weight: float
    flip_delta: float | None


@dataclass(frozen=True)
class SweepPoint:
    """The full ranking at one preference-slot weight."""

    slot_weight: float
    ranking: tuple[str, ...]

    @property
    def winner(self) -> str:
        return self.ranking[0]


def destination_point(option: OptionScreening, weights: Mapping[str, float]) -> float:
    """Sum of weight * score over the criteria — the destination-only estimate."""
    return sum(weights[cs.criterion_id] * cs.score for cs in option.criterion_scores)


def combined_point(
    option: OptionScreening, weights: Mapping[str, float], slot_weight: float
) -> float:
    """(1 - w_p) * destination + w_p * preference — the screener's own formula,
    recomputed in code. An option without a preference score is its
    destination point at every slot weight."""
    dest = destination_point(option, weights)
    if option.preference_score is None:
        return dest
    return (1 - slot_weight) * dest + slot_weight * option.preference_score.score


def scoreboard(
    options: Sequence[OptionScreening], weights: Mapping[str, float], slot_weight: float
) -> list[RankedOption]:
    """Rank every option by its combined point at this slot weight. Sorted by
    descending score with option-id tie-break for a stable order; dense ranks
    so epsilon-tied scores share one."""
    scored = sorted(
        ((round(combined_point(o, weights, slot_weight), 4), o.option_id) for o in options),
        key=lambda pair: (-pair[0], pair[1]),
    )
    board: list[RankedOption] = []
    rank = 0
    previous: float | None = None
    for score, option_id in scored:
        if previous is None or previous - score > EPSILON:
            rank += 1
        board.append(RankedOption(option_id=option_id, score=score, rank=rank))
        previous = score
    return board


def dual_scoreboards(
    screening: ScreeningResult, rubric: Rubric
) -> tuple[list[RankedOption], list[RankedOption]]:
    """(destination-only, preference-adjusted): the slot weight forced to 0
    versus as configured at Gate B."""
    weights = {c.id: c.weight for c in rubric.criteria}
    return (
        scoreboard(screening.options, weights, 0.0),
        scoreboard(screening.options, weights, rubric.preference_slot.weight),
    )


def rank_inversions(
    destination: Sequence[RankedOption], adjusted: Sequence[RankedOption]
) -> list[tuple[str, str]]:
    """Every (demoted, promoted) pair whose strict order flips between the
    boards: destination says demoted > promoted, the adjusted board says the
    reverse. Ties refined by the other board are not inversions. Pairs come
    back in adjusted-board order of the demoted option."""
    dest_score = {row.option_id: row.score for row in destination}
    adj_order = {row.option_id: i for i, row in enumerate(adjusted)}
    inversions: list[tuple[str, str]] = []
    for row_x in adjusted:  # x = the option preferences demoted
        for row_y in adjusted:
            if (
                dest_score[row_x.option_id] - dest_score[row_y.option_id] > EPSILON
                and row_y.score - row_x.score > EPSILON
            ):
                inversions.append((row_x.option_id, row_y.option_id))
    inversions.sort(key=lambda pair: (adj_order[pair[0]], adj_order[pair[1]]))
    return inversions


def steelman_docket(
    destination: Sequence[RankedOption], adjusted: Sequence[RankedOption]
) -> list[tuple[str, str]]:
    """The tournament's priority docket: (option_id, trigger) for every option
    owed a steelman (design §5/S7-2) — the adjusted board's runner(s)-up, plus
    every option an inversion demoted (trigger 'rank-inversion', which wins
    when both apply: the inversion is the sharper reason). The adjusted
    winner is never steelmanned — the case *for* it is the status quo the
    prosecutors attack."""
    demoted = {x for x, _ in rank_inversions(destination, adjusted)}
    docket: list[tuple[str, str]] = []
    for row in adjusted:
        if row.rank == 1:
            continue
        if row.option_id in demoted:
            docket.append((row.option_id, "rank-inversion"))
        elif row.rank == 2:
            docket.append((row.option_id, "runner-up"))
    return docket


def criterion_flip_delta(
    leader: OptionScreening,
    runner_up: OptionScreening,
    weights: Mapping[str, float],
    slot_weight: float,
    criterion_id: str,
) -> float | None:
    """The signed change to `criterion_id`'s weight that ties leader and
    runner-up on the combined board, with the criterion pinned and every other
    criterion rescaled proportionally (exactly Gate B's edit semantics).

    Derivation: with w_k -> w_k + d and the rest scaled by
    (1 - w_k - d)/(1 - w_k), an option's destination point moves linearly:
    dest'(o) = dest(o) + d * (s_k(o) - rest_mean(o)), where rest_mean is the
    weighted mean of its other criteria. The combined gap is then
    gap(d) = gap(0) + d * g, and the tie sits at d = -gap(0)/g.

    Returns None when the gap cannot close: the criterion cannot be rescaled
    against (w_k ~ 1), the options move together (g ~ 0), or the required
    weight leaves [0, 1].
    """
    w_k = weights[criterion_id]
    if 1 - w_k < EPSILON:
        return None

    def sensitivity(option: OptionScreening) -> float:
        dest = destination_point(option, weights)
        s_k = next(cs.score for cs in option.criterion_scores if cs.criterion_id == criterion_id)
        rest_mean = (dest - w_k * s_k) / (1 - w_k)
        factor = 1 - slot_weight if option.preference_score is not None else 1.0
        return factor * (s_k - rest_mean)

    gap = combined_point(leader, weights, slot_weight) - combined_point(
        runner_up, weights, slot_weight
    )
    g = sensitivity(leader) - sensitivity(runner_up)
    if abs(g) < EPSILON:
        return None if abs(gap) > EPSILON else 0.0
    delta = -gap / g
    if delta < -w_k - EPSILON or delta > 1 - w_k + EPSILON:
        return None
    return round(delta, 4)


def weight_sensitivity(
    options: Sequence[OptionScreening], weights: Mapping[str, float], slot_weight: float
) -> list[CriterionFlip]:
    """§5/S8-3's per-criterion table for the board's top two options: the
    weight delta that flips rank 1 <-> 2. Empty when fewer than two options."""
    board = scoreboard(options, weights, slot_weight)
    if len(board) < 2:
        return []
    by_id = {o.option_id: o for o in options}
    leader, runner_up = by_id[board[0].option_id], by_id[board[1].option_id]
    return [
        CriterionFlip(
            criterion_id=cid,
            weight=weights[cid],
            flip_delta=criterion_flip_delta(leader, runner_up, weights, slot_weight, cid),
        )
        for cid in weights
    ]


def preference_sweep(
    options: Sequence[OptionScreening],
    weights: Mapping[str, float],
    *,
    max_weight: float = SWEEP_MAX_WEIGHT,
    step: float = SWEEP_STEP,
) -> list[SweepPoint]:
    """The ranking at every slot weight from 0 to `max_weight` in `step`s —
    the report's 0% -> 40% sweep, computed once and reused by S7 and S8."""
    points: list[SweepPoint] = []
    for i in range(round(max_weight / step) + 1):
        w = round(i * step, 4)
        board = scoreboard(options, weights, w)
        points.append(SweepPoint(slot_weight=w, ranking=tuple(row.option_id for row in board)))
    return points


# -- rendered views (prompt inputs for the frame-checker; S8 narration source) ---


def render_scoreboards(
    destination: Sequence[RankedOption], adjusted: Sequence[RankedOption]
) -> str:
    """Both boards side by side, with every rank inversion called out."""
    dest_by_id = {row.option_id: row for row in destination}
    lines = [
        "| option | destination-only score (rank) | preference-adjusted score (rank) |",
        "|---|---|---|",
    ]
    for row in adjusted:
        dest = dest_by_id[row.option_id]
        lines.append(
            f"| {row.option_id} | {dest.score:g} (#{dest.rank}) | {row.score:g} (#{row.rank}) |"
        )
    inversions = rank_inversions(destination, adjusted)
    if inversions:
        lines.append("")
        lines.append("Rank inversions (destination-only order flipped by preferences):")
        lines.extend(
            f"- destination-only ranks '{x}' above '{y}'; the preference-adjusted "
            f"board reverses them"
            for x, y in inversions
        )
    else:
        lines.append("")
        lines.append("No rank inversions between the two boards.")
    return "\n".join(lines)


def render_sensitivity(
    flips: Sequence[CriterionFlip], sweep: Sequence[SweepPoint], slot_weight: float
) -> str:
    """The code-computed sensitivity evidence, rendered for the frame-checker's
    rubric-fragility check and the S8 narration."""
    lines = [
        f"Criterion-weight flips (top two options, slot weight {slot_weight:g}; "
        "the named criterion is pinned at weight+delta, the others rescale "
        "proportionally):",
        "",
        "| criterion | weight | delta that ties ranks 1-2 |",
        "|---|---|---|",
    ]
    for flip in flips:
        if flip.flip_delta is None:
            verdict = "none in range - robust to reweighting this criterion"
        else:
            verdict = f"{flip.flip_delta:+g} (new weight {flip.weight + flip.flip_delta:g})"
        lines.append(f"| {flip.criterion_id} | {flip.weight:g} | {verdict} |")
    lines += [
        "",
        "Preference-slot sweep 0% -> 40%:",
        "",
        "| slot weight | winner | ranking |",
        "|---|---|---|",
    ]
    for point in sweep:
        lines.append(f"| {point.slot_weight:g} | {point.winner} | {' > '.join(point.ranking)} |")
    winners = {point.winner for point in sweep}
    lines.append("")
    if len(winners) > 1:
        changes = [
            f"{prev.winner} -> {cur.winner} between {prev.slot_weight:g} and {cur.slot_weight:g}"
            for prev, cur in zip(sweep, sweep[1:], strict=False)
            if prev.winner != cur.winner
        ]
        lines.append(
            "The winner DEPENDS on the preference-slot weight: " + "; ".join(changes) + "."
        )
    else:
        lines.append("The winner is stable across the whole preference-slot sweep.")
    return "\n".join(lines)
