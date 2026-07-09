"""Unit tests for the pure report machinery: the mechanical citation pass
(resolution, ambiguity, the recommendation's annotation requirement), the
code-computed decision matrix, the top sensitivity flag, and the rendered
report's structure."""

from __future__ import annotations

from deeper.report import (
    AppendixContext,
    citation_pass,
    collect_claims,
    decision_matrix_table,
    link_annotations,
    render_report,
    strip_annotations,
    top_sensitivity_flag,
)
from deeper.schemas import (
    AllocationTable,
    AngleMap,
    AngleRemoval,
    Claim,
    Confidence,
    ContradictionLedger,
    CoverageReport,
    DecisionReport,
    Dossier,
    Rubric,
    ScreeningResult,
    Shortlist,
    ShortlistCause,
    ShortlistDecision,
    ShortlistOutcome,
    SourceRef,
    SourceTier,
    VerificationReport,
)
from deeper.sensitivity import CriterionFlip, SweepPoint

from .helpers import FIXTURES


def _claim(claim_id: str) -> Claim:
    return Claim(
        id=claim_id,
        text=f"the fact behind {claim_id}",
        confidence=Confidence.HIGH,
        source=SourceRef(url="https://example.org/evidence", tier=SourceTier.T1),
        load_bearing=False,
    )


def _index(**by_option: list[str]):
    index: dict[str, list[tuple[str, Claim]]] = {}
    for option_id, claim_ids in by_option.items():
        option = option_id.replace("_", "-")
        for claim_id in claim_ids:
            index.setdefault(claim_id, []).append((option, _claim(claim_id)))
    return index


# -- collect_claims ------------------------------------------------------------------


def test_collect_claims_indexes_every_dossier_claim():
    dossier = Dossier.from_yaml_file(FIXTURES / "analyst" / "dossier.sae-feature-atlas-r2.yaml")
    index = collect_claims([dossier])
    assert set(index) == {"c-sae-precedent", "c-sae-compute", "c-sae-annotation", "c-sae-reuse"}
    assert index["c-sae-compute"][0][0] == "sae-feature-atlas"


def test_collect_claims_tracks_duplicate_ids_across_dossiers():
    dossier = Dossier.from_yaml_file(FIXTURES / "analyst" / "dossier.sae-feature-atlas-r2.yaml")
    other = dossier.model_copy(update={"option_id": "other-option"})
    index = collect_claims([dossier, other])
    assert [o for o, _ in index["c-sae-compute"]] == ["sae-feature-atlas", "other-option"]


# -- citation_pass --------------------------------------------------------------------


def test_citation_pass_accepts_resolvable_annotations():
    claims = _index(opt_a=["c-one", "c-two"])
    sections = {
        "recommendation": "It wins because of [[c-one]] and [[opt-a:c-two]].",
        "dissent": "The counter-case rests on [[c-two]].",
    }
    assert citation_pass(sections, claims) == []


def test_citation_pass_reports_unknown_ids_with_section_names():
    claims = _index(opt_a=["c-one"])
    problems = citation_pass({"recommendation": "see [[c-ghost]]"}, claims)
    assert len(problems) == 1
    assert "c-ghost" in problems[0] and "recommendation" in problems[0]


def test_citation_pass_flags_ambiguous_bare_ids_and_lists_qualified_forms():
    claims = _index(opt_a=["c-shared"], opt_b=["c-shared"])
    problems = citation_pass({"recommendation": "both say [[c-shared]]"}, claims)
    assert len(problems) == 1
    assert "[[opt-a:c-shared]]" in problems[0] and "[[opt-b:c-shared]]" in problems[0]


def test_citation_pass_accepts_qualified_form_for_ambiguous_ids():
    claims = _index(opt_a=["c-shared"], opt_b=["c-shared"])
    assert citation_pass({"recommendation": "per [[opt-a:c-shared]]"}, claims) == []


def test_citation_pass_rejects_qualified_form_naming_the_wrong_option():
    claims = _index(opt_a=["c-one"])
    problems = citation_pass({"recommendation": "per [[opt-b:c-one]]"}, claims)
    assert len(problems) == 1 and "opt-b" in problems[0]


def test_citation_pass_requires_an_annotation_in_the_recommendation():
    claims = _index(opt_a=["c-one"])
    problems = citation_pass({"recommendation": "trust me", "dissent": "but see [[c-one]]"}, claims)
    assert len(problems) == 1
    assert "recommendation" in problems[0]


# -- annotation rendering ---------------------------------------------------------------


def test_link_annotations_links_bare_and_qualified_forms():
    claims = _index(opt_a=["c-one"])
    linked = link_annotations("because [[c-one]] and [[opt-a:c-one]]", claims)
    assert linked == "because [c-one](#claim-opt-a--c-one) and [c-one](#claim-opt-a--c-one)"


def test_strip_annotations_removes_markers_for_terminal_summaries():
    assert strip_annotations("It wins [[c-one]] cleanly [[opt-a:c-two]].") == "It wins cleanly."


# -- decision matrix -------------------------------------------------------------------


def test_decision_matrix_orders_columns_by_adjusted_rank_and_shows_bands():
    rubric = Rubric.from_yaml_file(FIXTURES / "rubric-builder" / "rubric.yaml")
    scores = ScreeningResult.from_yaml_file(FIXTURES / "screener" / "screening-result.yaml")
    table = decision_matrix_table(scores, rubric)
    header = table.splitlines()[0]
    # sae (4.1 combined) leads supervisor-submission-extension on the tie-break,
    # and every criterion row carries score [lo, hi] cells.
    assert header.index("sae-feature-atlas") < header.index("contamination-robust-benchmark")
    assert "| feasibility (0.2) |" in table
    assert "4 [3.5, 4.5]" in table
    assert f"| preference slot ({rubric.preference_slot.weight:g}) |" in table
    assert "| weighted point |" in table and "| weighted UCB |" in table


# -- top sensitivity flag ---------------------------------------------------------------


def test_flag_names_a_sweep_flip_first():
    sweep = [
        SweepPoint(slot_weight=0.0, ranking=("a", "b")),
        SweepPoint(slot_weight=0.05, ranking=("b", "a")),
    ]
    flips = [CriterionFlip(criterion_id="cost", weight=0.5, flip_delta=0.1)]
    flag = top_sensitivity_flag(flips, sweep, 0.2)
    assert flag.startswith("FRAGILE")
    assert "'a' -> 'b' between slot weights 0 and 0.05" in flag


def test_flag_falls_back_to_the_closest_criterion_flip():
    sweep = [SweepPoint(slot_weight=w, ranking=("a", "b")) for w in (0.0, 0.2, 0.4)]
    flips = [
        CriterionFlip(criterion_id="cost", weight=0.5, flip_delta=-0.3),
        CriterionFlip(criterion_id="speed", weight=0.2, flip_delta=0.05),
        CriterionFlip(criterion_id="fit", weight=0.3, flip_delta=None),
    ]
    flag = top_sensitivity_flag(flips, sweep, 0.2)
    assert "'speed'" in flag and "+0.05" in flag


def test_flag_reports_robustness_when_nothing_flips():
    sweep = [SweepPoint(slot_weight=w, ranking=("a", "b")) for w in (0.0, 0.4)]
    flips = [CriterionFlip(criterion_id="cost", weight=0.5, flip_delta=None)]
    assert top_sensitivity_flag(flips, sweep, 0.2).startswith("robust")


# -- render_report ----------------------------------------------------------------------


def _report() -> DecisionReport:
    return DecisionReport(
        winner_option_id="sae-feature-atlas",
        recommendation="It wins on precedent [[c-sae-precedent]].",
        decision_matrix_narration="Strong across the board.",
        sensitivity_narration="The winner flips below slot weight 0.17.",
        dissent="Compute contention slips the December artifact [[c-sae-compute]].",
        dissent_unrebutted=True,
        dissent_source="tournament/sae-feature-atlas-prosecution.md",
        residual_uncertainty="The benchmark dossier is BUDGET-CAPPED.",
        next_actions=["Confirm cluster headroom in writing."],
        appendix_notes=None,
    )


def _appendix() -> AppendixContext:
    dossier = Dossier.from_yaml_file(FIXTURES / "analyst" / "dossier.sae-feature-atlas-r2.yaml")
    return AppendixContext(
        angle_map=AngleMap.from_yaml_file(FIXTURES / "merger" / "angle-map.yaml"),
        coverage=CoverageReport.from_yaml_file(FIXTURES / "merger" / "coverage-report.yaml"),
        allocation=AllocationTable(
            total_budget_units=16,
            floor=1,
            gamma=1.0,
            per_angle_cap_pct=25.0,
            rows=[{"angle_id": "interpretability-research", "relevance_prior": 0.8, "units": 16}],
        ),
        shortlist=Shortlist(
            threshold=3.5,
            decisions=[
                ShortlistDecision(
                    option_id="sae-feature-atlas",
                    decision=ShortlistOutcome.ADVANCED,
                    cause=ShortlistCause.UCB_ABOVE_THRESHOLD,
                    reason="Top UCB.",
                )
            ],
            finalist_ids=["sae-feature-atlas"],
        ),
        removed_angles=[AngleRemoval(angle_id="research-tooling", reason="Out of scope.")],
        verification={
            "sae-feature-atlas": VerificationReport.from_yaml_file(
                FIXTURES / "verifier" / "verification-report.sae-feature-atlas.yaml"
            )
        },
        spend_by_stage={"S6": 0.42, "S7": 0.13},
        contradictions=ContradictionLedger(),
        claims=collect_claims([dossier]),
    )


def test_render_report_has_all_seven_numbered_sections():
    text = render_report(
        _report(),
        boards_text="(boards)",
        sensitivity_text="(sensitivity tables)",
        matrix_table="(matrix)",
        sensitivity_flag="(flag)",
        appendix=_appendix(),
    )
    for heading in (
        "## 1. Recommendation",
        "## 2. Decision matrix",
        "## 3. Sensitivity analysis",
        "## 4. Dissent",
        "## 5. Residual uncertainty",
        "## 6. Next actions",
        "## 7. Appendix",
    ):
        assert heading in text


def test_render_report_links_annotations_and_embeds_code_tables():
    text = render_report(
        _report(),
        boards_text="(the two scoreboards)",
        sensitivity_text="(sensitivity tables)",
        matrix_table="(the matrix)",
        sensitivity_flag="(flag line)",
        appendix=_appendix(),
    )
    assert "[c-sae-precedent](#claim-sae-feature-atlas--c-sae-precedent)" in text
    assert '<a id="claim-sae-feature-atlas--c-sae-precedent"></a>' in text
    assert "(the two scoreboards)" in text and "(the matrix)" in text
    assert "Top flag: (flag line)" in text
    assert "UNREBUTTED" in text  # the dissent flag is rendered prominently
    assert "**Winner: sae-feature-atlas**" in text
    assert "- **research-tooling** - Out of scope." in text  # Gate-A removal audit
    assert "| S6 | $0.4200 |" in text  # spend by stage
