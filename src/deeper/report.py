"""Report machinery: the mechanical citation pass + markdown rendering (§5/S8, §6).

Everything here is deterministic code. The synthesist narrates; this module
verifies that every inline ``[[claim-id]]`` annotation resolves to a dossier
claim (the design's "final mechanical citation pass links every factual claim
in the report body back to a dossier claim and its source"), renders the
seven-section report/decision-report.md with the code-computed scoreboards,
sensitivity tables, decision matrix, and appendix tables embedded verbatim,
and turns each annotation into a link to the claims index (which carries the
claim text and its source).

Annotation syntax: ``[[claim-id]]``, or ``[[option-id:claim-id]]`` when the
bare claim id exists in more than one dossier (claim ids are unique only
within a dossier).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from deeper.schemas import (
    AllocationTable,
    AngleMap,
    AngleRemoval,
    Claim,
    ContradictionLedger,
    ContradictionStatus,
    CoverageReport,
    DecisionReport,
    Dossier,
    Rubric,
    ScreeningResult,
    Shortlist,
    ShortlistOutcome,
    VerificationReport,
)
from deeper.sensitivity import CriterionFlip, SweepPoint, dual_scoreboards

# One inline citation: [[claim-id]] or [[option-id:claim-id]] (slug syntax).
ANNOTATION_RE = re.compile(r"\[\[([a-z0-9][a-z0-9-]*(?::[a-z0-9][a-z0-9-]*)?)\]\]")

# claim id -> every (option_id, claim) carrying it. A bare annotation is
# resolvable only when exactly one dossier holds the id.
ClaimIndex = dict[str, list[tuple[str, Claim]]]


def collect_claims(dossiers: Sequence[Dossier]) -> ClaimIndex:
    """Index every finalist dossier's claims by claim id."""
    index: ClaimIndex = {}
    for dossier in dossiers:
        for claim in dossier.claims:
            index.setdefault(claim.id, []).append((dossier.option_id, claim))
    return index


def report_sections(report: DecisionReport) -> dict[str, str]:
    """The report's prose fields, as the citation pass scans them."""
    return {
        "recommendation": report.recommendation,
        "decision_matrix_narration": report.decision_matrix_narration,
        "sensitivity_narration": report.sensitivity_narration,
        "dissent": report.dissent,
        "residual_uncertainty": report.residual_uncertainty,
        "next_actions": "\n".join(report.next_actions),
        "appendix_notes": report.appendix_notes or "",
    }


def citation_pass(sections: Mapping[str, str], claims: ClaimIndex) -> list[str]:
    """Validate every annotation; empty list = pass. Each problem line is
    LLM-facing (fed back verbatim on the one synthesist retry). The
    recommendation must carry at least one annotation — the mechanical proxy
    for 'every factual sentence resolves to a dossier claim'."""
    problems: list[str] = []
    for name, text in sections.items():
        for match in ANNOTATION_RE.finditer(text):
            body = match.group(1)
            if ":" in body:
                option_id, claim_id = body.split(":", 1)
                holders = claims.get(claim_id, [])
                if not any(o == option_id for o, _ in holders):
                    problems.append(
                        f"- [[{body}]] in '{name}': option '{option_id}' has no "
                        f"dossier claim '{claim_id}'"
                    )
            else:
                found = claims.get(body)
                if not found:
                    problems.append(f"- [[{body}]] in '{name}': no dossier claim has id '{body}'")
                elif len(found) > 1:
                    qualified = ", ".join(f"[[{o}:{body}]]" for o, _ in found)
                    problems.append(
                        f"- [[{body}]] in '{name}': the id exists in more than one "
                        f"dossier; qualify it as one of: {qualified}"
                    )
    if not ANNOTATION_RE.search(sections.get("recommendation", "")):
        problems.append(
            "- the recommendation carries no [[claim-id]] annotation; every factual "
            "sentence must cite the dossier claim it rests on"
        )
    return problems


def strip_annotations(text: str) -> str:
    """Prose without the citation markers (terminal summaries)."""
    return re.sub(r"\s*\[\[[a-z0-9:-]+\]\]", "", text)


def _anchor(option_id: str, claim_id: str) -> str:
    return f"claim-{option_id}--{claim_id}"


def link_annotations(text: str, claims: ClaimIndex) -> str:
    """Replace every annotation with a link into the appendix claims index.
    Assumes the citation pass already ran clean."""

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if ":" in body:
            option_id, claim_id = body.split(":", 1)
        else:
            claim_id = body
            option_id = claims[claim_id][0][0]
        return f"[{claim_id}](#{_anchor(option_id, claim_id)})"

    return ANNOTATION_RE.sub(replace, text)


# -- code-computed tables (P8: the agent narrates, never produces, these) ---------


def decision_matrix_table(scores: ScreeningResult, rubric: Rubric) -> str:
    """§5/S8-2: all finalists x all criteria with confidence bands, columns in
    preference-adjusted rank order, plus the preference slot and the stored
    weighted aggregates."""
    _, adjusted = dual_scoreboards(scores, rubric)
    by_id = {o.option_id: o for o in scores.options}
    ordered = [by_id[row.option_id] for row in adjusted]
    header = "| criterion (weight) | " + " | ".join(o.option_id for o in ordered) + " |"
    lines = [header, "|---" * (len(ordered) + 1) + "|"]
    for criterion in rubric.criteria:
        cells = []
        for option in ordered:
            cs = next(s for s in option.criterion_scores if s.criterion_id == criterion.id)
            cells.append(f"{cs.score:g} [{cs.band.lo:g}, {cs.band.hi:g}]")
        lines.append(f"| {criterion.id} ({criterion.weight:g}) | " + " | ".join(cells) + " |")
    slot_cells = [
        f"{o.preference_score.score:g} "
        f"[{o.preference_score.band.lo:g}, {o.preference_score.band.hi:g}]"
        if o.preference_score is not None
        else "-"
        for o in ordered
    ]
    lines.append(
        f"| preference slot ({rubric.preference_slot.weight:g}) | " + " | ".join(slot_cells) + " |"
    )
    lines.append(
        "| weighted point | " + " | ".join(f"{o.weighted_point:g}" for o in ordered) + " |"
    )
    lines.append("| weighted UCB | " + " | ".join(f"{o.weighted_ucb:g}" for o in ordered) + " |")
    return "\n".join(lines)


def top_sensitivity_flag(
    flips: Sequence[CriterionFlip], sweep: Sequence[SweepPoint], slot_weight: float
) -> str:
    """One line naming the sharpest fragility: a sweep flip beats a criterion
    flip beats robustness. Shared by the report header and `deeper report`."""
    changes = [
        f"'{prev.winner}' -> '{cur.winner}' between slot weights "
        f"{prev.slot_weight:g} and {cur.slot_weight:g}"
        for prev, cur in zip(sweep, sweep[1:], strict=False)
        if prev.winner != cur.winner
    ]
    if changes:
        return "FRAGILE: the winner depends on the preference-slot weight — " + "; ".join(changes)
    in_range = [f for f in flips if f.flip_delta is not None]
    if in_range:
        closest = min(in_range, key=lambda f: abs(f.flip_delta or 0.0))
        return (
            f"stable across the preference sweep; the closest criterion flip is "
            f"'{closest.criterion_id}' (weight {closest.weight:g}): a "
            f"{closest.flip_delta:+g} change ties ranks 1-2"
        )
    return (
        f"robust: no in-range criterion reweighting and no preference-slot setting "
        f"(0 -> 0.4; configured {slot_weight:g}) changes the winner"
    )


# -- rendering --------------------------------------------------------------------


@dataclass(frozen=True)
class AppendixContext:
    """Everything §5/S8-7 embeds, read from the run's earlier-stage artifacts."""

    angle_map: AngleMap
    coverage: CoverageReport
    allocation: AllocationTable
    shortlist: Shortlist
    removed_angles: Sequence[AngleRemoval]
    verification: Mapping[str, VerificationReport]  # option_id -> report
    spend_by_stage: Mapping[str, float]
    contradictions: ContradictionLedger
    claims: ClaimIndex


def _angle_map_table(angle_map: AngleMap) -> str:
    lines = ["| angle | relevance prior | name |", "|---|---|---|"]
    lines += [f"| {a.id} | {a.relevance_prior:g} | {a.name} |" for a in angle_map.angles]
    return "\n".join(lines)


def _allocation_table(allocation: AllocationTable) -> str:
    lines = [
        f"Allocation ({allocation.kind.value}): {allocation.total_budget_units} units, "
        f"floor {allocation.floor}, gamma {allocation.gamma:g}, per-angle cap "
        f"{allocation.per_angle_cap_pct:g}%.",
        "",
        "| angle | prior | units |",
        "|---|---|---|",
    ]
    lines += [f"| {r.angle_id} | {r.relevance_prior:g} | {r.units} |" for r in allocation.rows]
    return "\n".join(lines)


def _cut_audit(shortlist: Shortlist, removed_angles: Sequence[AngleRemoval]) -> str:
    lines = []
    if removed_angles:
        lines.append("Angles removed by the human at Gate A:")
        lines += [f"- **{r.angle_id}** - {r.reason}" for r in removed_angles]
        lines.append("")
    lines.append(f"Screening decisions (UCB floor {shortlist.threshold:g}):")
    for d in shortlist.decisions:
        verb = "advanced" if d.decision is ShortlistOutcome.ADVANCED else "cut"
        lines.append(f"- **{d.option_id}** - {verb} ({d.cause.value}): {d.reason}")
    return "\n".join(lines)


def pass_rate_table(verification: Mapping[str, VerificationReport]) -> str:
    lines = [
        "| option | verifier pass rate | sampled (load-bearing + other) |",
        "|---|---|---|",
    ]
    for option_id in sorted(verification):
        report = verification[option_id]
        lines.append(
            f"| {option_id} | {report.pass_rate:.0%} | {len(report.results)} "
            f"({report.sampled_load_bearing_count} + {report.sampled_other_count}) |"
        )
    return "\n".join(lines)


def spend_table(spend_by_stage: Mapping[str, float]) -> str:
    lines = ["| stage | usd |", "|---|---|"]
    lines += [f"| {stage} | ${usd:.4f} |" for stage, usd in sorted(spend_by_stage.items())]
    lines.append(f"| **total** | **${sum(spend_by_stage.values()):.4f}** |")
    return "\n".join(lines)


def _claims_index(claims: ClaimIndex) -> str:
    lines = []
    for claim_id in sorted(claims):
        for option_id, claim in claims[claim_id]:
            lines.append(
                f'- <a id="{_anchor(option_id, claim_id)}"></a>`{claim_id}` '
                f"({option_id}, {claim.confidence.value}"
                f"{', load-bearing' if claim.load_bearing else ''}) - {claim.text} "
                f"[{claim.source.tier.value}: {claim.source.url}]"
            )
    return "\n".join(lines)


def unresolved_contradictions_block(ledger: ContradictionLedger) -> str:
    """§6: 'unresolved contradictions surface in the report' — the code-rendered
    part of the residual-uncertainty register. Empty string when every entry is
    adjudicated (the full ledger still renders in the appendix)."""
    open_entries = [e for e in ledger.entries if e.status is ContradictionStatus.OPEN]
    if not open_entries:
        return ""
    lines = [
        f"- `{e.id}` (detected by {e.detected_by}): `{e.statement_a.artifact}` says "
        f'"{e.statement_a.statement}" — but `{e.statement_b.artifact}` says '
        f'"{e.statement_b.statement}"'
        for e in open_entries
    ]
    return (
        f"**Unresolved contradictions ({len(open_entries)} open in "
        "`ledger/contradictions.md` — no statement below was adjudicated; treat "
        "both sides as unsettled):**\n\n" + "\n".join(lines)
    )


def render_report(
    report: DecisionReport,
    *,
    boards_text: str,
    sensitivity_text: str,
    matrix_table: str,
    sensitivity_flag: str,
    appendix: AppendixContext,
) -> str:
    """report/decision-report.md: the seven numbered sections (§5/S8), agent
    narration linked to the claims index, code-computed tables embedded verbatim."""
    link = lambda text: link_annotations(text, appendix.claims)  # noqa: E731
    dissent_banner = (
        "**This dissent is UNREBUTTED — nothing in the tournament answered it.**\n\n"
        if report.dissent_unrebutted
        else ""
    )
    contradictions = ""
    if appendix.contradictions.entries:
        entries = "\n".join(
            f"- `{e.id}` ({e.status.value}): {e.statement_a.artifact} vs {e.statement_b.artifact}"
            for e in appendix.contradictions.entries
        )
        contradictions = f"### Contradiction ledger\n\n{entries}\n\n"
    next_actions = "\n".join(f"{i}. {link(a)}" for i, a in enumerate(report.next_actions, 1))
    appendix_notes = f"{link(report.appendix_notes)}\n\n" if report.appendix_notes else ""
    unresolved = unresolved_contradictions_block(appendix.contradictions)
    residual = link(report.residual_uncertainty) + (f"\n\n{unresolved}" if unresolved else "")
    return f"""# Decision report

## 1. Recommendation

**Winner: {report.winner_option_id}**

{link(report.recommendation)}

## 2. Decision matrix

{matrix_table}

{link(report.decision_matrix_narration)}

## 3. Sensitivity analysis

Top flag: {sensitivity_flag}

{boards_text}

{sensitivity_text}

{link(report.sensitivity_narration)}

## 4. Dissent

{dissent_banner}{link(report.dissent)}

(Best surviving prosecution argument, from `{report.dissent_source}`.)

## 5. Residual uncertainty

{residual}

## 6. Next actions

{next_actions}

## 7. Appendix

{appendix_notes}### Angle map

{_angle_map_table(appendix.angle_map)}

### Allocation

{_allocation_table(appendix.allocation)}

### Cut-option audit trail

{_cut_audit(appendix.shortlist, appendix.removed_angles)}

### Verification pass rates

{pass_rate_table(appendix.verification)}

### Spend by stage

{spend_table(appendix.spend_by_stage)}

{contradictions}### Claims index

{_claims_index(appendix.claims)}
"""
