"""One eval over one run workspace (design §10): load whatever artifacts the
run has produced, compute every metric they support, dispatch the judge where
a benchmark's reference union exists, and persist eval-report.{yaml,md}.

A partial run evaluates partially: each metric section is None when its input
artifacts are absent, with the reason recorded in `skipped` — evaluating an
M1-shaped run (S0–S5 only) is a supported case, not an error.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import create_dispatcher
from deeper.schemas import (
    AllocationTable,
    AngleMap,
    ArtifactModel,
    BaselineEval,
    BenchmarkSpec,
    BreadthEval,
    CardCritique,
    DeepDiveRoundLog,
    Dossier,
    EvalReport,
    Heuristic,
    OptionCardSet,
    Rubric,
    ScreeningResult,
    Shortlist,
    Stage,
    Steelman,
    VerificationReport,
)
from deeper.stages.s5_screening import SCORES_PATH as SCREENING_SCORES_PATH
from deeper.stages.s5_screening import SHORTLIST_PATH
from deeper.stages.s6_deepdive import dossier_path, rounds_path, verification_path
from deeper.stages.s7_tournament import UPDATED_SCORES_PATH
from deeper.workspace import Workspace, WorkspaceError

from . import metrics
from .benchmarks import read_baseline
from .judge import match_baseline, match_run_angles
from .report import render_compare, render_eval_report

EVAL_YAML_PATH = "eval/eval-report.yaml"
EVAL_MD_PATH = "eval/eval-report.md"

A = TypeVar("A", bound=ArtifactModel)

__all__ = [
    "EVAL_MD_PATH",
    "EVAL_YAML_PATH",
    "compare_path",
    "evaluate_run",
    "render_compare",
]


def compare_path(other_run_id: str) -> str:
    return f"eval/compare-vs-{other_run_id}.md"


def _try_read(workspace: Workspace, relpath: str, model: type[A]) -> A | None:
    try:
        return workspace.read_artifact(relpath, model)
    except (WorkspaceError, ValidationError, yaml.YAMLError):
        return None


def _glob_read(workspace: Workspace, pattern: str, model: type[A]) -> dict[str, A]:
    """Read every file matching a workspace glob, keyed by workspace-relative
    path; files that fail validation are skipped (the caller's tolerance rule)."""
    out: dict[str, A] = {}
    for path in sorted(workspace.root.glob(pattern)):
        relpath = path.relative_to(workspace.root).as_posix()
        artifact = _try_read(workspace, relpath, model)
        if artifact is not None:
            out[relpath] = artifact
    return out


async def evaluate_run(
    workspace: Workspace,
    spec: BenchmarkSpec | None = None,
    spec_path: Path | None = None,
    *,
    include_baseline: bool = False,
    emit: Callable[[str], None] = print,
) -> EvalReport:
    """Compute the eval, write eval/eval-report.{yaml,md}, commit, and return
    the report. Judge dispatches (breadth, baseline) go through the run's own
    dispatch layer under stage EVAL — mock runs judge from fixtures, live runs
    meter the run's ledger and respect its spend cap."""
    config = workspace.load_config()
    state = workspace.load_state()
    skipped: list[str] = []
    spend_entries_before = len(state.spend)

    angle_map = _try_read(workspace, "angles/map.yaml", AngleMap)
    allocation = _try_read(workspace, "allocation.yaml", AllocationTable)
    screening = _try_read(workspace, SCREENING_SCORES_PATH, ScreeningResult)
    shortlist = _try_read(workspace, SHORTLIST_PATH, Shortlist)
    rubric = _try_read(workspace, "rubric.yaml", Rubric)
    tournament_scores = _try_read(workspace, UPDATED_SCORES_PATH, ScreeningResult)

    card_sets = list(_glob_read(workspace, "options/*/cards.yaml", OptionCardSet).values())
    critiques = {
        c.angle_id: c for c in _glob_read(workspace, "options/*/critique.md", CardCritique).values()
    }

    # -- breadth (the one judged metric) --------------------------------------
    breadth = None
    baseline = None
    if spec is None:
        skipped.append("breadth: no benchmark given (--against) — there is no reference union")
    elif angle_map is None:
        skipped.append("breadth: angles/map.yaml missing or invalid — the run has no map yet")
    else:
        option_checks = metrics.run_option_checks(spec, card_sets)
        if spec.reference_angles:
            dispatcher = create_dispatcher(workspace, config)
            emit(
                f"eval: judging {len(angle_map.angles)} run angles against "
                f"{len(spec.reference_angles)} reference angles ({spec.id})"
            )
            match_report = await match_run_angles(dispatcher, spec, angle_map)
            # Gate-A-added angles carry provenance [human] — the breadth metric
            # must see them, or a human rescue reads as ensemble coverage.
            human_ids = {
                a.id
                for a in angle_map.angles
                if set(a.contributing_heuristics) == {Heuristic.HUMAN}
            }
            breadth = metrics.breadth(
                spec, match_report, len(angle_map.angles), option_checks, human_ids
            )
            if include_baseline:
                assert spec_path is not None  # the CLI resolves the spec from a path
                text = read_baseline(spec, spec_path)
                emit(f"eval: judging the pasted baseline answer ({spec.baseline_file})")
                base_report = await match_baseline(dispatcher, spec, text)
                by_ref = {a.id: a for a in spec.reference_angles}
                base_hits = [m.reference_id for m in base_report.matches if m.matched_candidate_id]
                base_misses = [
                    m.reference_id for m in base_report.matches if not m.matched_candidate_id
                ]
                baseline = BaselineEval(
                    source_file=spec.baseline_file or "",
                    hits=base_hits,
                    misses=base_misses,
                    practitioner_obvious_misses=[
                        m for m in base_misses if by_ref[m].practitioner_obvious
                    ],
                    novel_angles=list(base_report.novel_candidate_ids),
                )
        else:
            breadth = BreadthEval(
                run_angle_count=len(angle_map.angles),
                reference_total=0,
                option_checks=option_checks,
            )
            skipped.append(
                f"breadth: benchmark {spec.id!r} has an empty reference union — "
                "this run's map seeds it; no judge dispatched"
            )
            if include_baseline:
                skipped.append(
                    "baseline: comparison needs a non-empty reference union to score against"
                )

    # -- informedness ---------------------------------------------------------
    informedness = None
    if allocation is None or screening is None or shortlist is None:
        skipped.append(
            "informedness: needs allocation.yaml + screening scores + shortlist "
            "(the run has not completed S5)"
        )
    else:
        informedness = metrics.informedness(allocation, screening, shortlist, config.floor)

    # -- quality --------------------------------------------------------------
    quality = None
    if not critiques:
        skipped.append("quality: no options/*/critique.md yet (the run has not completed S3)")
    else:
        quality = metrics.quality(critiques, state.retry_counts)

    # -- depth ----------------------------------------------------------------
    depth = None
    dossiers: dict[str, Dossier] = {}
    verifications: dict[str, VerificationReport] = {}
    round_logs: dict[str, DeepDiveRoundLog] = {}
    for verification_file in sorted(workspace.root.glob("dossiers/*-verification.md")):
        option_id = verification_file.name.removesuffix("-verification.md")
        dossier = _try_read(workspace, dossier_path(option_id), Dossier)
        verification = _try_read(workspace, verification_path(option_id), VerificationReport)
        if dossier is None or verification is None:
            continue
        dossiers[option_id] = dossier
        verifications[option_id] = verification
        log = _try_read(workspace, rounds_path(option_id), DeepDiveRoundLog)
        if log is not None:
            round_logs[option_id] = log
    if not dossiers:
        skipped.append(
            "depth: no settled dossier+verification pairs (the run has not completed S6)"
        )
    else:
        depth = metrics.depth(dossiers, verifications, round_logs)

    # -- anti-overfit ---------------------------------------------------------
    anti_overfit = None
    if tournament_scores is None or rubric is None:
        skipped.append(
            "anti-overfit: needs tournament/scores.yaml + rubric.yaml "
            "(the run has not completed S7)"
        )
    else:
        steelmen = {
            s.option_id: s
            for s in _glob_read(workspace, "tournament/*-steelman.md", Steelman).values()
        }
        anti_overfit = metrics.anti_overfit(tournament_scores, rubric, steelmen)

    # -- assemble, persist, commit -------------------------------------------
    state = workspace.load_state()  # judge dispatches appended spend entries
    eval_usd = sum(e.usd for e in state.spend[spend_entries_before:] if e.stage is Stage.EVAL)
    report = EvalReport(
        run_id=state.run_id,
        profile=state.profile,
        benchmark_id=spec.id if spec is not None else None,
        generated_at=datetime.now(UTC),
        breadth=breadth,
        informedness=informedness,
        quality=quality,
        depth=depth,
        anti_overfit=anti_overfit,
        baseline=baseline,
        spend_by_stage=state.spend_by_stage(),
        total_usd=state.total_usd(),
        eval_usd=eval_usd,
        skipped=skipped,
    )
    workspace.write_artifact(EVAL_YAML_PATH, report)
    md_target = workspace.path(EVAL_MD_PATH)
    md_target.parent.mkdir(parents=True, exist_ok=True)
    md_target.write_text(render_eval_report(report, spec), encoding="utf-8", newline="\n")
    workspace.commit(
        "deeper eval: report written" + (f" (benchmark {spec.id})" if spec is not None else "")
    )
    return report
