"""Markdown renders of eval results: the per-run eval-report.md and the
before/after compare view. Pure formatting over validated EvalReport models —
no metric is ever computed here.
"""

from __future__ import annotations

from deeper.schemas import BenchmarkSpec, EvalReport


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _fmt(value: float | None, pattern: str = "{:.2f}", none: str = "n/a") -> str:
    return none if value is None else pattern.format(value)


def render_eval_report(report: EvalReport, spec: BenchmarkSpec | None = None) -> str:
    parts = [f"# Eval report — run `{report.run_id}`", ""]
    parts.append(f"- profile: **{report.profile}**")
    if report.benchmark_id:
        parts.append(f"- benchmark: **{report.benchmark_id}**")
        if spec is not None:
            parts.append(f"- question type: {spec.type}")
    parts.append(f"- generated: {report.generated_at.isoformat(timespec='seconds')}")
    parts.append("")

    b = report.breadth
    parts.append("## Breadth — distinct angles vs the reference union")
    parts.append("")
    if b is None:
        parts.append("_not computed (see Skipped below)._")
    else:
        parts.append(f"- distinct angles in the run's map: **{b.run_angle_count}**")
        if b.reference_total:
            ensemble_hits = len(b.hits) - len(b.human_assisted_hits)
            coverage = (
                f"- reference union coverage: **{len(b.hits)}/{b.reference_total}** in the map"
            )
            if b.human_assisted_hits:
                coverage += (
                    f" — **ensemble coverage {ensemble_hits}/{b.reference_total}** "
                    f"({len(b.human_assisted_hits)} human-rescued at a gate)"
                )
            parts.append(coverage)
            if b.human_assisted_hits:
                parts.append(
                    "- **HUMAN-RESCUED (the ensemble missed these; a gate edit added them): "
                    + ", ".join(f"`{m}`" for m in b.human_assisted_hits)
                    + "**"
                )
            if b.practitioner_obvious_misses:
                parts.append(
                    "- **PRACTITIONER-OBVIOUS MISSES (penalty flag; includes "
                    "human-rescued angles the ensemble did not produce): "
                    + ", ".join(f"`{m}`" for m in b.practitioner_obvious_misses)
                    + "**"
                )
            if b.misses:
                parts.append("- missed: " + ", ".join(f"`{m}`" for m in b.misses))
            if b.matched:
                parts.append("")
                parts.append(
                    _table(
                        ["reference angle", "matched run angle"],
                        [[f"`{r}`", f"`{c}`"] for r, c in sorted(b.matched.items())],
                    )
                )
            if b.novel_angles:
                parts.append("")
                parts.append(
                    "- novel run angles (candidate union additions): "
                    + ", ".join(f"`{a}`" for a in b.novel_angles)
                )
        else:
            parts.append(
                "- reference union empty — this run seeds it (build the union from "
                "the map plus a manual pass, then future runs score against it)"
            )
        for check in b.option_checks:
            # A term match is evidence, not proof (the M2 run's "compression"
            # matched a context-compression card) — never render it as a pass.
            verdict = (
                "TERM MATCH — confirm by hand: "
                + ", ".join(f"`{c}`" for c in check.matching_card_ids)
                + " (word overlap only; the card may not embody the option)"
                if check.carded
                else "**NOT CARDED** — an option-level scouting miss"
            )
            parts.append(f"- option-level ground truth `{check.id}`: {verdict}")
    parts.append("")

    i = report.informedness
    parts.append("## Informedness — allocation vs post-hoc angle value")
    parts.append("")
    if i is None:
        parts.append("_not computed (see Skipped below)._")
    else:
        parts.append(
            f"- Spearman(units, finalist share): **{_fmt(i.spearman)}**"
            + (
                " (no signal — one side has no variance; see floor share)"
                if i.spearman is None
                else ""
            )
        )
        parts.append(
            f"- floor compliance: {'**yes**' if i.floor_compliant else '**VIOLATED**'} "
            f"(floor {i.floor}); floor consumes {i.floor_share_pct:g}% of the budget"
            + (" — gamma is nearly inert at this angle count" if i.floor_share_pct >= 80 else "")
        )
        parts.append("")
        parts.append(
            _table(
                ["angle", "prior", "units", "finalists", "value share"],
                [
                    [
                        f"`{r.angle_id}`",
                        f"{r.relevance_prior:g}",
                        str(r.units),
                        str(r.finalist_count),
                        f"{r.value_share:.0%}",
                    ]
                    for r in sorted(i.rows, key=lambda r: -r.units)
                ],
            )
        )
    parts.append("")

    q = report.quality
    parts.append("## Quality — critic revision rate + schema failures")
    parts.append("")
    if q is None:
        parts.append("_not computed (see Skipped below)._")
    else:
        parts.append(
            f"- critic revision rate: **{q.revision_rate:.0%}** "
            f"({sum(r.revised for r in q.rows)}/{len(q.rows)} angles revised)"
        )
        retries_line = ", ".join(f"{s}: {n}" for s, n in q.schema_retries_by_stage.items())
        parts.append(
            f"- schema/coherence retries: **{q.schema_retry_total}**"
            + (f" ({retries_line})" if retries_line else "")
            + " — causes preserved under logs/retries/"
        )
        parts.append("")
        parts.append(
            _table(
                ["angle", "revised", "redundancy", "missed options", "retries"],
                [
                    [
                        f"`{r.angle_id}`",
                        "yes" if r.revised else "no",
                        f"{r.redundancy_pct:g}%",
                        str(r.missed_options),
                        str(r.schema_retries),
                    ]
                    for r in q.rows
                ],
            )
        )
    parts.append("")

    d = report.depth
    parts.append("## Depth — verification and score stability")
    parts.append("")
    if d is None:
        parts.append("_not computed (see Skipped below)._")
    else:
        parts.append(f"- verifier pass rate (all sampled claims): **{d.overall_pass_rate:.0%}**")
        lb_high = _fmt(d.load_bearing_high_pct, "{:g}%")
        parts.append(f"- load-bearing claims at high confidence: **{lb_high}**")
        parts.append(f"- BUDGET-CAPPED dossiers: **{d.budget_capped_count}**")
        parts.append("")
        parts.append(
            _table(
                ["finalist", "pass rate", "load-bearing high/total", "rounds", "capped"],
                [
                    [
                        f"`{r.option_id}`",
                        f"{r.pass_rate:.0%}",
                        f"{r.load_bearing_high}/{r.load_bearing_total}",
                        str(r.rounds),
                        "**yes**" if r.budget_capped else "no",
                    ]
                    for r in d.rows
                ],
            )
        )
    parts.append("")

    a = report.anti_overfit
    parts.append("## Anti-overfit — the two boards and the inversion docket")
    parts.append("")
    if a is None:
        parts.append("_not computed (see Skipped below)._")
    else:
        parts.append(f"- preference-slot weight: {a.preference_slot_weight:g}")
        parts.append(f"- scoreboards differ: {'**yes**' if a.boards_differ else 'no'}")
        if a.inversions:
            pairs = ", ".join(f"`{p.demoted}`>`{p.promoted}`" for p in a.inversions)
            parts.append(f"- rank inversions (destination order shown): {pairs}")
            parts.append(
                "- every inversion steelmanned: "
                + (
                    "**yes**"
                    if a.inversions_steelmanned
                    else "**NO — missing: " + ", ".join(f"`{m}`" for m in a.missing_steelmen) + "**"
                )
            )
        else:
            parts.append("- rank inversions: none (the inversion docket ran empty)")
    parts.append("")

    base = report.baseline
    if base is not None:
        parts.append("## Baseline A/B — plain Deep Research on the same question")
        parts.append("")
        run_hits = len(report.breadth.hits) if report.breadth else 0
        ref_total = report.breadth.reference_total if report.breadth else 0
        parts.append(f"- baseline answer: `{base.source_file}`")
        parts.append(
            f"- angle coverage: run **{run_hits}/{ref_total}** vs baseline "
            f"**{len(base.hits)}/{ref_total}**"
            + (
                " — the system beats the baseline"
                if run_hits > len(base.hits)
                else " — **the system does NOT beat the baseline; the miss lists "
                "below name the stage to fix (S1 cartography for angle misses)**"
            )
        )
        if base.misses:
            parts.append("- baseline missed: " + ", ".join(f"`{m}`" for m in base.misses))
        if base.practitioner_obvious_misses:
            parts.append(
                "- baseline practitioner-obvious misses: "
                + ", ".join(f"`{m}`" for m in base.practitioner_obvious_misses)
            )
        if base.novel_angles:
            parts.append(
                "- baseline-only angles (outside the union): "
                + ", ".join(f"`{a}`" for a in base.novel_angles)
            )
        parts.append("")

    parts.append("## Spend")
    parts.append("")
    parts.append(
        _table(
            ["stage", "usd"],
            [[s, f"${usd:.4f}"] for s, usd in sorted(report.spend_by_stage.items())]
            + [["**total**", f"**${report.total_usd:.4f}**"]],
        )
    )
    parts.append("")
    parts.append(f"- this eval's judge spend: ${report.eval_usd:.4f}")
    parts.append("")

    if report.skipped:
        parts.append("## Skipped")
        parts.append("")
        parts.extend(f"- {reason}" for reason in report.skipped)
        parts.append("")

    return "\n".join(parts)


def _headline_rows(a: EvalReport, b: EvalReport) -> list[list[str]]:
    def breadth_cov(r: EvalReport) -> str:
        if r.breadth is None or not r.breadth.reference_total:
            return "n/a"
        cov = f"{len(r.breadth.hits)}/{r.breadth.reference_total}"
        if r.breadth.human_assisted_hits:
            cov += f" ({len(r.breadth.human_assisted_hits)} human-rescued)"
        return cov

    def obvious(r: EvalReport) -> str:
        return "n/a" if r.breadth is None else str(len(r.breadth.practitioner_obvious_misses))

    def sp(r: EvalReport) -> str:
        return "n/a" if r.informedness is None else _fmt(r.informedness.spearman)

    def rev(r: EvalReport) -> str:
        return "n/a" if r.quality is None else f"{r.quality.revision_rate:.0%}"

    def retries(r: EvalReport) -> str:
        return "n/a" if r.quality is None else str(r.quality.schema_retry_total)

    def pass_rate(r: EvalReport) -> str:
        return "n/a" if r.depth is None else f"{r.depth.overall_pass_rate:.0%}"

    def lb_high(r: EvalReport) -> str:
        return "n/a" if r.depth is None else _fmt(r.depth.load_bearing_high_pct, "{:g}%")

    def capped(r: EvalReport) -> str:
        return "n/a" if r.depth is None else str(r.depth.budget_capped_count)

    def overfit(r: EvalReport) -> str:
        if r.anti_overfit is None:
            return "n/a"
        boards = "differ" if r.anti_overfit.boards_differ else "identical"
        steel = "all steelmanned" if r.anti_overfit.inversions_steelmanned else "STEELMAN MISSING"
        return f"{boards}; {len(r.anti_overfit.inversions)} inversion(s), {steel}"

    metrics = [
        ("breadth: union coverage", breadth_cov),
        ("breadth: practitioner-obvious misses", obvious),
        ("informedness: Spearman", sp),
        ("quality: revision rate", rev),
        ("quality: schema retries", retries),
        ("depth: verifier pass rate", pass_rate),
        ("depth: load-bearing high-conf", lb_high),
        ("depth: BUDGET-CAPPED", capped),
        ("anti-overfit", overfit),
        ("spend (USD)", lambda r: f"${r.total_usd:.2f}"),
    ]
    return [[label, fn(a), fn(b)] for label, fn in metrics]


def render_compare(a: EvalReport, b: EvalReport) -> str:
    """The before/after view for one knob or prompt change: run A's eval next
    to run B's, metric by metric. Reads top-to-bottom as 'did the change move
    the property it was aimed at, and did anything else regress'."""
    parts = [
        f"# Eval compare — `{a.run_id}` vs `{b.run_id}`",
        "",
        f"- A: `{a.run_id}` (profile {a.profile}"
        + (f", benchmark {a.benchmark_id}" if a.benchmark_id else "")
        + f", evaluated {a.generated_at.isoformat(timespec='seconds')})",
        f"- B: `{b.run_id}` (profile {b.profile}"
        + (f", benchmark {b.benchmark_id}" if b.benchmark_id else "")
        + f", evaluated {b.generated_at.isoformat(timespec='seconds')})",
        "",
        _table(["metric", f"A: {a.run_id}", f"B: {b.run_id}"], _headline_rows(a, b)),
        "",
    ]
    if a.benchmark_id != b.benchmark_id:
        parts.append(
            "**Note: the runs were evaluated against different benchmarks — "
            "breadth numbers are not comparable.**"
        )
        parts.append("")
    if a.breadth is not None and b.breadth is not None and a.benchmark_id == b.benchmark_id:
        gained = sorted(set(b.breadth.hits) - set(a.breadth.hits))
        lost = sorted(set(a.breadth.hits) - set(b.breadth.hits))
        if gained:
            parts.append("- reference angles gained in B: " + ", ".join(f"`{g}`" for g in gained))
        if lost:
            parts.append("- reference angles lost in B: " + ", ".join(f"`{m}`" for m in lost))
        if gained or lost:
            parts.append("")
    return "\n".join(parts)
