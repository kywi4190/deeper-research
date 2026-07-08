"""S5 screening artifacts (design §5/S5).

Every criterion score carries an uncertainty band; the shortlist rule advances
on the UPPER confidence bound (optimism under uncertainty), so the band — and
the stored weighted_ucb — are the audit trail for why a dark horse advanced.
Kill-risks are checked first; a confirmed one eliminates regardless of score.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from .base import ArtifactModel, NonEmptyStr, Score, Slug
from .common import SourceRef


class UncertaintyBand(ArtifactModel):
    lo: Score
    hi: Score

    @model_validator(mode="after")
    def _ordered(self) -> UncertaintyBand:
        if self.lo > self.hi:
            raise ValueError(f"band lo ({self.lo}) must be <= hi ({self.hi})")
        return self


class CriterionScore(ArtifactModel):
    criterion_id: Slug
    score: Score
    band: UncertaintyBand = Field(
        description="Wider when evidence is thin — under-information triggers more "
        "research, not elimination."
    )
    evidence_pointer: NonEmptyStr = Field(
        description="Where in the card/dossier the score comes from."
    )

    @model_validator(mode="after")
    def _score_inside_band(self) -> CriterionScore:
        if not (self.band.lo <= self.score <= self.band.hi):
            raise ValueError(
                f"score ({self.score}) must lie inside its uncertainty band "
                f"[{self.band.lo}, {self.band.hi}]"
            )
        return self


class KillRiskOutcome(StrEnum):
    CONFIRMED = "confirmed"
    CLEARED = "cleared"
    UNRESOLVED = "unresolved"


class KillRiskCheck(ArtifactModel):
    """Result of the cheap single-lookup check on a card's kill-risk."""

    fact: NonEmptyStr
    outcome: KillRiskOutcome
    evidence: SourceRef | None = None


class OptionScreening(ArtifactModel):
    """One option's full screening record in screening/scores.yaml."""

    option_id: Slug
    angle_id: Slug
    criterion_scores: list[CriterionScore] = Field(min_length=1)
    preference_score: CriterionScore | None = Field(
        default=None,
        description="The preference-slot score — the ONLY place tastes enter (P9).",
    )
    kill_risk_checks: list[KillRiskCheck] = Field(default_factory=list)
    weighted_point: Score = Field(
        description="Weighted point estimate. Stored so the file alone explains "
        "the shortlist decision."
    )
    weighted_ucb: Score = Field(
        description="Weighted upper confidence bound — the number the shortlist "
        "rule actually compares against the threshold."
    )
    notes: str | None = None


class ScreeningResult(ArtifactModel):
    """screening/scores.yaml."""

    options: list[OptionScreening] = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _unique_options(self) -> ScreeningResult:
        ids = [o.option_id for o in self.options]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"option ids must be unique; duplicated: {dupes}")
        return self


class ShortlistOutcome(StrEnum):
    ADVANCED = "advanced"
    CUT = "cut"


class ShortlistCause(StrEnum):
    UCB_ABOVE_THRESHOLD = "ucb-above-threshold"
    DIVERSITY_GUARDRAIL_ADD = "diversity-guardrail-add"
    KILL_RISK_CONFIRMED = "kill-risk-confirmed"
    BELOW_THRESHOLD = "below-threshold"
    BELOW_CUTOFF = "below-cutoff"
    ANGLE_CAP = "angle-cap"


_ADVANCE_CAUSES = {ShortlistCause.UCB_ABOVE_THRESHOLD, ShortlistCause.DIVERSITY_GUARDRAIL_ADD}
_CUT_CAUSES = {
    ShortlistCause.KILL_RISK_CONFIRMED,
    ShortlistCause.BELOW_THRESHOLD,
    ShortlistCause.BELOW_CUTOFF,
    ShortlistCause.ANGLE_CAP,
}


class ShortlistDecision(ArtifactModel):
    """Advance/cut with a mandatory reason — cuts are auditable (design §5/S5)."""

    option_id: Slug
    decision: ShortlistOutcome
    cause: ShortlistCause
    reason: NonEmptyStr = Field(
        description="One-paragraph why-advanced / why-cut note for the audit trail."
    )

    @model_validator(mode="after")
    def _cause_matches_decision(self) -> ShortlistDecision:
        allowed = _ADVANCE_CAUSES if self.decision is ShortlistOutcome.ADVANCED else _CUT_CAUSES
        if self.cause not in allowed:
            raise ValueError(
                f"cause '{self.cause}' is not valid for decision '{self.decision}'; "
                f"valid causes: {sorted(c.value for c in allowed)}"
            )
        return self


class Shortlist(ArtifactModel):
    """screening/shortlist: one decision per screened option, plus the finalists."""

    threshold: Score = Field(description="The UCB threshold the rule compared against.")
    decisions: list[ShortlistDecision] = Field(min_length=1)
    finalist_ids: list[Slug] = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _finalists_match_decisions(self) -> Shortlist:
        ids = [d.option_id for d in self.decisions]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"each option gets exactly one decision; duplicated: {dupes}")
        advanced = sorted(
            d.option_id for d in self.decisions if d.decision is ShortlistOutcome.ADVANCED
        )
        if sorted(self.finalist_ids) != advanced:
            raise ValueError(
                f"finalist_ids must be exactly the advanced options; "
                f"finalist_ids={sorted(self.finalist_ids)}, advanced={advanced}"
            )
        return self
