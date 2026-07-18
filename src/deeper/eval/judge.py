"""The eval-judge dispatch (design §10's LLM-judge, breadth metric only).

The judge goes through the SAME dispatch layer as every pipeline agent — one
AgentContract, the schema-retry loop, a coherence callback riding it, and a
SpendEntry per attempt under stage EVAL — so it is mockable offline and its
cost lands in the run's own ledger.
"""

from __future__ import annotations

import yaml

from deeper.agents_runtime import AgentContract, Dispatcher
from deeper.config import SizeClass
from deeper.schemas import AngleMap, AngleMatchReport, ArtifactModel, BenchmarkSpec, Stage

JUDGE_ROLE = "eval-judge"


def _reference_yaml(spec: BenchmarkSpec) -> str:
    return yaml.safe_dump(
        [{"id": a.id, "note": a.note or ""} for a in spec.reference_angles],
        sort_keys=False,
        allow_unicode=True,
    )


def _candidates_yaml(angle_map: AngleMap) -> str:
    return yaml.safe_dump(
        [{"id": a.id, "name": a.name, "definition": a.definition} for a in angle_map.angles],
        sort_keys=False,
        allow_unicode=True,
    )


def judge_contract(
    spec: BenchmarkSpec, *, candidate_material: str, mode: str, context: str
) -> AgentContract:
    """One eval-judge invocation. `mode` is 'map' (structured run angles) or
    'baseline' (a prose answer to extract angles from first)."""
    if mode == "map":
        task = (
            "Match the benchmark's reference angles against the run's candidate "
            "angle map. Adjudicate every reference angle exactly once; "
            "matched_candidate_id must be a candidate id from the inputs."
        )
        input_name = "candidate-angles"
    else:
        task = (
            "PROSE-BASELINE MODE. The candidate material is a plain research "
            "answer, not a structured map: first extract the distinct solution "
            "angles it actually discusses (short kebab-case ids, listed in "
            "notes), then adjudicate every reference angle exactly once against "
            "your extracted angles."
        )
        input_name = "baseline-answer"
    return AgentContract(
        role=JUDGE_ROLE,
        stage=Stage.EVAL,
        task_objective=f"{task}\nBenchmark question: {spec.question}",
        input_artifacts={
            "reference-angles": _reference_yaml(spec),
            input_name: candidate_material,
        },
        output_schemas=("angle-match-report",),
        size_class=SizeClass.S,
        budget_line="No searches. One matching pass over the given inputs only.",
        context=context,
    )


def match_coherence(spec: BenchmarkSpec, candidate_ids: set[str] | None):
    """The stage-owned coherence callback ridden by the dispatcher's retry
    loop: the reference set must be adjudicated exactly, and (map mode, where
    `candidate_ids` is known) every named candidate must exist and every
    unmatched candidate must be listed as novel."""

    def check(artifacts: dict[str, ArtifactModel]) -> str | None:
        report = artifacts["angle-match-report"]
        assert isinstance(report, AngleMatchReport)
        problems: list[str] = []
        want = {a.id for a in spec.reference_angles}
        got = {m.reference_id for m in report.matches}
        if missing := sorted(want - got):
            problems.append(f"- reference angles missing a verdict: {missing}")
        if extra := sorted(got - want):
            problems.append(f"- verdicts on ids that are not reference angles: {extra}")
        if candidate_ids is not None:
            matched = {m.matched_candidate_id for m in report.matches if m.matched_candidate_id}
            if unknown := sorted(matched - candidate_ids):
                problems.append(
                    f"- matched_candidate_id values that are not candidate ids: {unknown}"
                )
            if unknown := sorted(set(report.novel_candidate_ids) - candidate_ids):
                problems.append(f"- novel_candidate_ids that are not candidate ids: {unknown}")
            unaccounted = sorted(candidate_ids - matched - set(report.novel_candidate_ids))
            if unaccounted:
                problems.append(
                    "- every candidate must be either matched or listed in "
                    f"novel_candidate_ids; unaccounted for: {unaccounted}"
                )
        if problems:
            return (
                "Your angle-match-report is schema-valid but incoherent with the "
                "task inputs. Fix exactly these problems and resubmit the complete "
                "artifact:\n" + "\n".join(problems)
            )
        return None

    return check


async def match_run_angles(
    dispatcher: Dispatcher, spec: BenchmarkSpec, angle_map: AngleMap
) -> AngleMatchReport:
    contract = judge_contract(
        spec,
        candidate_material=_candidates_yaml(angle_map),
        mode="map",
        context=spec.id,
    )
    candidate_ids = {a.id for a in angle_map.angles}
    result = await dispatcher.run_agent(contract, validate=match_coherence(spec, candidate_ids))
    report = result.artifacts["angle-match-report"]
    assert isinstance(report, AngleMatchReport)
    return report


async def match_baseline(
    dispatcher: Dispatcher, spec: BenchmarkSpec, baseline_text: str
) -> AngleMatchReport:
    contract = judge_contract(
        spec,
        candidate_material=baseline_text,
        mode="baseline",
        context=f"{spec.id}-baseline",
    )
    # Prose mode: the judge invents candidate ids, so only reference coverage
    # is checkable.
    result = await dispatcher.run_agent(contract, validate=match_coherence(spec, None))
    report = result.artifacts["angle-match-report"]
    assert isinstance(report, AngleMatchReport)
    return report
