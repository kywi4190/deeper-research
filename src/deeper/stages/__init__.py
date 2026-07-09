"""Per-stage logic S0–S8 (design §5). `STAGES` is the registry the orchestrator
engine runs; tests inject scripted registries through the engine constructor."""

from deeper.schemas import Stage

from .base import (
    NotImplementedYet,
    StageBase,
    StageContext,
    StageInputsMissing,
    StageInterrupted,
)
from .s0_intake import IntakeStage
from .s1_cartography import CartographyStage
from .s2_allocation import AllocationStage
from .s3_scouting import ScoutingStage
from .s4_rubric import RubricStage
from .s5_screening import ScreeningStage
from .s6_deepdive import DeepDiveStage
from .s7_tournament import TournamentStage
from .s8_synthesis import SynthesisStage

STAGES: dict[Stage, type[StageBase]] = {
    Stage.S0: IntakeStage,
    Stage.S1: CartographyStage,
    Stage.S2: AllocationStage,
    Stage.S3: ScoutingStage,
    Stage.S4: RubricStage,
    Stage.S5: ScreeningStage,
    Stage.S6: DeepDiveStage,
    Stage.S7: TournamentStage,
    Stage.S8: SynthesisStage,
}

__all__ = [
    "STAGES",
    "AllocationStage",
    "CartographyStage",
    "DeepDiveStage",
    "IntakeStage",
    "NotImplementedYet",
    "RubricStage",
    "ScoutingStage",
    "ScreeningStage",
    "StageBase",
    "StageContext",
    "StageInputsMissing",
    "StageInterrupted",
    "SynthesisStage",
    "TournamentStage",
]
