"""S8 synthesis artifact: the decision report (design §5/S8).

The synthesist emits this validated model; code renders the human-facing
report/decision-report.md from it, embedding the code-computed scoreboards,
sensitivity tables, and decision matrix verbatim (P8 — the agent narrates the
arithmetic, never produces it). Prose fields carry inline ``[[claim-id]]``
annotations that the mechanical citation pass (design §5: "a final mechanical
citation pass links every factual claim in the report body back to a dossier
claim") resolves against the dossiers before the stage may settle.
"""

from __future__ import annotations

from pydantic import Field

from .base import ArtifactModel, NonEmptyStr, Slug


class DecisionReport(ArtifactModel):
    """report/decision-report.yaml — the seven design components (§5/S8).

    Components 2, 3, and 7 pair an agent narration field with code-rendered
    tables: the matrix, both scoreboards, the sensitivity tables, and the
    appendix tables are computed and embedded by code at render time.
    """

    winner_option_id: Slug = Field(
        description="Must be rank 1 on the preference-adjusted scoreboard — code cross-checks it."
    )
    recommendation: NonEmptyStr = Field(
        description="§5/S8-1: the recommendation with its decisive reasons, every "
        "factual sentence annotated [[claim-id]] (or [[option-id:claim-id]] when "
        "the bare id is ambiguous across dossiers)."
    )
    decision_matrix_narration: NonEmptyStr = Field(
        description="§5/S8-2: what the finalists x criteria matrix (code-rendered "
        "beside this narration) says — where the winner is strong, weak, uncertain."
    )
    sensitivity_narration: NonEmptyStr = Field(
        description="§5/S8-3: narrates the CODE-computed flip deltas and the "
        "preference-slot sweep. If the winner is fragile to plausible weight "
        "changes, this must say so prominently."
    )
    dissent: NonEmptyStr = Field(
        description="§5/S8-4: the prosecution's best surviving argument against "
        "the winner, stated at full strength."
    )
    dissent_unrebutted: bool = Field(
        description="True when nothing in the tournament rebutted the dissent — "
        "the report must then say so explicitly."
    )
    dissent_source: NonEmptyStr = Field(
        description="Workspace-relative path of the prosecution the dissent survives from."
    )
    residual_uncertainty: NonEmptyStr = Field(
        description="§5/S8-5: open questions, BUDGET-CAPPED areas, and what new "
        "information should trigger revisiting the decision."
    )
    next_actions: list[NonEmptyStr] = Field(
        min_length=1,
        description="§5/S8-6: concrete next steps, folding in any execution-kind "
        "strategic notes that apply to the winner.",
    )
    appendix_notes: str | None = Field(
        default=None,
        description="§5/S8-7 commentary; the appendix tables themselves (angle "
        "map, allocation, cut audit, pass rates, spend) are code-rendered.",
    )
    notes: str | None = None
