"""S3 — Option scouting (design §5/S3).

One scout per allocated angle, dispatched in parallel; each contract carries a
budget line derived from allocation.yaml ("you have ~N units ≈ target 2N option
cards"). A card-critic then reviews each angle's set, and the scout gets exactly
one revision round against the critique — unless the critique's redundancy_pct
exceeds the stop threshold, in which case the angle stops early and its unused
units return to a global pool.

After every angle completes, the pool reflows (deeper.allocation.reflow — the
same P8 formula, floor 0, cap 100%) onto the angles whose critics named
missed_options and did NOT trip the redundancy stop; a top-up scout is
dispatched per reflow row targeting those specific missed options, and its new
cards merge into the angle's cards.yaml. The reflow table persists at
options/reflow.yaml so the redistribution is auditable.

All stop/reflow decisions are pure code over the critique files (design P8):
the LLM reports redundancy and misses; the orchestrator decides what they mean.
Write order makes re-entry honest — cards.yaml before critique.md within an
angle (a present critique attests the revision round is settled), and
options/reflow.yaml last (its presence attests the top-ups are merged).
"""

from __future__ import annotations

import asyncio
import math

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import AgentContract, AgentOutputInvalid
from deeper.allocation import reflow
from deeper.config import SizeClass
from deeper.schemas import (
    AllocationTable,
    AngleMap,
    Brief,
    CardCritique,
    DestinationModel,
    OptionCardSet,
)
from deeper.schemas import Stage as StageEnum
from deeper.workspace import WorkspaceError

from .base import StageBase, StageContext

REFLOW_PATH = "options/reflow.yaml"
# Design §5/S3: target option-card count per angle = allocated units × ~2.
CARDS_PER_UNIT = 2


def cards_path(angle_id: str) -> str:
    return f"options/{angle_id}/cards.yaml"


def critique_path(angle_id: str) -> str:
    return f"options/{angle_id}/critique.md"


# -- pure reflow math (unit-tested; no I/O) --------------------------------------


def distinct_cards(n_cards: int, redundancy_pct: float) -> int:
    """Cards that are not minor variants, per the critic's redundancy estimate
    (redundant count floored: the benefit of the doubt goes to the scout)."""
    return n_cards - math.floor(n_cards * redundancy_pct / 100.0)


def returned_units(allocated: int, n_cards: int, redundancy_pct: float, stop_pct: float) -> int:
    """Units an early-stopped angle returns to the reflow pool (design §5/S3:
    "the scout stops early and the unused units return to a global pool").

    Only angles that tripped the redundancy stop return units; a non-saturated
    angle that merely came up short keeps its allocation spent. Consumption is
    measured in the allocation's own currency: 1 unit ≈ 2 distinct cards.
    """
    if redundancy_pct <= stop_pct:
        return 0
    consumed = math.ceil(distinct_cards(n_cards, redundancy_pct) / CARDS_PER_UNIT)
    return max(allocated - consumed, 0)


def needs_revision(critique: CardCritique) -> bool:
    """The one revision round targets fixable defects; missed_options are the
    reflow pool's business, not the revising scout's."""
    return bool(critique.completeness_issues or critique.distinctness_issues)


class ScoutingStage(StageBase):
    stage = StageEnum.S3
    required_inputs = (
        ("brief.md", Brief),
        ("destination.md", DestinationModel),
        ("angles/map.yaml", AngleMap),
        ("allocation.yaml", AllocationTable),
    )

    @staticmethod
    def _try_read(ctx: StageContext, relpath: str, model: type):
        try:
            return ctx.workspace.read_artifact(relpath, model)
        except (WorkspaceError, ValidationError, yaml.YAMLError):
            return None

    # -- engine protocol ------------------------------------------------------

    def outputs(self, ctx: StageContext):
        table = self._try_read(ctx, "allocation.yaml", AllocationTable)
        if table is None:
            return []
        out: list[tuple[str, type]] = []
        for row in table.rows:
            if row.units > 0:
                out.append((cards_path(row.angle_id), OptionCardSet))
                out.append((critique_path(row.angle_id), CardCritique))
        if ctx.workspace.path(REFLOW_PATH).is_file():
            out.append((REFLOW_PATH, AllocationTable))
        return out

    def is_complete(self, ctx: StageContext) -> bool:
        """Complete when every angle's cards+critique validate AND the reflow
        question is settled: replaying the deterministic pool computation over
        the files on disk either yields nothing to redistribute, or the persisted
        reflow table (written after the top-ups merged) exists."""
        if not super().is_complete(ctx):
            return False
        if self._expected_reflow(ctx) is None:
            return True
        return self._try_read(ctx, REFLOW_PATH, AllocationTable) is not None

    # -- execution ------------------------------------------------------------

    async def execute(self, ctx: StageContext) -> None:
        table = ctx.workspace.read_artifact("allocation.yaml", AllocationTable)
        angle_map = ctx.workspace.read_artifact("angles/map.yaml", AngleMap)
        angles = {a.id: a for a in angle_map.angles}
        unknown = sorted(r.angle_id for r in table.rows if r.angle_id not in angles)
        if unknown:
            raise WorkspaceError(
                f"allocation.yaml names angles that are not in angles/map.yaml: {unknown}"
            )
        base_inputs = {
            "brief": ctx.workspace.path("brief.md").read_text(encoding="utf-8"),
            "destination": ctx.workspace.path("destination.md").read_text(encoding="utf-8"),
        }
        rows = [r for r in table.rows if r.units > 0]
        ctx.emit(f"S3: scouting {len(rows)} angles in parallel…")
        await asyncio.gather(
            *(self._scout_angle(ctx, row, angles[row.angle_id], base_inputs) for row in rows)
        )
        await self._reflow(ctx, table, angle_map, base_inputs)
        ctx.emit("S3: option cards + critiques written for every allocated angle")

    async def _scout_angle(
        self, ctx: StageContext, row, angle, base_inputs: dict[str, str]
    ) -> None:
        """One angle's pipeline: scout → critic → (early stop | one revision).
        critique.md is written LAST so its presence marks the angle settled."""
        angle_id = row.angle_id
        have_cards = self._try_read(ctx, cards_path(angle_id), OptionCardSet) is not None
        if have_cards and self._try_read(ctx, critique_path(angle_id), CardCritique) is not None:
            ctx.emit(f"S3: {angle_id} cards + critique already valid — skipping")
            return
        angle_yaml = angle.dump_yaml()
        if not have_cards:
            card_set = await self._dispatch_scout(
                ctx,
                context=angle_id,
                angle_id=angle_id,
                task_objective="",
                budget_line=(
                    f"You have ~{row.units} budget unit(s) ≈ target "
                    f"{CARDS_PER_UNIT * row.units} option cards for this angle."
                ),
                input_artifacts={**base_inputs, "angle": angle_yaml},
            )
            ctx.workspace.write_artifact(cards_path(angle_id), card_set)
            ctx.emit(f"S3: {angle_id} scout done ({len(card_set.cards)} cards)")

        cards = ctx.workspace.read_artifact(cards_path(angle_id), OptionCardSet)
        critic_contract = AgentContract(
            role="card-critic",
            stage=StageEnum.S3,
            input_artifacts={**base_inputs, "angle": angle_yaml, "cards": cards.dump_yaml()},
            output_schemas=("card-critique",),
            size_class=SizeClass.M,
            budget_line=(
                "A handful of cheap lookups only — audit the scout's research, do not redo it."
            ),
            context=angle_id,
        )
        result = await ctx.dispatcher.run_agent(critic_contract)
        critique = result.artifacts["card-critique"]
        assert isinstance(critique, CardCritique)
        if critique.angle_id != angle_id:
            raise AgentOutputInvalid(
                critic_contract,
                errors=(
                    f"card-critique.angle_id is '{critique.angle_id}' but this critic "
                    f"was assigned angle '{angle_id}'"
                ),
                raw_output=result.raw_text,
            )
        stop_pct = ctx.config.caps.scout_redundancy_stop_pct
        if critique.redundancy_pct > stop_pct:
            ctx.emit(
                f"S3: {angle_id} redundancy {critique.redundancy_pct:g}% > {stop_pct:g}% — "
                f"early stop, returning "
                f"{returned_units(row.units, len(cards.cards), critique.redundancy_pct, stop_pct)} "
                "unit(s) to the reflow pool"
            )
        elif needs_revision(critique):
            ctx.emit(f"S3: {angle_id} critique found issues — one scout revision round")
            revised = await self._dispatch_scout(
                ctx,
                context=f"{angle_id}-rev",
                angle_id=angle_id,
                task_objective=(
                    "# REVISION ROUND\nYour card set for this angle was reviewed by an "
                    "independent critic; its critique is in your inputs. Fix exactly what "
                    "it reports — completeness gaps, source-tier dishonesty, and cards that "
                    "are really one option — and re-emit the COMPLETE corrected card set. "
                    "This is your one revision round; do not add unrelated new options."
                ),
                budget_line=(
                    f"Same budget as the original pass: ~{row.units} unit(s) ≈ target "
                    f"{CARDS_PER_UNIT * row.units} cards. Fixing beats adding."
                ),
                input_artifacts={
                    **base_inputs,
                    "angle": angle_yaml,
                    "cards": cards.dump_yaml(),
                    "critique": critique.dump_yaml(),
                },
            )
            ctx.workspace.write_artifact(cards_path(angle_id), revised)
            ctx.emit(f"S3: {angle_id} revision applied ({len(revised.cards)} cards)")
        ctx.workspace.write_artifact(critique_path(angle_id), critique)
        ctx.emit(f"S3: {angle_id} critique written (redundancy {critique.redundancy_pct:g}%)")

    async def _dispatch_scout(
        self,
        ctx: StageContext,
        *,
        context: str,
        angle_id: str,
        task_objective: str,
        budget_line: str,
        input_artifacts: dict[str, str],
    ) -> OptionCardSet:
        contract = AgentContract(
            role="scout",
            stage=StageEnum.S3,
            task_objective=task_objective,
            input_artifacts=input_artifacts,
            output_schemas=("option-card-set",),
            size_class=SizeClass.M,
            budget_line=budget_line,
            context=context,
        )
        result = await ctx.dispatcher.run_agent(contract)
        card_set = result.artifacts["option-card-set"]
        assert isinstance(card_set, OptionCardSet)
        if card_set.angle_id != angle_id:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    f"option-card-set.angle_id is '{card_set.angle_id}' but this scout "
                    f"was assigned angle '{angle_id}'"
                ),
                raw_output=result.raw_text,
            )
        return card_set

    # -- reflow ---------------------------------------------------------------

    def _expected_reflow(self, ctx: StageContext) -> AllocationTable | None:
        """Replay the deterministic pool computation from the files on disk.
        Stable across re-entry: only redundancy-stopped angles return units, and
        those angles never receive revisions or top-ups, so their card counts
        and critiques cannot drift under our feet."""
        table = self._try_read(ctx, "allocation.yaml", AllocationTable)
        angle_map = self._try_read(ctx, "angles/map.yaml", AngleMap)
        if table is None or angle_map is None:
            return None
        stop_pct = ctx.config.caps.scout_redundancy_stop_pct
        critiques: list[CardCritique] = []
        pool = 0
        for row in table.rows:
            if row.units <= 0:
                continue
            cards = self._try_read(ctx, cards_path(row.angle_id), OptionCardSet)
            critique = self._try_read(ctx, critique_path(row.angle_id), CardCritique)
            if cards is None or critique is None:
                return None
            critiques.append(critique)
            pool += returned_units(row.units, len(cards.cards), critique.redundancy_pct, stop_pct)
        priors = {a.id: a.relevance_prior for a in angle_map.angles}
        return reflow(
            priors,
            critiques,
            returned_units=pool,
            gamma=ctx.config.gamma,
            redundancy_stop_pct=stop_pct,
        )

    async def _reflow(
        self, ctx: StageContext, table: AllocationTable, angle_map: AngleMap, base_inputs
    ) -> None:
        if self._try_read(ctx, REFLOW_PATH, AllocationTable) is not None:
            ctx.emit("S3: reflow already settled — skipping")
            return
        reflow_table = self._expected_reflow(ctx)
        if reflow_table is None:
            ctx.emit(
                "S3: reflow — nothing to redistribute (no early-stopped units, or no "
                "eligible angle with missed options)"
            )
            return
        targets = [r for r in reflow_table.rows if r.units > 0]
        ctx.emit(
            f"S3: reflow — {reflow_table.total_budget_units} returned unit(s) over "
            f"{len(targets)} angle(s) whose critics named missed options: "
            + ", ".join(f"{r.angle_id} +{r.units}" for r in targets)
        )
        angles = {a.id: a for a in angle_map.angles}
        await asyncio.gather(
            *(self._top_up(ctx, row, angles[row.angle_id], base_inputs) for row in targets)
        )
        ctx.workspace.write_artifact(REFLOW_PATH, reflow_table)
        ctx.emit("S3: reflow table written to options/reflow.yaml")

    async def _top_up(self, ctx: StageContext, row, angle, base_inputs: dict[str, str]) -> None:
        angle_id = row.angle_id
        existing = ctx.workspace.read_artifact(cards_path(angle_id), OptionCardSet)
        critique = ctx.workspace.read_artifact(critique_path(angle_id), CardCritique)
        missed = "\n".join(f"- {m}" for m in critique.missed_options)
        top_up = await self._dispatch_scout(
            ctx,
            context=f"{angle_id}-topup",
            angle_id=angle_id,
            task_objective=(
                "# REFLOW TOP-UP\nBudget returned by saturated angles has been reflowed "
                "to this angle because its critic named specific missed options. Scout "
                "THESE missed options — card the ones that hold up, skip any that do "
                "not survive contact with evidence (say why in notes):\n"
                f"{missed}\n"
                "Your existing cards are in your inputs; do not re-emit or duplicate them "
                "— emit only the NEW cards."
            ),
            budget_line=(
                f"You have {row.units} reflowed unit(s) ≈ target "
                f"{CARDS_PER_UNIT * row.units} additional option cards."
            ),
            input_artifacts={
                **base_inputs,
                "angle": angle.dump_yaml(),
                "cards": existing.dump_yaml(),
            },
        )
        known = {c.id for c in existing.cards}
        new_cards = [c for c in top_up.cards if c.id not in known]
        if not new_cards:
            ctx.emit(f"S3: {angle_id} top-up produced no new cards")
            return
        merged = OptionCardSet(
            angle_id=angle_id, cards=[*existing.cards, *new_cards], notes=existing.notes
        )
        ctx.workspace.write_artifact(cards_path(angle_id), merged)
        ctx.emit(
            f"S3: {angle_id} top-up merged {len(new_cards)} card(s): "
            + ", ".join(c.id for c in new_cards)
        )
