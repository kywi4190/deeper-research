"""Gate decision artifacts — gates/gate-{a,b,c}.yaml (design §5, Gates A/B/C).

Gates are pause states: the orchestrator writes a template, the human edits it
(or a viewer writes it), and `resume` validates it before advancing. Each model
supports exactly the actions the design lists for its gate.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from .base import ArtifactModel, NonEmptyStr, Probability, Slug
from .rubric import Criterion


class AngleAddition(ArtifactModel):
    name: NonEmptyStr
    note: NonEmptyStr = Field(description="Guidance for the scout that will be assigned.")


class AngleRemoval(ArtifactModel):
    angle_id: Slug
    reason: NonEmptyStr = Field(
        description="Mandatory — removals are logged and re-examined by the S7 frame-checker."
    )


class PriorAdjustment(ArtifactModel):
    angle_id: Slug
    new_prior: Probability


class GateADecision(ArtifactModel):
    """Frame review: approve / add angle / remove angle / adjust prior / request
    another cartography pass with a hint."""

    approved: bool
    added_angles: list[AngleAddition] = Field(default_factory=list)
    removed_angles: list[AngleRemoval] = Field(default_factory=list)
    prior_adjustments: list[PriorAdjustment] = Field(default_factory=list)
    rerun_hint: str | None = Field(
        default=None,
        description="Set to request another cartography pass with this hint — "
        "mutually exclusive with approval.",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _approve_xor_rerun(self) -> GateADecision:
        if self.approved and self.rerun_hint is not None:
            raise ValueError(
                "cannot both approve the map and request another cartography pass; "
                "clear rerun_hint to approve, or set approved: false to rerun"
            )
        return self


class GateBDecision(ArtifactModel):
    """Values review: adjust weights, edit criteria, and set the preference-slot
    weight — the one number that says how much tastes may bend the
    destination-optimal answer."""

    approved: bool
    preference_slot_weight: float = Field(
        ge=0,
        le=0.4,
        description="0-0.4, matching the report's sensitivity sweep range.",
    )
    weight_overrides: dict[Slug, float] = Field(
        default_factory=dict, description="Criterion id -> new weight."
    )
    edited_criteria: list[Criterion] = Field(
        default_factory=list,
        description="Full replacements for edited criteria (diffable, no patch ops).",
    )
    notes: str | None = None


class ReactionDirection(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class ContenderReaction(ArtifactModel):
    """Structured preference feedback the screener converts into preference-slot
    adjustments — a free re-score, no new research."""

    option_id: Slug
    reaction: NonEmptyStr
    direction: ReactionDirection


class EvidenceChallenge(ArtifactModel):
    """'I don't believe claim X' -> a targeted verifier/analyst task."""

    option_id: Slug
    claim_id: Slug
    challenge: NonEmptyStr


class GateCDecision(ArtifactModel):
    """Contender review: preference feedback / evidence challenge / accept
    re-divergence / approve. Approve means proceed to synthesis, so it excludes
    the loop actions — run those in their own gate iteration first."""

    approved: bool
    preference_feedback: list[ContenderReaction] = Field(default_factory=list)
    evidence_challenges: list[EvidenceChallenge] = Field(default_factory=list)
    accept_redivergence: bool = False
    notes: str | None = None

    @model_validator(mode="after")
    def _approve_excludes_loops(self) -> GateCDecision:
        if self.approved and (
            self.preference_feedback or self.evidence_challenges or self.accept_redivergence
        ):
            raise ValueError(
                "approve means proceed to synthesis; submit preference feedback, "
                "evidence challenges, or re-divergence acceptance in their own gate "
                "iteration with approved: false"
            )
        return self
