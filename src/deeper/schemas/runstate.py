"""Orchestrator checkpoint — state.json (design §7, §8).

The workspace is the complete state of a run; RunState is the machine-readable
core of it: current stage, spend audit trail, retry counts, gate statuses.
Serialized as JSON, not YAML.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from .base import ArtifactModel, NonEmptyStr


class Stage(StrEnum):
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"
    S7 = "S7"
    S8 = "S8"


class RunStatus(StrEnum):
    RUNNING = "running"
    GATE_PENDING = "gate-pending"
    PAUSED_ATTENTION = "paused-attention"  # e.g. repeated schema failure — human needed
    DONE = "done"


class GateName(StrEnum):
    A = "gate-a"
    B = "gate-b"
    C = "gate-c"


class GateStatus(StrEnum):
    NOT_REACHED = "not-reached"
    PENDING = "pending"
    APPROVED = "approved"
    RERUN_REQUESTED = "rerun-requested"


class SpendEntry(ArtifactModel):
    """One agent invocation's cost — the audit trail behind every budget cap.
    Aggregations are computed from the entries, so they cannot drift."""

    stage: Stage
    role: NonEmptyStr
    context: str | None = Field(
        default=None, description="Angle or option this invocation worked on, if any."
    )
    usd: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    at: datetime


class RunState(ArtifactModel):
    """state.json."""

    run_id: NonEmptyStr
    profile: NonEmptyStr = Field(description="quick | standard | exhaustive (from config).")
    stage: Stage
    status: RunStatus
    pending_gate: GateName | None = None
    gates: dict[GateName, GateStatus] = Field(
        default_factory=lambda: {g: GateStatus.NOT_REACHED for g in GateName}
    )
    spend: list[SpendEntry] = Field(default_factory=list)
    retry_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Schema-retry counts keyed by agent invocation (max 2 before "
        "PAUSED_ATTENTION).",
    )
    gate_c_iterations: int = Field(
        default=0,
        ge=0,
        description="Gate-C feedback loops consumed (§12: hard cap, then decide). "
        "Never reset — the cap is per run, surviving rerun invalidation.",
    )
    redivergence_runs: int = Field(
        default=0,
        ge=0,
        description="Re-divergence mini-loops executed (§12: 1 per run). Never reset.",
    )
    updated_at: datetime

    @model_validator(mode="after")
    def _gate_pending_consistent(self) -> RunState:
        if (self.status is RunStatus.GATE_PENDING) != (self.pending_gate is not None):
            raise ValueError("pending_gate must be set exactly when status is 'gate-pending'")
        return self

    # -- spend aggregations (design: 'spend by stage/agent') --------------------

    def spend_by_stage(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.spend:
            out[e.stage.value] = out.get(e.stage.value, 0.0) + e.usd
        return out

    def spend_by_role(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.spend:
            out[e.role] = out.get(e.role, 0.0) + e.usd
        return out

    def total_usd(self) -> float:
        return sum(e.usd for e in self.spend)
