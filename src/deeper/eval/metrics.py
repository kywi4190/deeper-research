"""The five property metrics' deterministic math (design §10).

Every function here is pure: artifacts in, metric section out. The only
non-deterministic ingredient in the whole eval — the semantic angle matching
behind breadth — arrives pre-computed as an AngleMatchReport; combining it
with the spec is still just arithmetic.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from deeper.schemas import (
    AllocationTable,
    AngleMatchReport,
    AntiOverfitEval,
    BenchmarkSpec,
    BreadthEval,
    CardCritique,
    Confidence,
    DeepDiveRoundLog,
    DepthEval,
    DepthRow,
    Dossier,
    InformednessEval,
    InformednessRow,
    InversionPair,
    OptionCardSet,
    OptionCheckResult,
    QualityEval,
    QualityRow,
    Rubric,
    ScreeningResult,
    Shortlist,
    Steelman,
    SteelmanTrigger,
    Verdict,
    VerificationReport,
)
from deeper.sensitivity import dual_scoreboards, rank_inversions
from deeper.stages.s3_scouting import needs_revision

# -- rank correlation ---------------------------------------------------------


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Ascending ranks 1..n with ties sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman rank correlation with average-rank ties (Pearson over ranks).
    None when there are fewer than two pairs or either side has no variance —
    a floor-dominated allocation gives every angle the same units, and 'no
    signal' must not masquerade as correlation 0."""
    if len(xs) != len(ys):
        raise ValueError(f"length mismatch: {len(xs)} vs {len(ys)}")
    n = len(xs)
    if n < 2:
        return None
    rx, ry = _average_ranks(xs), _average_ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((r - mx) ** 2 for r in rx)
    syy = sum((r - my) ** 2 for r in ry)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    return round(sxy / (sxx * syy) ** 0.5, 4)


# -- breadth ------------------------------------------------------------------


def breadth(
    spec: BenchmarkSpec,
    match_report: AngleMatchReport,
    run_angle_count: int,
    option_checks: Sequence[OptionCheckResult] = (),
) -> BreadthEval:
    """Combine the judge's match report with the spec's reference union."""
    by_ref = {a.id: a for a in spec.reference_angles}
    hits, misses, matched = [], [], {}
    for match in match_report.matches:
        if match.matched_candidate_id is None:
            misses.append(match.reference_id)
        else:
            hits.append(match.reference_id)
            matched[match.reference_id] = match.matched_candidate_id
    obvious = [m for m in misses if by_ref[m].practitioner_obvious]
    return BreadthEval(
        run_angle_count=run_angle_count,
        reference_total=len(spec.reference_angles),
        hits=hits,
        misses=misses,
        practitioner_obvious_misses=obvious,
        matched=matched,
        novel_angles=list(match_report.novel_candidate_ids),
        option_checks=list(option_checks),
    )


def run_option_checks(
    spec: BenchmarkSpec, card_sets: Sequence[OptionCardSet]
) -> list[OptionCheckResult]:
    """The mechanical carded-at-all check for option-level ground truth: a
    case-insensitive term scan over every card's full text (the M2 Trap-2
    verdict was exactly ' "compression" appears in zero cards ')."""
    results = []
    for check in spec.option_checks:
        terms = [t.lower() for t in check.evidence_terms]
        matching = [
            card.id
            for cs in card_sets
            for card in cs.cards
            if any(term in card.dump_yaml().lower() for term in terms)
        ]
        results.append(
            OptionCheckResult(
                id=check.id,
                description=check.description,
                carded=bool(matching),
                matching_card_ids=matching,
            )
        )
    return results


# -- informedness -------------------------------------------------------------


def informedness(
    allocation: AllocationTable,
    screening: ScreeningResult,
    shortlist: Shortlist,
    floor: int,
) -> InformednessEval:
    """Spearman(allocation units, post-hoc angle value) where an angle's value
    is its share of the finalists — alongside floor compliance and the floor's
    budget share (near 100% means gamma had nothing to work with, M2 F7)."""
    angle_of = {o.option_id: o.angle_id for o in screening.options}
    finalist_angles = Counter(angle_of[f] for f in shortlist.finalist_ids if f in angle_of)
    total_finalists = sum(finalist_angles.values())
    rows = [
        InformednessRow(
            angle_id=row.angle_id,
            relevance_prior=row.relevance_prior,
            units=row.units,
            finalist_count=finalist_angles.get(row.angle_id, 0),
            value_share=(
                finalist_angles.get(row.angle_id, 0) / total_finalists if total_finalists else 0.0
            ),
        )
        for row in allocation.rows
    ]
    floor_units = floor * len(allocation.rows)
    return InformednessEval(
        spearman=spearman([r.units for r in rows], [r.value_share for r in rows]),
        rows=rows,
        floor=floor,
        floor_compliant=all(r.units >= floor for r in rows),
        floor_share_pct=round(100 * floor_units / allocation.total_budget_units, 1),
    )


# -- quality ------------------------------------------------------------------


def quality(critiques: Mapping[str, CardCritique], retry_counts: Mapping[str, int]) -> QualityEval:
    """Critic revision rate (S3's own needs_revision rule, so the metric can
    never drift from the pipeline) + schema/coherence retries from the ledger's
    retry_counts (keyed stage:role:context; every counted retry's raw output
    is preserved under logs/retries/)."""
    by_stage: dict[str, int] = {}
    by_context: dict[str, int] = {}
    for key, count in retry_counts.items():
        stage, _, context = key.split(":", 2)
        by_stage[stage] = by_stage.get(stage, 0) + count
        by_context[context] = by_context.get(context, 0) + count
    rows = [
        QualityRow(
            angle_id=angle_id,
            revised=needs_revision(critique),
            redundancy_pct=critique.redundancy_pct,
            missed_options=len(critique.missed_options),
            schema_retries=by_context.get(angle_id, 0),
        )
        for angle_id, critique in sorted(critiques.items())
    ]
    return QualityEval(
        rows=rows,
        revision_rate=round(sum(r.revised for r in rows) / len(rows), 4),
        schema_retry_total=sum(retry_counts.values()),
        schema_retries_by_stage=dict(sorted(by_stage.items())),
    )


# -- depth --------------------------------------------------------------------


def depth(
    dossiers: Mapping[str, Dossier],
    verifications: Mapping[str, VerificationReport],
    round_logs: Mapping[str, DeepDiveRoundLog],
) -> DepthEval:
    """Verifier pass rate (overall = verified/sampled across all reports, so
    small samples don't over-weight), load-bearing high-confidence share, and
    the BUDGET-CAPPED count — the §10 depth triple."""
    rows = []
    verified_total = sampled_total = 0
    lb_total = lb_high = 0
    for option_id, dossier in sorted(dossiers.items()):
        report = verifications[option_id]
        lb = [c for c in dossier.claims if c.load_bearing]
        high = [c for c in lb if c.confidence is Confidence.HIGH]
        lb_total += len(lb)
        lb_high += len(high)
        verified_total += sum(1 for r in report.results if r.verdict is Verdict.VERIFIED)
        sampled_total += len(report.results)
        log = round_logs.get(option_id)
        rows.append(
            DepthRow(
                option_id=option_id,
                pass_rate=round(report.pass_rate, 4),
                load_bearing_total=len(lb),
                load_bearing_high=len(high),
                budget_capped=dossier.budget_capped,
                rounds=len(log.rounds) if log is not None else dossier.rounds_completed,
            )
        )
    return DepthEval(
        rows=rows,
        overall_pass_rate=round(verified_total / sampled_total, 4) if sampled_total else 0.0,
        load_bearing_high_pct=round(100 * lb_high / lb_total, 1) if lb_total else None,
        budget_capped_count=sum(r.budget_capped for r in rows),
    )


# -- anti-overfit -------------------------------------------------------------


def anti_overfit(
    scores: ScreeningResult, rubric: Rubric, steelmen: Mapping[str, Steelman]
) -> AntiOverfitEval:
    """Do the two boards differ, and was every inversion-demoted option
    steelmanned with the rank-inversion trigger? Computed from the same
    sensitivity code S7 used, asserted against the tournament artifacts."""
    destination, adjusted = dual_scoreboards(scores, rubric)
    dest_rank = {row.option_id: row.rank for row in destination}
    inversions = rank_inversions(destination, adjusted)
    missing = sorted(
        {
            demoted
            for demoted, _ in inversions
            if demoted not in steelmen
            or steelmen[demoted].trigger is not SteelmanTrigger.RANK_INVERSION
        }
    )
    return AntiOverfitEval(
        preference_slot_weight=rubric.preference_slot.weight,
        boards_differ=any(dest_rank[row.option_id] != row.rank for row in adjusted),
        inversions=[InversionPair(demoted=d, promoted=p) for d, p in inversions],
        inversions_steelmanned=not missing,
        missing_steelmen=missing,
    )
