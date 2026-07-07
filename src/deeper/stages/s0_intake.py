"""S0 — Intake & destination modeling (design §5/S0).

The interviewer is the system's only conversational agent: the stage drives a
terminal interview through the dispatcher's `run_interview` loop (questions
stream to the user as the agent asks them; answers go back into the transcript;
the question budget is capped by config). In live mode the interviewer may use
WebSearch to verify destination-facts (its role prompt is `research: true`).
The stage ends by printing a brief summary and asking for explicit confirmation
before writing the three strictly-separated artifacts; declining writes nothing
and leaves the run resumable at S0. Non-interactive sessions (no tty, tests,
mock walks) skip both the questions — the agent is told every turn is final —
and the confirmation.
"""

from __future__ import annotations

from deeper.agents_runtime import AgentContract
from deeper.config import SizeClass
from deeper.schemas import Brief, DestinationModel, Preferences, Stage

from .base import StageBase, StageContext, StageInterrupted

S0_OUTPUTS: tuple[tuple[str, type], ...] = (
    ("brief.md", Brief),
    ("destination.md", DestinationModel),
    ("preferences.yaml", Preferences),
)


def summary_lines(brief: Brief, destination: DestinationModel, prefs: Preferences) -> list[str]:
    """The pre-confirmation brief summary (design §5/S0 stop condition:
    'artifacts validate; user confirms the brief')."""
    lines = [
        "S0: interview complete — brief summary:",
        f"  goal:        {brief.goal}",
        f"  answer type: {brief.answer_type.value}",
    ]
    if brief.scope_in:
        lines.append(f"  in scope:    {'; '.join(brief.scope_in)}")
    if brief.scope_out:
        lines.append(f"  out of scope: {'; '.join(brief.scope_out)}")
    for c in brief.constraints:
        lines.append(f"  constraint [{c.kind.value}]: {c.statement}")
    if brief.notes:
        lines.append(f"  brief notes: {brief.notes}")
    for judge in destination.judges:
        lines.append(f"  judge: {judge.description} ({len(judge.rewards)} reward signals)")
    lines.append(
        f"  preferences: {len(prefs.items)} item(s) recorded and quarantined "
        "(only the S5 screener and S8 synthesist will ever see them)"
    )
    return lines


class IntakeStage(StageBase):
    stage = Stage.S0
    required_inputs = ()  # S0 starts from the goal alone.

    def outputs(self, ctx: StageContext):
        return list(S0_OUTPUTS)

    async def execute(self, ctx: StageContext) -> None:
        goal = ctx.config.goal or ""
        if not goal.strip():
            raise StageInterrupted(
                "this run has no goal recorded in config.yaml; re-create it with "
                '`deeper new "<goal>"`'
            )
        max_questions = ctx.config.caps.max_interview_questions
        if ctx.ask_user is not None:
            session = f"interactive, up to {max_questions} questions"
        else:
            session = "non-interactive — the interviewer finalizes from the goal alone"
        ctx.emit(f"S0: interviewing ({session}, mode={ctx.config.mode})…")
        result = await ctx.dispatcher.run_interview(
            AgentContract(
                role="interviewer",
                stage=Stage.S0,
                task_objective=f"The user's goal, verbatim:\n{goal}",
                output_schemas=("brief", "destination", "preferences"),
                size_class=SizeClass.L,
                budget_line=(
                    f"Interview budget: at most {max_questions} questions, then finalize."
                ),
            ),
            ask_user=ctx.ask_user,
            max_questions=max_questions,
        )
        brief = result.artifacts["brief"]
        destination = result.artifacts["destination"]
        preferences = result.artifacts["preferences"]
        assert isinstance(brief, Brief)
        assert isinstance(destination, DestinationModel)
        assert isinstance(preferences, Preferences)

        for line in summary_lines(brief, destination, preferences):
            ctx.emit(line)
        if ctx.ask_user is not None:
            reply = ctx.ask_user(
                "Confirm this brief? [y = write the artifacts and start cartography; "
                "anything else = discard and stop]"
            )
            if not reply.strip().lower().startswith("y"):
                raise StageInterrupted(
                    "S0: brief not confirmed — nothing was written. "
                    "`deeper resume` re-runs the interview."
                )
        ctx.workspace.write_artifact("brief.md", brief)
        ctx.workspace.write_artifact("destination.md", destination)
        ctx.workspace.write_artifact("preferences.yaml", preferences)
        ctx.emit("S0: brief.md, destination.md, preferences.yaml written")
