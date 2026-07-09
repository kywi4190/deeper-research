"""S7 — Adversarial tournament (design §5/S7).

Code computes the two scoreboards from the post-deep-dive scores (destination-
only = preference-slot weight forced to 0; preference-adjusted = as configured
at Gate B) and their rank inversions — the tournament's priority docket,
because inversions are exactly where preference-overfitting would hide. Then
three adversarial roles run in parallel:

- one **prosecutor** per top-3 finalist (dossier evidence + at most 3 new
  targeted searches; must produce the most likely way choosing it leads to
  regret);
- one **steelman** per docket entry — the runner-up plus every option an
  inversion demoted;
- the **frame-checker**, the anti-overfitting backstop: original brief, the
  map with Gate A's removals log, every critique's missed options, the final
  ranking, and the code-computed sensitivity tables; PASS or a re-divergence
  proposal.

A **judge** then updates criterion scores ONLY where the tournament surfaced
decisive material; code verifies each update against the current scores,
applies it (widening a band that no longer contains its score), and logs every
change with its cause in tournament/score-updates.yaml.

Re-divergence proposals are surfaced at Gate C, never auto-executed (P7).

Idempotency: every adversarial artifact is written the moment its dispatch
completes, so a resume re-dispatches only what is missing; the updated
scoreboard (tournament/scores.yaml) is written last and marks the stage
settled.
"""

from __future__ import annotations

import asyncio

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import AgentContract, AgentOutputInvalid
from deeper.config import SizeClass
from deeper.schemas import (
    AngleMap,
    Brief,
    CoverageReport,
    DestinationModel,
    Dossier,
    FrameCheck,
    FrameCheckVerdict,
    GateADecision,
    OptionCardSet,
    OptionScreening,
    Prosecution,
    RedivergenceKind,
    Rubric,
    ScoreUpdate,
    ScoreUpdateLog,
    ScreeningResult,
    Shortlist,
    Steelman,
    SteelmanTrigger,
    UncertaintyBand,
    VerificationReport,
)
from deeper.schemas import Stage as StageEnum
from deeper.sensitivity import (
    dual_scoreboards,
    preference_sweep,
    rank_inversions,
    render_scoreboards,
    render_sensitivity,
    steelman_docket,
    weight_sensitivity,
)
from deeper.workspace import WorkspaceError

from .base import StageBase, StageContext, StageInputsMissing
from .s3_scouting import cards_path, critique_path
from .s6_deepdive import SCORES_PATH as DIVE_SCORES_PATH
from .s6_deepdive import dossier_path, verification_path
from .shortlist import recompute_aggregates

FRAME_CHECK_PATH = "tournament/frame-check.md"
UPDATE_LOG_PATH = "tournament/score-updates.yaml"
# Post-judge scoreboard — S8's scoring input and the stage-settled marker.
# The design names only the four tournament artifacts; the judge's updated
# scores need a home S8 can read without replaying the ledger (see README
# design deviations).
UPDATED_SCORES_PATH = "tournament/scores.yaml"
PROSECUTED_TOP_N = 3


def prosecution_path(option_id: str) -> str:
    return f"tournament/{option_id}-prosecution.md"


def steelman_path(option_id: str) -> str:
    return f"tournament/{option_id}-steelman.md"


def apply_score_updates(
    scores: ScreeningResult, updates: list[ScoreUpdate], rubric: Rubric
) -> ScreeningResult:
    """Apply judge updates to the scoreboard in code: replace each named
    criterion score (widening its band when the new score falls outside — the
    judge moved the estimate, not the recorded uncertainty), then recompute
    every weighted aggregate from the rubric. Pure; callers persist."""
    by_option: dict[str, list[ScoreUpdate]] = {}
    for update in updates:
        by_option.setdefault(update.option_id, []).append(update)
    options: list[OptionScreening] = []
    for option in scores.options:
        todo = {u.criterion_id: u for u in by_option.get(option.option_id, [])}
        if not todo:
            options.append(option)
            continue
        criterion_scores = []
        for cs in option.criterion_scores:
            change = todo.get(cs.criterion_id)
            if change is None:
                criterion_scores.append(cs)
                continue
            band = UncertaintyBand(
                lo=min(cs.band.lo, change.new_score), hi=max(cs.band.hi, change.new_score)
            )
            criterion_scores.append(cs.model_copy(update={"score": change.new_score, "band": band}))
        options.append(option.model_copy(update={"criterion_scores": criterion_scores}))
    merged, _drift = recompute_aggregates(
        ScreeningResult(options=options, notes=scores.notes), rubric, label="S7"
    )
    return merged


class TournamentStage(StageBase):
    stage = StageEnum.S7
    required_inputs = (
        ("brief.md", Brief),
        ("destination.md", DestinationModel),
        ("rubric.yaml", Rubric),
        ("angles/map.yaml", AngleMap),
        ("angles/map-report.md", CoverageReport),
        ("gates/gate-a.yaml", GateADecision),
        ("screening/shortlist.md", Shortlist),
        (DIVE_SCORES_PATH, ScreeningResult),
    )

    @staticmethod
    def _try_read(ctx: StageContext, relpath: str, model: type):
        try:
            return ctx.workspace.read_artifact(relpath, model)
        except (WorkspaceError, ValidationError, yaml.YAMLError):
            return None

    # -- the docket: pure code over the post-deep-dive scores ------------------

    def _docket(self, scores: ScreeningResult, rubric: Rubric):
        """(destination board, adjusted board, top-3 ids, steelman docket)."""
        dest_board, adj_board = dual_scoreboards(scores, rubric)
        top3 = [row.option_id for row in adj_board[:PROSECUTED_TOP_N]]
        return dest_board, adj_board, top3, steelman_docket(dest_board, adj_board)

    # -- engine protocol ---------------------------------------------------------

    def outputs(self, ctx: StageContext):
        scores = self._try_read(ctx, DIVE_SCORES_PATH, ScreeningResult)
        rubric = self._try_read(ctx, "rubric.yaml", Rubric)
        if scores is None or rubric is None:
            return []
        _, _, top3, docket = self._docket(scores, rubric)
        out: list[tuple[str, type]] = [
            (prosecution_path(option_id), Prosecution) for option_id in top3
        ]
        out.extend((steelman_path(option_id), Steelman) for option_id, _ in docket)
        out.append((FRAME_CHECK_PATH, FrameCheck))
        out.append((UPDATE_LOG_PATH, ScoreUpdateLog))
        out.append((UPDATED_SCORES_PATH, ScreeningResult))
        return out

    def validate_inputs(self, ctx: StageContext) -> None:
        super().validate_inputs(ctx)
        scores = ctx.workspace.read_artifact(DIVE_SCORES_PATH, ScreeningResult)
        problems = []
        for option in scores.options:
            for relpath, model in (
                (dossier_path(option.option_id), Dossier),
                (verification_path(option.option_id), VerificationReport),
            ):
                if self._try_read(ctx, relpath, model) is None:
                    problems.append(f"- {relpath} is missing or invalid")
        if problems:
            raise StageInputsMissing(
                "stage S7 cannot start; the post-deep-dive scoreboard does not "
                "resolve against the dossiers:\n" + "\n".join(problems)
            )

    # -- execution ------------------------------------------------------------------

    async def execute(self, ctx: StageContext) -> None:
        rubric = ctx.workspace.read_artifact("rubric.yaml", Rubric)
        scores = ctx.workspace.read_artifact(DIVE_SCORES_PATH, ScreeningResult)
        dest_board, adj_board, top3, docket = self._docket(scores, rubric)
        inversions = rank_inversions(dest_board, adj_board)
        for demoted, promoted in inversions:
            ctx.emit(
                f"S7: rank inversion — destination-only ranks '{demoted}' above "
                f"'{promoted}'; the preference-adjusted board reverses them "
                "(priority docket)"
            )
        docket_note = (
            ", ".join(f"{oid} [{trigger}]" for oid, trigger in docket) if docket else "empty"
        )
        ctx.emit(
            f"S7: tournament — {len(top3)} prosecution(s) (top-3 by preference-"
            f"adjusted rank: {', '.join(top3)}), {len(docket)} steelman(s) "
            f"(docket: {docket_note}), frame-check"
        )

        weights = {c.id: c.weight for c in rubric.criteria}
        slot_weight = rubric.preference_slot.weight
        boards_text = render_scoreboards(dest_board, adj_board)
        sensitivity_text = render_sensitivity(
            weight_sensitivity(scores.options, weights, slot_weight),
            preference_sweep(scores.options, weights),
            slot_weight,
        )
        base_inputs = {
            "brief": ctx.workspace.path("brief.md").read_text(encoding="utf-8"),
            "destination": ctx.workspace.path("destination.md").read_text(encoding="utf-8"),
            "rubric": rubric.dump_yaml(),
        }
        records = {o.option_id: o for o in scores.options}
        winner_id = adj_board[0].option_id

        jobs = []
        for option_id in top3:
            if self._try_read(ctx, prosecution_path(option_id), Prosecution) is None:
                jobs.append(self._prosecute(ctx, option_id, base_inputs, records[option_id]))
        for option_id, trigger in docket:
            if self._try_read(ctx, steelman_path(option_id), Steelman) is None:
                jobs.append(
                    self._steelman(ctx, option_id, trigger, base_inputs, boards_text, winner_id)
                )
        if self._try_read(ctx, FRAME_CHECK_PATH, FrameCheck) is None:
            jobs.append(self._frame_check(ctx, base_inputs, boards_text, sensitivity_text))
        if jobs:
            await asyncio.gather(*jobs)

        frame_check = ctx.workspace.read_artifact(FRAME_CHECK_PATH, FrameCheck)
        self._surface_frame_check(ctx, frame_check)
        await self._judge(ctx, rubric, scores)

    # -- prosecutors -------------------------------------------------------------

    async def _prosecute(
        self,
        ctx: StageContext,
        option_id: str,
        base_inputs: dict[str, str],
        record: OptionScreening,
    ) -> None:
        dossier = ctx.workspace.read_artifact(dossier_path(option_id), Dossier)
        verification = ctx.workspace.read_artifact(verification_path(option_id), VerificationReport)
        max_searches = ctx.config.caps.tournament_new_searches
        contract = AgentContract(
            role="prosecutor",
            stage=StageEnum.S7,
            task_objective=(
                f"Prosecute option '{option_id}' — a top-3 finalist. Build the "
                "strongest good-faith case against it from its dossier (and the "
                "verifier's report), and produce the most likely way choosing it "
                "leads to regret."
            ),
            input_artifacts={
                **base_inputs,
                "dossier": dossier.dump_yaml(),
                "verification": verification.dump_yaml(),
                "screening": record.dump_yaml(),
            },
            output_schemas=("prosecution",),
            size_class=SizeClass.M,
            budget_line=(
                "The dossier is your evidence base; at most "
                f"{max_searches} new targeted searches, each closing a named hole "
                "in the case."
            ),
            context=option_id,
        )
        result = await ctx.dispatcher.run_agent(contract)
        prosecution = result.artifacts["prosecution"]
        assert isinstance(prosecution, Prosecution)
        problems = []
        if prosecution.option_id != option_id:
            problems.append(
                f"- prosecution.option_id is '{prosecution.option_id}' but this "
                f"prosecutor was assigned option '{option_id}'"
            )
        known = {c.id for c in dossier.claims}
        unknown = sorted(set(prosecution.supporting_claim_ids) - known)
        if unknown:
            problems.append(
                f"- supporting_claim_ids must reference the dossier's claims; "
                f"unknown ids: {unknown}"
            )
        if problems:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    "the prosecution validates in isolation but does not cohere "
                    "with its assignment:\n" + "\n".join(problems)
                ),
                raw_output=result.raw_text,
            )
        ctx.workspace.write_artifact(prosecution_path(option_id), prosecution)
        ctx.emit(
            f"S7: {option_id} prosecution written "
            f"({len(prosecution.new_evidence)} new evidence item(s))"
        )

    # -- steelmen ------------------------------------------------------------------

    async def _steelman(
        self,
        ctx: StageContext,
        option_id: str,
        trigger: str,
        base_inputs: dict[str, str],
        boards_text: str,
        winner_id: str,
    ) -> None:
        dossier = ctx.workspace.read_artifact(dossier_path(option_id), Dossier)
        winner_dossier = ctx.workspace.read_artifact(dossier_path(winner_id), Dossier)
        reason = (
            "the destination-only board ranks it above where the preference-"
            "adjusted board put it — a rank inversion, the priority docket"
            if trigger == SteelmanTrigger.RANK_INVERSION.value
            else "it is the preference-adjusted runner-up"
        )
        contract = AgentContract(
            role="steelman",
            stage=StageEnum.S7,
            task_objective=(
                f"Steelman option '{option_id}' (trigger: {trigger}) — {reason}. "
                f"Build the strongest honest case that it should win over the "
                f"current leader '{winner_id}' (whose dossier is in your inputs). "
                f"Set trigger: {trigger}."
            ),
            input_artifacts={
                **base_inputs,
                "dossier": dossier.dump_yaml(),
                "winner-dossier": winner_dossier.dump_yaml(),
                "scoreboards": boards_text,
            },
            output_schemas=("steelman",),
            size_class=SizeClass.M,
            budget_line=(
                "Argue from the dossiers and scoreboards; at most "
                f"{ctx.config.caps.tournament_new_searches} new targeted searches."
            ),
            context=option_id,
        )
        result = await ctx.dispatcher.run_agent(contract)
        steelman = result.artifacts["steelman"]
        assert isinstance(steelman, Steelman)
        problems = []
        if steelman.option_id != option_id:
            problems.append(
                f"- steelman.option_id is '{steelman.option_id}' but this steelman "
                f"was assigned option '{option_id}'"
            )
        if steelman.trigger.value != trigger:
            problems.append(
                f"- trigger must be '{trigger}' (the docket's reason for this "
                f"steelman), not '{steelman.trigger.value}'"
            )
        known = {c.id for c in dossier.claims}
        unknown = sorted(set(steelman.supporting_claim_ids) - known)
        if unknown:
            problems.append(
                f"- supporting_claim_ids must reference the option's own dossier "
                f"claims; unknown ids: {unknown}"
            )
        if problems:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    "the steelman validates in isolation but does not cohere with "
                    "its assignment:\n" + "\n".join(problems)
                ),
                raw_output=result.raw_text,
            )
        ctx.workspace.write_artifact(steelman_path(option_id), steelman)
        ctx.emit(f"S7: {option_id} steelman written (trigger: {trigger})")

    # -- the frame-checker -----------------------------------------------------------

    def _scouted_index(self, ctx: StageContext, angle_map: AngleMap) -> tuple[str, str]:
        """(critiques, scouted-options) rendered for the frame-checker: every
        angle's critique verbatim and every option actually scouted — the raw
        material of the missed-options check."""
        critiques: list[str] = []
        scouted: list[str] = []
        for angle in angle_map.angles:
            scouted.append(f"angle {angle.id}:")
            card_set = self._try_read(ctx, cards_path(angle.id), OptionCardSet)
            if card_set is None:
                scouted.append("  (no cards on disk)")
            else:
                scouted.extend(f"  - {card.id}: {card.name}" for card in card_set.cards)
            raw = ctx.workspace.path(critique_path(angle.id))
            if raw.is_file():
                text = raw.read_text(encoding="utf-8")
                critiques.append(f"## critique of angle {angle.id}\n{text}")
        return "\n\n".join(critiques) or "(no critiques on disk)", "\n".join(scouted)

    async def _frame_check(
        self,
        ctx: StageContext,
        base_inputs: dict[str, str],
        boards_text: str,
        sensitivity_text: str,
    ) -> None:
        angle_map = ctx.workspace.read_artifact("angles/map.yaml", AngleMap)
        coverage = ctx.workspace.read_artifact("angles/map-report.md", CoverageReport)
        gate_a = ctx.workspace.read_artifact("gates/gate-a.yaml", GateADecision)
        critiques_text, scouted_text = self._scouted_index(ctx, angle_map)
        removals = (
            "\n".join(f"- {r.angle_id}: {r.reason}" for r in gate_a.removed_angles)
            or "(no angles were removed at Gate A)"
        )
        contract = AgentContract(
            role="frame-checker",
            stage=StageEnum.S7,
            task_objective=(
                "Audit this run's frame: is there a plausible answer to the brief "
                "this map could not have produced? Run your three checks — "
                "consequential Gate-A removals, critiqued-but-never-scouted missed "
                "options, rubric fragility — and emit PASS or a re-divergence "
                "proposal."
            ),
            input_artifacts={
                "brief": base_inputs["brief"],
                "angle-map": angle_map.dump_yaml(),
                "coverage-report": coverage.dump_yaml(),
                "gate-a-decision": f"Angles removed at Gate A (with reasons):\n{removals}",
                "critiques": critiques_text,
                "scouted-options": scouted_text,
                "scoreboards": boards_text,
                "sensitivity": sensitivity_text,
            },
            output_schemas=("frame-check",),
            size_class=SizeClass.L,
            budget_line=(
                "The run's artifacts are the evidence; at most "
                f"{ctx.config.caps.tournament_new_searches} new searches, only to "
                "test whether a suspected gap is real."
            ),
        )
        result = await ctx.dispatcher.run_agent(contract)
        frame_check = result.artifacts["frame-check"]
        assert isinstance(frame_check, FrameCheck)
        proposal = frame_check.proposal
        if proposal is not None and proposal.kind is RedivergenceKind.SCOUT_TASK:
            known = {a.id for a in angle_map.angles}
            if proposal.target_angle_id not in known:
                raise AgentOutputInvalid(
                    contract,
                    errors=(
                        f"- a scout-task proposal must target an existing angle; "
                        f"'{proposal.target_angle_id}' is not in the map "
                        f"(known: {sorted(known)}). Use kind: new-angle for a "
                        "region the map lacks."
                    ),
                    raw_output=result.raw_text,
                )
        ctx.workspace.write_artifact(FRAME_CHECK_PATH, frame_check)

    def _surface_frame_check(self, ctx: StageContext, frame_check: FrameCheck) -> None:
        """Surface, never execute (P7): the proposal is the human's to approve
        at Gate C."""
        if frame_check.verdict is FrameCheckVerdict.PASS:
            ctx.emit("S7: frame-check PASS — no plausible answer lies outside this map")
            return
        proposal = frame_check.proposal
        assert proposal is not None  # schema: gap-found requires a proposal
        target = f" (target angle: {proposal.target_angle_id})" if proposal.target_angle_id else ""
        ctx.emit(
            "S7: frame-check found a credible gap — re-divergence proposal "
            f"[{proposal.kind.value}]{target}, estimated cost "
            f"{proposal.estimated_cost_units} unit(s): {proposal.description}\n"
            "S7: the proposal is NOT auto-executed — review tournament/"
            "frame-check.md and approve or decline it at Gate C"
        )

    # -- the judge ----------------------------------------------------------------------

    async def _judge(self, ctx: StageContext, rubric: Rubric, scores: ScreeningResult) -> None:
        log = self._try_read(ctx, UPDATE_LOG_PATH, ScoreUpdateLog)
        if log is None:
            log = await self._dispatch_judge(ctx, rubric, scores)
            ctx.workspace.write_artifact(UPDATE_LOG_PATH, log)
        if log.updates:
            for update in log.updates:
                ctx.emit(
                    f"S7: judge — {update.option_id} '{update.criterion_id}' "
                    f"{update.old_score:g} -> {update.new_score:g} "
                    f"(cause: {update.source_artifact})"
                )
        else:
            ctx.emit("S7: judge — no decisive tournament material; the scores stand")
        updated = apply_score_updates(scores, list(log.updates), rubric)
        ctx.workspace.write_artifact(UPDATED_SCORES_PATH, updated)
        ctx.emit(
            f"S7: post-tournament scores written to {UPDATED_SCORES_PATH} "
            f"({len(log.updates)} update(s) applied) — tournament settled"
        )

    async def _dispatch_judge(
        self, ctx: StageContext, rubric: Rubric, scores: ScreeningResult
    ) -> ScoreUpdateLog:
        _, adj_board, top3, docket = self._docket(scores, rubric)
        prosecutions = "\n\n".join(
            f"## prosecution of {oid} ({prosecution_path(oid)})\n"
            + ctx.workspace.read_artifact(prosecution_path(oid), Prosecution).dump_yaml()
            for oid in top3
        )
        steelmen = (
            "\n\n".join(
                f"## steelman of {oid} ({steelman_path(oid)})\n"
                + ctx.workspace.read_artifact(steelman_path(oid), Steelman).dump_yaml()
                for oid, _ in docket
            )
            or "(the docket was empty — no steelmen this run)"
        )
        frame_check = ctx.workspace.read_artifact(FRAME_CHECK_PATH, FrameCheck)
        contract = AgentContract(
            role="judge",
            stage=StageEnum.S7,
            task_objective=(
                "Adjudicate the tournament: update criterion scores ONLY where the "
                "material in your inputs is decisive, logging every change with its "
                "cause and source artifact. An empty update list is a legitimate "
                "verdict."
            ),
            input_artifacts={
                "rubric": rubric.dump_yaml(),
                "scores": scores.dump_yaml(),
                "prosecutions": prosecutions,
                "steelmen": steelmen,
                "frame-check": f"({FRAME_CHECK_PATH})\n" + frame_check.dump_yaml(),
            },
            output_schemas=("score-update-log",),
            size_class=SizeClass.L,
            budget_line=(
                "No research: adjudicate the tournament material as presented — "
                "one update round (design §12)."
            ),
        )
        result = await ctx.dispatcher.run_agent(contract)
        log = result.artifacts["score-update-log"]
        assert isinstance(log, ScoreUpdateLog)
        self._check_updates(contract, log, scores, rubric, result.raw_text)
        return log

    def _check_updates(
        self,
        contract: AgentContract,
        log: ScoreUpdateLog,
        scores: ScreeningResult,
        rubric: Rubric,
        raw_text: str,
    ) -> None:
        """Cross-artifact integrity: every update names a real option and rubric
        criterion, amends the score as it currently stands, and stays off the
        preference slot."""
        records = {o.option_id: o for o in scores.options}
        criterion_ids = {c.id for c in rubric.criteria}
        problems: list[str] = []
        seen: set[tuple[str, str]] = set()
        for update in log.updates:
            key = (update.option_id, update.criterion_id)
            if key in seen:
                problems.append(
                    f"- ({update.option_id}, {update.criterion_id}) is updated twice; "
                    "one update round means one change per score (design §12)"
                )
            seen.add(key)
            record = records.get(update.option_id)
            if record is None:
                problems.append(
                    f"- option '{update.option_id}' is not on the post-deep-dive scoreboard"
                )
                continue
            if update.criterion_id not in criterion_ids:
                problems.append(
                    f"- criterion '{update.criterion_id}' is not in the rubric "
                    "(the preference slot is never the judge's to touch)"
                )
                continue
            current = next(
                cs.score for cs in record.criterion_scores if cs.criterion_id == update.criterion_id
            )
            if abs(current - update.old_score) > 1e-6:
                problems.append(
                    f"- old_score for ({update.option_id}, {update.criterion_id}) is "
                    f"{update.old_score:g}, but the current score is {current:g} — "
                    "amend the ledger as it stands"
                )
        if problems:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    "the score-update log validates in isolation but does not "
                    "cohere with the scoreboard:\n" + "\n".join(problems)
                ),
                raw_output=raw_text,
            )
