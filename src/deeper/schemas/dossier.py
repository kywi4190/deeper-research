"""S6 deep-dive artifacts: Dossier, Claim, VerificationReport (design §5/S6).

Dossiers are structured BY RUBRIC CRITERION so evidence maps directly onto the
decision, plus five fixed standing sections. Claims carry confidence + tiered
source + a load_bearing flag: load-bearing claims (those that move a criterion
score >= 1 point) are all verified, and the stopping rule requires no remaining
low-confidence load-bearing claim.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from .base import ArtifactModel, NonEmptyStr, Slug
from .common import Confidence, SourceRef, Verdict


class Claim(ArtifactModel):
    id: Slug
    text: NonEmptyStr
    confidence: Confidence
    source: SourceRef
    load_bearing: bool = Field(
        description="True if this claim moves a criterion score by >= 1 point; "
        "drives verifier sampling and the depth stopping rule."
    )


class DossierSection(ArtifactModel):
    content: NonEmptyStr
    claim_ids: list[Slug] = Field(
        default_factory=list,
        description="Claims this section's content rests on.",
    )


class Dossier(ArtifactModel):
    """dossiers/{option}: criterion-keyed sections + the five standing sections.

    The standing sections are fixed fields (not a dict) so a missing one is a
    validation error, per design §5/S6.
    """

    option_id: Slug
    criterion_sections: dict[Slug, DossierSection] = Field(
        min_length=1, description="Rubric criterion id -> evidence section."
    )
    failure_modes: DossierSection = Field(description="Failure modes & prerequisites.")
    cost_of_adoption: DossierSection = Field(
        description="Total cost of adoption: time, money, optionality."
    )
    second_order_effects: DossierSection
    strongest_criticism: DossierSection = Field(
        description="The strongest published criticism, sought explicitly."
    )
    comparable_cases: DossierSection = Field(description="Who chose this and what happened.")
    claims: list[Claim] = Field(min_length=1)
    rounds_completed: int = Field(
        ge=1, description="Research rounds run before the stopping rule fired."
    )
    budget_capped: bool = Field(
        default=False,
        description="True when the per-option cap hit before score stability — "
        "unfinished depth is visible, never silent.",
    )
    open_questions: list[NonEmptyStr] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Dossier:
        ids = [c.id for c in self.claims]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"claim ids must be unique; duplicated: {dupes}")
        known = set(ids)
        sections: dict[str, DossierSection] = {
            **{f"criterion_sections.{k}": v for k, v in self.criterion_sections.items()},
            "failure_modes": self.failure_modes,
            "cost_of_adoption": self.cost_of_adoption,
            "second_order_effects": self.second_order_effects,
            "strongest_criticism": self.strongest_criticism,
            "comparable_cases": self.comparable_cases,
        }
        for name, section in sections.items():
            unknown = sorted(set(section.claim_ids) - known)
            if unknown:
                raise ValueError(
                    f"section '{name}' references claim ids not present in 'claims': {unknown}"
                )
        if self.budget_capped and not self.open_questions:
            raise ValueError(
                "a BUDGET-CAPPED dossier must list its open_questions so the "
                "unfinished depth is visible in the report"
            )
        return self


class VerificationResult(ArtifactModel):
    claim_id: Slug
    verdict: Verdict
    evidence_quote: str | None = Field(
        default=None, description="Quoted source text supporting the verdict."
    )
    note: str | None = None


class VerificationReport(ArtifactModel):
    """dossiers/{option}-verification: the independent verifier's adjudication.
    Sampling covers ALL load-bearing claims plus a random 20% of the rest."""

    option_id: Slug
    results: list[VerificationResult] = Field(min_length=1)
    sampled_load_bearing_count: int = Field(ge=0)
    sampled_other_count: int = Field(ge=0)
    notes: str | None = None

    @property
    def pass_rate(self) -> float:
        """Fraction of sampled claims verified — computed, so it cannot go stale."""
        verified = sum(1 for r in self.results if r.verdict is Verdict.VERIFIED)
        return verified / len(self.results)
