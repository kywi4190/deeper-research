"""Evaluation artifacts (design §10): benchmark specs, the LLM-judge's
angle-match report, and the per-run eval report.

The five property metrics (breadth, informedness, quality, depth, anti-overfit)
are computed by `deeper.eval` over a completed run's workspace; only the
breadth metric needs an LLM (matching run angles to a benchmark's reference
union is semantic work), and that judge's output is `AngleMatchReport` — a
schema'd artifact dispatched through the same layer as every pipeline agent.

The `EvalReport` is what `deeper eval` persists (eval/eval-report.yaml) and
what `deeper eval --compare` diffs — the evidence base for every prompt/knob
change (design P10: knobs are tuned against measurement, never vibes).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from .base import ArtifactModel, NonEmptyStr, Slug


class ReferenceProvenance(StrEnum):
    """How a reference angle entered the benchmark's union. The distinction is
    load-bearing: a HUMAN_ADDED angle was missed by the original run's ensemble
    and added at Gate A — a fresh run is scored on producing it WITHOUT help
    (the 'practitioner-obvious miss' test case, M2 notes F3)."""

    ORGANIC = "organic"
    HUMAN_ADDED = "human-added"


class ReferenceAngle(ArtifactModel):
    """One angle of a benchmark's reference union (design §10 breadth: 'built
    once per benchmark question by an exhaustive manual + multi-tool pass')."""

    id: Slug
    provenance: ReferenceProvenance = ReferenceProvenance.ORGANIC
    practitioner_obvious: bool = Field(
        default=False,
        description="Missing this angle draws the design's explicit penalty flag: "
        "'penalty for missing any angle a domain practitioner would consider obvious'.",
    )
    note: str | None = None


class OptionCheck(ArtifactModel):
    """Option-level ground truth: a specific option the run should have carded.
    The M2 Trap-2 divergence was an option-level scouting miss the angle-level
    metric alone cannot catch ('compression' appeared in zero cards), so specs
    that know a ground-truth option carry a mechanical carded-at-all check."""

    id: Slug
    description: NonEmptyStr
    evidence_terms: list[NonEmptyStr] = Field(
        min_length=1,
        description="Case-insensitive substrings searched over every option card's "
        "text; any match marks the option as plausibly carded (candidates are "
        "listed for the human to confirm).",
    )
    expectation: str | None = Field(
        default=None,
        description="What the withheld prior says should happen — recorded, never "
        "settled by the eval (adjudication stays the user's).",
    )


class BenchmarkSpec(ArtifactModel):
    """One benchmarks/<id>.yaml question spec (design §10's 4-6 question set)."""

    id: Slug
    question: NonEmptyStr = Field(description="The goal string, verbatim.")
    type: NonEmptyStr = Field(
        description="Question shape tag (design §10): personal-decision, "
        "technical-selection, open-advice — suffixed '-known-ground-truth' "
        "where the user holds a withheld prior."
    )
    profile: NonEmptyStr = Field(description="Run profile the benchmark is calibrated for.")
    fixtures: str | None = Field(
        default=None,
        description="Where reusable run artifacts live (kept locally, gitignored) "
        "and any caveats about their formats.",
    )
    reference_angles: list[ReferenceAngle] = Field(
        default_factory=list,
        description="The reference angle union; empty until a first run seeds it.",
    )
    ground_truth: str | None = Field(
        default=None,
        description="What is actually known vs what remains the user's adjudication "
        "— recorded exactly, never settled by the eval.",
    )
    option_checks: list[OptionCheck] = Field(default_factory=list)
    cost_calibration: str | None = Field(
        default=None, description="Observed spend checkpoints for this question/profile."
    )
    baseline_file: str | None = Field(
        default=None,
        description="Path (relative to this spec) where a plain Deep Research answer "
        "to the same question can be pasted for `deeper eval --compare-baseline`.",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> BenchmarkSpec:
        ref_ids = [a.id for a in self.reference_angles]
        dupes = sorted({i for i in ref_ids if ref_ids.count(i) > 1})
        if dupes:
            raise ValueError(f"reference angle ids must be unique; duplicated: {dupes}")
        check_ids = [c.id for c in self.option_checks]
        dupes = sorted({i for i in check_ids if check_ids.count(i) > 1})
        if dupes:
            raise ValueError(f"option check ids must be unique; duplicated: {dupes}")
        return self


class AngleMatch(ArtifactModel):
    """The judge's verdict on one reference angle: which candidate angle (if
    any) covers substantially the same solution region."""

    reference_id: Slug
    matched_candidate_id: Slug | None = Field(
        default=None,
        description="The candidate angle covering this reference region, or null "
        "when no candidate does (a breadth miss).",
    )
    rationale: NonEmptyStr


class AngleMatchReport(ArtifactModel):
    """The eval-judge's output: every reference angle adjudicated exactly once,
    plus the candidate angles that matched no reference (novel coverage —
    candidate additions to the union, not errors)."""

    matches: list[AngleMatch] = Field(min_length=1)
    novel_candidate_ids: list[Slug] = Field(
        default_factory=list,
        description="Candidate angles that match no reference angle.",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> AngleMatchReport:
        ref_ids = [m.reference_id for m in self.matches]
        dupes = sorted({i for i in ref_ids if ref_ids.count(i) > 1})
        if dupes:
            raise ValueError(
                f"each reference angle gets exactly one match verdict; duplicated: {dupes}"
            )
        matched = {m.matched_candidate_id for m in self.matches if m.matched_candidate_id}
        overlap = sorted(matched & set(self.novel_candidate_ids))
        if overlap:
            raise ValueError(
                f"a candidate cannot be both matched and novel: {overlap}; "
                "novel_candidate_ids are candidates that matched NO reference angle"
            )
        return self


# -- the eval report's per-metric sections ------------------------------------


class OptionCheckResult(ArtifactModel):
    """One option-level ground-truth check's outcome."""

    id: Slug
    description: NonEmptyStr
    carded: bool = Field(description="Any card's text matched an evidence term.")
    matching_card_ids: list[Slug] = Field(
        default_factory=list,
        description="Candidate cards for the human to confirm — a term match is "
        "evidence, not proof, that the ground-truth option was carded.",
    )


class BreadthEval(ArtifactModel):
    """Design §10 breadth: distinct-angle count vs the reference union."""

    run_angle_count: int = Field(ge=0)
    reference_total: int = Field(ge=0)
    hits: list[Slug] = Field(default_factory=list)
    misses: list[Slug] = Field(default_factory=list)
    human_assisted_hits: list[Slug] = Field(
        default_factory=list,
        description="Reference angles whose only match is a human-provenance run "
        "angle (added at a gate): in the map, but the ensemble did not produce "
        "them — a Gate-A rescue must not hide an ensemble miss (M2 F3).",
    )
    practitioner_obvious_misses: list[Slug] = Field(
        default_factory=list,
        description="The design's explicit penalty flag. Includes practitioner-"
        "obvious references that are human-assisted hits: the benchmark scores "
        "the ensemble on producing them WITHOUT human help.",
    )
    matched: dict[Slug, Slug] = Field(
        default_factory=dict, description="Reference angle id -> matching run angle id."
    )
    novel_angles: list[Slug] = Field(
        default_factory=list,
        description="Run angles matching no reference — candidate union additions.",
    )
    option_checks: list[OptionCheckResult] = Field(default_factory=list)


class InformednessRow(ArtifactModel):
    angle_id: Slug
    relevance_prior: float = Field(ge=0, le=1)
    units: int = Field(ge=0)
    finalist_count: int = Field(ge=0)
    value_share: float = Field(
        ge=0, le=1, description="Share of finalists sourced from this angle."
    )


class InformednessEval(ArtifactModel):
    """Design §10 informedness: did budget go where the finalists came from —
    without collapsing the floor?"""

    spearman: float | None = Field(
        default=None,
        ge=-1,
        le=1,
        description="Rank correlation between allocation units and post-hoc angle "
        "value; null when either side has no variance (e.g. floor-dominated "
        "allocations — see floor_share_pct).",
    )
    rows: list[InformednessRow] = Field(min_length=1)
    floor: int = Field(ge=0)
    floor_compliant: bool = Field(description="Every allocated angle received at least the floor.")
    floor_share_pct: float = Field(
        ge=0,
        le=100,
        description="Share of the budget consumed by the exploration floor "
        "(n*floor/B). Near 100 means gamma was inert (M2 notes F7).",
    )


class QualityRow(ArtifactModel):
    angle_id: Slug
    revised: bool = Field(description="The critique reported fixable issues (S3's rule).")
    redundancy_pct: float = Field(ge=0, le=100)
    missed_options: int = Field(ge=0)
    schema_retries: int = Field(
        ge=0, description="Schema/coherence retries in this angle's context."
    )


class QualityEval(ArtifactModel):
    """Design §10 quality: critic revision rate + schema-failure rate — falling
    over time = prompts improving."""

    rows: list[QualityRow] = Field(min_length=1)
    revision_rate: float = Field(ge=0, le=1)
    schema_retry_total: int = Field(ge=0)
    schema_retries_by_stage: dict[str, int] = Field(default_factory=dict)


class DepthRow(ArtifactModel):
    option_id: Slug
    pass_rate: float = Field(ge=0, le=1)
    load_bearing_total: int = Field(ge=0)
    load_bearing_high: int = Field(ge=0)
    budget_capped: bool
    rounds: int = Field(ge=1)


class DepthEval(ArtifactModel):
    """Design §10 depth: verifier pass rate, load-bearing confidence, capped count."""

    rows: list[DepthRow] = Field(min_length=1)
    overall_pass_rate: float = Field(
        ge=0, le=1, description="Verified / sampled across every verification report."
    )
    load_bearing_high_pct: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Share of load-bearing claims at high confidence; null when "
        "no dossier carries a load-bearing claim.",
    )
    budget_capped_count: int = Field(ge=0)


class InversionPair(ArtifactModel):
    demoted: Slug = Field(description="Wins on the destination-only board.")
    promoted: Slug = Field(description="Wins on the preference-adjusted board.")


class AntiOverfitEval(ArtifactModel):
    """Design §10 anti-overfit: do the scoreboards differ, and did the
    tournament examine every inversion? Asserted from tournament artifacts."""

    preference_slot_weight: float = Field(ge=0, le=1)
    boards_differ: bool = Field(description="Any option ranks differently on the two boards.")
    inversions: list[InversionPair] = Field(default_factory=list)
    inversions_steelmanned: bool = Field(
        description="Every inversion-demoted option has a rank-inversion steelman "
        "in tournament/ — vacuously true when there are no inversions."
    )
    missing_steelmen: list[Slug] = Field(default_factory=list)


class BaselineEval(ArtifactModel):
    """The A/B scaffold: a plain Deep Research answer's angle coverage against
    the same reference union, scored by the same judge (design §10: the system
    must visibly beat it — if it doesn't, the eval names the stage to fix)."""

    source_file: NonEmptyStr
    hits: list[Slug] = Field(default_factory=list)
    misses: list[Slug] = Field(default_factory=list)
    practitioner_obvious_misses: list[Slug] = Field(default_factory=list)
    novel_angles: list[Slug] = Field(
        default_factory=list,
        description="Angles the baseline answer covers that are outside the union.",
    )


class EvalReport(ArtifactModel):
    """eval/eval-report.yaml — the machine-readable eval; the .md render is the
    human view. A metric section is null when the run has not produced its
    input artifacts yet (a partial run evaluates partially, with the gaps
    named in `skipped`)."""

    run_id: NonEmptyStr
    profile: NonEmptyStr
    benchmark_id: Slug | None = None
    generated_at: datetime
    breadth: BreadthEval | None = None
    informedness: InformednessEval | None = None
    quality: QualityEval | None = None
    depth: DepthEval | None = None
    anti_overfit: AntiOverfitEval | None = None
    baseline: BaselineEval | None = None
    spend_by_stage: dict[str, float] = Field(default_factory=dict)
    total_usd: float = Field(ge=0)
    eval_usd: float = Field(
        ge=0, description="What this eval's own judge dispatches cost (stage EVAL)."
    )
    skipped: list[NonEmptyStr] = Field(
        default_factory=list, description="Why any null metric could not be computed."
    )
    notes: str | None = None
