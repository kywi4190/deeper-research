"""S2 — Budget allocation (design §5/S2). Pure code, no agents.

Reads the Gate-A-approved angle map's relevance priors and runs the P8
allocation formula from `deeper.allocation`. Near-final already: Prompt 7 adds
only the application of Gate A edits (added angles, prior adjustments) to the
map *before* this stage runs — the allocation itself is complete.
"""

from __future__ import annotations

from deeper.allocation import allocate
from deeper.schemas import AllocationTable, AngleMap, Stage

from .base import StageBase, StageContext


class AllocationStage(StageBase):
    stage = Stage.S2
    required_inputs = (("angles/map.yaml", AngleMap),)

    def outputs(self, ctx: StageContext):
        return [("allocation.yaml", AllocationTable)]

    async def execute(self, ctx: StageContext) -> None:
        angle_map = ctx.workspace.read_artifact("angles/map.yaml", AngleMap)
        priors = {a.id: a.relevance_prior for a in angle_map.angles}
        table = allocate(
            priors,
            total_budget_units=ctx.config.total_budget_units,
            floor=ctx.config.floor,
            gamma=ctx.config.gamma,
            per_angle_cap_pct=ctx.config.per_angle_cap_pct,
        )
        ctx.workspace.write_artifact("allocation.yaml", table)
        ctx.emit(
            f"S2: allocated {table.total_budget_units} units across "
            f"{len(table.rows)} angles -> allocation.yaml"
        )
