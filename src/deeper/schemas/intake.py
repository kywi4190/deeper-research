"""S0 intake artifacts: Brief, DestinationModel, Preferences (design §5/S0).

The interviewer classifies every user statement as constraint (fact) vs
destination-fact vs preference (taste); the three artifacts keep those strictly
separated. Preferences are quarantined until scoring (design P1/P9).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .base import ArtifactModel, NonEmptyStr
from .common import SourceRef


class AnswerType(StrEnum):
    """The type of answer the run must produce (drives report shape)."""

    DECISION = "decision"
    LANDSCAPE = "landscape"
    RECOMMENDATION_WITH_FALLBACKS = "recommendation-with-fallbacks"


class ConstraintKind(StrEnum):
    DEADLINE = "deadline"
    BUDGET = "budget"
    HARD_REQUIREMENT = "hard-requirement"


class Constraint(ArtifactModel):
    """A fact about the world, not a taste — the interviewer pushes back on
    preferences stated as constraints before anything lands here."""

    statement: NonEmptyStr
    kind: ConstraintKind


class Brief(ArtifactModel):
    """The goal restated, with scope boundaries and world-fact constraints.
    The frame-checker later audits the whole run against this exact text."""

    goal: NonEmptyStr
    answer_type: AnswerType
    scope_in: list[NonEmptyStr] = Field(default_factory=list)
    scope_out: list[NonEmptyStr] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    notes: str | None = None


class RewardSignal(ArtifactModel):
    """One thing the judge rewards. Evidence refs when the destination is
    external and verifiable (the interviewer may web-research it)."""

    description: NonEmptyStr
    evidence: list[SourceRef] = Field(default_factory=list)


class Judge(ArtifactModel):
    description: NonEmptyStr = Field(
        description="Who or what ultimately judges the outcome "
        "(an admissions committee, a production environment, a reader)."
    )
    rewards: list[RewardSignal] = Field(min_length=1)


class DestinationModel(ArtifactModel):
    """What success looks like from the target's perspective. This — not the
    user's tastes — anchors the S4 rubric (design P9)."""

    judges: list[Judge] = Field(min_length=1)
    notes: str | None = None


class PreferenceStrength(StrEnum):
    MILD = "mild"
    MODERATE = "moderate"
    STRONG = "strong"


class PreferenceItem(ArtifactModel):
    statement: NonEmptyStr
    strength: PreferenceStrength = Field(
        description="Drives how hard this item pulls the S5 preference-slot score."
    )


class Preferences(ArtifactModel):
    """Tastes only: interests, aesthetics, risk appetite, soft dislikes.
    QUARANTINED — readable only by the screener and synthesist, enforced by a
    PreToolUse hook, not prompt goodwill (design §6)."""

    items: list[PreferenceItem] = Field(default_factory=list)
    risk_appetite: NonEmptyStr
    notes: str | None = None
