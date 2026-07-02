"""S3 scouting artifacts: OptionCard, OptionCardSet, CardCritique (design §5/S3).

Cards are deliberately shallow-but-structured (screening depth). The critique is
the per-angle quality mechanism: its redundancy_pct drives the early-stop rule
(>40%) and its missed_options drive budget reflow.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from .base import ArtifactModel, NonEmptyStr, Slug
from .common import EvidenceItem


class KillRisk(ArtifactModel):
    """A single fact that, if true, eliminates the option. Checked FIRST in
    screening with one cheap lookup — the check_hint is what keeps it cheap."""

    fact: NonEmptyStr
    check_hint: NonEmptyStr = Field(
        description="How to verify this with a single cheap lookup or computation."
    )


class OptionCard(ArtifactModel):
    """One specific solution within an angle, at screening depth (~300-500 words)."""

    id: Slug
    name: NonEmptyStr
    angle_id: Slug
    description: NonEmptyStr
    mechanism: NonEmptyStr = Field(description="How it works, in about two sentences.")
    preliminary_evidence: list[EvidenceItem] = Field(
        min_length=1,
        description="Evidence of viability, each item with a tiered source ref — "
        "source-tier honesty is a critic checklist item.",
    )
    uncertainties: list[NonEmptyStr] = Field(
        min_length=1, description="2-3 key unknowns that screening/deep-dive must resolve."
    )
    kill_risks: list[KillRisk] = Field(default_factory=list)
    misplaced_flag: str | None = Field(
        default=None,
        description="If this option really belongs to another angle, name that angle "
        "here instead of absorbing it (scout boundary rule).",
    )
    notes: str | None = None


class OptionCardSet(ArtifactModel):
    """options/{angle}/cards.yaml — one scout's output for one angle."""

    angle_id: Slug
    cards: list[OptionCard] = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> OptionCardSet:
        ids = [c.id for c in self.cards]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"card ids must be unique within the set; duplicated: {dupes}")
        strays = sorted({c.id for c in self.cards if c.angle_id != self.angle_id})
        if strays:
            raise ValueError(
                f"every card's angle_id must equal the set's angle_id "
                f"('{self.angle_id}'); mismatched cards: {strays} — use misplaced_flag "
                "to point at another angle, not a different angle_id"
            )
        return self


class CompletenessIssue(ArtifactModel):
    card_id: Slug
    issue: NonEmptyStr


class DistinctnessIssue(ArtifactModel):
    """Two-or-more cards that are really one option."""

    card_ids: list[Slug] = Field(min_length=2)
    rationale: NonEmptyStr


class CardCritique(ArtifactModel):
    """options/{angle}/critique — the card-critic's checklist review."""

    angle_id: Slug
    completeness_issues: list[CompletenessIssue] = Field(default_factory=list)
    redundancy_pct: float = Field(
        ge=0,
        le=100,
        description="Share of cards that are minor variants of earlier cards; "
        ">40 stops the angle early and returns units to the reflow pool.",
    )
    distinctness_issues: list[DistinctnessIssue] = Field(default_factory=list)
    missed_options: list[NonEmptyStr] = Field(
        default_factory=list,
        max_length=3,
        description="Plausible options the scout missed — targets for reflowed budget "
        "and a frame-check input if never scouted.",
    )
    notes: str | None = None
