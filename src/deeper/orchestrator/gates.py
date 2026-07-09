"""Gates as file-edit pause states (design §5 Gates A/B/C, §8).

Entering a gate writes a commented template whose body already parses as a
*valid but undecided* decision (`approved: false`, no actions), so the file the
human edits is the same file `deeper resume` validates. Interpretation is
deterministic: each gate's `interpret` maps a validated decision to exactly one
outcome (advance / loop back / stay paused) — an LLM never touches this.

Template files are never overwritten once they exist: a half-edited gate file
must survive any number of resume attempts.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import yaml
from pydantic import ValidationError

from deeper.allocation import feasibility_problem
from deeper.schemas import (
    Angle,
    AngleMap,
    ArtifactModel,
    GateADecision,
    GateBDecision,
    GateCDecision,
    GateName,
    GateStatus,
    Heuristic,
    PreferenceSlot,
    Rubric,
    Stage,
    format_validation_error,
)
from deeper.workspace import Workspace, WorkspaceError

# Where the Gate A "another pass" hint survives S1 invalidation (angles/ is
# wiped; gates/ keeps everything but the decision file). S1 consumes it.
GATE_A_HINT_PATH = "gates/gate-a-hint.txt"
# A human-added angle enters the map with a neutral prior; the human can pin it
# with a prior_adjustments entry targeting the angle's slug in the same decision.
ADDED_ANGLE_PRIOR = 0.5

# An apply step mutates the workspace once a decision advances the gate:
# (messages to emit, problem). A problem keeps the gate pending — nothing else
# has been committed yet.
ApplyResult = tuple[list[str], str | None]


@dataclass(frozen=True)
class GateOutcome:
    """What one validated gate decision means for the state machine."""

    advanced: bool
    next_stage: Stage | None = None
    gate_status: GateStatus | None = None
    invalidate_to: Stage | None = None  # loop edges re-enter via rerun invalidation
    commit_label: str | None = None
    messages: tuple[str, ...] = ()
    # Workspace mutation the decision requires (Gate A edit actions, hint
    # recording); runs before the state transition so its writes land in the
    # gate's commit, and its problems re-pause instead of advancing.
    apply: Callable[[Workspace], ApplyResult] | None = None
    # Gate C loop actions (§5): interpretation stays pure — the ENGINE executes
    # the loop (it owns the dispatcher, the caps, and the iteration counter).
    loop_decision: GateCDecision | None = None


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def apply_gate_a_actions(workspace: Workspace, decision: GateADecision) -> ApplyResult:
    """Apply an approved Gate A decision's edit actions to angles/map.yaml
    (design §5 Gate A): removals drop the angle (reason logged to the audit
    trail via the returned messages, the kept decision file, and the map diff
    in the gate commit); additions enter the map as human-provenance angles —
    which queues them for S2 allocation and an S3 scout — and prior adjustments
    overwrite relevance priors. All referential problems are reported together
    and nothing is written when any action cannot be applied."""
    angle_map = workspace.read_artifact("angles/map.yaml", AngleMap)
    ids = {a.id for a in angle_map.angles}
    removed_ids = {r.angle_id for r in decision.removed_angles}
    problems: list[str] = []
    additions: list[tuple[str, Any]] = []
    for removal in decision.removed_angles:
        if removal.angle_id not in ids:
            problems.append(f"removed_angles: no angle '{removal.angle_id}' in the map")
    for added in decision.added_angles:
        slug = _slugify(added.name)
        if not slug:
            problems.append(f"added_angles: name {added.name!r} yields an empty slug")
        elif slug in ids or any(s == slug for s, _ in additions):
            problems.append(
                f"added_angles: '{added.name}' collides with existing angle id '{slug}'"
            )
        else:
            additions.append((slug, added))
    known_after = (ids - removed_ids) | {slug for slug, _ in additions}
    for adjustment in decision.prior_adjustments:
        if adjustment.angle_id not in known_after:
            problems.append(
                f"prior_adjustments: no angle '{adjustment.angle_id}' in the post-edit map"
            )
    if not known_after:
        problems.append("the decision removes every angle — the map cannot be emptied")
    if problems:
        return [], (
            "gate-a decision cannot be applied:\n- "
            + "\n- ".join(problems)
            + "\nFix gates/gate-a.yaml, then `deeper resume` again."
        )

    messages: list[str] = []
    angles = [a for a in angle_map.angles if a.id not in removed_ids]
    for removal in decision.removed_angles:
        messages.append(f"gate-a: removed angle '{removal.angle_id}' — {removal.reason}")
    dedup = [e for e in angle_map.dedup_map if e.merged_into not in removed_ids]
    for slug, added in additions:
        angles.append(
            Angle(
                id=slug,
                name=added.name,
                definition=added.note,
                distinctness_rationale=(
                    "Added by the human at Gate A; the assigned scout establishes "
                    "the region's boundaries."
                ),
                example_options=["(to be scouted — human-added angle)"],
                relevance_prior=ADDED_ANGLE_PRIOR,
                prior_justification=(f"Human judgment at Gate A. Note for the scout: {added.note}"),
                contributing_heuristics=[Heuristic.HUMAN],
                notes=f"Scout guidance: {added.note}",
            )
        )
        messages.append(
            f"gate-a: added angle '{slug}' (prior {ADDED_ANGLE_PRIOR}) — queued for scouting"
        )
    by_id = {a.id: a for a in angles}
    for adjustment in decision.prior_adjustments:
        angle = by_id[adjustment.angle_id]
        messages.append(
            f"gate-a: prior of '{angle.id}' {angle.relevance_prior} -> {adjustment.new_prior}"
        )
        angle.relevance_prior = adjustment.new_prior
    config = workspace.load_config()
    infeasible = feasibility_problem(
        len(angles),
        total_budget_units=config.total_budget_units,
        floor=config.floor,
        per_angle_cap_pct=config.per_angle_cap_pct,
    )
    if infeasible is not None:
        # Warn, don't block: the decision is still applied (the human may
        # intend to fix config.yaml next), and S2 pauses on the same check
        # before any budget is spent.
        messages.append(
            f"gate-a: WARNING — the post-edit map is infeasible for this budget: "
            f"{infeasible}. S2 will pause until it fits."
        )
    workspace.write_artifact(
        "angles/map.yaml", AngleMap(angles=angles, dedup_map=dedup, notes=angle_map.notes)
    )
    return messages, None


def record_rerun_hint(workspace: Workspace, hint: str) -> ApplyResult:
    """Persist the Gate A rerun hint where S1 invalidation cannot delete it;
    the S1 pass injects it into every cartographer contract, then consumes it."""
    target = workspace.path(GATE_A_HINT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(hint.strip() + "\n", encoding="utf-8", newline="\n")
    return [
        "gate-a: rerun hint recorded — cartography re-runs with it injected "
        "into every cartographer prompt (this pass only)"
    ], None


def _interpret_a(decision: GateADecision) -> GateOutcome:
    if decision.approved:
        n_added = len(decision.added_angles)
        n_removed = len(decision.removed_angles)
        n_adjusted = len(decision.prior_adjustments)
        has_actions = bool(n_added or n_removed or n_adjusted)
        label = "approved"
        if has_actions:
            label = (
                f"approved ({n_added} added, {n_removed} removed, {n_adjusted} prior(s) adjusted)"
            )
        return GateOutcome(
            advanced=True,
            next_stage=Stage.S2,
            gate_status=GateStatus.APPROVED,
            commit_label=label,
            apply=partial(apply_gate_a_actions, decision=decision) if has_actions else None,
        )
    if decision.rerun_hint is not None:
        return GateOutcome(
            advanced=True,
            next_stage=Stage.S1,
            gate_status=GateStatus.RERUN_REQUESTED,
            invalidate_to=Stage.S1,
            commit_label=f"rerun requested (hint: {decision.rerun_hint})",
            apply=partial(record_rerun_hint, hint=decision.rerun_hint),
        )
    return GateOutcome(
        advanced=False,
        messages=(
            "gates/gate-a.yaml holds no decision yet: set `approved: true` to accept "
            "the angle map (optionally with edit actions), or set `rerun_hint` to "
            "request another cartography pass.",
        ),
    )


def apply_gate_b_actions(workspace: Workspace, decision: GateBDecision) -> ApplyResult:
    """Apply an approved Gate B decision to rubric.yaml (design §5 Gate B):
    criterion edits are full replacements, weight overrides pin the named
    criteria, and the criteria the human left untouched rescale proportionally
    so criterion weights still sum to 1.0 (design P9 — the alternative is
    forcing the human to hand-normalize the whole vector). The preference-slot
    weight is always written: it is the decision this gate exists to record.
    All referential problems are reported together and nothing is written when
    any action cannot be applied."""
    try:
        rubric = workspace.read_artifact("rubric.yaml", Rubric)
    except (WorkspaceError, ValidationError, yaml.YAMLError) as err:
        return [], f"gate-b decision cannot be applied: rubric.yaml is missing or invalid: {err}"

    ids = {c.id for c in rubric.criteria}
    problems: list[str] = []
    edited_ids: set[str] = set()
    for crit in decision.edited_criteria:
        if crit.id not in ids:
            problems.append(f"edited_criteria: no criterion '{crit.id}' in the rubric")
        elif crit.id in edited_ids:
            problems.append(f"edited_criteria: criterion '{crit.id}' is edited twice")
        edited_ids.add(crit.id)
    for cid, weight in decision.weight_overrides.items():
        if cid not in ids:
            problems.append(f"weight_overrides: no criterion '{cid}' in the rubric")
        elif cid in edited_ids:
            problems.append(
                f"weight_overrides: '{cid}' is also in edited_criteria — the edit "
                "already carries a weight; drop one of the two"
            )
        if not 0 < weight <= 1:
            problems.append(f"weight_overrides: '{cid}' weight {weight} must be in (0, 1]")

    replacements = {c.id: c for c in decision.edited_criteria}
    criteria = [replacements.get(c.id, c) for c in rubric.criteria]
    pinned = edited_ids | set(decision.weight_overrides)
    new_weights = {c.id: decision.weight_overrides.get(c.id, c.weight) for c in criteria}
    pinned_sum = sum(w for cid, w in new_weights.items() if cid in pinned)
    free = [c.id for c in criteria if c.id not in pinned]
    factor = 1.0
    if not problems and pinned:
        if not free:
            if abs(pinned_sum - 1.0) > 1e-6:
                problems.append(
                    f"every criterion weight is pinned but they sum to {pinned_sum:g}, "
                    "not 1.0 — adjust the values so the total is exactly 1.0"
                )
        elif pinned_sum >= 1.0 - 1e-9:
            problems.append(
                f"pinned weights sum to {pinned_sum:g}, leaving nothing for the "
                f"{len(free)} untouched criteria (weights must stay > 0)"
            )
        else:
            free_sum = sum(new_weights[cid] for cid in free)
            factor = (1.0 - pinned_sum) / free_sum
            for cid in free:
                new_weights[cid] *= factor
    if problems:
        return [], (
            "gate-b decision cannot be applied:\n- "
            + "\n- ".join(problems)
            + "\nFix gates/gate-b.yaml, then `deeper resume` again."
        )

    messages = [
        f"gate-b: preference-slot weight set to {decision.preference_slot_weight:g} — "
        "the one number that says how much your tastes may bend the "
        "destination-optimal answer"
    ]
    for cid in sorted(edited_ids):
        messages.append(f"gate-b: criterion '{cid}' replaced")
    for cid, weight in decision.weight_overrides.items():
        old = next(c.weight for c in rubric.criteria if c.id == cid)
        messages.append(f"gate-b: weight of '{cid}' {old:g} -> {weight:g}")
    if pinned and free and abs(factor - 1.0) > 1e-9:
        messages.append(
            f"gate-b: rescaled {len(free)} untouched criterion weight(s) by "
            f"{factor:.4g} so criterion weights still sum to 1.0"
        )
    workspace.write_artifact(
        "rubric.yaml",
        Rubric(
            criteria=[c.model_copy(update={"weight": new_weights[c.id]}) for c in criteria],
            preference_slot=PreferenceSlot(weight=decision.preference_slot_weight),
            notes=rubric.notes,
        ),
    )
    return messages, None


def _interpret_b(decision: GateBDecision) -> GateOutcome:
    if decision.approved:
        n_over = len(decision.weight_overrides)
        n_edit = len(decision.edited_criteria)
        label = f"approved (preference-slot weight {decision.preference_slot_weight:g}"
        if n_over or n_edit:
            label += f", {n_over} weight override(s), {n_edit} criterion edit(s)"
        label += ")"
        return GateOutcome(
            advanced=True,
            next_stage=Stage.S5,
            gate_status=GateStatus.APPROVED,
            commit_label=label,
            apply=partial(apply_gate_b_actions, decision=decision),
        )
    return GateOutcome(
        advanced=False,
        messages=(
            "gates/gate-b.yaml holds no decision yet: review rubric.yaml (and "
            "rubric-rationale.md), set `preference_slot_weight`, then set "
            "`approved: true`.",
        ),
    )


def _interpret_c(decision: GateCDecision) -> GateOutcome:
    if decision.approved:
        return GateOutcome(
            advanced=True,
            next_stage=Stage.S8,
            gate_status=GateStatus.APPROVED,
            commit_label="approved",
        )
    if decision.preference_feedback or decision.evidence_challenges or decision.accept_redivergence:
        return GateOutcome(advanced=False, loop_decision=decision)
    return GateOutcome(
        advanced=False,
        messages=(
            "gates/gate-c.yaml holds no decision yet: review the contender pack, then "
            "set `approved: true` — or submit loop actions (preference feedback, "
            "evidence challenges, accept_redivergence) in their own iteration.",
        ),
    )


_TEMPLATE_A = """\
# Gate A — frame review (design §5). THE highest-leverage gate: five minutes here
# prevent a beautifully executed pipeline optimizing inside the wrong map.
#
# Review: angles/map.yaml and angles/map-report.md (read the strategic notes —
# reframe-kind notes are exactly what to weigh before approving the frame).
#
# Record your decision below, then run `deeper resume <run>`. Actions are
# APPLIED on resume: edits land in angles/map.yaml before S2 allocates.
#
#   approved: true              accept the map and continue to S2 allocation
#   rerun_hint: "<hint>"        request another cartography pass with this hint
#                               injected into every cartographer prompt
#                               (mutually exclusive with approved: true)
#   added_angles:               angles the map missed; each enters the map with
#     - name: "..."             #   prior 0.5 and is queued for scouting; its id
#       note: "guidance for the scout"  # is the name as a kebab-case slug —
#                               #   target that slug in prior_adjustments to
#                               #   set a different prior in the same decision
#   removed_angles:
#     - angle_id: some-angle-id
#       reason: "why — logged; the S7 frame-checker re-examines removals"
#   prior_adjustments:
#     - angle_id: some-angle-id
#       new_prior: 0.5
#   notes: "anything else"

approved: false
"""

_TEMPLATE_B = """\
# Gate B — values review (design §5).
#
# THE PREFERENCE-SLOT WEIGHT — set this first. It is the one number that says
# how much your tastes may bend the destination-optimal answer: 0.0 scores every
# option on the destination model alone; 0.4 (the maximum) lets your stated
# preferences claim 40% of every combined score. The final report shows how the
# ranking shifts as this weight sweeps 0 -> 0.4, so you will see exactly what
# your setting did.
preference_slot_weight: 0.2          # design default range 0.15-0.25

# Review rubric.yaml (rubric-rationale.md explains each weight), record your
# decision below, then run `deeper resume <run>`. Edits are APPLIED on resume:
# they land in rubric.yaml before S5 scores anything against it.
#
#   approved: true
#   weight_overrides:                # criterion id -> new weight; criteria you
#     some-criterion-id: 0.3         #   leave untouched rescale proportionally
#                                    #   so criterion weights still sum to 1.0
#   edited_criteria: []              # full replacement Criterion objects
#   notes: "anything else"

approved: false
"""

_TEMPLATE_C = """\
# Gate C — contender review (design §5).
#
# Review: dossiers/, tournament/ (prosecutions, steelman, frame-check), and
# screening/shortlist.md.
#
# Record your decision below, then run `deeper resume <run>`.
#
#   approved: true                   proceed to S8 synthesis
#
# Loop actions (mutually exclusive with approval; each submission is one
# Gate-C iteration, capped at `caps.max_gate_c_loops` per run — design §12):
#   preference_feedback:             the screener converts reactions into
#     - option_id: some-option-id    #   preference-slot adjustments and code
#       reaction: "the ops burden bothers me more than I expected"
#       direction: negative          # positive | negative | neutral
#                                    #   re-scores both scoreboards — a free
#                                    #   re-score, no new research
#   evidence_challenges:             each fires one scoped verifier task; a
#     - option_id: some-option-id    #   contradicted claim lands in the
#       claim_id: some-claim-id      #   contradiction ledger
#       challenge: "I don't believe this"
#   accept_redivergence: true        accept the frame-checker's proposal (only
#                                    #   if tournament/frame-check.md carries
#                                    #   one — the pipeline never re-diverges
#                                    #   on its own): a mini-loop S1->S6 scoped
#                                    #   to the proposed region, on its own
#                                    #   budget, then S7 reruns over the merged
#                                    #   finalists. Max 1 per run (design §12).
#   notes: "anything else"

approved: false
"""

_TEMPLATE_C_CAPPED = """\
# Gate C — contender review (design §5).
#
# ALL GATE-C FEEDBACK LOOPS ARE USED for this run: the §12 diminishing-returns
# policy caps Gate-C iterations (`caps.max_gate_c_loops`), and every further
# loop was judged not worth its cost when the cap was set. The remaining
# action is to approve — the loops already taken are in gates/gate-c.N.yaml
# and the run's commit history.
#
#   approved: true                   proceed to S8 synthesis

approved: false
"""


@dataclass(frozen=True)
class GateSpec:
    """Everything deterministic about one gate."""

    name: GateName
    relpath: str
    model: type[ArtifactModel]
    review_paths: tuple[str, ...]
    template: str
    # Each interpret takes its own gate's decision model; typed loosely because
    # dataclass fields cannot express the per-instance model/interpret pairing.
    interpret: Callable[[Any], GateOutcome] = field(repr=False)


GATE_SPECS: dict[GateName, GateSpec] = {
    GateName.A: GateSpec(
        name=GateName.A,
        relpath="gates/gate-a.yaml",
        model=GateADecision,
        review_paths=("angles/map.yaml", "angles/map-report.md"),
        template=_TEMPLATE_A,
        interpret=_interpret_a,
    ),
    GateName.B: GateSpec(
        name=GateName.B,
        relpath="gates/gate-b.yaml",
        model=GateBDecision,
        review_paths=("rubric.yaml", "rubric-rationale.md"),
        template=_TEMPLATE_B,
        interpret=_interpret_b,
    ),
    GateName.C: GateSpec(
        name=GateName.C,
        relpath="gates/gate-c.yaml",
        model=GateCDecision,
        review_paths=("dossiers/", "tournament/", "screening/shortlist.md"),
        template=_TEMPLATE_C,
        interpret=_interpret_c,
    ),
}

# The stage whose completion opens each gate (also drives rerun invalidation).
GATE_AFTER_STAGE: dict[Stage, GateName] = {
    Stage.S1: GateName.A,
    Stage.S4: GateName.B,
    Stage.S7: GateName.C,
}


def write_template_if_absent(
    workspace: Workspace, spec: GateSpec, template: str | None = None
) -> bool:
    """Write the gate template; returns True when a fresh template was written.
    Never overwrites — a half-edited decision must survive resume. `template`
    overrides the spec's default (the engine selects the capped Gate-C variant
    once the §12 iteration cap is spent)."""
    target = workspace.path(spec.relpath)
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        template if template is not None else spec.template, encoding="utf-8", newline="\n"
    )
    return True


def read_decision(workspace: Workspace, spec: GateSpec) -> tuple[ArtifactModel | None, str | None]:
    """Parse + validate the gate file: (decision, None) or (None, problem)."""
    target = workspace.path(spec.relpath)
    if not target.is_file():
        return None, f"{spec.relpath} is missing"
    text = target.read_text(encoding="utf-8")
    try:
        decision = spec.model.load_yaml(text)
    except yaml.YAMLError as err:
        return None, f"{spec.relpath} is not valid YAML: {err}"
    except ValidationError as err:
        return None, f"{spec.relpath} is not a valid decision:\n" + format_validation_error(
            err, spec.model
        )
    return decision, None
