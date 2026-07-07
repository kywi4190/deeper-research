"""S1 — Angle cartography (design §5/S1). PROVISIONAL.

Dispatches the profile's cartographer ensemble in parallel (through the
dispatcher's semaphore), then the merger. The saturation rule — marginal
novelty from the merger's dedup map, spawn-up-to-2-more, hard cap 8 — arrives
in Prompt 7; this version runs exactly the initial ensemble so the machine is
walkable today. Idempotent re-entry: a raw report that already validates is
not re-dispatched, and the merger is skipped when the map already validates.
"""

from __future__ import annotations

import asyncio

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import AgentContract
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


def raw_report_path(heuristic: Heuristic) -> str:
    return f"angles/raw/{heuristic.value}.yaml"


class CartographyStage(StageBase):
    stage = StageEnum.S1
    required_inputs = (
        ("brief.md", Brief),
        ("destination.md", DestinationModel),
    )

    def ensemble(self, ctx: StageContext) -> tuple[Heuristic, ...]:
        return ENSEMBLE_ORDER[: ctx.config.initial_cartographers]

    def outputs(self, ctx: StageContext):
        out: list[tuple[str, type]] = [
            (raw_report_path(h), CartographerReport) for h in self.ensemble(ctx)
        ]
        out += [("angles/map.yaml", AngleMap), ("angles/map-report.md", CoverageReport)]
        return out

    async def execute(self, ctx: StageContext) -> None:
        brief_text = ctx.workspace.path("brief.md").read_text(encoding="utf-8")
        destination_text = ctx.workspace.path("destination.md").read_text(encoding="utf-8")
        base_inputs = {"brief": brief_text, "destination": destination_text}

        async def run_cartographer(heuristic: Heuristic) -> None:
            relpath = raw_report_path(heuristic)
            try:  # already-completed sub-work is not re-run (crash-safe re-entry)
                ctx.workspace.read_artifact(relpath, CartographerReport)
                ctx.emit(f"S1: {heuristic.value} raw report already valid — skipping")
                return
            except (WorkspaceError, ValidationError, yaml.YAMLError):
                pass
            result = await ctx.dispatcher.run_agent(
                AgentContract(
                    role=ROLE_FOR_HEURISTIC[heuristic],
                    stage=StageEnum.S1,
                    input_artifacts=base_inputs,
                    output_schemas=("cartographer-report",),
                    size_class=SizeClass.M,
                    budget_line="Return 5-12 candidate angles; existence proofs only.",
                    context=heuristic.value,
                )
            )
            ctx.workspace.write_artifact(relpath, result.artifacts["cartographer-report"])
            ctx.emit(f"S1: {heuristic.value} cartographer done")

        ensemble = self.ensemble(ctx)
        ctx.emit(f"S1: dispatching {len(ensemble)} cartographers in parallel…")
        await asyncio.gather(*(run_cartographer(h) for h in ensemble))

        try:
            ctx.workspace.read_artifact("angles/map.yaml", AngleMap)
            ctx.workspace.read_artifact("angles/map-report.md", CoverageReport)
            ctx.emit("S1: merged map already valid — skipping merger")
            return
        except (WorkspaceError, ValidationError, yaml.YAMLError):
            pass
        merger_inputs = dict(base_inputs)
        for heuristic in ensemble:
            path = ctx.workspace.path(raw_report_path(heuristic))
            merger_inputs[f"cartographer-report ({heuristic.value})"] = path.read_text(
                encoding="utf-8"
            )
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
        ctx.workspace.write_artifact("angles/map.yaml", result.artifacts["angle-map"])
        ctx.workspace.write_artifact("angles/map-report.md", result.artifacts["coverage-report"])
        ctx.emit("S1: angles/map.yaml + map-report.md written")
