"""Registered-but-unbuilt stages (now only S8): the machine has full shape
today; these raise NotImplementedYet with a pointer to the build-guide prompt
that fills them in. The engine reports the message and leaves state untouched,
so the run resumes exactly here once the stage lands."""

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


SynthesisStage = _not_yet_stage(Stage.S8, "synthesis", 12)
