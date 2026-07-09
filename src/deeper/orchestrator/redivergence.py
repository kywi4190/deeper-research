"""The Gate-C re-divergence mini-loop (design §5 Gate C, §12: 1 per run).

A human-approved frame-checker proposal spawns "a mini-loop of Stages 1-6
scoped to the new region, budgeted separately": the proposal's
`estimated_cost_units` are the mini-loop's OWN budget — 1 unit buys the
scouting pass, the rest cap the deep dive (the same `deep_dive_unit_cap`
mechanism S6 uses, via a scoped config copy).

The loop compresses the stages honestly (recorded in README design
deviations): a `new-angle` proposal edits the map (S1-equivalent), one scout
pass populates the region (S3 — no critic round at this budget), one screener
batch scores the new cards and the threshold rule seats at most one new
finalist (S5 — the §12 `max_finalists` cap still binds), and the real
`DeepDiveStage` round/verification machinery dives it under the scoped cap
(S6). S4 is deliberately not re-run: the Gate-B-approved rubric is fixed.
The caller (the engine's Gate-C loop handler) then invalidates S7 so the
tournament reruns over the merged finalists.
"""

from __future__ import annotations

import re

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import AgentContract, AgentOutputInvalid
from deeper.config import SizeClass
from deeper.schemas import (
    Angle,
    AngleMap,
    Heuristic,
    OptionCard,
    OptionCardSet,
    OptionScreening,
    RedivergenceKind,
    RedivergenceProposal,
    Rubric,
    ScreeningResult,
    Shortlist,
    ShortlistCause,
    ShortlistDecision,
    ShortlistOutcome,
    Stage,
)
from deeper.stages import StageContext
from deeper.stages.s3_scouting import cards_path
from deeper.stages.s5_screening import SCORES_PATH as S5_SCORES_PATH
from deeper.stages.s5_screening import SHORTLIST_PATH
from deeper.stages.s6_deepdive import SCORES_PATH as DIVE_SCORES_PATH
from deeper.stages.s6_deepdive import DeepDiveStage, _Finalist
from deeper.stages.shortlist import recompute_aggregates, verify_screening
from deeper.workspace import WorkspaceError


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "redivergence-angle"


async def run_mini_loop(ctx: StageContext, proposal: RedivergenceProposal) -> bool:
    """Execute the approved proposal on its own budget. Returns True when a
    new finalist merged into dossiers/scores.yaml (an S7 rerun is owed);
    False when nothing cleared the bar (the boards stand)."""
    ws = ctx.workspace
    rubric = ws.read_artifact("rubric.yaml", Rubric)
    units = proposal.estimated_cost_units
    dive_cap = max(1, units - 1)
    angle_id = _target_angle(ctx, proposal)
    ctx.emit(
        f"gate-c: re-divergence mini-loop on angle '{angle_id}' — its own budget of "
        f"{units} unit(s): 1 for scouting, deep-dive cap {dive_cap} round(s)"
    )
    new_cards = await _scout(ctx, proposal, angle_id)
    if not new_cards:
        ctx.emit("gate-c: the mini-loop scout produced no new distinct cards — the boards stand")
        return False
    screened = await _screen(ctx, rubric, angle_id, new_cards)
    champion = _merge_screening(ctx, screened)
    if champion is None:
        return False
    card = next(c for c in new_cards if c.id == champion.option_id)
    scoped = StageContext(
        workspace=ws,
        config=ctx.config.model_copy(update={"deep_dive_unit_cap": dive_cap}),
        dispatcher=ctx.dispatcher,
        emit=ctx.emit,
        ask_user=ctx.ask_user,
    )
    base_inputs = {
        "brief": ws.path("brief.md").read_text(encoding="utf-8"),
        "destination": ws.path("destination.md").read_text(encoding="utf-8"),
        "rubric": rubric.dump_yaml(),
    }
    final = await DeepDiveStage()._deep_dive(
        scoped, _Finalist(champion.option_id, card, champion), rubric, base_inputs
    )
    dive_scores = ws.read_artifact(DIVE_SCORES_PATH, ScreeningResult)
    ws.write_artifact(
        DIVE_SCORES_PATH,
        ScreeningResult(options=[*dive_scores.options, final], notes=dive_scores.notes),
    )
    ctx.emit(
        f"gate-c: mini-loop finalist '{champion.option_id}' merged into "
        f"{DIVE_SCORES_PATH} — S7 reruns over the merged scoreboard"
    )
    return True


def _target_angle(ctx: StageContext, proposal: RedivergenceProposal) -> str:
    """S1-equivalent: a scout-task proposal targets its existing angle; a
    new-angle proposal enters the map with human provenance (the human
    approved it) and a neutral prior."""
    ws = ctx.workspace
    angle_map = ws.read_artifact("angles/map.yaml", AngleMap)
    known = {a.id for a in angle_map.angles}
    if proposal.kind is RedivergenceKind.SCOUT_TASK:
        assert proposal.target_angle_id in known  # S7 checked this at emission
        return proposal.target_angle_id
    slug = _slugify(proposal.description)
    while slug in known:
        slug += "-2"
    angle = Angle(
        id=slug,
        name=proposal.description[:80],
        definition=proposal.description,
        distinctness_rationale=(
            "Proposed by the S7 frame-checker as a region the map could not have "
            "produced; approved by the human at Gate C."
        ),
        example_options=["(to be scouted — Gate-C re-divergence)"],
        relevance_prior=0.5,
        prior_justification=(
            "Human judgment at Gate C, accepting the frame-checker's gap finding."
        ),
        contributing_heuristics=[Heuristic.HUMAN],
        notes=f"Gate-C re-divergence; estimated cost {proposal.estimated_cost_units} unit(s).",
    )
    ws.write_artifact(
        "angles/map.yaml",
        AngleMap(
            angles=[*angle_map.angles, angle],
            dedup_map=angle_map.dedup_map,
            notes=angle_map.notes,
        ),
    )
    ctx.emit(f"gate-c: new angle '{slug}' entered the map (prior 0.5, human provenance)")
    return slug


async def _scout(
    ctx: StageContext, proposal: RedivergenceProposal, angle_id: str
) -> list[OptionCard]:
    """S3-equivalent: one scout pass targeted at exactly the proposed miss;
    new cards merge into the angle's cards.yaml (existing ids are kept, not
    re-scouted). No critic round — a critique pass would double the budget."""
    ws = ctx.workspace
    angle_map = ws.read_artifact("angles/map.yaml", AngleMap)
    angle = next(a for a in angle_map.angles if a.id == angle_id)
    try:
        existing = ws.read_artifact(cards_path(angle_id), OptionCardSet)
    except (WorkspaceError, ValidationError, yaml.YAMLError):
        existing = None
    existing_ids = {c.id for c in existing.cards} if existing else set()
    already = (
        "Cards already scouted for this angle (do NOT re-emit these): "
        + ", ".join(sorted(existing_ids))
        if existing_ids
        else "No cards exist for this angle yet."
    )
    contract = AgentContract(
        role="scout",
        stage=Stage.S7,
        task_objective=(
            "# GATE-C RE-DIVERGENCE SCOUT\n"
            "The S7 frame-checker found a credible gap and the human approved this "
            f"proposal (verbatim):\n{proposal.description}\n"
            f"Scout angle '{angle_id}' for exactly the proposed miss(es). {already}"
        ),
        input_artifacts={
            "brief": ws.path("brief.md").read_text(encoding="utf-8"),
            "destination": ws.path("destination.md").read_text(encoding="utf-8"),
            "angle": angle.dump_yaml(),
        },
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="1 budget unit: target ~2 option cards, for the proposed region only.",
        context=f"redivergence-{angle_id}",
    )
    result = await ctx.dispatcher.run_agent(contract)
    card_set = result.artifacts["option-card-set"]
    assert isinstance(card_set, OptionCardSet)
    if card_set.angle_id != angle_id:
        raise AgentOutputInvalid(
            contract,
            errors=(
                f"- option-card-set.angle_id is '{card_set.angle_id}' but this scout "
                f"was assigned angle '{angle_id}'"
            ),
            raw_output=result.raw_text,
        )
    new_cards = [c for c in card_set.cards if c.id not in existing_ids]
    skipped = len(card_set.cards) - len(new_cards)
    if skipped:
        ctx.emit(f"gate-c: scout re-emitted {skipped} existing card(s) — skipped")
    if new_cards:
        merged = OptionCardSet(
            angle_id=angle_id,
            cards=[*(existing.cards if existing else []), *new_cards],
            notes=existing.notes if existing else card_set.notes,
        )
        ws.write_artifact(cards_path(angle_id), merged)
        ctx.emit(f"gate-c: {len(new_cards)} new card(s) merged into {cards_path(angle_id)}")
    return new_cards


async def _screen(
    ctx: StageContext, rubric: Rubric, angle_id: str, new_cards: list[OptionCard]
) -> ScreeningResult:
    """S5-equivalent: one screener batch over only the new cards, the same
    integrity path as S5 (verify + code-recomputed aggregates)."""
    ws = ctx.workspace
    new_set = OptionCardSet(angle_id=angle_id, cards=new_cards)
    contract = AgentContract(
        role="screener",
        stage=Stage.S7,
        task_objective=(
            "# GATE-C MINI-LOOP SCREENING\n"
            "Score exactly these newly scouted cards against the rubric at "
            "screening confidence — the rest of the field is already screened."
        ),
        input_artifacts={
            "brief": ws.path("brief.md").read_text(encoding="utf-8"),
            "destination": ws.path("destination.md").read_text(encoding="utf-8"),
            "rubric": rubric.dump_yaml(),
            "preferences": ws.path("preferences.yaml").read_text(encoding="utf-8"),
            "cards": new_set.dump_yaml(),
        },
        output_schemas=("screening-result",),
        size_class=SizeClass.M,
        budget_line=(
            f"Score all {len(new_cards)} new card(s) of this angle against every "
            "rubric criterion at screening confidence; kill-risk checks first."
        ),
        context="redivergence",
    )
    result = await ctx.dispatcher.run_agent(contract)
    screening = result.artifacts["screening-result"]
    assert isinstance(screening, ScreeningResult)
    own_ids = {c.id for c in new_cards}
    kept = [o for o in screening.options if o.option_id in own_ids]
    if len(kept) < len(screening.options):
        ctx.emit(
            f"gate-c: mini-loop screening included "
            f"{len(screening.options) - len(kept)} already-screened option(s) — dropped"
        )
    if not kept:
        raise AgentOutputInvalid(
            contract,
            errors=(
                "the mini-loop screening scored none of the new cards; score "
                f"exactly these: {sorted(own_ids)}"
            ),
            raw_output=result.raw_text,
        )
    batch = ScreeningResult(options=kept, notes=screening.notes)
    problems = verify_screening(batch, rubric, [new_set])
    if problems:
        raise AgentOutputInvalid(
            contract,
            errors=(
                "the screening result validates in isolation but does not cohere "
                "with the rubric and the new cards:\n" + "\n".join(problems)
            ),
            raw_output=result.raw_text,
        )
    batch, drift = recompute_aggregates(batch, rubric, label="gate-c")
    for message in drift:
        ctx.emit(message)
    return batch


def _merge_screening(ctx: StageContext, screened: ScreeningResult) -> OptionScreening | None:
    """Seat at most one new finalist: the highest-UCB new option clearing the
    absolute shortlist floor, respecting the §12 `max_finalists` hard cap.
    Every new option gets an audited decision appended to shortlist.md."""
    ws = ctx.workspace
    scores = ws.read_artifact(S5_SCORES_PATH, ScreeningResult)
    shortlist = ws.read_artifact(SHORTLIST_PATH, Shortlist)
    threshold = ctx.config.shortlist_threshold
    at_cap = len(shortlist.finalist_ids) >= ctx.config.caps.max_finalists
    candidates = sorted(screened.options, key=lambda o: (-o.weighted_ucb, o.option_id))
    champion: OptionScreening | None = None
    decisions: list[ShortlistDecision] = []
    for option in candidates:
        if option.weighted_ucb < threshold:
            decisions.append(
                ShortlistDecision(
                    option_id=option.option_id,
                    decision=ShortlistOutcome.CUT,
                    cause=ShortlistCause.BELOW_THRESHOLD,
                    reason=(
                        f"Cut in the Gate-C mini-loop: its weighted UCB of "
                        f"{option.weighted_ucb:g} does not reach the shortlist floor "
                        f"{threshold:g}."
                    ),
                )
            )
        elif champion is None and at_cap:
            decisions.append(
                ShortlistDecision(
                    option_id=option.option_id,
                    decision=ShortlistOutcome.CUT,
                    cause=ShortlistCause.BELOW_CUTOFF,
                    reason=(
                        f"Cut in the Gate-C mini-loop by the hard finalist cap: its "
                        f"weighted UCB of {option.weighted_ucb:g} clears the floor "
                        f"{threshold:g}, but {ctx.config.caps.max_finalists} finalists "
                        "(design §12) are already seated."
                    ),
                )
            )
        elif champion is None:
            champion = option
            decisions.append(
                ShortlistDecision(
                    option_id=option.option_id,
                    decision=ShortlistOutcome.ADVANCED,
                    cause=ShortlistCause.UCB_ABOVE_THRESHOLD,
                    reason=(
                        f"Advanced by the Gate-C re-divergence mini-loop: the highest "
                        f"weighted UCB ({option.weighted_ucb:g}) among the newly "
                        f"scouted options, above the shortlist floor {threshold:g}. "
                        "The human approved the frame-checker's gap proposal at "
                        "Gate C; this option is its champion."
                    ),
                )
            )
        else:
            decisions.append(
                ShortlistDecision(
                    option_id=option.option_id,
                    decision=ShortlistOutcome.CUT,
                    cause=ShortlistCause.BELOW_CUTOFF,
                    reason=(
                        f"Cut in the Gate-C mini-loop: its weighted UCB of "
                        f"{option.weighted_ucb:g} clears the floor {threshold:g}, but "
                        f"the mini-loop's budget seats only its top option "
                        f"('{champion.option_id}')."
                    ),
                )
            )
    ws.write_artifact(
        S5_SCORES_PATH,
        ScreeningResult(options=[*scores.options, *screened.options], notes=scores.notes),
    )
    finalist_ids = shortlist.finalist_ids
    if champion is not None:
        finalist_ids = [*finalist_ids, champion.option_id]
    ws.write_artifact(
        SHORTLIST_PATH,
        Shortlist(
            threshold=shortlist.threshold,
            decisions=[*shortlist.decisions, *decisions],
            finalist_ids=finalist_ids,
            notes=shortlist.notes,
        ),
    )
    if champion is None:
        ctx.emit(
            "gate-c: no newly scouted option cleared the shortlist floor — the "
            "mini-loop ends with the boards standing (every cut has a written "
            "reason in screening/shortlist.md)"
        )
    else:
        ctx.emit(
            f"gate-c: mini-loop champion '{champion.option_id}' seated as a finalist "
            f"(weighted UCB {champion.weighted_ucb:g}); deep dive next"
        )
    return champion
