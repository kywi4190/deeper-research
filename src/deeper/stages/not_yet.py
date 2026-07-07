"""Registered-but-unbuilt stages S3–S8: the machine has full shape today; these
raise NotImplementedYet with a pointer to the build-guide prompt that fills
them in. The engine reports the message and leaves state untouched, so the run
resumes exactly here once the stage lands."""

from __future__ import annotations

from deeper.schemas import Stage

from .base import NotImplementedYet, StageBase, StageContext


def _not_yet_stage(stage: Stage, description: str, prompt_no: int) -> type[StageBase]:
    class NotYetStage(StageBase):
        pass

    NotYetStage.stage = stage
    message = f"{stage.value} {description} is not implemented yet (arrives in Prompt {prompt_no})"

    async def execute(self: StageBase, ctx: StageContext) -> None:
        raise NotImplementedYet(message)

    NotYetStage.execute = execute  # type: ignore[method-assign]
    NotYetStage.__name__ = NotYetStage.__qualname__ = f"NotYetStage{stage.value}"
    return NotYetStage


ScoutingStage = _not_yet_stage(Stage.S3, "option scouting", 8)
RubricStage = _not_yet_stage(Stage.S4, "rubric construction", 8)
ScreeningStage = _not_yet_stage(Stage.S5, "screening & shortlist", 8)
DeepDiveStage = _not_yet_stage(Stage.S6, "deep dives", 9)
TournamentStage = _not_yet_stage(Stage.S7, "adversarial tournament", 10)
SynthesisStage = _not_yet_stage(Stage.S8, "synthesis", 12)
