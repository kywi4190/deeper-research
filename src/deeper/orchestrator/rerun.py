"""Surgical re-execution: `deeper rerun <run> --stage S3 [--angle x]` (design §8).

Invalidation is git-tracked deletion: the target stage's output subtree (angle-
scoped for S3) plus *everything downstream* — stage outputs, gate decision
files, gate statuses — is removed and committed. Deletion, not a stale flag, is
what keeps the engine's idempotency checks honest: stale outputs must not look
complete on re-entry. Spend entries and retry counts are never touched (audit
trail). Recovery is one `git revert` away because the deletion is a commit.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

from deeper.schemas import AngleMap, GateStatus, RunStatus, Stage
from deeper.workspace import Workspace

from .gates import GATE_AFTER_STAGE, GATE_SPECS


class RerunError(Exception):
    """A rerun request that cannot be honored (unknown angle, bad scope)."""


# What each stage owns in the workspace tree (design §7).
STAGE_ARTIFACTS: dict[Stage, tuple[str, ...]] = {
    Stage.S0: ("brief.md", "destination.md", "preferences.yaml"),
    Stage.S1: ("angles",),
    Stage.S2: ("allocation.yaml",),
    Stage.S3: ("options",),
    Stage.S4: ("rubric.yaml", "rubric-rationale.md"),
    Stage.S5: ("screening",),
    Stage.S6: ("dossiers",),
    Stage.S7: ("tournament",),
    Stage.S8: ("report",),
}

# Directory skeleton to restore after deleting a directory-owned subtree, so the
# §7 tree shape survives invalidation.
_SKELETON: dict[str, tuple[str, ...]] = {
    "angles": ("angles", "angles/raw"),
    "options": ("options",),
    "screening": ("screening",),
    "dossiers": ("dossiers",),
    "tournament": ("tournament",),
    "report": ("report",),
}

STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)


def _resolve_angle(workspace: Workspace, angle: str) -> str:
    """Match --angle against the map's ids and names; returns the angle id."""
    angle_map = workspace.read_artifact("angles/map.yaml", AngleMap)
    for a in angle_map.angles:
        if angle == a.id or angle.lower() == a.name.lower():
            return a.id
    known = ", ".join(a.id for a in angle_map.angles)
    raise RerunError(f"unknown angle {angle!r}; the map has: {known}")


def _delete(workspace: Workspace, relpath: str) -> bool:
    """Remove a file or subtree (restoring the dir skeleton); True if it existed."""
    target = workspace.path(relpath)
    if target.is_dir():
        shutil.rmtree(target)
        for d in _SKELETON.get(relpath, ()):
            workspace.path(d).mkdir(parents=True, exist_ok=True)
        return True
    if target.is_file():
        target.unlink()
        return True
    return False


def invalidate(workspace: Workspace, stage: Stage, angle: str | None = None) -> list[str]:
    """Invalidate `stage`'s outputs (angle-scoped for S3) and everything
    downstream; point the run back at `stage`; commit. Returns what was removed."""
    if angle is not None and stage is not Stage.S3:
        raise RerunError("--angle only applies to --stage S3 (per-angle scout outputs)")
    angle_id = _resolve_angle(workspace, angle) if angle is not None else None

    removed: list[str] = []
    for s in STAGE_ORDER[STAGE_ORDER.index(stage) :]:
        for relpath in STAGE_ARTIFACTS[s]:
            if s is Stage.S3 and angle_id is not None:
                relpath = f"options/{angle_id}"
            if _delete(workspace, relpath):
                removed.append(relpath)
        gate = GATE_AFTER_STAGE.get(s)
        if gate is not None and _delete(workspace, GATE_SPECS[gate].relpath):
            removed.append(GATE_SPECS[gate].relpath)

    state = workspace.load_state()
    for s in STAGE_ORDER[STAGE_ORDER.index(stage) :]:
        gate = GATE_AFTER_STAGE.get(s)
        if gate is not None:
            state.gates[gate] = GateStatus.NOT_REACHED
    state.stage = stage
    state.status = RunStatus.RUNNING
    state.pending_gate = None
    state.updated_at = datetime.now(UTC)
    workspace.save_state(state)
    scope = f" (angle: {angle_id})" if angle_id is not None else ""
    workspace.commit(f"rerun {stage.value}{scope}: invalidated {', '.join(removed) or 'nothing'}")
    return removed
