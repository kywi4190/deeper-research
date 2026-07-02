"""Artifact schema layer: the stage-to-stage contracts (design P2).

Every pipeline artifact is a strict Pydantic model with YAML/JSON round-trip
helpers; validation errors format into actionable retry instructions for LLM
agents; JSON Schemas export into schemas/ for inlining into agent prompts.
"""

from .allocation import AllocationKind, AllocationRow, AllocationTable
from .angles import Angle, AngleMap, CoverageReport, DedupEntry, SubAngle
from .base import (
    SLUG_PATTERN,
    ArtifactModel,
    format_validation_error,
)
from .common import (
    Confidence,
    ContradictionEntry,
    ContradictionLedger,
    ContradictionStatement,
    ContradictionStatus,
    EvidenceItem,
    Heuristic,
    SourceRecord,
    SourceRef,
    SourceTier,
    Verdict,
)
from .dossier import Claim, Dossier, DossierSection, VerificationReport, VerificationResult
from .export import ARTIFACT_REGISTRY, export_all
from .gates import (
    AngleAddition,
    AngleRemoval,
    ContenderReaction,
    EvidenceChallenge,
    GateADecision,
    GateBDecision,
    GateCDecision,
    PriorAdjustment,
    ReactionDirection,
)
from .intake import (
    AnswerType,
    Brief,
    Constraint,
    ConstraintKind,
    DestinationModel,
    Judge,
    PreferenceItem,
    Preferences,
    PreferenceStrength,
    RewardSignal,
)
from .options import (
    CardCritique,
    CompletenessIssue,
    DistinctnessIssue,
    KillRisk,
    OptionCard,
    OptionCardSet,
)
from .rubric import Criterion, PreferenceSlot, Rubric
from .runstate import GateName, GateStatus, RunState, RunStatus, SpendEntry, Stage
from .screening import (
    CriterionScore,
    KillRiskCheck,
    KillRiskOutcome,
    OptionScreening,
    ScreeningResult,
    Shortlist,
    ShortlistCause,
    ShortlistDecision,
    ShortlistOutcome,
    UncertaintyBand,
)
from .tournament import (
    CheckFinding,
    FrameCheck,
    FrameCheckVerdict,
    Prosecution,
    RedivergenceKind,
    RedivergenceProposal,
    ScoreUpdate,
    ScoreUpdateLog,
    Steelman,
    SteelmanTrigger,
)

__all__ = [
    "SLUG_PATTERN",
    "ARTIFACT_REGISTRY",
    "ArtifactModel",
    "format_validation_error",
    "export_all",
    # common
    "Confidence",
    "ContradictionEntry",
    "ContradictionLedger",
    "ContradictionStatement",
    "ContradictionStatus",
    "EvidenceItem",
    "Heuristic",
    "SourceRecord",
    "SourceRef",
    "SourceTier",
    "Verdict",
    # S0 intake
    "AnswerType",
    "Brief",
    "Constraint",
    "ConstraintKind",
    "DestinationModel",
    "Judge",
    "PreferenceItem",
    "Preferences",
    "PreferenceStrength",
    "RewardSignal",
    # S1 angles
    "Angle",
    "AngleMap",
    "CoverageReport",
    "DedupEntry",
    "SubAngle",
    # S2 allocation
    "AllocationKind",
    "AllocationRow",
    "AllocationTable",
    # S3 options
    "CardCritique",
    "CompletenessIssue",
    "DistinctnessIssue",
    "KillRisk",
    "OptionCard",
    "OptionCardSet",
    # S4 rubric
    "Criterion",
    "PreferenceSlot",
    "Rubric",
    # S5 screening
    "CriterionScore",
    "KillRiskCheck",
    "KillRiskOutcome",
    "OptionScreening",
    "ScreeningResult",
    "Shortlist",
    "ShortlistCause",
    "ShortlistDecision",
    "ShortlistOutcome",
    "UncertaintyBand",
    # S6 dossier
    "Claim",
    "Dossier",
    "DossierSection",
    "VerificationReport",
    "VerificationResult",
    # S7 tournament
    "CheckFinding",
    "FrameCheck",
    "FrameCheckVerdict",
    "Prosecution",
    "RedivergenceKind",
    "RedivergenceProposal",
    "ScoreUpdate",
    "ScoreUpdateLog",
    "Steelman",
    "SteelmanTrigger",
    # gates
    "AngleAddition",
    "AngleRemoval",
    "ContenderReaction",
    "EvidenceChallenge",
    "GateADecision",
    "GateBDecision",
    "GateCDecision",
    "PriorAdjustment",
    "ReactionDirection",
    # run state
    "GateName",
    "GateStatus",
    "RunState",
    "RunStatus",
    "SpendEntry",
    "Stage",
]
