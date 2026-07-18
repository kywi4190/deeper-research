"""The eval harness (design §10): metric math on synthetic, hand-computable
artifacts; judge contract assembly + coherence; the four seeded benchmark
specs; the runner over a completed mock run; and the compare-report structure.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from deeper.agents_runtime.contracts import assemble_prompt
from deeper.eval import (
    BENCHMARKS_DIR,
    EVAL_MD_PATH,
    EVAL_YAML_PATH,
    BaselineNotPasted,
    EvalError,
    evaluate_run,
    load_benchmark,
    metrics,
    render_compare,
)
from deeper.eval import benchmarks as bench_mod
from deeper.eval.judge import judge_contract, match_coherence
from deeper.eval.report import render_eval_report
from deeper.schemas import (
    AllocationRow,
    AllocationTable,
    AngleMatch,
    AngleMatchReport,
    BenchmarkSpec,
    CardCritique,
    CompletenessIssue,
    Confidence,
    Criterion,
    CriterionScore,
    Dossier,
    DossierSection,
    EvalReport,
    EvidenceItem,
    OptionCard,
    OptionCardSet,
    OptionScreening,
    PreferenceSlot,
    ReferenceProvenance,
    Rubric,
    ScreeningResult,
    Shortlist,
    ShortlistCause,
    ShortlistDecision,
    ShortlistOutcome,
    SourceRef,
    SourceTier,
    Stage,
    Steelman,
    SteelmanTrigger,
    UncertaintyBand,
    Verdict,
    VerificationReport,
    VerificationResult,
)
from deeper.schemas.dossier import Claim

from .helpers import walk_engine_to_gate_c

# -- builders ------------------------------------------------------------------


def screening_option(
    option_id: str, angle_id: str, dest: float = 4.0, pref: float | None = None
) -> OptionScreening:
    def cs(cid: str, score: float) -> CriterionScore:
        return CriterionScore(
            criterion_id=cid,
            score=score,
            band=UncertaintyBand(lo=max(1.0, score - 0.5), hi=min(5.0, score + 0.5)),
            evidence_pointer="synthetic",
        )

    return OptionScreening(
        option_id=option_id,
        angle_id=angle_id,
        criterion_scores=[cs(f"c{i}", dest) for i in range(1, 6)],
        preference_score=cs("preference-slot", pref) if pref is not None else None,
        weighted_point=dest,
        weighted_ucb=min(5.0, dest + 0.5),
    )


def rubric(slot_weight: float = 0.2) -> Rubric:
    levels = {i: f"level {i}" for i in range(1, 6)}
    return Rubric(
        criteria=[
            Criterion(
                id=f"c{i}",
                name=f"criterion {i}",
                definition="synthetic",
                measurement_method="synthetic",
                levels=levels,
                weight=0.2,
                justification="synthetic",
            )
            for i in range(1, 6)
        ],
        preference_slot=PreferenceSlot(weight=slot_weight),
    )


def shortlist_over(finalists: list[str], cut: list[str]) -> Shortlist:
    decisions = [
        ShortlistDecision(
            option_id=o,
            decision=ShortlistOutcome.ADVANCED,
            cause=ShortlistCause.UCB_ABOVE_THRESHOLD,
            reason="synthetic",
        )
        for o in finalists
    ] + [
        ShortlistDecision(
            option_id=o,
            decision=ShortlistOutcome.CUT,
            cause=ShortlistCause.BELOW_CUTOFF,
            reason="synthetic",
        )
        for o in cut
    ]
    return Shortlist(threshold=3.5, decisions=decisions, finalist_ids=finalists)


def source() -> SourceRef:
    return SourceRef(url="https://example.org", tier=SourceTier.T1)


def dossier_with(option_id: str, claims: list[Claim], budget_capped: bool = False) -> Dossier:
    section = DossierSection(content="synthetic", claim_ids=[claims[0].id])
    return Dossier(
        option_id=option_id,
        criterion_sections={"c1": section},
        failure_modes=section,
        cost_of_adoption=section,
        second_order_effects=section,
        strongest_criticism=section,
        comparable_cases=section,
        claims=claims,
        rounds_completed=2,
        budget_capped=budget_capped,
        open_questions=["what remains"] if budget_capped else [],
    )


def claim(cid: str, load_bearing: bool, confidence: Confidence) -> Claim:
    return Claim(
        id=cid, text="synthetic", confidence=confidence, source=source(), load_bearing=load_bearing
    )


def verification(option_id: str, verdicts: list[Verdict]) -> VerificationReport:
    return VerificationReport(
        option_id=option_id,
        results=[VerificationResult(claim_id=f"vc{i}", verdict=v) for i, v in enumerate(verdicts)],
        sampled_load_bearing_count=len(verdicts),
        sampled_other_count=0,
    )


# -- spearman -----------------------------------------------------------------


def test_spearman_hand_computed_values() -> None:
    assert metrics.spearman([1, 2, 3], [10, 20, 30]) == 1.0
    assert metrics.spearman([1, 2, 3], [30, 20, 10]) == -1.0
    # Ties on one side: rx = [3, 1.5, 1.5], ry = [3, 2, 1] -> 1.5/sqrt(1.5*2)
    assert metrics.spearman([4, 2, 2], [2 / 3, 1 / 3, 0.0]) == pytest.approx(0.866, abs=1e-3)


def test_spearman_degenerate_inputs_are_none_not_zero() -> None:
    assert metrics.spearman([1], [1]) is None
    assert metrics.spearman([2, 2, 2], [1, 2, 3]) is None  # floor-dominated allocation
    with pytest.raises(ValueError):
        metrics.spearman([1, 2], [1])


# -- informedness -------------------------------------------------------------


def test_informedness_hand_computed() -> None:
    allocation = AllocationTable(
        total_budget_units=8,
        floor=2,
        gamma=1.0,
        per_angle_cap_pct=50.0,
        rows=[
            AllocationRow(angle_id="a", relevance_prior=0.9, units=4),
            AllocationRow(angle_id="b", relevance_prior=0.5, units=2),
            AllocationRow(angle_id="c", relevance_prior=0.5, units=2),
        ],
    )
    screening = ScreeningResult(
        options=[
            screening_option("o1", "a"),
            screening_option("o2", "a"),
            screening_option("o3", "b"),
            screening_option("o4", "c"),
        ]
    )
    result = metrics.informedness(
        allocation, screening, shortlist_over(["o1", "o2", "o3"], ["o4"]), floor=2
    )
    by_angle = {r.angle_id: r for r in result.rows}
    assert by_angle["a"].value_share == pytest.approx(2 / 3)
    assert by_angle["b"].value_share == pytest.approx(1 / 3)
    assert by_angle["c"].value_share == 0.0
    assert result.spearman == pytest.approx(0.866, abs=1e-3)
    assert result.floor_compliant is True
    assert result.floor_share_pct == 75.0  # 3 angles * floor 2 / 8 units


def test_informedness_flags_floor_violation() -> None:
    allocation = AllocationTable(
        total_budget_units=4,
        floor=0,
        gamma=1.0,
        per_angle_cap_pct=100.0,
        rows=[
            AllocationRow(angle_id="a", relevance_prior=0.9, units=4),
            AllocationRow(angle_id="b", relevance_prior=0.5, units=0),
        ],
    )
    screening = ScreeningResult(options=[screening_option("o1", "a")])
    result = metrics.informedness(allocation, screening, shortlist_over(["o1"], []), floor=1)
    assert result.floor_compliant is False


# -- quality ------------------------------------------------------------------


def test_quality_revision_rate_and_retries() -> None:
    critiques = {
        "a1": CardCritique(
            angle_id="a1",
            completeness_issues=[CompletenessIssue(card_id="o1", issue="missing evidence")],
            redundancy_pct=10.0,
            missed_options=["an obvious option"],
        ),
        "a2": CardCritique(angle_id="a2", redundancy_pct=55.0),
    }
    retry_counts = {"S3:scout:a1": 2, "S5:screener:a1": 1, "S3:scout:-": 1}
    result = metrics.quality(critiques, retry_counts)
    assert result.revision_rate == 0.5  # a1 revised (fixable issues), a2 not
    by_angle = {r.angle_id: r for r in result.rows}
    assert by_angle["a1"].revised is True and by_angle["a1"].schema_retries == 3
    assert by_angle["a2"].revised is False and by_angle["a2"].schema_retries == 0
    assert result.schema_retry_total == 4
    assert result.schema_retries_by_stage == {"S3": 3, "S5": 1}


def test_quality_missed_options_do_not_count_as_revision() -> None:
    critiques = {
        "a1": CardCritique(angle_id="a1", redundancy_pct=0.0, missed_options=["x"]),
    }
    assert metrics.quality(critiques, {}).revision_rate == 0.0


# -- depth --------------------------------------------------------------------


def test_depth_hand_computed() -> None:
    dossiers = {
        "opt-a": dossier_with(
            "opt-a",
            [
                claim("c1", True, Confidence.HIGH),
                claim("c2", True, Confidence.LOW),
                claim("c3", False, Confidence.MED),
            ],
        ),
        "opt-b": dossier_with("opt-b", [claim("c4", True, Confidence.HIGH)], budget_capped=True),
    }
    verifications = {
        "opt-a": verification("opt-a", [Verdict.VERIFIED, Verdict.VERIFIED, Verdict.UNSUPPORTED]),
        "opt-b": verification("opt-b", [Verdict.VERIFIED]),
    }
    result = metrics.depth(dossiers, verifications, {})
    assert result.overall_pass_rate == 0.75  # 3 verified of 4 sampled
    assert result.load_bearing_high_pct == pytest.approx(66.7)  # 2 of 3 lb claims high
    assert result.budget_capped_count == 1
    by_option = {r.option_id: r for r in result.rows}
    assert by_option["opt-a"].pass_rate == pytest.approx(2 / 3, abs=1e-3)
    assert by_option["opt-a"].rounds == 2  # falls back to dossier.rounds_completed


# -- anti-overfit -------------------------------------------------------------


def _inverted_scores() -> ScreeningResult:
    # Destination: alpha 4.0 > beta 3.9. At slot weight 0.25:
    # alpha 0.75*4.0+0.25*2 = 3.5 < beta 0.75*3.9+0.25*5 = 4.175 — an inversion.
    return ScreeningResult(
        options=[
            screening_option("alpha", "a", dest=4.0, pref=2.0),
            screening_option("beta", "b", dest=3.9, pref=5.0),
        ]
    )


def test_anti_overfit_detects_inversion_and_steelman() -> None:
    steelman = Steelman(option_id="alpha", trigger=SteelmanTrigger.RANK_INVERSION, case="synthetic")
    result = metrics.anti_overfit(_inverted_scores(), rubric(0.25), {"alpha": steelman})
    assert result.boards_differ is True
    assert [(p.demoted, p.promoted) for p in result.inversions] == [("alpha", "beta")]
    assert result.inversions_steelmanned is True and result.missing_steelmen == []


def test_anti_overfit_flags_missing_or_mistriggered_steelman() -> None:
    result = metrics.anti_overfit(_inverted_scores(), rubric(0.25), {})
    assert result.inversions_steelmanned is False
    assert result.missing_steelmen == ["alpha"]
    wrong = Steelman(option_id="alpha", trigger=SteelmanTrigger.RUNNER_UP, case="synthetic")
    assert (
        metrics.anti_overfit(
            _inverted_scores(), rubric(0.25), {"alpha": wrong}
        ).inversions_steelmanned
        is False
    )


def test_anti_overfit_zero_slot_weight_boards_identical() -> None:
    result = metrics.anti_overfit(_inverted_scores(), rubric(0.0), {})
    assert result.boards_differ is False
    assert result.inversions == [] and result.inversions_steelmanned is True


# -- breadth + option checks --------------------------------------------------


def _spec(tmp_path: Path | None = None, **overrides) -> BenchmarkSpec:
    data = {
        "id": "test-bench",
        "question": "a question",
        "type": "technical-selection",
        "profile": "quick",
        "reference_angles": [
            {"id": "region-one"},
            {"id": "region-two"},
            {
                "id": "region-three",
                "provenance": "human-added",
                "practitioner_obvious": True,
            },
        ],
        **overrides,
    }
    return BenchmarkSpec.model_validate(data)


def test_breadth_combines_judge_report_with_the_union() -> None:
    report = AngleMatchReport(
        matches=[
            AngleMatch(reference_id="region-one", matched_candidate_id="run-a", rationale="same"),
            AngleMatch(reference_id="region-two", matched_candidate_id=None, rationale="absent"),
            AngleMatch(reference_id="region-three", matched_candidate_id=None, rationale="absent"),
        ],
        novel_candidate_ids=["run-z"],
    )
    result = metrics.breadth(_spec(), report, run_angle_count=5)
    assert result.hits == ["region-one"] and result.matched == {"region-one": "run-a"}
    assert result.misses == ["region-two", "region-three"]
    assert result.practitioner_obvious_misses == ["region-three"]
    assert result.novel_angles == ["run-z"] and result.run_angle_count == 5


def test_option_checks_scan_card_text_case_insensitively() -> None:
    card = OptionCard(
        id="fork-test",
        name="Fork test",
        angle_id="a",
        description="A nanoGPT build with Compression framing at a designed fork.",
        mechanism="Reveals the build/understand axis by choice.",
        preliminary_evidence=[EvidenceItem(text="exists", source=source())],
        uncertainties=["scope"],
    )
    spec = _spec(
        option_checks=[
            {
                "id": "compression-check",
                "description": "compression-framed fork test",
                "evidence_terms": ["compression"],
            },
            {
                "id": "absent-check",
                "description": "never carded",
                "evidence_terms": ["quantum-golf"],
            },
        ]
    )
    results = metrics.run_option_checks(spec, [OptionCardSet(angle_id="a", cards=[card])])
    by_id = {r.id: r for r in results}
    assert by_id["compression-check"].carded is True
    assert by_id["compression-check"].matching_card_ids == ["fork-test"]
    assert by_id["absent-check"].carded is False


# -- judge contract + coherence -----------------------------------------------


def test_judge_contract_assembles_against_the_real_prompt_library() -> None:
    contract = judge_contract(
        _spec(), candidate_material="- id: run-a\n", mode="map", context="test-bench"
    )
    assert contract.role == "eval-judge" and contract.stage is Stage.EVAL
    prompt = assemble_prompt(contract)  # raises on frontmatter/schema drift
    assert "angle-match-report" in prompt
    assert "region-three" in prompt and "run-a" in prompt
    baseline = judge_contract(
        _spec(), candidate_material="prose answer", mode="baseline", context="t-b"
    )
    assert "PROSE-BASELINE MODE" in assemble_prompt(baseline)


def _match_report(**overrides) -> dict:
    artifacts = {
        "angle-match-report": AngleMatchReport(
            matches=[
                AngleMatch(
                    reference_id="region-one", matched_candidate_id="run-a", rationale="same"
                ),
                AngleMatch(reference_id="region-two", matched_candidate_id=None, rationale="no"),
                AngleMatch(reference_id="region-three", matched_candidate_id=None, rationale="no"),
            ],
            novel_candidate_ids=["run-b"],
            **overrides,
        )
    }
    return artifacts


def test_match_coherence_accepts_a_complete_report() -> None:
    check = match_coherence(_spec(), {"run-a", "run-b"})
    assert check(_match_report()) is None


def test_match_coherence_names_every_problem() -> None:
    check = match_coherence(_spec(), {"run-a", "run-b", "run-c"})
    artifacts = {
        "angle-match-report": AngleMatchReport(
            matches=[
                AngleMatch(reference_id="region-one", matched_candidate_id="ghost", rationale="?"),
                AngleMatch(
                    reference_id="not-a-reference", matched_candidate_id=None, rationale="?"
                ),
            ],
        )
    }
    problem = check(artifacts)
    assert problem is not None
    assert "region-two" in problem and "region-three" in problem  # missing verdicts
    assert "not-a-reference" in problem  # extra verdict
    assert "ghost" in problem  # unknown candidate
    assert "unaccounted" in problem  # run-a/b/c neither matched nor novel


def test_match_coherence_baseline_mode_checks_reference_coverage_only() -> None:
    check = match_coherence(_spec(), None)
    assert check(_match_report()) is None  # invented candidate ids are fine


# -- the seeded benchmark specs ------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "probe-space-mapping",
        "vector-store-selection",
        "faculty-targeting-decision",
        "cold-email-advice",
    ],
)
def test_seeded_benchmark_specs_validate(name: str) -> None:
    spec, path = load_benchmark(name)
    assert spec.id == name and path.parent == BENCHMARKS_DIR
    assert spec.baseline_file is not None
    assert (path.parent / spec.baseline_file).is_file()


def test_probe_space_mapping_spec_carries_the_m2_ground_truth() -> None:
    spec, _ = load_benchmark("probe-space-mapping")
    assert len(spec.reference_angles) == 16
    human_added = [
        a for a in spec.reference_angles if a.provenance is ReferenceProvenance.HUMAN_ADDED
    ]
    assert [a.id for a in human_added] == ["llm-agent-systems-build"]
    assert human_added[0].practitioner_obvious is True
    assert spec.ground_truth is not None and "adjudication" in spec.ground_truth
    assert [c.evidence_terms for c in spec.option_checks] == [["compression"]]


def test_unknown_benchmark_lists_the_available_ones() -> None:
    with pytest.raises(EvalError, match="probe-space-mapping"):
        load_benchmark("no-such-benchmark")


def test_baseline_placeholder_is_refused(tmp_path: Path) -> None:
    spec, path = load_benchmark("probe-space-mapping")
    with pytest.raises(BaselineNotPasted, match="placeholder"):
        bench_mod.read_baseline(spec, path)
    pasted = tmp_path / "answer.md"
    pasted.write_text("# header\nA real pasted answer.\n", encoding="utf-8")
    spec2 = spec.model_copy(update={"baseline_file": str(pasted)})
    assert "real pasted answer" in bench_mod.read_baseline(spec2, tmp_path / "spec.yaml")


# -- the runner over a completed mock run -------------------------------------

MOCK_BENCH = {
    "id": "mock-bench",
    "question": "pick a senior research project",
    "type": "personal-decision-known-ground-truth",
    "profile": "quick",
    "reference_angles": [
        {"id": "interpretability-research"},
        {"id": "evaluation-science"},
        {
            "id": "llm-agent-systems-build",
            "provenance": "human-added",
            "practitioner_obvious": True,
        },
    ],
    "option_checks": [
        {
            "id": "sae-carded",
            "description": "a sparse-autoencoder option",
            "evidence_terms": ["sparse autoencoder"],
        },
        {
            "id": "never-carded",
            "description": "an option no scout produced",
            "evidence_terms": ["quantum-golf-caddying"],
        },
    ],
    "baseline_file": "baselines/mock-bench.md",
}


async def _completed_run(tmp_path: Path):
    ws, engine, _ = await walk_engine_to_gate_c(tmp_path)
    ws.path("gates/gate-c.yaml").write_text("approved: true\n", encoding="utf-8")
    from deeper.orchestrator import Node

    assert await engine.run() is Node.DONE
    return ws


def _write_mock_bench(tmp_path: Path) -> Path:
    bench_dir = tmp_path / "benchmarks"
    (bench_dir / "baselines").mkdir(parents=True)
    spec_path = bench_dir / "mock-bench.yaml"
    BenchmarkSpec.model_validate(MOCK_BENCH).to_yaml_file(spec_path)
    (bench_dir / "baselines" / "mock-bench.md").write_text(
        "<!-- placeholder -->\n", encoding="utf-8"
    )
    return spec_path


async def test_evaluate_run_over_a_completed_mock_run(tmp_path: Path) -> None:
    ws = await _completed_run(tmp_path)
    spec_path = _write_mock_bench(tmp_path)
    spec, path = load_benchmark(spec_path)
    emitted: list[str] = []
    report = await evaluate_run(ws, spec, path, emit=emitted.append)

    # breadth: judged from the eval-judge fixture, combined with the union
    assert report.breadth is not None
    assert sorted(report.breadth.hits) == ["evaluation-science", "interpretability-research"]
    assert report.breadth.practitioner_obvious_misses == ["llm-agent-systems-build"]
    assert report.breadth.run_angle_count == len(report.breadth.novel_angles) + 2
    checks = {c.id: c for c in report.breadth.option_checks}
    assert checks["sae-carded"].carded is True and checks["never-carded"].carded is False

    # informedness: the floor held; the correlation is computable
    assert report.informedness is not None and report.informedness.floor_compliant is True
    assert 0 < report.informedness.floor_share_pct < 100

    # quality: every scouted angle has a row; rates in range
    assert report.quality is not None and len(report.quality.rows) >= 3
    assert 0 <= report.quality.revision_rate <= 1

    # depth: the scenario exercises all three S6 termination paths, including
    # exactly one BUDGET-CAPPED dossier — the count must surface it
    assert report.depth is not None and len(report.depth.rows) >= 3
    assert report.depth.budget_capped_count == 1
    assert 0 < report.depth.overall_pass_rate <= 1

    # anti-overfit: the engineered inversion, steelmanned
    assert report.anti_overfit is not None and report.anti_overfit.boards_differ is True
    demoted = [p.demoted for p in report.anti_overfit.inversions]
    assert "contamination-robust-benchmark" in demoted
    assert report.anti_overfit.inversions_steelmanned is True

    # spend + persistence
    assert report.eval_usd == 0.0  # mock costs nothing, truthfully
    assert report.total_usd == ws.load_state().total_usd()
    persisted = ws.read_artifact(EVAL_YAML_PATH, EvalReport)
    assert persisted.run_id == report.run_id
    md = ws.path(EVAL_MD_PATH).read_text(encoding="utf-8")
    assert "PRACTITIONER-OBVIOUS MISSES" in md and "llm-agent-systems-build" in md
    assert any(s.startswith("deeper eval: report written") for s in ws.history())


async def test_evaluate_run_without_benchmark_skips_breadth_only(tmp_path: Path) -> None:
    ws = await _completed_run(tmp_path)
    report = await evaluate_run(ws, emit=lambda _line: None)
    assert report.breadth is None and report.benchmark_id is None
    assert any("breadth" in s for s in report.skipped)
    assert report.informedness is not None and report.depth is not None


async def test_evaluate_run_on_a_partial_run_names_whats_missing(tmp_path: Path) -> None:
    from deeper.orchestrator import Engine, Node

    from .helpers import make_workspace

    ws = make_workspace(tmp_path)
    engine = Engine(ws, emit=lambda _line: None)
    assert await engine.run() is Node.GATE_A  # S0+S1 done, nothing further
    report = await evaluate_run(ws, emit=lambda _line: None)
    assert report.informedness is None and report.depth is None and report.anti_overfit is None
    assert len(report.skipped) >= 4


async def test_empty_reference_union_seeds_without_a_judge(tmp_path: Path) -> None:
    ws = await _completed_run(tmp_path)
    spec = BenchmarkSpec.model_validate(
        {**MOCK_BENCH, "reference_angles": [], "option_checks": MOCK_BENCH["option_checks"]}
    )
    report = await evaluate_run(ws, spec, None, emit=lambda _line: None)
    assert report.breadth is not None and report.breadth.reference_total == 0
    assert report.breadth.run_angle_count >= 8  # the map still counts
    assert {c.id for c in report.breadth.option_checks} == {"sae-carded", "never-carded"}
    assert any("seeds it" in s for s in report.skipped)


# -- compare ------------------------------------------------------------------


def _eval_report(run_id: str, hits: list[str], usd: float) -> EvalReport:
    breadth = metrics.breadth(
        _spec(),
        AngleMatchReport(
            matches=[
                AngleMatch(
                    reference_id=r.id,
                    matched_candidate_id="run-x" if r.id in hits else None,
                    rationale="synthetic",
                )
                for r in _spec().reference_angles
            ],
        ),
        run_angle_count=4,
    )
    return EvalReport(
        run_id=run_id,
        profile="quick",
        benchmark_id="test-bench",
        generated_at=datetime.now(UTC),
        breadth=breadth,
        total_usd=usd,
        eval_usd=0.0,
    )


def test_render_compare_structure() -> None:
    a = _eval_report("run-a", hits=["region-one"], usd=10.0)
    b = _eval_report("run-b", hits=["region-one", "region-two"], usd=12.0)
    markdown = render_compare(a, b)
    assert "`run-a` vs `run-b`" in markdown
    assert "| metric |" in markdown
    assert "1/3" in markdown and "2/3" in markdown  # union coverage side by side
    assert "reference angles gained in B: `region-two`" in markdown
    assert "$10.00" in markdown and "$12.00" in markdown


def test_render_compare_warns_on_different_benchmarks() -> None:
    a = _eval_report("run-a", hits=[], usd=1.0)
    b = _eval_report("run-b", hits=[], usd=1.0).model_copy(update={"benchmark_id": None})
    assert "different benchmarks" in render_compare(a, b)


def test_render_eval_report_handles_all_none_sections() -> None:
    report = EvalReport(
        run_id="bare",
        profile="quick",
        generated_at=datetime.now(UTC),
        total_usd=0.0,
        eval_usd=0.0,
        skipped=["everything: the run just started"],
    )
    markdown = render_eval_report(report)
    assert "not computed" in markdown and "everything: the run just started" in markdown


# -- CLI ----------------------------------------------------------------------


def test_cli_eval_and_compare(tmp_path: Path) -> None:
    # Sync on purpose: the CLI command owns its own asyncio.run.
    from typer.testing import CliRunner

    from deeper.orchestrator.cli import app

    ws = asyncio.run(_completed_run(tmp_path))
    spec_path = _write_mock_bench(tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["eval", str(ws.root), "--against", str(spec_path)])
    assert result.exit_code == 0, result.output
    assert "eval report written" in result.output
    assert "breadth 2/3" in result.output and "OBVIOUS MISSES 1" in result.output
    assert ws.path(EVAL_MD_PATH).is_file()

    result = runner.invoke(app, ["eval", "--compare", str(ws.root), str(ws.root)])
    assert result.exit_code == 0, result.output
    assert "Eval compare" in result.output
    compare_file = ws.path(f"eval/compare-vs-{ws.run_id}.md")
    assert compare_file.is_file()


def test_cli_eval_compare_without_persisted_report_fails_helpfully(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    from deeper.orchestrator.cli import app

    ws = asyncio.run(_completed_run(tmp_path))
    result = CliRunner().invoke(app, ["eval", "--compare", str(ws.root), str(ws.root)])
    assert result.exit_code == 1
    assert "no persisted eval report" in result.output


def test_cli_eval_argument_validation(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from deeper.orchestrator.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["eval"])
    assert result.exit_code == 1 and "give a run" in result.output
    result = runner.invoke(app, ["eval", "some-run", "--compare-baseline"])
    assert result.exit_code == 1 and "--against" in result.output


# -- rerun integration ---------------------------------------------------------


async def test_rerun_invalidates_stale_eval_reports(tmp_path: Path) -> None:
    from deeper.orchestrator.rerun import RerunError, invalidate

    ws = await _completed_run(tmp_path)
    await evaluate_run(ws, emit=lambda _line: None)
    assert ws.path(EVAL_YAML_PATH).is_file()
    removed = invalidate(ws, Stage.S7)
    assert "eval" in removed
    assert not ws.path(EVAL_YAML_PATH).exists()
    with pytest.raises(RerunError, match="not a pipeline stage"):
        invalidate(ws, Stage.EVAL)
