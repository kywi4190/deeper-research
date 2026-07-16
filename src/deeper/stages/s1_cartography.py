"""S1 — Angle cartography (design §5/S1).

The profile's initial cartographer ensemble is dispatched in parallel (one
gather_strict fan-out through the dispatcher's semaphore), then the merger folds the
raw reports into angles/map.yaml + map-report.md. The orchestrator — never an
LLM — then applies the saturation rule (see `saturation.py`): while the mean
marginal novelty of the trailing window stays at or above the threshold, it
spawns up to 2 more cartographers re-running the most-novel heuristics (raw
output at angles/raw/{heuristic}-{n}.yaml), re-merges, and re-measures, under
the hard cap of 8 invocations.

A Gate-A "another pass" rerun leaves its hint at gates/gate-a-hint.txt (the
rest of angles/ is invalidated); every cartographer contract carries the hint
verbatim, and the stage consumes the file on completion so the hint applies to
exactly one pass.

Idempotent re-entry: raw reports that validate are not re-dispatched, the
merger is skipped while its dedup map still covers every raw report on disk,
and `is_complete` replays the (deterministic) expansion plan against the files
so a crash mid-expansion resumes mid-plan.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import AgentContract
from deeper.aio import gather_strict
from deeper.config import SizeClass
from deeper.schemas import (
    AngleMap,
    Brief,
    CartographerReport,
    CoverageReport,
    DestinationModel,
    Heuristic,
)
from deeper.schemas import Stage as StageEnum
from deeper.workspace import WorkspaceError

from .base import StageBase, StageContext
from .saturation import (
    CartographerRun,
    expansion_heuristics,
    is_saturated,
    marginal_novelty,
    uncovered_raw_angles,
)

# Ensemble order (design §5/S1): profiles take the first N. The horizon scanner
# is last because it is the design's "optional 6th, for forward-looking questions".
ENSEMBLE_ORDER: tuple[Heuristic, ...] = (
    Heuristic.FIRST_PRINCIPLES,
    Heuristic.ANALOGIST,
    Heuristic.CONTRARIAN,
    Heuristic.PRACTITIONER,
    Heuristic.TAXONOMIST,
    Heuristic.HORIZON_SCANNER,
)

# agents/ role file per heuristic (the horizon role file drops the "-scanner").
ROLE_FOR_HEURISTIC: dict[Heuristic, str] = {
    Heuristic.FIRST_PRINCIPLES: "cartographer-first-principles",
    Heuristic.ANALOGIST: "cartographer-analogist",
    Heuristic.CONTRARIAN: "cartographer-contrarian",
    Heuristic.PRACTITIONER: "cartographer-practitioner",
    Heuristic.TAXONOMIST: "cartographer-taxonomist",
    Heuristic.HORIZON_SCANNER: "cartographer-horizon",
}

MAP_PATH = "angles/map.yaml"
REPORT_PATH = "angles/map-report.md"
# Written by Gate A's "another pass" action; survives angles/ invalidation
# because it lives under gates/. Consumed (deleted) when S1 completes.
HINT_PATH = "gates/gate-a-hint.txt"


@dataclass(frozen=True)
class PlannedRun:
    """One planned cartographer invocation. `occurrence` counts passes of the
    same heuristic within this stage execution (1 = initial ensemble)."""

    heuristic: Heuristic
    occurrence: int

    @property
    def context(self) -> str:
        if self.occurrence == 1:
            return self.heuristic.value
        return f"{self.heuristic.value}-{self.occurrence}"

    @property
    def relpath(self) -> str:
        return f"angles/raw/{self.context}.yaml"


def raw_report_path(heuristic: Heuristic) -> str:
    return f"angles/raw/{heuristic.value}.yaml"


class CartographyStage(StageBase):
    stage = StageEnum.S1
    required_inputs = (
        ("brief.md", Brief),
        ("destination.md", DestinationModel),
    )

    # -- plan helpers ---------------------------------------------------------

    def initial_plan(self, ctx: StageContext) -> list[PlannedRun]:
        ensemble = ENSEMBLE_ORDER[: ctx.config.initial_cartographers]
        return [PlannedRun(h, 1) for h in ensemble]

    @staticmethod
    def _next_occurrence(plan: list[PlannedRun], heuristic: Heuristic) -> int:
        return 1 + sum(1 for p in plan if p.heuristic is heuristic)

    @staticmethod
    def _try_read(ctx: StageContext, relpath: str, model: type):
        try:
            return ctx.workspace.read_artifact(relpath, model)
        except (WorkspaceError, ValidationError, yaml.YAMLError):
            return None

    def _read_hint(self, ctx: StageContext) -> str | None:
        target = ctx.workspace.path(HINT_PATH)
        if target.is_file():
            hint = target.read_text(encoding="utf-8").strip()
            return hint or None
        return None

    # -- engine protocol ------------------------------------------------------

    def outputs(self, ctx: StageContext):
        out: list[tuple[str, type]] = [
            (p.relpath, CartographerReport) for p in self.initial_plan(ctx)
        ]
        declared = {relpath for relpath, _ in out}
        raw_dir = ctx.workspace.path("angles/raw")
        if raw_dir.is_dir():  # expansion passes are data-dependent: declare what exists
            for path in sorted(raw_dir.glob("*.yaml")):
                relpath = f"angles/raw/{path.name}"
                if relpath not in declared:
                    out.append((relpath, CartographerReport))
        out += [(MAP_PATH, AngleMap), (REPORT_PATH, CoverageReport)]
        return out

    def is_complete(self, ctx: StageContext) -> bool:
        """Replay the deterministic expansion plan against the files on disk:
        complete only when every planned raw report exists, the map covers all
        of them, and the saturation rule says stop. A pending Gate-A hint means
        another pass is owed, so never complete."""
        if ctx.workspace.path(HINT_PATH).is_file():
            return False
        angle_map = self._try_read(ctx, MAP_PATH, AngleMap)
        if angle_map is None or self._try_read(ctx, REPORT_PATH, CoverageReport) is None:
            return False
        plan = self.initial_plan(ctx)
        runs: list[CartographerRun] = []
        for planned in plan:
            report = self._try_read(ctx, planned.relpath, CartographerReport)
            if report is None:
                return False
            runs.append(_as_run(planned, report))
        caps = ctx.config.caps
        while True:
            if uncovered_raw_angles(runs, angle_map.dedup_map):
                return False  # map is stale relative to the raw reports
            novelties = marginal_novelty(runs, angle_map.dedup_map)
            if len(plan) >= caps.max_cartographers:
                return True
            if is_saturated(
                novelties,
                threshold=caps.cartography_novelty_threshold,
                window=caps.cartography_novelty_window,
            ):
                return True
            extras = expansion_heuristics(novelties, max_cartographers=caps.max_cartographers)
            if not extras:
                return True
            for heuristic in extras:
                planned = PlannedRun(heuristic, self._next_occurrence(plan, heuristic))
                report = self._try_read(ctx, planned.relpath, CartographerReport)
                if report is None:
                    return False  # a planned expansion pass has not run yet
                plan.append(planned)
                runs.append(_as_run(planned, report))

    # -- execution ------------------------------------------------------------

    async def execute(self, ctx: StageContext) -> None:
        brief_text = ctx.workspace.path("brief.md").read_text(encoding="utf-8")
        destination_text = ctx.workspace.path("destination.md").read_text(encoding="utf-8")
        base_inputs = {"brief": brief_text, "destination": destination_text}
        hint = self._read_hint(ctx)
        if hint is not None:
            ctx.emit(f"S1: Gate A rerun hint active — {hint}")
        caps = ctx.config.caps

        plan = self.initial_plan(ctx)
        ctx.emit(f"S1: dispatching {len(plan)} cartographers in parallel…")
        await gather_strict(
            *(self._run_cartographer(ctx, p, base_inputs, hint, None) for p in plan)
        )
        reported = 0
        while True:
            runs = self._load_runs(ctx, plan)
            angle_map = await self._ensure_merged(ctx, plan, runs)
            novelties = marginal_novelty(runs, angle_map.dedup_map)
            for n in novelties[reported:]:
                ctx.emit(
                    f"S1 saturation: {n.run.context} novelty "
                    f"{n.novelty:.2f} ({len(n.novel_ids)}/{n.total_angles} new)"
                )
            reported = len(novelties)
            if len(plan) >= caps.max_cartographers:
                ctx.emit(f"S1 saturation: hard cap of {caps.max_cartographers} reached — stopping")
                break
            if is_saturated(
                novelties,
                threshold=caps.cartography_novelty_threshold,
                window=caps.cartography_novelty_window,
            ):
                window = novelties[-caps.cartography_novelty_window :]
                mean = sum(n.novelty for n in window) / len(window)
                ctx.emit(
                    f"S1 saturation: saturated (trailing-{len(window)} mean novelty "
                    f"{mean:.2f} < {caps.cartography_novelty_threshold})"
                )
                break
            extras = expansion_heuristics(novelties, max_cartographers=caps.max_cartographers)
            if not extras:
                ctx.emit("S1 saturation: no heuristic left with novel angles — stopping")
                break
            new_runs = [PlannedRun(h, self._next_occurrence(plan, h)) for h in extras]
            plan.extend(new_runs)
            ctx.emit(
                "S1 saturation: novelty still high — spawning "
                + ", ".join(p.context for p in new_runs)
            )
            mapped_names = [a.name for a in angle_map.angles]
            await gather_strict(
                *(self._run_cartographer(ctx, p, base_inputs, hint, mapped_names) for p in new_runs)
            )
        if hint is not None:
            ctx.workspace.path(HINT_PATH).unlink(missing_ok=True)
            ctx.emit("S1: rerun hint consumed — it applied to this pass only")
        ctx.emit("S1: angles/map.yaml + map-report.md written")

    async def _run_cartographer(
        self,
        ctx: StageContext,
        planned: PlannedRun,
        base_inputs: dict[str, str],
        hint: str | None,
        mapped_names: list[str] | None,
    ) -> None:
        if self._try_read(ctx, planned.relpath, CartographerReport) is not None:
            ctx.emit(f"S1: {planned.context} raw report already valid — skipping")
            return
        task_parts: list[str] = []
        if hint is not None:
            task_parts.append(
                "# GATE A RERUN HINT\nThe human reviewed a previous angle map and "
                f"requested another cartography pass with this hint:\n{hint}"
            )
        if planned.occurrence > 1 and mapped_names:
            listed = "\n".join(f"- {name}" for name in mapped_names)
            task_parts.append(
                "# EXPANSION PASS\nYour framing heuristic produced the most novel "
                "angles so far, so you are being run again. The merged map already "
                f"contains these angles:\n{listed}\nHunt for distinct regions beyond "
                "them; do not re-emit angles that would fold into the ones listed."
            )
        result = await ctx.dispatcher.run_agent(
            AgentContract(
                role=ROLE_FOR_HEURISTIC[planned.heuristic],
                stage=StageEnum.S1,
                task_objective="\n\n".join(task_parts),
                input_artifacts=base_inputs,
                output_schemas=("cartographer-report",),
                size_class=SizeClass.M,
                budget_line="Return 5-12 candidate angles; existence proofs only.",
                context=planned.context,
            )
        )
        ctx.workspace.write_artifact(planned.relpath, result.artifacts["cartographer-report"])
        ctx.emit(f"S1: {planned.context} cartographer done")

    def _load_runs(self, ctx: StageContext, plan: list[PlannedRun]) -> list[CartographerRun]:
        runs = []
        for planned in plan:
            report = ctx.workspace.read_artifact(planned.relpath, CartographerReport)
            runs.append(_as_run(planned, report))
        return runs

    async def _ensure_merged(
        self, ctx: StageContext, plan: list[PlannedRun], runs: list[CartographerRun]
    ) -> AngleMap:
        """Dispatch the merger unless the existing map already covers every raw
        report on disk (idempotent re-entry; also how each expansion round
        forces a re-merge)."""
        angle_map = self._try_read(ctx, MAP_PATH, AngleMap)
        if (
            angle_map is not None
            and self._try_read(ctx, REPORT_PATH, CoverageReport) is not None
            and not uncovered_raw_angles(runs, angle_map.dedup_map)
        ):
            ctx.emit("S1: merged map already covers all raw reports — skipping merger")
            return angle_map
        merger_inputs = {
            "brief": ctx.workspace.path("brief.md").read_text(encoding="utf-8"),
            "destination": ctx.workspace.path("destination.md").read_text(encoding="utf-8"),
        }
        for planned in plan:
            merger_inputs[f"cartographer-report ({planned.context})"] = ctx.workspace.path(
                planned.relpath
            ).read_text(encoding="utf-8")
        result = await ctx.dispatcher.run_agent(
            AgentContract(
                role="merger",
                stage=StageEnum.S1,
                input_artifacts=merger_inputs,
                output_schemas=("angle-map", "coverage-report"),
                size_class=SizeClass.L,
                budget_line="No web research; merge, dedup, and assign priors only.",
            )
        )
        angle_map = result.artifacts["angle-map"]
        assert isinstance(angle_map, AngleMap)
        ctx.workspace.write_artifact(MAP_PATH, angle_map)
        ctx.workspace.write_artifact(REPORT_PATH, result.artifacts["coverage-report"])
        ctx.emit(f"S1: merger folded {len(plan)} raw reports into {len(angle_map.angles)} angles")
        return angle_map


def _as_run(planned: PlannedRun, report: CartographerReport) -> CartographerRun:
    return CartographerRun(
        heuristic=planned.heuristic,
        context=planned.context,
        raw_angle_names=tuple(a.name for a in report.angles),
    )
