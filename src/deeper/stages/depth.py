"""The S6 depth stopping rule and verifier sampling — pure deterministic code
(design §5/S6, P8).

The three-clause stopping rule, verbatim from the design: stop when (a) the
option's weighted score has moved < 0.15 (`caps.deep_dive_delta_score_stop`)
across the last round, AND (b) no remaining `low`-confidence claim is
load-bearing; OR (c) the per-option budget cap is hit — in which case the
dossier is stamped BUDGET-CAPPED with its open questions listed. Stability is
checked before the cap, so a final budgeted round that also stabilizes counts
as converged, not capped.

Load-bearing means "moves any criterion score >= 1 point". The analyst tags it,
and the re-score diff cross-checks the tag with teeth: any criterion whose
score moved >= 1 point since the previous round promotes every claim in that
criterion's dossier section into the load-bearing set, tagged or not — the
*effective* load-bearing set is the union, and it drives both clause (b) and
verifier sampling. Under-tagged claims are reported so prompt calibration has
evidence.

Verifier sampling (design §5/S6): ALL effective load-bearing claims plus a
random 20% of the rest. "Random" is a seeded draw keyed on the option id, so
resume re-samples identically and mock fixtures can be authored against the
exact sample.
"""

from __future__ import annotations

import math
import random

from deeper.schemas import Confidence, Dossier, OptionScreening

# Design §5/S6: a load-bearing claim moves a criterion score by >= 1 point.
LOAD_BEARING_MOVE = 1.0
# Design §5/S6: the verifier samples a random 20% of the non-load-bearing claims.
VERIFIER_SAMPLE_FRACTION = 0.2


def should_stop(
    delta: float,
    has_low_conf_load_bearing: bool,
    rounds_used: int,
    cap: int,
    *,
    delta_stop: float,
) -> tuple[bool, bool]:
    """The three-clause rule -> (stop, budget_capped)."""
    if delta < delta_stop and not has_low_conf_load_bearing:
        return True, False
    if rounds_used >= cap:
        return True, True
    return False, False


def weighted_delta(prev: OptionScreening, cur: OptionScreening) -> float:
    """Clause (a)'s number: how far the weighted point estimate moved."""
    return abs(cur.weighted_point - prev.weighted_point)


def criterion_moves(prev: OptionScreening, cur: OptionScreening) -> dict[str, float]:
    """Absolute per-criterion score movement between two re-scores (criteria
    present in both; integrity checks upstream guarantee identical rubric ids)."""
    before = {cs.criterion_id: cs.score for cs in prev.criterion_scores}
    return {
        cs.criterion_id: abs(cs.score - before[cs.criterion_id])
        for cs in cur.criterion_scores
        if cs.criterion_id in before
    }


def effective_load_bearing(dossier: Dossier, moves: dict[str, float]) -> tuple[set[str], list[str]]:
    """Analyst tags unioned with the re-score-diff cross-check.

    Returns (effective load-bearing claim ids, one warning per criterion whose
    >= 1-point move promoted untagged claims).
    """
    effective = {c.id for c in dossier.claims if c.load_bearing}
    warnings: list[str] = []
    for criterion_id, move in sorted(moves.items()):
        if move < LOAD_BEARING_MOVE:
            continue
        section = dossier.criterion_sections.get(criterion_id)
        if section is None:
            continue
        untagged = sorted(set(section.claim_ids) - effective)
        effective.update(section.claim_ids)
        if untagged:
            warnings.append(
                f"criterion '{criterion_id}' moved {move:g} points but these claims "
                f"in its section were not tagged load_bearing: {untagged} — treated "
                "as load-bearing anyway (the re-score diff cross-checks the tag)"
            )
    return effective, warnings


def low_conf_load_bearing(dossier: Dossier, effective_ids: set[str]) -> list[str]:
    """Clause (b): claim ids that are still low-confidence AND load-bearing."""
    return sorted(
        c.id for c in dossier.claims if c.confidence is Confidence.LOW and c.id in effective_ids
    )


def verifier_sample(dossier: Dossier, effective_ids: set[str]) -> tuple[list[str], int, int]:
    """ALL effective load-bearing claims + a seeded 20% (ceiling) of the rest.

    Returns (sampled ids: load-bearing first, then the drawn others, each group
    sorted; count of load-bearing sampled; count of others sampled).
    """
    load_bearing = sorted(i for i in effective_ids if i in {c.id for c in dossier.claims})
    rest = sorted(c.id for c in dossier.claims if c.id not in effective_ids)
    k = math.ceil(VERIFIER_SAMPLE_FRACTION * len(rest)) if rest else 0
    rng = random.Random(f"verifier-sample:{dossier.option_id}")
    drawn = sorted(rng.sample(rest, k))
    return [*load_bearing, *drawn], len(load_bearing), len(drawn)


def derive_open_questions(dossier: Dossier, low_conf_lb_ids: list[str]) -> list[str]:
    """The BUDGET-CAPPED fallback: when the capped dossier lists no open
    questions, the remaining low-confidence load-bearing claims ARE the open
    questions — the schema requires them listed so unfinished depth is visible."""
    by_id = {c.id: c for c in dossier.claims}
    return [
        f"Unresolved at the budget cap — low-confidence load-bearing claim "
        f"'{claim_id}': {by_id[claim_id].text}"
        for claim_id in low_conf_lb_ids
        if claim_id in by_id
    ] or [
        "The per-option budget cap hit before the score stabilized; the last "
        "round still moved the weighted score beyond the stability threshold."
    ]
