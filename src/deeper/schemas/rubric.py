"""S4 rubric artifacts (design §5/S4).

Derived from the destination model, never from preferences. Preferences enter
later through the single, separately-weighted preference slot (design P9):
criterion weights normalize to 1.0 among themselves, and the slot weight is the
fraction of the combined score preferences may claim — the S7 dual-scoreboard
math (destination-only vs preference-adjusted) builds on this convention.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from .base import ArtifactModel, NonEmptyStr, Slug


class Criterion(ArtifactModel):
    id: Slug
    name: NonEmptyStr
    definition: NonEmptyStr
    measurement_method: NonEmptyStr = Field(
        description="What evidence would move this score — tells analysts what to fetch."
    )
    levels: dict[int, str] = Field(
        description="Anchored score levels: exactly keys 1-5, each a concrete "
        "description of what that score means."
    )
    weight: float = Field(gt=0, le=1)
    justification: NonEmptyStr = Field(
        description="Why this weight — traceable to the destination model."
    )

    @field_validator("levels")
    @classmethod
    def _levels_are_1_to_5(cls, v: dict[int, str]) -> dict[int, str]:
        if set(v) != {1, 2, 3, 4, 5}:
            raise ValueError(
                "anchored levels must have exactly the integer keys 1, 2, 3, 4, 5, "
                "each with a non-empty description of what that score means"
            )
        if any(not desc.strip() for desc in v.values()):
            raise ValueError("every anchored level needs a non-empty description")
        return v


class PreferenceSlot(ArtifactModel):
    """The one bounded, visible place where tastes may bend the destination-optimal
    answer. Its weight is set at Gate B."""

    weight: float = Field(
        default=0.20,
        ge=0,
        le=1,
        description="Fraction of the combined score the preference component may claim.",
    )


class Rubric(ArtifactModel):
    """rubric.yaml: 5-9 destination-derived criteria plus the preference slot."""

    criteria: list[Criterion] = Field(min_length=5, max_length=9)
    preference_slot: PreferenceSlot
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Rubric:
        ids = [c.id for c in self.criteria]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"criterion ids must be unique; duplicated: {dupes}")
        total = sum(c.weight for c in self.criteria)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"criterion weights must sum to 1.0 (the preference slot is weighted "
                f"separately); they sum to {total:.4f}"
            )
        return self
