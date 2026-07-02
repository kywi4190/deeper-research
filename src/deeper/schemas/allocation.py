"""S2 allocation artifact (design §5/S2). Pure code produces this — no agent —
but it is written to the workspace and included in the final report: the breadth
decision is itself an inspectable artifact.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from .base import ArtifactModel, Probability, Slug


class AllocationKind(StrEnum):
    INITIAL = "initial"
    REFLOW = "reflow"  # S3 saturation returns unused units through the same formula


class AllocationRow(ArtifactModel):
    angle_id: Slug
    relevance_prior: Probability
    units: int = Field(ge=0, description="Budget units allocated to this angle.")


class AllocationTable(ArtifactModel):
    """allocation.yaml: the formula's inputs and outputs together, so the
    allocation is reproducible and auditable from the file alone."""

    kind: AllocationKind = AllocationKind.INITIAL
    total_budget_units: int = Field(ge=1)
    floor: int = Field(ge=0, description="Minimum units per surviving angle.")
    gamma: float = Field(gt=0, description="Concentration exponent — the breadth dial.")
    per_angle_cap_pct: float = Field(gt=0, le=100)
    rows: list[AllocationRow] = Field(min_length=1)

    @model_validator(mode="after")
    def _conserves_budget(self) -> AllocationTable:
        total = sum(r.units for r in self.rows)
        if total != self.total_budget_units:
            raise ValueError(
                f"allocated units must sum exactly to total_budget_units "
                f"({self.total_budget_units}); rows sum to {total} — "
                "integer rounding must conserve the budget"
            )
        return self
