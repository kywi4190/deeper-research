"""S0 — Intake & destination modeling (design §5/S0). PROVISIONAL.

The real S0 (Prompt 7) is an interactive terminal interview: stream the
interviewer's questions, capture answers, cap at ~8 questions, confirm the
brief before writing. This provisional version exists so the state machine is
walkable end-to-end today: in mock mode it dispatches the interviewer once
(fixtures answer) and writes the three strictly-separated artifacts; in live
mode it declines rather than fake an interview.
"""

from __future__ import annotations

from deeper.agents_runtime import AgentContract
from deeper.config import SizeClass
from deeper.schemas import Brief, DestinationModel, Preferences, Stage

from .base import NotImplementedYet, StageBase, StageContext

S0_OUTPUTS: tuple[tuple[str, type], ...] = (
    ("brief.md", Brief),
    ("destination.md", DestinationModel),
    ("preferences.yaml", Preferences),
)


class IntakeStage(StageBase):
    stage = Stage.S0
    required_inputs = ()  # S0 starts from the goal alone.

    def outputs(self, ctx: StageContext):
        return list(S0_OUTPUTS)

    async def execute(self, ctx: StageContext) -> None:
        if ctx.config.mode == "live":
            raise NotImplementedYet(
                "S0 interactive intake interview is not implemented yet (Prompt 7); "
                "run in mock mode to walk the pipeline"
            )
        goal = ctx.config.goal or ""
        if not goal.strip():
            raise NotImplementedYet(
                "this run has no goal recorded in config.yaml; re-create it with "
                '`deeper new "<goal>"`'
            )
        ctx.emit("S0: interviewing (mock)…")
        result = await ctx.dispatcher.run_agent(
            AgentContract(
                role="interviewer",
                stage=Stage.S0,
                task_objective=f"The user's goal, verbatim:\n{goal}",
                output_schemas=("brief", "destination", "preferences"),
                size_class=SizeClass.L,
                budget_line="Interview budget: at most 8 questions, then finalize.",
            )
        )
        ctx.workspace.write_artifact("brief.md", result.artifacts["brief"])
        ctx.workspace.write_artifact("destination.md", result.artifacts["destination"])
        ctx.workspace.write_artifact("preferences.yaml", result.artifacts["preferences"])
        ctx.emit("S0: brief.md, destination.md, preferences.yaml written")
