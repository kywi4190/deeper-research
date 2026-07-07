"""Shared vocabulary: enums, source records, evidence items, contradiction ledger.

These types cross stage boundaries — source tiers and confidence tags (design
§5/S6), the cartographer heuristic ensemble (§5/S1), the sources/ cache and the
contradiction ledger (§6).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from .base import ArtifactModel, NonEmptyStr, Slug


class SourceTier(StrEnum):
    """T1 primary/official, T2 reputable secondary, T3 forum/anecdote."""

    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class Confidence(StrEnum):
    HIGH = "high"
    MED = "med"
    LOW = "low"


class Verdict(StrEnum):
    """Verifier adjudication of a dossier claim (design §5/S6)."""

    VERIFIED = "verified"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"


class Heuristic(StrEnum):
    """The cartographer framing personas — ensemble diversity is the breadth
    mechanism (design P3, §5/S1). HUMAN is not a cartographer: it marks angles
    the human added at Gate A, so provenance survives into the map."""

    FIRST_PRINCIPLES = "first-principles"
    ANALOGIST = "analogist"
    CONTRARIAN = "contrarian"
    PRACTITIONER = "practitioner"
    TAXONOMIST = "taxonomist"
    HORIZON_SCANNER = "horizon-scanner"
    HUMAN = "human"


class SourceRef(ArtifactModel):
    """Lightweight per-claim source reference carried inside evidence and claims."""

    url: NonEmptyStr
    tier: SourceTier
    title: str | None = None
    content_hash: str | None = Field(
        default=None,
        description="Hash linking this reference to the cached copy in sources/, "
        "so the verifier can re-check it offline.",
    )


class SourceRecord(ArtifactModel):
    """Registry entry for a fetched source in the content-addressed sources/ cache
    (design §6 source hygiene): tier, retrieval date, and URL make the run
    auditable offline."""

    url: NonEmptyStr
    tier: SourceTier
    retrieved_at: datetime
    content_hash: NonEmptyStr


class EvidenceItem(ArtifactModel):
    """One piece of evidence with its source — the unit of 'preliminary evidence'
    on option cards and of new tournament material."""

    text: NonEmptyStr
    source: SourceRef


class ContradictionStatus(StrEnum):
    OPEN = "open"
    ADJUDICATED = "adjudicated"


class ContradictionStatement(ArtifactModel):
    artifact: NonEmptyStr = Field(
        description="Workspace-relative path of the artifact making this statement."
    )
    statement: NonEmptyStr


class ContradictionEntry(ArtifactModel):
    """Two artifacts disagree on a fact. The detecting agent appends here instead
    of silently picking one; the verifier adjudicates (design §6). Unresolved
    entries surface in the final report."""

    id: Slug
    statement_a: ContradictionStatement
    statement_b: ContradictionStatement
    detected_by: NonEmptyStr = Field(description="Role of the agent that noticed it.")
    status: ContradictionStatus = ContradictionStatus.OPEN
    resolution: str | None = None

    @model_validator(mode="after")
    def _adjudicated_has_resolution(self) -> ContradictionEntry:
        if self.status is ContradictionStatus.ADJUDICATED and not self.resolution:
            raise ValueError(
                "an adjudicated contradiction must record its resolution "
                "(which statement stands, and why)"
            )
        return self


class ContradictionLedger(ArtifactModel):
    """The ledger/contradictions file for a run."""

    entries: list[ContradictionEntry] = Field(default_factory=list)
