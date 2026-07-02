"""S7 adversarial-tournament artifacts (design §5/S7).

Prosecutions attack the top-3; steelmen defend the runner-up and every
destination-vs-preference rank inversion (the priority docket); the
frame-checker audits the map itself and may propose re-divergence — surfaced at
Gate C, never auto-executed. The judge logs every score change with its cause.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from .base import ArtifactModel, NonEmptyStr, Score, Slug
from .common import EvidenceItem


class Prosecution(ArtifactModel):
    """The strongest good-faith case AGAINST one top-3 finalist."""

    option_id: Slug
    case: NonEmptyStr
    regret_path: NonEmptyStr = Field(
        description="The most likely way choosing this option leads to regret — mandatory."
    )
    supporting_claim_ids: list[Slug] = Field(
        default_factory=list, description="Dossier claims the case rests on."
    )
    new_evidence: list[EvidenceItem] = Field(
        default_factory=list,
        max_length=3,
        description="Material from the prosecutor's up-to-3 new targeted searches.",
    )
    notes: str | None = None


class SteelmanTrigger(StrEnum):
    RUNNER_UP = "runner-up"
    RANK_INVERSION = "rank-inversion"


class Steelman(ArtifactModel):
    """The strongest case that this option should win."""

    option_id: Slug
    trigger: SteelmanTrigger = Field(
        description="Why this option earned a steelman — inversions are exactly "
        "where preference-overfitting would hide."
    )
    case: NonEmptyStr
    supporting_claim_ids: list[Slug] = Field(default_factory=list)
    notes: str | None = None


class RedivergenceKind(StrEnum):
    NEW_ANGLE = "new-angle"
    SCOUT_TASK = "scout-task"


class RedivergenceProposal(ArtifactModel):
    """A specific new angle or scouting task with an estimated cost — approved
    (or not) by the human at Gate C."""

    kind: RedivergenceKind
    description: NonEmptyStr
    target_angle_id: Slug | None = Field(
        default=None, description="Existing angle to re-scout, for scout-task proposals."
    )
    estimated_cost_units: int = Field(ge=1)


class FrameCheckVerdict(StrEnum):
    PASS = "pass"
    GAP_FOUND = "gap-found"


class CheckFinding(ArtifactModel):
    finding: NonEmptyStr
    consequential: bool = Field(
        description="True if this finding could plausibly change the answer."
    )


class FrameCheck(ArtifactModel):
    """tournament/frame-check: 'Is there a plausible answer to the brief that
    this map could not have produced?' — the anti-overfitting backstop."""

    verdict: FrameCheckVerdict
    removals_check: CheckFinding = Field(
        description="Gate-A removals whose absence now looks consequential."
    )
    missed_options_check: CheckFinding = Field(
        description="Critique-flagged missed options that were never scouted."
    )
    rubric_fragility_check: CheckFinding = Field(
        description="Would a defensible alternative weighting change the winner?"
    )
    proposal: RedivergenceProposal | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _gap_iff_proposal(self) -> FrameCheck:
        gap = self.verdict is FrameCheckVerdict.GAP_FOUND
        if gap and self.proposal is None:
            raise ValueError(
                "verdict 'gap-found' requires a re-divergence proposal describing "
                "the specific new angle or scout task and its estimated cost"
            )
        if not gap and self.proposal is not None:
            raise ValueError("verdict 'pass' must not carry a re-divergence proposal")
        return self


class ScoreUpdate(ArtifactModel):
    """One judge-applied score change, logged with its cause."""

    option_id: Slug
    criterion_id: Slug
    old_score: Score
    new_score: Score
    cause: NonEmptyStr = Field(description="The decisive tournament material behind the change.")
    source_artifact: NonEmptyStr = Field(
        description="Workspace-relative path of the prosecution/steelman/frame-check "
        "that surfaced it."
    )


class ScoreUpdateLog(ArtifactModel):
    """tournament/score-updates.yaml."""

    updates: list[ScoreUpdate] = Field(default_factory=list)
    notes: str | None = None
