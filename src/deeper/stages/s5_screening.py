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
against its own angle's cards, then code merges the batches. An angle whose
card count exceeds `screener_batch_max_cards` is itself split into balanced
SUB-BATCHES (M2 live run: a 25%-cap angle's ~20-card reply overflowed the M
class's 16k output ceiling — per-angle batching bounds the number of batches,
not one batch's size), each persisted as a part file the moment it passes its
chunk-scoped checks, then merged in code into the angle's settled batch file.

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
import math

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


def batch_path(angle_id: str) -> str:
    """Where one angle's screener batch persists the moment it passes its
    integrity checks (M1 finding 7: a merge-time failure used to discard all
    12 paid batches; resume re-paid every one). Wiped with the rest of
    screening/ on invalidation."""
    return f"screening/batches/{angle_id}.yaml"


def part_path(angle_id: str, part: int) -> str:
    """One sub-batch of an oversized angle, persisted the moment IT passes —
    a crash mid-angle resumes from the completed parts. Parts are deleted
    once the angle's settled batch file is written (angle ids are slugs, so
    the `.part` suffix cannot collide with another angle's batch file)."""
    return f"screening/batches/{angle_id}.part{part}.yaml"


def chunk_cards(cards: list, max_cards: int) -> list[list]:
    """Split an angle's cards into the fewest balanced, order-preserving
    chunks of at most `max_cards` (21 cards, max 10 -> 7/7/7 rather than
    10/10/1 — near-equal replies, no rump dispatch). Deterministic, so a
    resume recomputes the same chunk boundaries."""
    n = len(cards)
    if n <= max_cards:
        return [list(cards)]
    k = math.ceil(n / max_cards)
    base, extra = divmod(n, k)
    chunks, start = [], 0
    for i in range(k):
        size = base + (1 if i < extra else 0)
        chunks.append(list(cards[start : start + size]))
        start += size
    return chunks


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
            f"(one batch per angle; angles over {ctx.config.screener_batch_max_cards} "
            "cards sub-batched)…"
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
                    f"result cannot be built: {dupes}. The cheap fix: hand-rename "
                    "one colliding card id in its options/<angle>/cards.yaml (and "
                    "in its screening/batches/<angle>.yaml if present), then "
                    "`deeper resume` — a full angle re-scout is NOT needed. S3 now "
                    "auto-suffixes collisions at write time, so this arises only "
                    "in hand-edited workspaces. The overlap itself is signal the "
                    "two angles may partially overlap — worth a Gate A look."
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
        self,
        rubric: Rubric,
        card_set: OptionCardSet,
        base_inputs: dict[str, str],
        *,
        part: int | None = None,
        n_parts: int = 1,
    ) -> AgentContract:
        scope = (
            "Other angles are screened by parallel batches"
            if part is None
            else f"This is sub-batch {part} of {n_parts} for this angle — its other "
            "cards, and other angles, are screened by parallel batches"
        )
        return AgentContract(
            role="screener",
            stage=StageEnum.S5,
            input_artifacts={**base_inputs, "cards": card_set.dump_yaml()},
            output_schemas=("screening-result",),
            size_class=SizeClass.M,
            budget_line=(
                f"Score all {len(card_set.cards)} cards in your inputs against every "
                "rubric criterion at screening confidence. Kill-risk checks are single "
                "cheap lookups, done FIRST; beyond those, only spot-confirm where a "
                "score pivots on one dubious claim — depth is Stage 6's budget. "
                f"{scope} — score ONLY these cards."
            ),
            context=(card_set.angle_id if part is None else f"{card_set.angle_id}-part{part}"),
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
        cards alone (the per-batch checks union to full-map integrity).
        Persisted per angle the moment it passes, so a later failure (a
        cross-angle collision, a sibling batch, a crash) never re-pays it.
        An angle over `screener_batch_max_cards` is dispatched as balanced
        sub-batches (the reply of ONE call is bounded by construction — a
        near-cap angle's single reply overflowed the M-class output ceiling
        on the M2 live run), merged in code into the same settled file."""
        persisted = self._existing_batch(ctx, rubric, card_set)
        if persisted is not None:
            ctx.emit(f"S5: {card_set.angle_id} batch already valid — skipping (persisted)")
            return persisted
        max_cards = ctx.config.screener_batch_max_cards
        chunks = chunk_cards(card_set.cards, max_cards)
        if len(chunks) == 1:
            parts = [
                await self._screen_chunk(ctx, rubric, card_set, chunks[0], base_inputs, all_ids)
            ]
        else:
            ctx.emit(
                f"S5: {card_set.angle_id} has {len(card_set.cards)} cards > "
                f"{max_cards} per screener call — splitting into {len(chunks)} "
                "sub-batches"
            )
            parts = await asyncio.gather(
                *(
                    self._screen_chunk(
                        ctx,
                        rubric,
                        card_set,
                        chunk,
                        base_inputs,
                        all_ids,
                        part=i,
                        n_parts=len(chunks),
                    )
                    for i, chunk in enumerate(chunks, start=1)
                )
            )
        notes = "\n".join(p.notes for p in parts if p.notes) or None
        batch = ScreeningResult(options=[o for p in parts for o in p.options], notes=notes)
        problems = verify_screening(batch, rubric, [card_set])
        if problems:
            # Belt over the chunk-scoped checks (their union should equal this).
            raise AgentOutputInvalid(
                self._batch_contract(rubric, card_set, base_inputs),
                errors=(
                    "the merged sub-batches do not cohere with the rubric and "
                    "option cards:\n" + "\n".join(problems)
                ),
                raw_output="",
            )
        ctx.workspace.write_artifact(batch_path(card_set.angle_id), batch)
        if len(chunks) > 1:  # the settled file supersedes the parts
            for i in range(1, len(chunks) + 1):
                ctx.workspace.path(part_path(card_set.angle_id, i)).unlink(missing_ok=True)
        return batch

    async def _screen_chunk(
        self,
        ctx: StageContext,
        rubric: Rubric,
        card_set: OptionCardSet,
        cards: list,
        base_inputs: dict[str, str],
        all_ids: dict[str, str],
        *,
        part: int | None = None,
        n_parts: int = 1,
    ) -> ScreeningResult:
        """One screener dispatch over `cards` (the whole angle, or one
        sub-batch of it), integrity-checked against exactly those cards. A
        sub-batch persists at its part path the moment it passes, so a crash
        mid-angle resumes from the completed parts."""
        chunk_set = OptionCardSet(angle_id=card_set.angle_id, cards=cards, notes=card_set.notes)
        if part is not None:
            existing = self._existing_part(ctx, rubric, chunk_set, part)
            if existing is not None:
                ctx.emit(
                    f"S5: {card_set.angle_id} sub-batch {part} already valid — skipping (persisted)"
                )
                return existing
        contract = self._batch_contract(rubric, chunk_set, base_inputs, part=part, n_parts=n_parts)
        result = await ctx.dispatcher.run_agent(contract)
        screening = result.artifacts["screening-result"]
        assert isinstance(screening, ScreeningResult)
        own_ids = {c.id for c in cards}
        kept = [o for o in screening.options if o.option_id in own_ids]
        foreign = [
            o.option_id
            for o in screening.options
            if o.option_id not in own_ids and o.option_id in all_ids
        ]
        if foreign:
            ctx.emit(
                f"S5: {contract.context} batch scored {len(foreign)} option(s) covered "
                "by other batches — dropped (their own batches score them)"
            )
        unknown = [o for o in screening.options if o.option_id not in all_ids]
        if not kept and not unknown:
            # Every option belonged to other batches: an empty batch cannot even
            # be constructed for the integrity check, so report it directly.
            raise AgentOutputInvalid(
                contract,
                errors=(
                    f"the screening batch '{contract.context}' scored none of its "
                    f"assigned cards; score exactly these: {sorted(own_ids)}"
                ),
                raw_output=result.raw_text,
            )
        batch = ScreeningResult(options=kept + unknown, notes=screening.notes)
        problems = verify_screening(batch, rubric, [chunk_set])
        if problems:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    "the screening result validates in isolation but does not cohere "
                    "with the rubric and option cards:\n" + "\n".join(problems)
                ),
                raw_output=result.raw_text,
            )
        if part is not None:
            ctx.workspace.write_artifact(part_path(card_set.angle_id, part), batch)
        return batch  # problems empty implies batch == kept exactly

    def _existing_batch(
        self, ctx: StageContext, rubric: Rubric, card_set: OptionCardSet
    ) -> ScreeningResult | None:
        """A persisted batch counts only while it still coheres with the
        current rubric and cards — a Gate-B rubric edit or an angle re-scout
        silently invalidates it into a re-dispatch."""
        try:
            batch = ctx.workspace.read_artifact(batch_path(card_set.angle_id), ScreeningResult)
        except Exception:  # noqa: BLE001 — absent/invalid means re-screen
            return None
        if verify_screening(batch, rubric, [card_set]):
            return None
        return batch

    def _existing_part(
        self, ctx: StageContext, rubric: Rubric, chunk_set: OptionCardSet, part: int
    ) -> ScreeningResult | None:
        """Same trust rule as `_existing_batch`, scoped to one sub-batch: a
        persisted part counts only while it coheres with the current rubric
        and the (deterministically re-chunked) cards it covered."""
        try:
            batch = ctx.workspace.read_artifact(
                part_path(chunk_set.angle_id, part), ScreeningResult
            )
        except Exception:  # noqa: BLE001 — absent/invalid means re-screen
            return None
        if verify_screening(batch, rubric, [chunk_set]):
            return None
        return batch

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
