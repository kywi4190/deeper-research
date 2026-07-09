"""S2 — Budget allocation (design §5/S2). Pure code, no agents.

Runs the P8 allocation formula from `deeper.allocation` over the Gate-A-approved
angle map (Gate A's edit actions — additions, removals, prior adjustments —
have already been applied to angles/map.yaml by the gate machinery, so the map
read here IS the approved map). Besides writing allocation.yaml, the stage
prints the full table: S2 runs at the Gate A exit, so the user sees where the
budget will go before S3 spends any of it.
"""

from __future__ import annotations

from deeper.allocation import allocate
from deeper.schemas import AllocationTable, AngleMap, Stage

from .base import StageBase, StageContext, StageInterrupted


def table_lines(table: AllocationTable) -> list[str]:
    """The allocation table as printable lines (rows sorted by units, desc)."""
    rows = sorted(table.rows, key=lambda r: (-r.units, r.angle_id))
    width = max(len(r.angle_id) for r in rows)
    lines = [
        f"S2: allocation — {table.total_budget_units} units over {len(rows)} angles "
        f"(floor {table.floor}, gamma {table.gamma}, cap {table.per_angle_cap_pct:g}%):",
        f"  {'angle':<{width}}  prior  units  share",
    ]
    for r in rows:
        share = r.units / table.total_budget_units
        lines.append(
            f"  {r.angle_id:<{width}}  {r.relevance_prior:>5.2f}  {r.units:>5}  {share:>5.1%}"
        )
    lines.append(
        f"  {'total':<{width}}         {table.total_budget_units:>5}   100%   "
        "-> allocation.yaml (this is what S3 will spend per angle)"
    )
    return lines


class AllocationStage(StageBase):
    stage = Stage.S2
    required_inputs = (("angles/map.yaml", AngleMap),)

    def outputs(self, ctx: StageContext):
        return [("allocation.yaml", AllocationTable)]

    async def execute(self, ctx: StageContext) -> None:
        angle_map = ctx.workspace.read_artifact("angles/map.yaml", AngleMap)
        priors = {a.id: a.relevance_prior for a in angle_map.angles}
        try:
            table = allocate(
                priors,
                total_budget_units=ctx.config.total_budget_units,
                floor=ctx.config.floor,
                gamma=ctx.config.gamma,
                per_angle_cap_pct=ctx.config.per_angle_cap_pct,
            )
        except ValueError as err:
            # An approved map the budget formula cannot satisfy is a human
            # decision, not a crash (M1 finding 2): pause cleanly with the
            # exact fix, before any S3 spend. State stays resumable at S2.
            raise StageInterrupted(
                f"S2: the approved angle map is infeasible for this run's budget — "
                f"{err}.\nEither remove angles (edit angles/map.yaml directly, or "
                f"`deeper rerun <run> --stage S1` for a fresh map) or raise "
                f"total_budget_units in config.yaml, then `deeper resume <run>`."
            ) from err
        ctx.workspace.write_artifact("allocation.yaml", table)
        for line in table_lines(table):
            ctx.emit(line)
