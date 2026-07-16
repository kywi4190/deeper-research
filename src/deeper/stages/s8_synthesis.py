"""S8 — Synthesis (design §5/S8).

Code computes everything numeric — the two scoreboards, the criterion-flip
deltas, the preference-slot sweep, the decision matrix, pass rates, spend —
and hands it to the synthesist (the only agent besides the screener permitted
preferences), which narrates it into the validated `decision-report` artifact.
Code then runs the mechanical **citation pass** (§5: every factual claim in
the report body links back to a dossier claim): unresolvable annotations fail
the pass with the exact list and buy ONE synthesist retry; a second failure
pauses the run. On success code renders report/decision-report.md — the human
deliverable, code-computed tables embedded verbatim, annotations linked to
the appendix claims index — and writes report/decision-report.yaml last (the
declared output, so `is_complete` implies the markdown exists).
"""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import AgentContract
from deeper.config import SizeClass
from deeper.contradictions import load_ledger
from deeper.report import (
    AppendixContext,
    citation_pass,
    collect_claims,
    decision_matrix_table,
    pass_rate_table,
    render_report,
    report_sections,
    spend_table,
    top_sensitivity_flag,
)
from deeper.schemas import (
    AllocationTable,
    AngleMap,
    Brief,
    CoverageReport,
    DecisionReport,
    DestinationModel,
    Dossier,
    FrameCheck,
    GateADecision,
    Preferences,
    Prosecution,
    Rubric,
    ScoreUpdateLog,
    ScreeningResult,
    Shortlist,
    Steelman,
    StrategicNoteKind,
    VerificationReport,
)
from deeper.schemas import Stage as StageEnum
from deeper.sensitivity import (
    dual_scoreboards,
    preference_sweep,
    render_scoreboards,
    render_sensitivity,
    steelman_docket,
    weight_sensitivity,
)
from deeper.workspace import WorkspaceError

from .base import StageBase, StageContext, StageInputsMissing
from .s6_deepdive import dossier_path, verification_path
from .s7_tournament import (
    FRAME_CHECK_PATH,
    PROSECUTED_TOP_N,
    UPDATE_LOG_PATH,
    UPDATED_SCORES_PATH,
    prosecution_path,
    steelman_path,
)

REPORT_MD_PATH = "report/decision-report.md"
REPORT_YAML_PATH = "report/decision-report.yaml"


def coherence_problems(report: DecisionReport, winner_id: str) -> list[str]:
    """Pure: the report must agree with the code-computed boards — the winner
    is the adjusted board's rank 1, and the dissent comes from the winner's
    prosecution file."""
    problems: list[str] = []
    if report.winner_option_id != winner_id:
        problems.append(
            f"- winner_option_id is '{report.winner_option_id}' but the "
            f"preference-adjusted scoreboard ranks '{winner_id}' first — the "
            "report narrates the boards, it does not re-decide them"
        )
    expected_source = prosecution_path(winner_id)
    if report.dissent_source != expected_source:
        problems.append(
            f"- dissent_source is '{report.dissent_source}'; the dissent is the "
            f"best surviving argument against the winner, so it must be "
            f"'{expected_source}'"
        )
    return problems


class SynthesisStage(StageBase):
    stage = StageEnum.S8
    required_inputs = (
        ("brief.md", Brief),
        ("destination.md", DestinationModel),
        ("preferences.yaml", Preferences),
        ("rubric.yaml", Rubric),
        ("angles/map.yaml", AngleMap),
        ("angles/map-report.md", CoverageReport),
        ("allocation.yaml", AllocationTable),
        ("screening/shortlist.md", Shortlist),
        ("gates/gate-a.yaml", GateADecision),
        (UPDATED_SCORES_PATH, ScreeningResult),
        (FRAME_CHECK_PATH, FrameCheck),
        (UPDATE_LOG_PATH, ScoreUpdateLog),
    )

    @staticmethod
    def _try_read(ctx: StageContext, relpath: str, model: type):
        try:
            return ctx.workspace.read_artifact(relpath, model)
        except (WorkspaceError, ValidationError, yaml.YAMLError):
            return None

    # -- engine protocol ------------------------------------------------------

    def outputs(self, ctx: StageContext):
        return [(REPORT_YAML_PATH, DecisionReport)]

    def is_complete(self, ctx: StageContext) -> bool:
        # The yaml is written last, so its validity attests the markdown —
        # but a hand-deleted markdown must still trigger a re-render.
        return super().is_complete(ctx) and ctx.workspace.path(REPORT_MD_PATH).is_file()

    def validate_inputs(self, ctx: StageContext) -> None:
        super().validate_inputs(ctx)
        scores = ctx.workspace.read_artifact(UPDATED_SCORES_PATH, ScreeningResult)
        rubric = ctx.workspace.read_artifact("rubric.yaml", Rubric)
        dest, adjusted = dual_scoreboards(scores, rubric)
        problems = []
        for option in scores.options:
            for relpath, model in (
                (dossier_path(option.option_id), Dossier),
                (verification_path(option.option_id), VerificationReport),
            ):
                if self._try_read(ctx, relpath, model) is None:
                    problems.append(f"- {relpath} is missing or invalid")
        for option_id in (row.option_id for row in adjusted[:PROSECUTED_TOP_N]):
            if self._try_read(ctx, prosecution_path(option_id), Prosecution) is None:
                problems.append(f"- {prosecution_path(option_id)} is missing or invalid")
        for option_id, _trigger in steelman_docket(dest, adjusted):
            if self._try_read(ctx, steelman_path(option_id), Steelman) is None:
                problems.append(f"- {steelman_path(option_id)} is missing or invalid")
        if problems:
            raise StageInputsMissing(
                "stage S8 cannot start; the tournament scoreboard does not resolve "
                "against its artifacts:\n" + "\n".join(problems)
            )

    # -- execution ----------------------------------------------------------------

    async def execute(self, ctx: StageContext) -> None:
        ws = ctx.workspace
        rubric = ws.read_artifact("rubric.yaml", Rubric)
        scores = ws.read_artifact(UPDATED_SCORES_PATH, ScreeningResult)
        weights = {c.id: c.weight for c in rubric.criteria}
        slot_weight = rubric.preference_slot.weight
        dest, adjusted = dual_scoreboards(scores, rubric)
        winner_id = adjusted[0].option_id
        flips = weight_sensitivity(scores.options, weights, slot_weight)
        sweep = preference_sweep(scores.options, weights)
        boards_text = render_scoreboards(dest, adjusted)
        sensitivity_text = render_sensitivity(flips, sweep, slot_weight)
        flag = top_sensitivity_flag(flips, sweep, slot_weight)
        matrix = decision_matrix_table(scores, rubric)

        dossiers = {
            o.option_id: ws.read_artifact(dossier_path(o.option_id), Dossier)
            for o in scores.options
        }
        verification = {
            o.option_id: ws.read_artifact(verification_path(o.option_id), VerificationReport)
            for o in scores.options
        }
        claims = collect_claims(list(dossiers.values()))
        top3 = [row.option_id for row in adjusted[:PROSECUTED_TOP_N]]
        docket = steelman_docket(dest, adjusted)
        coverage = ws.read_artifact("angles/map-report.md", CoverageReport)
        execution_notes = [
            n for n in coverage.strategic_notes if n.kind is StrategicNoteKind.EXECUTION
        ]
        state = ws.load_state()
        spend_by_stage = state.spend_by_stage()
        ledger = load_ledger(ws)

        inputs = {
            "brief": ws.path("brief.md").read_text(encoding="utf-8"),
            "destination": ws.path("destination.md").read_text(encoding="utf-8"),
            "preferences": ws.path("preferences.yaml").read_text(encoding="utf-8"),
            "rubric": rubric.dump_yaml(),
            "scores": scores.dump_yaml(),
            "scoreboards": boards_text,
            "sensitivity": f"Top flag (code-computed): {flag}\n\n{sensitivity_text}",
            "decision-matrix": matrix,
            "dossiers": "\n\n".join(
                f"## dossier: {oid} ({dossier_path(oid)})\n{d.dump_yaml()}"
                for oid, d in dossiers.items()
            ),
            "verification": pass_rate_table(verification),
            "prosecutions": "\n\n".join(
                f"## prosecution of {oid} ({prosecution_path(oid)})\n"
                + ws.read_artifact(prosecution_path(oid), Prosecution).dump_yaml()
                for oid in top3
            ),
            "steelmen": "\n\n".join(
                f"## steelman of {oid} ({steelman_path(oid)})\n"
                + ws.read_artifact(steelman_path(oid), Steelman).dump_yaml()
                for oid, _ in docket
            )
            or "(the docket was empty — no steelmen this run)",
            "frame-check": ws.read_artifact(FRAME_CHECK_PATH, FrameCheck).dump_yaml(),
            "shortlist": ws.read_artifact("screening/shortlist.md", Shortlist).dump_yaml(),
            "contradictions": ledger.dump_yaml() if ledger.entries else "(no contradictions)",
            "strategic-notes": (
                yaml.safe_dump(
                    [n.model_dump(mode="json") for n in execution_notes],
                    sort_keys=False,
                    allow_unicode=True,
                )
                if execution_notes
                else "(no execution-kind strategic notes this run)"
            ),
            "spend": spend_table(spend_by_stage),
        }

        report = self._try_read(ctx, REPORT_YAML_PATH, DecisionReport)
        if report is not None:
            # Idempotent re-entry: the settled artifact stands; only the
            # markdown render is owed (e.g. a hand-deleted deliverable).
            ctx.emit("S8: validated decision-report.yaml on disk — re-rendering only")
        else:
            report = await self._synthesize(ctx, inputs, claims, winner_id)

        gate_a = ws.read_artifact("gates/gate-a.yaml", GateADecision)
        appendix = AppendixContext(
            angle_map=ws.read_artifact("angles/map.yaml", AngleMap),
            coverage=coverage,
            allocation=ws.read_artifact("allocation.yaml", AllocationTable),
            shortlist=ws.read_artifact("screening/shortlist.md", Shortlist),
            removed_angles=gate_a.removed_angles,
            verification=verification,
            spend_by_stage=spend_by_stage,
            contradictions=ledger,
            claims=claims,
        )
        rendered = render_report(
            report,
            boards_text=boards_text,
            sensitivity_text=sensitivity_text,
            matrix_table=matrix,
            sensitivity_flag=flag,
            appendix=appendix,
        )
        md_target = ws.path(REPORT_MD_PATH)
        md_target.parent.mkdir(parents=True, exist_ok=True)
        md_target.write_text(rendered, encoding="utf-8", newline="\n")
        ws.write_artifact(REPORT_YAML_PATH, report)
        ctx.emit(
            f"S8: decision report written — {REPORT_MD_PATH} (winner "
            f"'{report.winner_option_id}'"
            + (", dissent UNREBUTTED" if report.dissent_unrebutted else "")
            + f"; citation pass clean over {len(claims)} indexed claim ids)"
        )

    async def _synthesize(
        self, ctx: StageContext, inputs: dict[str, str], claims, winner_id: str
    ) -> DecisionReport:
        """Dispatch the synthesist; board coherence and the mechanical citation
        pass ride the dispatcher's retry loop (M2 finding 5) under the shared
        caps.max_schema_retries budget — the bespoke one-citation-retry loop
        this replaces is recorded as a design deviation in the README."""
        ctx.emit(
            f"S8: synthesist drafting the decision report — adjusted-board winner '{winner_id}'"
        )
        contract = AgentContract(
            role="synthesist",
            stage=StageEnum.S8,
            task_objective=(
                "Synthesize this run into the decision report: all seven components, "
                "every factual sentence annotated with the dossier claim id it rests "
                "on ([[claim-id]], or [[option-id:claim-id]] when the bare id is "
                "ambiguous)."
            ),
            input_artifacts=inputs,
            output_schemas=("decision-report",),
            size_class=SizeClass.L,
            budget_line=(
                "No research — the run's artifacts are the complete evidence base; "
                "a fact you cannot annotate does not belong in the report."
            ),
        )

        def check_report(artifacts: dict) -> str | None:
            # Per-dispatch check, run inside the retry loop (M2 finding 5).
            report = artifacts["decision-report"]
            assert isinstance(report, DecisionReport)
            parts: list[str] = []
            problems = coherence_problems(report, winner_id)
            if problems:
                parts.append(
                    "the decision report validates in isolation but does not cohere "
                    "with the tournament's boards:\n" + "\n".join(problems)
                )
            citation = citation_pass(report_sections(report), claims)
            if citation:
                parts.append(
                    "the mechanical citation pass failed (design §5 — every factual "
                    "claim must link to a dossier claim); fix EXACTLY these "
                    "annotations, keep everything else:\n" + "\n".join(citation)
                )
            return "\n".join(parts) or None

        result = await ctx.dispatcher.run_agent(contract, validate=check_report)
        report = result.artifacts["decision-report"]
        assert isinstance(report, DecisionReport)
        return report
