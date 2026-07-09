"""Run configuration: profiles, budgets, size classes, hard caps (design §8, §12).

A run's `config.yaml` starts from one of three shipped profiles
(quick / standard / exhaustive) and may override any field. The materialized
RunConfig is written into the run workspace, so every knob that shaped a run
is auditable from the run directory alone.
"""

from __future__ import annotations

import copy
import math
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from .schemas import ArtifactModel, format_validation_error
from .schemas.base import NonEmptyStr


class ConfigError(Exception):
    """A config file that cannot become a valid RunConfig."""


class SizeClass(StrEnum):
    S = "S"
    M = "M"
    L = "L"


class SizeClassSpec(ArtifactModel):
    """One row of the size-class table: what a budget unit of this size buys
    (design §8 budget accounting)."""

    model: NonEmptyStr = Field(description="Model id dispatched for this size class.")
    max_searches: int = Field(ge=0, description="Web-search budget per invocation.")
    max_output_tokens: int = Field(ge=1)


class HardCaps(ArtifactModel):
    """The §12 diminishing-returns table: every expansion loop's stop condition
    and hard cap, as numbers code can enforce."""

    max_interview_questions: int = Field(
        default=8, ge=1, description="S0 interview question budget (design §5/S0: '~8')."
    )
    max_cartographers: int = Field(default=8, ge=1)
    cartography_novelty_threshold: float = Field(
        default=0.2,
        ge=0,
        le=1,
        description="Marginal novelty below this over the window = saturated.",
    )
    cartography_novelty_window: int = Field(
        default=2, ge=1, description="Number of trailing cartographers the novelty rule averages."
    )
    scout_redundancy_stop_pct: float = Field(
        default=40.0,
        ge=0,
        le=100,
        description="Critic redundancy above this stops the angle early (units reflow).",
    )
    max_finalists: int = Field(default=7, ge=1)
    max_finalists_per_angle: int = Field(
        default=3, ge=1, description="S5 angle-diversity guardrail."
    )
    deep_dive_delta_score_stop: float = Field(
        default=0.15,
        ge=0,
        description="S6 stability rule: stop when Δscore falls below this AND no "
        "load-bearing claim is still low-confidence.",
    )
    tournament_new_searches: int = Field(
        default=3, ge=0, description="Fresh searches each tournament role may spend."
    )
    max_judge_update_rounds: int = Field(default=1, ge=0)
    max_redivergence_loops: int = Field(default=1, ge=0)
    max_gate_c_loops: int = Field(default=3, ge=0)
    max_schema_retries: int = Field(
        default=2, ge=0, description="Validation retries before the run pauses for a human (§6)."
    )


class RunConfig(ArtifactModel):
    """config.yaml — the complete knob set for one run."""

    profile: NonEmptyStr = Field(description="quick | standard | exhaustive.")
    goal: str | None = Field(
        default=None,
        description="The user's research question, verbatim — set by `deeper new` "
        "before S0 exists to restate it into brief.md.",
    )
    mode: Literal["mock", "live"] = Field(
        default="mock", description="Agent dispatch mode; mock runs the pipeline offline."
    )
    billing: Literal["subscription", "api"] = Field(
        default="subscription",
        description="How live runs are authorized: 'subscription' (default) forces "
        "subagents onto the Claude Code CLI's stored login — plan usage, never the "
        "metered ANTHROPIC_API_KEY, even if one is set in the environment; 'api' "
        "requires ANTHROPIC_API_KEY and bills it directly.",
    )
    total_budget_units: int = Field(ge=1, description="B: the S2 option-scouting budget.")
    floor: int = Field(ge=0, description="Minimum units per surviving angle.")
    gamma: float = Field(gt=0, description="Concentration exponent — the breadth dial.")
    per_angle_cap_pct: float = Field(gt=0, le=100)
    initial_cartographers: int = Field(ge=1, description="S1 ensemble size before saturation.")
    shortlist_size: int = Field(ge=1)
    shortlist_threshold: float = Field(
        ge=1,
        le=5,
        description="Absolute UCB floor (1–5 rubric scale): no option advances with "
        "an upper confidence bound below this, however high it ranks.",
    )
    shortlist_dark_horse_margin: float = Field(
        default=0.25,
        ge=0,
        description="S5 concentration rule: beyond the top shortlist_size options by "
        "UCB, an option still advances when its UCB is within this margin of the "
        "shortlist_size-th finalist's UCB — the dark-horse property of the old "
        "absolute-threshold rule, kept relative so screener band inflation cannot "
        "advance the whole field (M1 live run: 26 'finalists' of 52).",
    )
    preference_slot_default_weight: float = Field(
        default=0.20,
        ge=0,
        le=0.4,
        description="Preference-slot weight S4 writes into the rubric (design §5/S4: "
        "default 15-25%); the human sets the final value at Gate B.",
    )
    max_spend_usd: float = Field(
        default=5.0,
        gt=0,
        description="USD spend guard: the dispatcher refuses new agent invocations "
        "once the run's ledger crosses this, pausing the run for human attention "
        "(design §11 runaway-cost mitigation). Raise it and resume to continue.",
    )
    deep_dive_unit_cap: int = Field(ge=1, description="S6 per-option research budget in units.")
    dispatch_timeout_s: float = Field(
        default=1200.0,
        gt=0,
        description="Per-invocation wall-clock timeout: a hung SDK call (observed "
        "live: 50 minutes) becomes a normal transient dispatch failure — backoff "
        "retries, then a resumable pause — instead of hanging the run forever.",
    )
    concurrency: int = Field(ge=1, description="Max simultaneous agent invocations.")
    size_classes: dict[SizeClass, SizeClassSpec]
    caps: HardCaps = Field(default_factory=HardCaps)

    @model_validator(mode="after")
    def _coherent(self) -> RunConfig:
        missing = sorted(c.value for c in SizeClass if c not in self.size_classes)
        if missing:
            raise ValueError(f"size_classes must define all of S, M, L; missing: {missing}")
        cap_units = math.floor(self.per_angle_cap_pct * self.total_budget_units / 100)
        if cap_units < max(self.floor, 1):
            raise ValueError(
                f"per_angle_cap_pct {self.per_angle_cap_pct}% of {self.total_budget_units} "
                f"units is {cap_units} units, below the floor of {max(self.floor, 1)} — "
                "no allocation could ever satisfy both"
            )
        if self.shortlist_size > self.caps.max_finalists:
            raise ValueError(
                f"shortlist_size {self.shortlist_size} exceeds the hard cap of "
                f"{self.caps.max_finalists} finalists (design §12)"
            )
        if self.initial_cartographers > self.caps.max_cartographers:
            raise ValueError(
                f"initial_cartographers {self.initial_cartographers} exceeds the hard cap "
                f"of {self.caps.max_cartographers} (design §12)"
            )
        return self


def _size_classes(
    s: tuple[int, int], m: tuple[int, int], length: tuple[int, int]
) -> dict[str, dict[str, Any]]:
    """(max_searches, max_output_tokens) per class. Model mix per design §6:
    Haiku-class for mechanical work, Sonnet-class workhorses, Opus-class at the
    reasoning leverage points."""
    models = {"S": "claude-haiku-4-5-20251001", "M": "claude-sonnet-5", "L": "claude-opus-4-8"}
    budgets = {"S": s, "M": m, "L": length}
    return {
        cls: {
            "model": models[cls],
            "max_searches": budgets[cls][0],
            "max_output_tokens": budgets[cls][1],
        }
        for cls in ("S", "M", "L")
    }


# Shipped defaults (design §8): quick ≈ sanity pass, standard = the design's
# defaults, exhaustive = wider ensemble, flatter gamma, deeper dossiers.
# Spend caps are calibrated against the M1 live run's ledger (finding 4: the
# old $5 quick cap tripped three times before Gate B on a run that honestly
# costs $20-25 post-fixes) — the cap is a runaway guard, not a target.
# Quick also caps cartography at ONE expansion pass (3 initial + 1): the
# sanity profile must not fund a saturation loop to the global 8-invocation
# ceiling the way the M1 run did.
PROFILES: dict[str, dict[str, Any]] = {
    "quick": {
        "profile": "quick",
        "total_budget_units": 16,
        "floor": 1,
        "gamma": 1.0,
        "per_angle_cap_pct": 25.0,
        "initial_cartographers": 3,
        "shortlist_size": 3,
        "shortlist_threshold": 3.5,
        "max_spend_usd": 30.0,
        "deep_dive_unit_cap": 2,
        "concurrency": 4,
        "size_classes": _size_classes((2, 4000), (6, 8000), (12, 16000)),
        "caps": {"max_cartographers": 4},
    },
    "standard": {
        "profile": "standard",
        "total_budget_units": 40,
        "floor": 2,
        "gamma": 1.0,
        "per_angle_cap_pct": 25.0,
        "initial_cartographers": 5,
        "shortlist_size": 5,
        "shortlist_threshold": 3.5,
        "max_spend_usd": 100.0,
        "deep_dive_unit_cap": 4,
        "concurrency": 4,
        "size_classes": _size_classes((3, 4000), (12, 16000), (25, 32000)),
    },
    "exhaustive": {
        "profile": "exhaustive",
        "total_budget_units": 80,
        "floor": 3,
        "gamma": 0.8,
        "per_angle_cap_pct": 25.0,
        "initial_cartographers": 6,
        "shortlist_size": 7,
        "shortlist_threshold": 3.25,
        "max_spend_usd": 200.0,
        "deep_dive_unit_cap": 6,
        "concurrency": 4,
        "size_classes": _size_classes((4, 4000), (18, 24000), (35, 48000)),
    },
}

DEFAULT_PROFILE = "standard"


def profile_config(name: str = DEFAULT_PROFILE) -> RunConfig:
    """A shipped profile's RunConfig, untouched."""
    if name not in PROFILES:
        raise ConfigError(f"unknown profile {name!r}; expected one of {sorted(PROFILES)}")
    return RunConfig.model_validate(copy.deepcopy(PROFILES[name]))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: str | Path) -> RunConfig:
    """Load a user config.yaml: a `profile:` name plus any field overrides,
    deep-merged over that profile's defaults and validated as a whole."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a YAML mapping, got {type(data).__name__}")
    profile = data.get("profile", DEFAULT_PROFILE)
    if profile not in PROFILES:
        raise ConfigError(f"unknown profile {profile!r}; expected one of {sorted(PROFILES)}")
    merged = _deep_merge(copy.deepcopy(PROFILES[profile]), data)
    try:
        return RunConfig.model_validate(merged)
    except ValidationError as e:
        raise ConfigError(format_validation_error(e, RunConfig)) from e
