"""The S5 shortlist rule — pure deterministic code (design §5/S5, P8).

Implemented exactly as specified:

(a) **Kill-risks first.** A confirmed kill-risk eliminates the option
    regardless of score.
(b) **Optimism under uncertainty.** A surviving option advances when its
    weighted UPPER confidence bound clears the shortlist threshold — never its
    point estimate. Under-researched dark horses (wide bands) advance alongside
    well-documented favorites: under-information triggers more research, not
    elimination.
(c) **Diversity guardrails.** No more than `max_per_angle` finalists from a
    single angle; and if the top-k finalists all come from ≤2 angles, the
    highest-UCB option from each unrepresented top-half angle (by relevance
    prior) is added — breadth insurance at the moment it is most at risk.

Every option gets a one-paragraph advanced/cut reason: cuts are auditable, and
the S7 frame-checker re-reads them.

This module also owns the screening arithmetic the orchestrator does not trust
an LLM with: `recompute_aggregates` re-derives every weighted point/UCB from
the criterion scores and the rubric weights, and `verify_screening` is the
cross-artifact referential-integrity check (an orchestrator concern by design —
each schema validates a single file).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping

from deeper.schemas import (
    KillRiskOutcome,
    OptionCardSet,
    OptionScreening,
    Rubric,
    ScreeningResult,
    Shortlist,
    ShortlistCause,
    ShortlistDecision,
    ShortlistOutcome,
)

# Aggregate drift above this is reported when code corrects the screener's
# arithmetic; the corrected numbers are used either way.
AGGREGATE_TOLERANCE = 0.05
# The guardrail's trigger: top-k finalists spanning this many angles or fewer.
GUARDRAIL_MAX_ANGLES = 2


def confirmed_kill(option: OptionScreening) -> str | None:
    """The first confirmed kill-risk fact, or None."""
    for check in option.kill_risk_checks:
        if check.outcome is KillRiskOutcome.CONFIRMED:
            return check.fact
    return None


# -- screening arithmetic ---------------------------------------------------------


def recompute_aggregates(
    screening: ScreeningResult, rubric: Rubric
) -> tuple[ScreeningResult, list[str]]:
    """Re-derive weighted_point / weighted_ucb in code (P8: arithmetic is the
    orchestrator's job). Returns the corrected result plus a message per option
    whose stored numbers drifted beyond AGGREGATE_TOLERANCE."""
    weights = {c.id: c.weight for c in rubric.criteria}
    w_p = rubric.preference_slot.weight
    corrected: list[OptionScreening] = []
    drift: list[str] = []
    for option in screening.options:
        dest_point = sum(weights[cs.criterion_id] * cs.score for cs in option.criterion_scores)
        dest_ucb = sum(weights[cs.criterion_id] * cs.band.hi for cs in option.criterion_scores)
        if option.preference_score is not None:
            point = (1 - w_p) * dest_point + w_p * option.preference_score.score
            ucb = (1 - w_p) * dest_ucb + w_p * option.preference_score.band.hi
        else:
            point, ucb = dest_point, dest_ucb
        point, ucb = round(point, 4), round(ucb, 4)
        if (
            abs(point - option.weighted_point) > AGGREGATE_TOLERANCE
            or abs(ucb - option.weighted_ucb) > AGGREGATE_TOLERANCE
        ):
            drift.append(
                f"S5: corrected {option.option_id} aggregates — screener said "
                f"point {option.weighted_point:g} / ucb {option.weighted_ucb:g}, "
                f"code computes {point:g} / {ucb:g}"
            )
        corrected.append(option.model_copy(update={"weighted_point": point, "weighted_ucb": ucb}))
    return ScreeningResult(options=corrected, notes=screening.notes), drift


def verify_screening(
    screening: ScreeningResult, rubric: Rubric, card_sets: list[OptionCardSet]
) -> list[str]:
    """Cross-artifact referential integrity: every card scored exactly once,
    every id resolvable, every rubric criterion covered, the preference slot
    scored. Returns LLM-facing problem strings (empty = coherent)."""
    problems: list[str] = []
    cards = {c.id: s.angle_id for s in card_sets for c in s.cards}
    rubric_ids = sorted(c.id for c in rubric.criteria)
    for option in screening.options:
        if option.option_id not in cards:
            problems.append(f"- option '{option.option_id}' does not match any option card")
        elif option.angle_id != cards[option.option_id]:
            problems.append(
                f"- option '{option.option_id}' carries angle_id '{option.angle_id}' "
                f"but its card belongs to angle '{cards[option.option_id]}'"
            )
        scored = sorted(cs.criterion_id for cs in option.criterion_scores)
        if scored != rubric_ids:
            problems.append(
                f"- option '{option.option_id}' must score every rubric criterion "
                f"exactly once; expected {rubric_ids}, got {scored}"
            )
        if option.preference_score is None:
            problems.append(
                f"- option '{option.option_id}' is missing its preference-slot score "
                "(preferences.yaml exists, so the slot must be scored)"
            )
    unscored = sorted(set(cards) - {o.option_id for o in screening.options})
    if unscored:
        problems.append(f"- these option cards were never scored: {unscored}")
    return problems


# -- the shortlist rule -------------------------------------------------------------


def _advance_reason(option: OptionScreening, threshold: float) -> str:
    base = (
        f"Advanced: weighted upper confidence bound {option.weighted_ucb:g} clears the "
        f"shortlist threshold {threshold:g} (point estimate {option.weighted_point:g})."
    )
    if option.weighted_point < threshold:
        base += (
            " The point estimate alone would not have cleared the bar — this option "
            "advances on its wide uncertainty band: the evidence is thin, and "
            "under-information triggers more research (a deep dive), not elimination."
        )
    else:
        base += (
            " Both the optimistic and the central read of the evidence put this option "
            "above the bar."
        )
    return base


def build_shortlist(
    screening: ScreeningResult,
    angle_priors: Mapping[str, float],
    *,
    threshold: float,
    top_k: int,
    max_per_angle: int,
) -> Shortlist:
    """Apply the shortlist rule to a screening result.

    `angle_priors` maps every angle id in the map to its relevance prior — it
    defines "top-half angle" for the diversity guardrail. Decisions come back
    in the screening result's option order (matching scores.yaml); finalist ids
    are ordered by descending UCB.
    """
    decisions: dict[str, ShortlistDecision] = {}

    # (a) Kill-risks eliminate regardless of score.
    alive: list[OptionScreening] = []
    for option in screening.options:
        fact = confirmed_kill(option)
        if fact is not None:
            decisions[option.option_id] = ShortlistDecision(
                option_id=option.option_id,
                decision=ShortlistOutcome.CUT,
                cause=ShortlistCause.KILL_RISK_CONFIRMED,
                reason=(
                    f"Eliminated by a confirmed kill-risk, regardless of score (its "
                    f"weighted UCB of {option.weighted_ucb:g} would otherwise have been "
                    f"considered): {fact} The kill-risk check was a single cheap lookup "
                    "made before scoring; a confirmed kill saves an entire wasted deep dive."
                ),
            )
        else:
            alive.append(option)

    # (b) Advance on the UPPER confidence bound, with (c1) the per-angle cap.
    alive.sort(key=lambda o: (-o.weighted_ucb, o.option_id))
    finalists: list[OptionScreening] = []
    per_angle: Counter[str] = Counter()
    for option in alive:
        if option.weighted_ucb >= threshold:
            if per_angle[option.angle_id] >= max_per_angle:
                holders = ", ".join(f.option_id for f in finalists if f.angle_id == option.angle_id)
                decisions[option.option_id] = ShortlistDecision(
                    option_id=option.option_id,
                    decision=ShortlistOutcome.CUT,
                    cause=ShortlistCause.ANGLE_CAP,
                    reason=(
                        f"Cut by the angle-diversity cap: its weighted UCB of "
                        f"{option.weighted_ucb:g} clears the threshold {threshold:g}, but "
                        f"angle '{option.angle_id}' already holds the maximum of "
                        f"{max_per_angle} finalists ({holders}), each with a higher UCB. "
                        "One angle may not crowd out the rest of the map."
                    ),
                )
            else:
                per_angle[option.angle_id] += 1
                finalists.append(option)
                decisions[option.option_id] = ShortlistDecision(
                    option_id=option.option_id,
                    decision=ShortlistOutcome.ADVANCED,
                    cause=ShortlistCause.UCB_ABOVE_THRESHOLD,
                    reason=_advance_reason(option, threshold),
                )
        else:
            decisions[option.option_id] = ShortlistDecision(
                option_id=option.option_id,
                decision=ShortlistOutcome.CUT,
                cause=ShortlistCause.BELOW_THRESHOLD,
                reason=(
                    f"Cut: even the optimistic upper bound of its band "
                    f"({option.weighted_ucb:g}) does not reach the shortlist threshold "
                    f"{threshold:g} (point estimate {option.weighted_point:g}). The band "
                    "already prices in the evidence gaps, so more research is not what "
                    "this option is missing."
                ),
            )

    # (c2) Breadth insurance: top-k finalists spanning <= 2 angles adds the
    # highest-UCB option from each unrepresented top-half angle.
    top = finalists[: min(top_k, len(finalists))]
    top_angles = {o.angle_id for o in top}
    if len(top_angles) <= GUARDRAIL_MAX_ANGLES:
        half = math.ceil(len(angle_priors) / 2)
        top_half = sorted(angle_priors, key=lambda a: (-angle_priors[a], a))[:half]
        represented = {o.angle_id for o in finalists}
        for angle_id in top_half:
            if angle_id in represented:
                continue
            candidates = [o for o in alive if o.angle_id == angle_id]
            if not candidates:
                continue
            best = candidates[0]  # alive is UCB-desc sorted
            finalists.append(best)
            decisions[best.option_id] = ShortlistDecision(
                option_id=best.option_id,
                decision=ShortlistOutcome.ADVANCED,
                cause=ShortlistCause.DIVERSITY_GUARDRAIL_ADD,
                reason=(
                    f"Added by the breadth guardrail: the top-{len(top)} finalists span "
                    f"only {len(top_angles)} angle(s), and '{angle_id}' is a top-half "
                    f"angle (relevance prior {angle_priors[angle_id]:g}) with no finalist. "
                    f"This is its highest-UCB option (weighted UCB {best.weighted_ucb:g}, "
                    f"threshold {threshold:g}) — breadth insurance at the moment it is "
                    "most at risk."
                ),
            )

    if not finalists:
        raise ValueError(
            "the shortlist rule eliminated every option (all confirmed-killed or "
            "below threshold with no guardrail candidates) — the run needs human "
            "attention, not an empty finalist list"
        )
    finalists.sort(key=lambda o: (-o.weighted_ucb, o.option_id))
    return Shortlist(
        threshold=threshold,
        decisions=[decisions[o.option_id] for o in screening.options],
        finalist_ids=[o.option_id for o in finalists],
    )
