"""Gate C loop actions: preference feedback and evidence challenges (design §5).

Both are bounded, typed loops rather than open-ended chat (§5 Gate C): each has
a known cost and a defined effect on the artifacts.

- **Preference feedback** — one screener dispatch converts the human's
  structured reactions into preference-slot adjustments; CODE applies them
  (only the slot — criterion-score drift is discarded, the quarantine by
  construction) and re-scores both scoreboards via `recompute_aggregates`.
  Free in research terms: no new evidence is gathered.
- **Evidence challenge** — "I don't believe claim X" fires one scoped verifier
  task per challenge. The verdict lands in gates/challenge-*.yaml; a
  contradicted claim is entered in the contradiction ledger. Verdicts never
  move scores — a re-score is the human's next loop, taken deliberately.

Validation is up-front and collective (the Gate A pattern): a decision that
cannot be applied is refused whole, consumes no iteration, and dispatches
nothing.
"""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import AgentContract, AgentOutputInvalid
from deeper.config import RunConfig, SizeClass
from deeper.contradictions import append_contradictions
from deeper.schemas import (
    ContenderReaction,
    ContradictionEntry,
    ContradictionStatement,
    Dossier,
    EvidenceChallenge,
    FrameCheck,
    GateCDecision,
    Rubric,
    RunState,
    ScreeningResult,
    Stage,
    Verdict,
    VerificationReport,
)
from deeper.sensitivity import dual_scoreboards, render_scoreboards
from deeper.stages import StageContext
from deeper.stages.s6_deepdive import SCORES_PATH as DIVE_SCORES_PATH
from deeper.stages.s6_deepdive import dossier_path
from deeper.stages.s7_tournament import FRAME_CHECK_PATH, UPDATED_SCORES_PATH
from deeper.stages.shortlist import recompute_aggregates
from deeper.workspace import Workspace, WorkspaceError


def challenge_path(iteration: int, option_id: str, claim_id: str) -> str:
    return f"gates/challenge-{iteration}-{option_id}-{claim_id}.yaml"


def _try_read(workspace: Workspace, relpath: str, model: type):
    try:
        return workspace.read_artifact(relpath, model)
    except (WorkspaceError, ValidationError, yaml.YAMLError):
        return None


def validate_gate_c_decision(
    workspace: Workspace, decision: GateCDecision, state: RunState, config: RunConfig
) -> list[str]:
    """Every referential/cap problem at once (Gate A style); empty = applicable.
    A refused decision consumes no iteration and dispatches nothing."""
    problems: list[str] = []
    scores = _try_read(workspace, UPDATED_SCORES_PATH, ScreeningResult)
    if scores is None:
        problems.append(f"{UPDATED_SCORES_PATH} is missing or invalid — rerun S7 first")
        return problems
    on_board = {o.option_id for o in scores.options}
    for reaction in decision.preference_feedback:
        if reaction.option_id not in on_board:
            problems.append(
                f"preference_feedback: no contender '{reaction.option_id}' on the "
                f"tournament scoreboard (contenders: {sorted(on_board)})"
            )
    for challenge in decision.evidence_challenges:
        if challenge.option_id not in on_board:
            problems.append(
                f"evidence_challenges: no contender '{challenge.option_id}' on the "
                f"tournament scoreboard (contenders: {sorted(on_board)})"
            )
            continue
        dossier = _try_read(workspace, dossier_path(challenge.option_id), Dossier)
        if dossier is None:
            problems.append(
                f"evidence_challenges: {dossier_path(challenge.option_id)} is missing or invalid"
            )
            continue
        known = {c.id for c in dossier.claims}
        if challenge.claim_id not in known:
            problems.append(
                f"evidence_challenges: dossier '{challenge.option_id}' has no claim "
                f"'{challenge.claim_id}' (its claims: {sorted(known)})"
            )
    if decision.accept_redivergence:
        if state.redivergence_runs >= config.caps.max_redivergence_loops:
            problems.append(
                f"accept_redivergence: this run's {config.caps.max_redivergence_loops} "
                "re-divergence mini-loop(s) are used (design §12: 1 mini-loop per run)"
            )
        else:
            frame_check = _try_read(workspace, FRAME_CHECK_PATH, FrameCheck)
            if frame_check is None or frame_check.proposal is None:
                problems.append(
                    "accept_redivergence: tournament/frame-check.md carries no "
                    "re-divergence proposal — there is nothing to accept"
                )
    return problems


# -- preference feedback ---------------------------------------------------------------


async def apply_preference_feedback(ctx: StageContext, reactions: list[ContenderReaction]) -> None:
    """One screener dispatch converts reactions into preference-slot
    adjustments; code applies them to BOTH score files (tournament/scores.yaml
    is S8's input; dossiers/scores.yaml is S7's, so a later mini-loop rerun
    keeps the feedback) and recomputes both scoreboards."""
    ws = ctx.workspace
    rubric = ws.read_artifact("rubric.yaml", Rubric)
    scores = ws.read_artifact(UPDATED_SCORES_PATH, ScreeningResult)
    reactions_yaml = yaml.safe_dump(
        [r.model_dump(mode="json") for r in reactions], sort_keys=False, allow_unicode=True
    )
    contract = AgentContract(
        role="screener",
        stage=Stage.S7,
        task_objective=(
            "# GATE-C PREFERENCE-FEEDBACK MODE\n"
            "The human reviewed the contenders and gave the structured reactions in "
            "your inputs. Re-read preferences.yaml through them and re-emit "
            "screening records with ONLY the preference slot moved — see your "
            "'Gate-C preference-feedback mode' section. Include a record for every "
            "option whose slot the reactions move (at minimum every option with a "
            "reaction); echo criterion scores unchanged."
        ),
        input_artifacts={
            "rubric": rubric.dump_yaml(),
            "preferences": ws.path("preferences.yaml").read_text(encoding="utf-8"),
            "scores": scores.dump_yaml(),
            "reactions": reactions_yaml,
        },
        output_schemas=("screening-result",),
        size_class=SizeClass.M,
        budget_line="No new research — a free re-score of the preference slot only.",
        context="gate-c-feedback",
    )
    result = await ctx.dispatcher.run_agent(contract)
    proposed = result.artifacts["screening-result"]
    assert isinstance(proposed, ScreeningResult)
    by_id = {o.option_id: o for o in proposed.options}
    current = {o.option_id: o for o in scores.options}
    problems = []
    for reaction in reactions:
        record = by_id.get(reaction.option_id)
        if record is None or record.preference_score is None:
            problems.append(
                f"- the reaction on '{reaction.option_id}' requires a re-emitted "
                "record with a preference_score; none came back"
            )
    unknown = sorted(set(by_id) - set(current))
    if unknown:
        problems.append(f"- these records match no contender on the scoreboard: {unknown}")
    if problems:
        raise AgentOutputInvalid(
            contract,
            errors=(
                "the re-score validates in isolation but does not cover the "
                "human's reactions:\n" + "\n".join(problems)
            ),
            raw_output=result.raw_text,
        )
    for option_id, record in by_id.items():
        old = current[option_id]
        if [(cs.criterion_id, cs.score) for cs in record.criterion_scores] != [
            (cs.criterion_id, cs.score) for cs in old.criterion_scores
        ]:
            ctx.emit(
                f"gate-c: discarded criterion-score drift on '{option_id}' — "
                "preference feedback may move ONLY the preference slot (P9)"
            )
        old_slot = old.preference_score.score if old.preference_score else None
        new_slot = record.preference_score.score if record.preference_score else None
        if new_slot is not None and old_slot != new_slot:
            before = f"{old_slot:g}" if old_slot is not None else "(none)"
            ctx.emit(f"gate-c: preference slot of '{option_id}' {before} -> {new_slot:g}")

    def apply_slots(screening: ScreeningResult) -> ScreeningResult:
        options = []
        for option in screening.options:
            record = by_id.get(option.option_id)
            if record is None or record.preference_score is None:
                options.append(option)
                continue
            options.append(option.model_copy(update={"preference_score": record.preference_score}))
        merged, drift = recompute_aggregates(
            ScreeningResult(options=options, notes=screening.notes), rubric, label="gate-c"
        )
        for message in drift:
            ctx.emit(message)
        return merged

    updated = apply_slots(scores)
    ws.write_artifact(UPDATED_SCORES_PATH, updated)
    dive_scores = ws.read_artifact(DIVE_SCORES_PATH, ScreeningResult)
    ws.write_artifact(DIVE_SCORES_PATH, apply_slots(dive_scores))
    dest, adjusted = dual_scoreboards(updated, rubric)
    ctx.emit(
        "gate-c: code re-scored both scoreboards (the destination-only board "
        "cannot move — the slot weighs 0 there):\n" + render_scoreboards(dest, adjusted)
    )


# -- evidence challenges -----------------------------------------------------------------


async def run_evidence_challenges(
    ctx: StageContext, challenges: list[EvidenceChallenge], *, iteration: int
) -> None:
    """One scoped verifier task per challenge; verdicts land in gates/ and a
    contradicted claim enters the ledger. No scores move here."""
    ws = ctx.workspace
    for challenge in challenges:
        dossier = ws.read_artifact(dossier_path(challenge.option_id), Dossier)
        claim = next(c for c in dossier.claims if c.id == challenge.claim_id)
        lb = 1 if claim.load_bearing else 0
        contract = AgentContract(
            role="verifier",
            stage=Stage.S7,
            task_objective=(
                "# GATE-C EVIDENCE CHALLENGE\n"
                f"The human challenged one claim of option '{challenge.option_id}':\n"
                f"- claim `{claim.id}`: {claim.text}\n"
                f"- source: {claim.source.url}\n"
                f"- the challenge, verbatim: {challenge.challenge}\n"
                "Adjudicate EXACTLY this one claim against its source. Set "
                f"sampled_load_bearing_count: {lb} and sampled_other_count: {1 - lb}."
            ),
            input_artifacts={"dossier": dossier.dump_yaml()},
            output_schemas=("verification-report",),
            size_class=SizeClass.M,
            budget_line=(
                "One source re-fetch (sources/ cache first, live only on a miss) "
                "and at most one corroborating lookup for a vanished source."
            ),
            context=f"challenge-{challenge.option_id}-{challenge.claim_id}",
        )
        result = await ctx.dispatcher.run_agent(contract)
        report = result.artifacts["verification-report"]
        assert isinstance(report, VerificationReport)
        problems = []
        if report.option_id != challenge.option_id:
            problems.append(
                f"- verification-report.option_id is '{report.option_id}' but the "
                f"challenge names option '{challenge.option_id}'"
            )
        adjudicated = [r.claim_id for r in report.results]
        if adjudicated != [challenge.claim_id]:
            problems.append(
                f"- adjudicate exactly ['{challenge.claim_id}']; the report covers {adjudicated}"
            )
        if problems:
            raise AgentOutputInvalid(
                contract,
                errors=(
                    "the verification report validates in isolation but does not "
                    "match the challenge:\n" + "\n".join(problems)
                ),
                raw_output=result.raw_text,
            )
        target = challenge_path(iteration, challenge.option_id, challenge.claim_id)
        ws.write_artifact(target, report)
        verdict = report.results[0].verdict
        if verdict is Verdict.CONTRADICTED:
            append_contradictions(
                ws,
                [
                    ContradictionEntry(
                        id=f"gate-c-{challenge.option_id}-{challenge.claim_id}",
                        statement_a=ContradictionStatement(
                            artifact=dossier_path(challenge.option_id), statement=claim.text
                        ),
                        statement_b=ContradictionStatement(
                            artifact=target,
                            statement=(
                                report.results[0].evidence_quote
                                or report.results[0].note
                                or f"the re-fetched source ({claim.source.url}) "
                                "contradicts the claim"
                            ),
                        ),
                        detected_by="verifier",
                    )
                ],
            )
            ctx.emit(
                f"gate-c: challenge upheld — claim '{challenge.claim_id}' of "
                f"'{challenge.option_id}' is CONTRADICTED by its source; entered in "
                f"ledger/contradictions.md (verdict at {target}). Scores did not "
                "move — follow with preference feedback or approve informed."
            )
        elif verdict is Verdict.VERIFIED:
            ctx.emit(
                f"gate-c: claim '{challenge.claim_id}' of '{challenge.option_id}' "
                f"held up — the source supports it as stated (verdict at {target})"
            )
        else:
            ctx.emit(
                f"gate-c: claim '{challenge.claim_id}' of '{challenge.option_id}' is "
                f"UNSUPPORTED — the source does not say this (verdict at {target})"
            )
