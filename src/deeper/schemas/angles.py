"""S1 cartography artifacts: Angle, AngleMap, CoverageReport (design §5/S1).

The merger produces the map from the cartographer ensemble's raw angles. The
dedup map is load-bearing: the orchestrator's saturation rule computes marginal
novelty per cartographer from it (new distinct angles / total angles).

Strategic notes are the quarantined side channel for meta-strategy insights
(README "Design deviations"): real levers on the goal that are not scoutable
solution regions. They surface at Gate A and route onward by kind; they never
enter S2 allocation, S3 scouting, or S5 screening.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from .base import ArtifactModel, NonEmptyStr, Probability, Slug
from .common import Heuristic


class StrategicNoteKind(StrEnum):
    """Where a strategic note routes downstream."""

    REFRAME = "reframe"  # the brief/destination may ask a subtly wrong question -> Gate A
    RUBRIC_WEIGHT = "rubric-weight"  # what the judge actually rewards -> S4 rubric-builder
    EXECUTION = "execution"  # how to execute/position whichever option wins -> S8 synthesis


class StrategicNote(ArtifactModel):
    """A meta-strategy insight that is deliberately NOT an angle.

    Selection criteria, goal reframes, and execution/timing tactics are real
    levers but not regions a scout can populate with option cards, so they
    travel here instead of the angle list. The human at Gate A reads them (and
    alone may enact a reframe, per P7/P8); rubric-weight notes feed the S4
    rubric-builder; execution notes feed S8 next-actions. This channel never
    receives scouting budget.
    """

    insight: NonEmptyStr
    kind: StrategicNoteKind = Field(
        description="Routing: 'reframe' -> Gate A frame review; 'rubric-weight' -> "
        "S4 rubric-builder; 'execution' -> S8 synthesis next-actions."
    )
    rationale: NonEmptyStr = Field(
        description="Why this lever matters, grounded only in the brief and "
        "destination model — never in preferences."
    )
    source_heuristics: list[Heuristic] = Field(
        default_factory=list,
        description="Filled by the merger: which cartographer framings proposed or "
        "corroborated this note. Cartographers leave it empty.",
    )


class RawAngle(ArtifactModel):
    """One candidate angle exactly as a cartographer emits it, before merger
    dedup. Deliberately has NO numeric relevance prior: assigning priors is the
    merger's job (design §5/S1); cartographers give a prose rationale only."""

    name: NonEmptyStr
    definition: NonEmptyStr
    distinctness_rationale: NonEmptyStr = Field(
        description="Why this is a distinct region and not a variant of another angle."
    )
    example_options: list[NonEmptyStr] = Field(
        min_length=1,
        description="2-3 concrete options proving the angle is non-empty — "
        "existence proofs, never endorsements.",
    )
    relevance_rationale: NonEmptyStr = Field(
        description="Estimated-relevance rationale in prose, grounded only in the "
        "brief and destination model — never a number, never a ranking."
    )
    notes: str | None = None


class CartographerReport(ArtifactModel):
    """angles/raw/{heuristic} — one cartographer's candidate angles (design §7
    lists per-cartographer raw output as a workspace artifact). The merger and
    the saturation rule consume these."""

    heuristic: Heuristic
    angles: list[RawAngle] = Field(
        min_length=3,
        max_length=12,
        description="Target 5-12 candidate angles; fewer than 5 only when the "
        "heuristic genuinely yields a narrow harvest (say why in notes).",
    )
    strategic_notes: list[StrategicNote] = Field(
        default_factory=list,
        max_length=3,
        description="Secondary channel: up to 3 meta-strategy levers that are not "
        "angles. Zero is the right answer when the map is the whole story.",
    )
    notes: str | None = None


class SubAngle(ArtifactModel):
    """Second (and final) taxonomy level — 'angle → sub-angle where warranted'."""

    name: NonEmptyStr
    definition: NonEmptyStr


class Angle(ArtifactModel):
    """A general solution area; a distinct region of the space."""

    id: Slug
    name: NonEmptyStr
    definition: NonEmptyStr
    distinctness_rationale: NonEmptyStr = Field(
        description="Why this is a distinct region and not a variant of another angle."
    )
    example_options: list[NonEmptyStr] = Field(
        min_length=1,
        description="2-3 concrete options proving the angle is non-empty — "
        "existence proofs, never endorsements.",
    )
    relevance_prior: Probability = Field(
        description="0-1 estimated relevance; drives the S2 budget allocation."
    )
    prior_justification: NonEmptyStr = Field(
        description="One paragraph grounded ONLY in the brief and destination model "
        "— never in preferences."
    )
    contributing_heuristics: list[Heuristic] = Field(
        min_length=1,
        description="Which cartographer framings produced or corroborated this angle.",
    )
    sub_angles: list[SubAngle] = Field(default_factory=list)
    notes: str | None = None


class DedupEntry(ArtifactModel):
    """One raw cartographer angle and where the merger folded it. This mapping is
    the input to the saturation rule's marginal-novelty computation."""

    heuristic: Heuristic
    raw_name: NonEmptyStr
    merged_into: Slug


class AngleMap(ArtifactModel):
    """The merged angle map — angles/map.yaml, reviewed at Gate A."""

    angles: list[Angle] = Field(min_length=1)
    dedup_map: list[DedupEntry] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> AngleMap:
        ids = [a.id for a in self.angles]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"angle ids must be unique; duplicated: {dupes}")
        known = set(ids)
        unknown = sorted({e.merged_into for e in self.dedup_map} - known)
        if unknown:
            raise ValueError(
                f"dedup_map entries point at angle ids that are not in the map: {unknown}; "
                "every merged_into must be the id of an angle in 'angles'"
            )
        return self


class CoverageReport(ArtifactModel):
    """The merger's coverage self-report: which heuristics contributed which
    angles, and where the map feels thin (design §5/S1)."""

    contributions: dict[Heuristic, list[NonEmptyStr]] = Field(
        description="Heuristic -> angle ids it contributed."
    )
    thin_areas: list[NonEmptyStr] = Field(
        default_factory=list,
        description="Regions the merger suspects are under-mapped — Gate A review fodder.",
    )
    strategic_notes: list[StrategicNote] = Field(
        default_factory=list,
        description="Merger-deduplicated strategic notes from the ensemble, with "
        "source_heuristics filled. Surfaced at Gate A; kind routes them onward "
        "(rubric-weight -> S4, execution -> S8, reframe -> the human's call).",
    )
    notes: str | None = None
