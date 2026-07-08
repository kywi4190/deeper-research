"""S5 — Screening & shortlist (design §5/S5).

The screener is the first agent permitted to read preferences.yaml (P9): its
contract carries the file's content — the ONLY contract that does — and the
quarantine hook allowlists the role for tool access. Its prompt bakes in the
order of operations: kill-risk checks first (one cheap lookup per declared
risk, recorded per option), then criterion scores with uncertainty bands, then
the preference slot from preferences alone.

Screening is dispatched as ONE BATCH PER ANGLE (mirroring S3's fan-out), not
one monolithic call: the M1 live run proved a full-map reply (52 options × 7
criteria) exceeds even a 64k output-token ceiling, and a single mega-call
re-pays the whole prompt on every retry. Each batch is integrity-checked
against its own angle's cards, then code merges the batches.

Everything after dispatch is deterministic code: cross-artifact integrity is
verified (a failure pauses the run — schema-valid but incoherent output is
still bad output), the weighted aggregates are recomputed from the
Gate-B-approved rubric weights (code owns arithmetic, P8), and the shortlist
rule in `shortlist.py` produces an advance/cut decision with a written reason
for every option. screening/scores.yaml is written before shortlist.md, so a
crash between the two resumes into pure code, never a re-spend.
"""

from __future__ import annotations

import asyncio

from deeper.agents_runtime import AgentContract, AgentOutputInvalid
from deeper.config import SizeClass
from deeper.schemas import (
    AllocationTable,
    AngleMap,
    Brief,
    DestinationModel,
    OptionCardSet,
    Preferences,
    Rubric,
    ScreeningResult,
    Shortlist,
    ShortlistCause,
    ShortlistOutcome,
)
from deeper.schemas import Stage as StageEnum

from .base import StageBase, StageContext
from .s3_scouting import cards_path
from .s4_rubric import require_cards
from .shortlist import build_shortlist, recompute_aggregates, verify_screening

SCORES_PATH = "screening/scores.yaml"
SHORTLIST_PATH = "screening/shortlist.md"


class ScreeningStage(StageBase):
    stage = StageEnum.S5
    required_inputs = (
        ("brief.md", Brief),
        ("destination.md", DestinationModel),
        ("preferences.yaml", Preferences),
        ("rubric.yaml", Rubric),
        ("allocation.yaml", AllocationTable),
        ("angles/map.yaml", AngleMap),
    )

    def outputs(self, ctx: StageContext):
        return [(SCORES_PATH, ScreeningResult), (SHORTLIST_PATH, Shortlist)]

    def validate_inputs(self, ctx: StageContext) -> None:
        super().validate_inputs(ctx)
        require_cards(ctx, self.stage.value)

    async def execute(self, ctx: StageContext) -> None:
        rubric = ctx.workspace.read_artifact("rubric.yaml", Rubric)
        table = ctx.workspace.read_artifact("allocation.yaml", AllocationTable)
        angle_map = ctx.workspace.read_artifact("angles/map.yaml", AngleMap)
        card_sets = [
            ctx.workspace.read_artifact(cards_path(row.angle_id), OptionCardSet)
            for row in table.rows
            if row.units > 0
        ]
        screening = self._existing_scores(ctx)
        if screening is None:
            screening = await self._screen(ctx, rubric, card_sets)
        shortlist = build_shortlist(
            screening,
            {a.id: a.relevance_prior for a in angle_map.angles},
            threshold=ctx.config.shortlist_threshold,
            top_k=ctx.config.shortlist_size,
            max_per_angle=ctx.config.caps.max_finalists_per_angle,
            margin=ctx.config.shortlist_dark_horse_margin,
            max_finalists=ctx.config.caps.max_finalists,
        )
        ctx.workspace.write_artifact(SHORTLIST_PATH, shortlist)
        self._report(ctx, shortlist)

    def _existing_scores(self, ctx: StageContext) -> ScreeningResult | None:
        try:
            return ctx.workspace.read_artifact(SCORES_PATH, ScreeningResult)
        except Exception:  # noqa: BLE001 — absent/invalid means re-screen
            return None

    async def _screen(
        self, ctx: StageContext, rubric: Rubric, card_sets: list[OptionCardSet]
    ) -> ScreeningResult:
        base_inputs = {
            "brief": ctx.workspace.path("brief.md").read_text(encoding="utf-8"),
            "destination": ctx.workspace.path("destination.md").read_text(encoding="utf-8"),
            "rubric": rubric.dump_yaml(),
            # The ONLY contract in the pipeline that carries preferences (P9);
            # the quarantine hook allowlists this role's tool access to match.
            "preferences": ctx.workspace.path("preferences.yaml").read_text(encoding="utf-8"),
        }
        n_cards = sum(len(s.cards) for s in card_sets)
        ctx.emit(
            f"S5: screener scoring {n_cards} cards across {len(card_sets)} angles "
            "(one batch per angle)…"
        )
        # Ids of every OTHER angle's cards, per angle: a batch reply naming one
        # of those is over-scoping (or the mock full-scenario fixture fallback)
        # and is dropped; an id matching nothing at all is incoherence.
        all_ids = {c.id: s.angle_id for s in card_sets for c in s.cards}
        batches = await asyncio.gather(
            *(self._screen_angle(ctx, rubric, cs, base_inputs, all_ids) for cs in card_sets)
        )
        merged_options = [o for batch in batches for o in batch.options]
        ids = [o.option_id for o in merged_options]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise AgentOutputInvalid(
                self._batch_contract(rubric, card_sets[0], base_inputs),
                errors=(
                    "option ids collide across angles, so the merged screening "
                    f"result cannot be built: {dupes}. Two scouts produced cards "
                    "with the same id — rerun S3 for one of the angles."
                ),
                raw_output="",
            )
        pairs = zip(card_sets, batches, strict=True)
        notes = [f"[{s.angle_id}] {b.notes}" for s, b in pairs if b.notes]
        screening = ScreeningResult(options=merged_options, notes="\n".join(notes) or None)
        screening, drift = recompute_aggregates(screening, rubric)
        for message in drift:
            ctx.emit(message)
        ctx.workspace.write_artifact(SCORES_PATH, screening)
        ctx.emit(f"S5: screening/scores.yaml written ({len(screening.options)} options)")
        return screening

    def _batch_contract(
        self, rubric: Rubric, card_set: OptionCardSet, base_inputs: dict[str, str]
    ) -> AgentContract:
        return AgentContract(
            role="screener",
            stage=StageEnum.S5,
            input_artifacts={**base_inputs, "cards": card_set.dump_yaml()},
            output_schemas=("screening-result",),
            size_class=SizeClass.M,
            budget_line=(
                f"Score all {len(card_set.cards)} cards of this angle against every "
                "rubric criterion at screening confidence. Kill-risk checks are single "
                "cheap lookups, done FIRST; beyond those, only spot-confirm where a "
                "score pivots on one dubious claim — depth is Stage 6's budget. Other "
                "angles are screened by parallel batches — score ONLY these cards."
            ),
            context=card_set.angle_id,
        )

    async def _screen_angle(
        self,
        ctx: StageContext,
        rubric: Rubric,
        card_set: OptionCardSet,
        base_inputs: dict[str, str],
        all_ids: dict[str, str],
    ) -> ScreeningResult:
        """One angle's screening batch, integrity-checked against that angle's
        cards alone (the per-batch checks union to full-map integrity)."""
        contract = self._batch_contract(rubric, card_set, base_inputs)
        result = await ctx.dispatcher.run_agent(contract)
        screening = result.artifacts["screening-result"]
        assert isinstance(screening, ScreeningResult)
        own_ids = {c.id for c in card_set.cards}
        kept = [o for o in screening.options if o.option_id in own_ids]
        foreign = [
            o.option_id
            for o in screening.options
            if o.option_id not in own_ids and o.option_id in all_ids
        ]
        if foreign:
            ctx.emit(
                f"S5: {card_set.angle_id} batch scored {len(foreign)} option(s) from "
                "other angles — dropped (their own batches score them)"
            )
        unknown = [o for o in screening.options if o.option_id not in all_ids]
        if not kept and not unknown:
            # Every option belonged to other angles: an empty batch cannot even
            # be constructed for the integrity check, so report it directly.
            raise AgentOutputInvalid(
                contract,
                errors=(
                    f"the screening batch for angle '{card_set.angle_id}' scored none "
                    f"of that angle's cards; score exactly these: {sorted(own_ids)}"
                ),
                raw_output=result.raw_text,
            )
        batch = ScreeningResult(options=kept + unknown, notes=screening.notes)
        problems = verify_screening(batch, rubric, [card_set])
        if problems:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    "the screening result validates in isolation but does not cohere "
                    "with the rubric and option cards:\n" + "\n".join(problems)
                ),
                raw_output=result.raw_text,
            )
        return batch  # problems empty implies batch == kept exactly

    def _report(self, ctx: StageContext, shortlist: Shortlist) -> None:
        by_id = {d.option_id: d for d in shortlist.decisions}
        ctx.emit(
            f"S5 shortlist (top-{ctx.config.shortlist_size} by UCB + dark-horse "
            f"margin {ctx.config.shortlist_dark_horse_margin:g}, "
            f"floor {shortlist.threshold:g}): "
            f"{len(shortlist.finalist_ids)} finalists, "
            f"{len(shortlist.decisions) - len(shortlist.finalist_ids)} cut "
            "(every cut has a written reason in screening/shortlist.md)"
        )
        for option_id in shortlist.finalist_ids:
            cause = by_id[option_id].cause
            tag = (
                " [breadth-guardrail add]"
                if cause is ShortlistCause.DIVERSITY_GUARDRAIL_ADD
                else ""
            )
            ctx.emit(f"S5:   finalist {option_id}{tag}")
        cut_causes = [
            d.cause.value for d in shortlist.decisions if d.decision is ShortlistOutcome.CUT
        ]
        for cause_value in sorted(set(cut_causes)):
            ctx.emit(f"S5:   cut ({cause_value}): {cut_causes.count(cause_value)}")
