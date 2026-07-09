"""S6 — Deep dives (design §5/S6).

One analyst per finalist, all finalists in parallel. Each analyst works in
ROUNDS: research → update dossier → the screener re-scores the option against
the rubric from the dossier (the same S5 scoring machinery — integrity check +
code-recomputed aggregates — pointed at the dossier instead of the card). The
three-clause stopping rule is pure code in `depth.py`: stop when the weighted
score moved < `caps.deep_dive_delta_score_stop` over the last round AND no
remaining low-confidence claim is load-bearing; or the per-option cap
(`config.deep_dive_unit_cap`, 1 round = 1 unit) hits — then the dossier is
stamped BUDGET-CAPPED with its open questions listed.

After each option stops, the verification pass: an independent verifier
adjudicates ALL effective load-bearing claims plus a seeded 20% of the rest;
contradicted claims land in ledger/contradictions.md and trigger exactly ONE
targeted analyst revision, then a final re-score.

Idempotency rides `dossiers/{option}-rounds.yaml` (the code-owned round log):
within a round the write order is dossier → re-score → one atomic log append,
so a recorded round is never re-dispatched, and a dossier "ahead" of the log
(crash between the two writes) re-runs only the re-score. The verification
record in the log settles the sampling and the revision leg the same way.
`dossiers/scores.yaml` — every finalist's final re-score, S7's scoreboard
input — is written last and marks the stage settled.
"""

from __future__ import annotations

import asyncio

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import AgentContract, AgentOutputInvalid
from deeper.config import SizeClass
from deeper.contradictions import append_contradictions
from deeper.schemas import (
    Brief,
    ContradictionEntry,
    ContradictionStatement,
    DeepDiveRound,
    DeepDiveRoundLog,
    DeepDiveStatus,
    DeepDiveVerificationRecord,
    DestinationModel,
    Dossier,
    OptionCard,
    OptionCardSet,
    OptionScreening,
    Preferences,
    Rubric,
    ScreeningResult,
    Shortlist,
    Verdict,
    VerificationReport,
)
from deeper.schemas import Stage as StageEnum
from deeper.workspace import WorkspaceError

from .base import StageBase, StageContext, StageInputsMissing
from .depth import (
    criterion_moves,
    derive_open_questions,
    effective_load_bearing,
    low_conf_load_bearing,
    should_stop,
    verifier_sample,
    weighted_delta,
)
from .s3_scouting import cards_path
from .shortlist import recompute_aggregates, verify_screening

SCORES_PATH = "dossiers/scores.yaml"


def dossier_path(option_id: str) -> str:
    return f"dossiers/{option_id}.md"


def verification_path(option_id: str) -> str:
    return f"dossiers/{option_id}-verification.md"


def rounds_path(option_id: str) -> str:
    return f"dossiers/{option_id}-rounds.yaml"


class _Finalist:
    """One finalist's static context: its card and its S5 baseline record."""

    def __init__(self, option_id: str, card: OptionCard, baseline: OptionScreening) -> None:
        self.option_id = option_id
        self.card = card
        self.baseline = baseline


class DeepDiveStage(StageBase):
    stage = StageEnum.S6
    required_inputs = (
        ("brief.md", Brief),
        ("destination.md", DestinationModel),
        ("preferences.yaml", Preferences),
        ("rubric.yaml", Rubric),
        ("screening/scores.yaml", ScreeningResult),
        ("screening/shortlist.md", Shortlist),
    )

    @staticmethod
    def _try_read(ctx: StageContext, relpath: str, model: type):
        try:
            return ctx.workspace.read_artifact(relpath, model)
        except (WorkspaceError, ValidationError, yaml.YAMLError):
            return None

    # -- engine protocol ------------------------------------------------------

    def outputs(self, ctx: StageContext):
        shortlist = self._try_read(ctx, "screening/shortlist.md", Shortlist)
        if shortlist is None:
            return []
        out: list[tuple[str, type]] = []
        for option_id in shortlist.finalist_ids:
            out.append((dossier_path(option_id), Dossier))
            out.append((verification_path(option_id), VerificationReport))
            out.append((rounds_path(option_id), DeepDiveRoundLog))
        out.append((SCORES_PATH, ScreeningResult))
        return out

    def is_complete(self, ctx: StageContext) -> bool:
        """Complete when every declared output validates AND every finalist's
        round log is settled: terminal status, verification recorded, and the
        one targeted revision (when contradictions triggered it) done."""
        if not super().is_complete(ctx):
            return False
        shortlist = self._try_read(ctx, "screening/shortlist.md", Shortlist)
        assert shortlist is not None  # super() validated it
        for option_id in shortlist.finalist_ids:
            log = self._try_read(ctx, rounds_path(option_id), DeepDiveRoundLog)
            if log is None or log.status is DeepDiveStatus.IN_PROGRESS:
                return False
            if log.verification is None:
                return False
            if log.verification.contradicted_claim_ids and not log.verification.revision_completed:
                return False
        return True

    def validate_inputs(self, ctx: StageContext) -> None:
        super().validate_inputs(ctx)
        self._finalists(ctx)  # raises StageInputsMissing on any broken linkage

    def _finalists(self, ctx: StageContext) -> list[_Finalist]:
        """Resolve every finalist to its S5 record and its option card —
        cross-artifact referential integrity is an orchestrator concern."""
        screening = ctx.workspace.read_artifact("screening/scores.yaml", ScreeningResult)
        shortlist = ctx.workspace.read_artifact("screening/shortlist.md", Shortlist)
        baselines = {o.option_id: o for o in screening.options}
        problems: list[str] = []
        finalists: list[_Finalist] = []
        card_sets: dict[str, OptionCardSet] = {}
        for option_id in shortlist.finalist_ids:
            baseline = baselines.get(option_id)
            if baseline is None:
                problems.append(f"- finalist '{option_id}' has no record in screening/scores.yaml")
                continue
            relpath = cards_path(baseline.angle_id)
            if baseline.angle_id not in card_sets:
                card_set = self._try_read(ctx, relpath, OptionCardSet)
                if card_set is None:
                    problems.append(f"- finalist '{option_id}': {relpath} is missing or invalid")
                    continue
                card_sets[baseline.angle_id] = card_set
            card = next((c for c in card_sets[baseline.angle_id].cards if c.id == option_id), None)
            if card is None:
                problems.append(f"- finalist '{option_id}' has no option card in {relpath}")
                continue
            finalists.append(_Finalist(option_id, card, baseline))
        if problems:
            raise StageInputsMissing(
                "stage S6 cannot start; the shortlist does not resolve against its "
                "upstream artifacts:\n" + "\n".join(problems)
            )
        return finalists

    # -- execution --------------------------------------------------------------

    async def execute(self, ctx: StageContext) -> None:
        rubric = ctx.workspace.read_artifact("rubric.yaml", Rubric)
        finalists = self._finalists(ctx)
        base_inputs = {
            "brief": ctx.workspace.path("brief.md").read_text(encoding="utf-8"),
            "destination": ctx.workspace.path("destination.md").read_text(encoding="utf-8"),
            "rubric": rubric.dump_yaml(),
        }
        cap = ctx.config.deep_dive_unit_cap
        ctx.emit(
            f"S6: deep-diving {len(finalists)} finalists in parallel "
            f"(per-option cap {cap} round(s), stability threshold "
            f"{ctx.config.caps.deep_dive_delta_score_stop:g})…"
        )
        finals = await asyncio.gather(
            *(self._deep_dive(ctx, f, rubric, base_inputs) for f in finalists)
        )
        merged = ScreeningResult(options=list(finals))
        ctx.workspace.write_artifact(SCORES_PATH, merged)
        ctx.emit(
            f"S6: post-deep-dive scores written to {SCORES_PATH} "
            f"({len(finals)} finalists) — dossiers + verification reports in dossiers/"
        )

    # -- one finalist's loop ------------------------------------------------------

    async def _deep_dive(
        self,
        ctx: StageContext,
        finalist: _Finalist,
        rubric: Rubric,
        base_inputs: dict[str, str],
    ) -> OptionScreening:
        option_id = finalist.option_id
        log = self._try_read(ctx, rounds_path(option_id), DeepDiveRoundLog)
        if log is None:
            log = DeepDiveRoundLog(option_id=option_id, baseline=finalist.baseline)
        log = await self._round_loop(ctx, finalist, rubric, base_inputs, log)
        log = await self._verification_pass(ctx, finalist, rubric, base_inputs, log)
        return log.final_screening or log.rounds[-1].screening

    async def _round_loop(
        self,
        ctx: StageContext,
        finalist: _Finalist,
        rubric: Rubric,
        base_inputs: dict[str, str],
        log: DeepDiveRoundLog,
    ) -> DeepDiveRoundLog:
        option_id = finalist.option_id
        cap = ctx.config.deep_dive_unit_cap
        delta_stop = ctx.config.caps.deep_dive_delta_score_stop
        while log.status is DeepDiveStatus.IN_PROGRESS:
            r = len(log.rounds) + 1
            dossier = self._try_read(ctx, dossier_path(option_id), Dossier)
            if dossier is not None and dossier.rounds_completed >= r:
                # Crash landed between the dossier write and the log append:
                # the research is on disk; only the re-score is owed.
                ctx.emit(f"S6: {option_id} round {r} dossier already on disk — re-scoring only")
            else:
                dossier = await self._dispatch_analyst(ctx, finalist, rubric, base_inputs, log, r)
                ctx.workspace.write_artifact(dossier_path(option_id), dossier)
                ctx.emit(
                    f"S6: {option_id} round {r} dossier written ({len(dossier.claims)} claims)"
                )
            rescore = await self._rescore(
                ctx, finalist, rubric, base_inputs, dossier, context=f"{option_id}-r{r}"
            )
            prev = log.rounds[-1].screening if log.rounds else log.baseline
            delta = weighted_delta(prev, rescore)
            effective, warnings = effective_load_bearing(dossier, criterion_moves(prev, rescore))
            for warning in warnings:
                ctx.emit(f"S6: {option_id} load-bearing cross-check — {warning}")
            low_conf = low_conf_load_bearing(dossier, effective)
            stop, capped = should_stop(delta, bool(low_conf), r, cap, delta_stop=delta_stop)
            if capped:
                dossier = dossier.model_copy(
                    update={
                        "budget_capped": True,
                        "open_questions": dossier.open_questions
                        or derive_open_questions(dossier, low_conf),
                    }
                )
                ctx.workspace.write_artifact(dossier_path(option_id), dossier)
            entry = DeepDiveRound(
                round=r, screening=rescore, delta=delta, low_confidence_load_bearing=low_conf
            )
            status = (
                DeepDiveStatus.CONVERGED
                if stop and not capped
                else DeepDiveStatus.BUDGET_CAPPED
                if capped
                else DeepDiveStatus.IN_PROGRESS
            )
            log = log.model_copy(update={"rounds": [*log.rounds, entry], "status": status})
            ctx.workspace.write_artifact(rounds_path(option_id), log)
            blockers = f", low-confidence load-bearing: {low_conf}" if low_conf else ""
            ctx.emit(
                f"S6: {option_id} round {r} re-score "
                f"{rescore.weighted_point:g} (Δ{delta:g}{blockers})"
            )
            if status is DeepDiveStatus.CONVERGED:
                ctx.emit(
                    f"S6: {option_id} converged after {r} round(s) — Δ{delta:g} < "
                    f"{delta_stop:g} and no low-confidence load-bearing claims"
                )
            elif status is DeepDiveStatus.BUDGET_CAPPED:
                ctx.emit(
                    f"S6: {option_id} BUDGET-CAPPED after {r} round(s) — "
                    f"{len(dossier.open_questions)} open question(s) listed in the dossier"
                )
        return log

    async def _verification_pass(
        self,
        ctx: StageContext,
        finalist: _Finalist,
        rubric: Rubric,
        base_inputs: dict[str, str],
        log: DeepDiveRoundLog,
    ) -> DeepDiveRoundLog:
        option_id = finalist.option_id
        dossier = ctx.workspace.read_artifact(dossier_path(option_id), Dossier)
        if log.verification is None:
            prev = log.rounds[-2].screening if len(log.rounds) > 1 else log.baseline
            effective, _ = effective_load_bearing(
                dossier, criterion_moves(prev, log.rounds[-1].screening)
            )
            sampled, n_lb, n_other = verifier_sample(dossier, effective)
            report = await self._dispatch_verifier(ctx, finalist, dossier, sampled, n_lb, n_other)
            contradicted = sorted(
                res.claim_id for res in report.results if res.verdict is Verdict.CONTRADICTED
            )
            if contradicted:
                self._ledger_contradictions(ctx, option_id, dossier, report, contradicted)
            ctx.workspace.write_artifact(verification_path(option_id), report)
            log = log.model_copy(
                update={
                    "verification": DeepDiveVerificationRecord(
                        sampled_claim_ids=sampled, contradicted_claim_ids=contradicted
                    )
                }
            )
            ctx.workspace.write_artifact(rounds_path(option_id), log)
            ctx.emit(
                f"S6: {option_id} verification — {len(report.results)} claims sampled "
                f"({n_lb} load-bearing + {n_other} of the rest), "
                f"pass rate {report.pass_rate:.0%}"
            )
        record = log.verification
        assert record is not None
        if record.contradicted_claim_ids and not record.revision_completed:
            ctx.emit(
                f"S6: {option_id} contradicted claim(s) "
                f"{record.contradicted_claim_ids} — one targeted analyst revision"
            )
            report = ctx.workspace.read_artifact(verification_path(option_id), VerificationReport)
            revised = await self._dispatch_revision(
                ctx, finalist, rubric, base_inputs, dossier, report, record.contradicted_claim_ids
            )
            if log.status is DeepDiveStatus.BUDGET_CAPPED:
                # The stamp is code's, and the revision must not launder it away.
                revised = revised.model_copy(
                    update={
                        "budget_capped": True,
                        "open_questions": revised.open_questions or dossier.open_questions,
                    }
                )
            ctx.workspace.write_artifact(dossier_path(option_id), revised)
            final = await self._rescore(
                ctx, finalist, rubric, base_inputs, revised, context=f"{option_id}-final"
            )
            log = log.model_copy(
                update={
                    "verification": record.model_copy(update={"revision_completed": True}),
                    "final_screening": final,
                }
            )
            ctx.workspace.write_artifact(rounds_path(option_id), log)
            before = log.rounds[-1].screening.weighted_point
            ctx.emit(
                f"S6: {option_id} final re-score after revision: "
                f"{final.weighted_point:g} (was {before:g})"
            )
        return log

    # -- agent dispatch -------------------------------------------------------------

    async def _dispatch_analyst(
        self,
        ctx: StageContext,
        finalist: _Finalist,
        rubric: Rubric,
        base_inputs: dict[str, str],
        log: DeepDiveRoundLog,
        r: int,
    ) -> Dossier:
        cap = ctx.config.deep_dive_unit_cap
        option_id = finalist.option_id
        inputs = {
            **base_inputs,
            "card": finalist.card.dump_yaml(),
            "screening": finalist.baseline.dump_yaml(),
        }
        if r == 1:
            task = (
                f"# ROUND 1 of at most {cap}\nBuild the full dossier for option "
                f"'{option_id}'. Set rounds_completed: 1."
            )
        else:
            prev_dossier = ctx.workspace.read_artifact(dossier_path(option_id), Dossier)
            inputs["dossier"] = prev_dossier.dump_yaml()
            inputs["rescore"] = log.rounds[-1].screening.dump_yaml()
            blockers = log.rounds[-1].low_confidence_load_bearing
            task = (
                f"# ROUND {r} of at most {cap}\nDeepen your previous dossier for "
                f"option '{option_id}' (in your inputs, with the latest re-score). "
                "Attack the remaining low-confidence load-bearing claims"
                + (f" — {blockers} —" if blockers else "")
                + " and the widest-band criteria first. Re-emit the COMPLETE dossier "
                f"with rounds_completed: {r}."
            )
        if r == cap:
            task += (
                "\nThis is the FINAL budgeted round: whatever you cannot resolve "
                "now, list carefully in open_questions — an unstable score gets "
                "this dossier stamped BUDGET-CAPPED and your open questions are "
                "what the report shows as unfinished depth."
            )
        contract = AgentContract(
            role="analyst",
            stage=StageEnum.S6,
            task_objective=task,
            input_artifacts=inputs,
            output_schemas=("dossier",),
            size_class=SizeClass.M,
            budget_line=(
                f"~1 budget unit this round (round {r} of at most {cap} for this "
                "option). At least one disconfirming-evidence search per rubric "
                "criterion; spend the rest where the re-score bands are widest."
            ),
            context=f"{option_id}-r{r}",
        )
        result = await ctx.dispatcher.run_agent(contract)
        dossier = result.artifacts["dossier"]
        assert isinstance(dossier, Dossier)
        self._check_dossier(contract, dossier, result.raw_text, option_id, rubric, expected_round=r)
        return dossier

    async def _dispatch_revision(
        self,
        ctx: StageContext,
        finalist: _Finalist,
        rubric: Rubric,
        base_inputs: dict[str, str],
        dossier: Dossier,
        report: VerificationReport,
        contradicted: list[str],
    ) -> Dossier:
        option_id = finalist.option_id
        contract = AgentContract(
            role="analyst",
            stage=StageEnum.S6,
            task_objective=(
                "# REVISION MODE — targeted, scoped\nAn independent verifier "
                f"CONTRADICTED these claims of option '{option_id}': {contradicted}. "
                "Its report (with evidence quotes) is in your inputs. Correct or "
                "remove exactly those claims and the section text resting on them; "
                "change nothing else; keep rounds_completed at "
                f"{dossier.rounds_completed}. Re-emit the COMPLETE dossier. This is "
                "the one targeted revision — no new research directions."
            ),
            input_artifacts={
                **base_inputs,
                "card": finalist.card.dump_yaml(),
                "dossier": dossier.dump_yaml(),
                "verification": report.dump_yaml(),
            },
            output_schemas=("dossier",),
            size_class=SizeClass.M,
            budget_line=(
                "A fraction of a unit: re-fetch/replace evidence for the "
                f"{len(contradicted)} contradicted claim(s) only."
            ),
            context=f"{option_id}-vrev",
        )
        result = await ctx.dispatcher.run_agent(contract)
        revised = result.artifacts["dossier"]
        assert isinstance(revised, Dossier)
        self._check_dossier(
            contract,
            revised,
            result.raw_text,
            option_id,
            rubric,
            expected_round=dossier.rounds_completed,
        )
        return revised

    def _check_dossier(
        self,
        contract: AgentContract,
        dossier: Dossier,
        raw_text: str,
        option_id: str,
        rubric: Rubric,
        *,
        expected_round: int,
    ) -> None:
        """Cross-artifact integrity: right option, right round, and sections
        keyed exactly by the rubric's criteria (design: structured BY RUBRIC
        CRITERION — the re-score depends on it)."""
        problems: list[str] = []
        if dossier.option_id != option_id:
            problems.append(
                f"- dossier.option_id is '{dossier.option_id}' but this analyst "
                f"was assigned option '{option_id}'"
            )
        if dossier.rounds_completed != expected_round:
            problems.append(
                f"- rounds_completed is {dossier.rounds_completed}; this dispatch "
                f"is round {expected_round}, so it must be {expected_round}"
            )
        rubric_ids = sorted(c.id for c in rubric.criteria)
        section_ids = sorted(dossier.criterion_sections)
        if section_ids != rubric_ids:
            problems.append(
                f"- criterion_sections must be keyed by exactly the rubric's "
                f"criterion ids; expected {rubric_ids}, got {section_ids}"
            )
        if problems:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    "the dossier validates in isolation but does not cohere with "
                    "its assignment:\n" + "\n".join(problems)
                ),
                raw_output=raw_text,
            )

    async def _rescore(
        self,
        ctx: StageContext,
        finalist: _Finalist,
        rubric: Rubric,
        base_inputs: dict[str, str],
        dossier: Dossier,
        *,
        context: str,
    ) -> OptionScreening:
        """The S5 scoring machinery pointed at the dossier: screener dispatch,
        per-batch integrity check, code-recomputed aggregates."""
        option_id = finalist.option_id
        contract = AgentContract(
            role="screener",
            stage=StageEnum.S6,
            task_objective=(
                "# DEEP-DIVE RE-SCORE\nRe-score exactly one finalist, option "
                f"'{option_id}', from its deep-dive dossier (in your inputs, in "
                "place of cards) — see your 'Deep-dive re-score mode' section. "
                "Emit a screening-result containing exactly this one option."
            ),
            input_artifacts={
                **base_inputs,
                "dossier": dossier.dump_yaml(),
                # The ONLY role allowed preferences (P9); the slot must be re-scored
                # every round so both scoreboards stay current for S7.
                "preferences": ctx.workspace.path("preferences.yaml").read_text(encoding="utf-8"),
            },
            output_schemas=("screening-result",),
            size_class=SizeClass.M,
            budget_line=(
                "No research: the dossier is the evidence base and judging it is "
                "the point. Score every rubric criterion and the preference slot."
            ),
            context=context,
        )
        result = await ctx.dispatcher.run_agent(contract)
        screening = result.artifacts["screening-result"]
        assert isinstance(screening, ScreeningResult)
        own = [o for o in screening.options if o.option_id == option_id]
        if len(screening.options) > len(own):
            # The mock full-scenario fixture fallback (and an over-eager live
            # screener) may score other options; their own re-scores own them.
            ctx.emit(
                f"S6: {option_id} re-score included {len(screening.options) - len(own)} "
                "other option(s) — dropped"
            )
        if not own:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    f"the re-score must contain exactly option '{option_id}'; it scored none of it"
                ),
                raw_output=result.raw_text,
            )
        batch = ScreeningResult(options=own, notes=screening.notes)
        card_set = OptionCardSet(angle_id=finalist.baseline.angle_id, cards=[finalist.card])
        problems = verify_screening(batch, rubric, [card_set])
        if problems:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    "the re-score validates in isolation but does not cohere with "
                    "the rubric and the option:\n" + "\n".join(problems)
                ),
                raw_output=result.raw_text,
            )
        batch, drift = recompute_aggregates(batch, rubric, label="S6")
        for message in drift:
            ctx.emit(message)
        return batch.options[0]

    async def _dispatch_verifier(
        self,
        ctx: StageContext,
        finalist: _Finalist,
        dossier: Dossier,
        sampled: list[str],
        n_lb: int,
        n_other: int,
    ) -> VerificationReport:
        option_id = finalist.option_id
        by_id = {c.id: c for c in dossier.claims}
        lines = [
            f"- {claim_id} ({'load-bearing' if i < n_lb else 'sampled'}): "
            f"{by_id[claim_id].source.url}"
            for i, claim_id in enumerate(sampled)
        ]
        contract = AgentContract(
            role="verifier",
            stage=StageEnum.S6,
            task_objective=(
                f"Adjudicate EXACTLY these {len(sampled)} sampled claims of option "
                f"'{option_id}' — all {n_lb} load-bearing claim(s) plus {n_other} "
                "randomly sampled other(s):\n" + "\n".join(lines) + "\n"
                f"Set sampled_load_bearing_count: {n_lb} and "
                f"sampled_other_count: {n_other}."
            ),
            input_artifacts={"dossier": dossier.dump_yaml()},
            output_schemas=("verification-report",),
            size_class=SizeClass.M,
            budget_line=(
                "One source re-fetch per sampled claim (sources/ cache first, live "
                "only on a miss) and at most one corroborating lookup for a "
                "vanished source."
            ),
            context=option_id,
        )
        result = await ctx.dispatcher.run_agent(contract)
        report = result.artifacts["verification-report"]
        assert isinstance(report, VerificationReport)
        problems: list[str] = []
        if report.option_id != option_id:
            problems.append(
                f"- verification-report.option_id is '{report.option_id}' but this "
                f"verifier was assigned option '{option_id}'"
            )
        adjudicated = sorted(res.claim_id for res in report.results)
        if adjudicated != sorted(sampled):
            problems.append(
                f"- adjudicate exactly the sampled claims; expected "
                f"{sorted(sampled)}, got {adjudicated}"
            )
        if (report.sampled_load_bearing_count, report.sampled_other_count) != (n_lb, n_other):
            problems.append(
                f"- sampled counts must be load-bearing {n_lb} / other {n_other}; "
                f"the report says {report.sampled_load_bearing_count} / "
                f"{report.sampled_other_count}"
            )
        if problems:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    "the verification report validates in isolation but does not "
                    "match its sampling assignment:\n" + "\n".join(problems)
                ),
                raw_output=result.raw_text,
            )
        return report

    # -- the contradiction ledger -----------------------------------------------------

    def _ledger_contradictions(
        self,
        ctx: StageContext,
        option_id: str,
        dossier: Dossier,
        report: VerificationReport,
        contradicted: list[str],
    ) -> None:
        """Every contradicted claim is a cross-artifact factual disagreement:
        dossier vs source. Entry ids are deterministic, so a resume replay of
        the same detection appends nothing (design §6)."""
        by_id = {c.id: c for c in dossier.claims}
        verdicts = {res.claim_id: res for res in report.results}
        entries = [
            ContradictionEntry(
                id=f"{option_id}-{claim_id}",
                statement_a=ContradictionStatement(
                    artifact=dossier_path(option_id), statement=by_id[claim_id].text
                ),
                statement_b=ContradictionStatement(
                    artifact=verification_path(option_id),
                    statement=(
                        verdicts[claim_id].evidence_quote
                        or verdicts[claim_id].note
                        or f"the re-fetched source ({by_id[claim_id].source.url}) "
                        "contradicts the claim"
                    ),
                ),
                detected_by="verifier",
            )
            for claim_id in contradicted
        ]
        append_contradictions(ctx.workspace, entries)
        ctx.emit(
            f"S6: {option_id} — {len(entries)} contradiction(s) appended to "
            "ledger/contradictions.md"
        )
