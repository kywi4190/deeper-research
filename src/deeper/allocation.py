"""S2 budget allocation — pure code, no agents (design §5/S2, P8).

The formula, exactly as specified:

    allocation_i = floor + (B − n·floor) · r_i^γ / Σ_j r_j^γ

with a per-angle cap (default 25% of B) and integer rounding that conserves B.
The same formula also drives the S3 reflow: units returned by early-stopped
scouts are redistributed over the angles whose critics flagged missed options.

Everything here is deterministic: same inputs, same table, byte for byte.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from .schemas import AllocationKind, AllocationRow, AllocationTable, CardCritique

# Float slack for cap-violation checks and integer flooring; allocations are a
# handful of units, so anything this small is a rounding artifact, not signal.
_EPS = 1e-9


def _validate_inputs(
    priors: Mapping[str, float],
    total_budget_units: int,
    floor: int,
    gamma: float,
    per_angle_cap_pct: float,
) -> int:
    """Check feasibility up front; return the integer per-angle cap in units."""
    if not priors:
        raise ValueError("allocation needs at least one angle")
    bad = {a: r for a, r in priors.items() if not (0.0 <= r <= 1.0)}
    if bad:
        raise ValueError(f"relevance priors must be in [0, 1]; out of range: {bad}")
    if total_budget_units < 1:
        raise ValueError(f"total_budget_units must be >= 1 (got {total_budget_units})")
    if floor < 0:
        raise ValueError(f"floor must be >= 0 (got {floor})")
    if gamma <= 0:
        raise ValueError(f"gamma must be > 0 (got {gamma})")
    if not (0 < per_angle_cap_pct <= 100):
        raise ValueError(f"per_angle_cap_pct must be in (0, 100] (got {per_angle_cap_pct})")

    n = len(priors)
    if n * floor > total_budget_units:
        raise ValueError(
            f"budget infeasible: {n} angles × floor {floor} = {n * floor} units "
            f"exceeds the total budget of {total_budget_units}"
        )
    cap_units = math.floor(per_angle_cap_pct * total_budget_units / 100 + _EPS)
    if cap_units < max(floor, 1):
        raise ValueError(
            f"per-angle cap of {per_angle_cap_pct}% of {total_budget_units} units "
            f"is {cap_units} units, below the floor of {max(floor, 1)} — "
            "the cap and floor contradict each other"
        )
    if n * cap_units < total_budget_units:
        raise ValueError(
            f"budget infeasible: {n} angles × cap {cap_units} = {n * cap_units} units "
            f"cannot absorb the total budget of {total_budget_units}"
        )
    return cap_units


def feasibility_problem(
    n_angles: int,
    *,
    total_budget_units: int,
    floor: int,
    per_angle_cap_pct: float,
) -> str | None:
    """The map-size feasibility check as a message, not an exception — what
    Gate A prints as a warning before S2 would pause on it (M1 finding 2: a
    41-angle approved map × floor 1 > B=16 was an unhandled crash)."""
    if n_angles * floor > total_budget_units:
        return (
            f"{n_angles} angles × floor {floor} = {n_angles * floor} units exceeds "
            f"the total budget of {total_budget_units} — remove angles or raise "
            "total_budget_units"
        )
    cap_units = math.floor(per_angle_cap_pct * total_budget_units / 100 + _EPS)
    if n_angles * cap_units < total_budget_units:
        return (
            f"{n_angles} angles × per-angle cap {cap_units} = {n_angles * cap_units} "
            f"units cannot absorb the total budget of {total_budget_units} — the map "
            "is too small for the budget; add angles, lower total_budget_units, or "
            "raise per_angle_cap_pct"
        )
    return None


def _continuous_extra(weights: list[float], extra: float, extra_caps: list[float]) -> list[float]:
    """Water-fill `extra` proportionally to `weights`, respecting per-angle caps.

    Angles pushed past their cap are fixed at it and their overflow is
    redistributed among the rest by the same proportional rule. If every
    remaining weight is zero (all positive-prior angles capped, or all priors
    zero), the residual splits uniformly — the floor's exploration guarantee
    extended to the leftovers.
    """
    assigned = [0.0] * len(weights)
    active = list(range(len(weights)))
    remaining = extra
    while active and remaining > _EPS:
        total_w = sum(weights[i] for i in active)
        shares = {
            i: remaining * (weights[i] / total_w if total_w > 0 else 1 / len(active))
            for i in active
        }
        violators = [i for i in active if shares[i] > extra_caps[i] + _EPS]
        if not violators:
            for i in active:
                assigned[i] = shares[i]
            return assigned
        for i in violators:
            assigned[i] = extra_caps[i]
            remaining -= extra_caps[i]
            active.remove(i)
    return assigned


def _round_conserving(
    reals: list[float],
    total: int,
    floor: int,
    cap_units: int,
    weights: list[float],
) -> list[int]:
    """Largest-remainder rounding that lands exactly on `total`, never breaching
    floor or cap. Ties break by remainder, then weight, then input order —
    fully deterministic."""
    units = []
    remainders = []
    for x in reals:
        base = min(max(math.floor(x + _EPS), floor), cap_units)
        units.append(base)
        remainders.append(x - base)
    leftover = total - sum(units)

    by_desc = sorted(range(len(reals)), key=lambda i: (-remainders[i], -weights[i], i))
    while leftover > 0:
        progressed = False
        for i in by_desc:
            if leftover == 0:
                break
            if units[i] < cap_units:
                units[i] += 1
                leftover -= 1
                progressed = True
        if not progressed:  # unreachable given the n·cap >= B feasibility check
            raise RuntimeError("allocation rounding could not conserve the budget")
    by_asc = sorted(range(len(reals)), key=lambda i: (remainders[i], weights[i], -i))
    while leftover < 0:  # float-artifact safety valve; symmetric to the loop above
        progressed = False
        for i in by_asc:
            if leftover == 0:
                break
            if units[i] > floor:
                units[i] -= 1
                leftover += 1
                progressed = True
        if not progressed:
            raise RuntimeError("allocation rounding could not conserve the budget")
    return units


def allocate(
    priors: Mapping[str, float],
    total_budget_units: int,
    floor: int,
    gamma: float,
    per_angle_cap_pct: float,
    kind: AllocationKind = AllocationKind.INITIAL,
) -> AllocationTable:
    """Allocate `total_budget_units` across angles by relevance prior.

    `priors` maps angle_id → relevance prior (0–1), in angle-map order — row
    order is preserved so gate-time diffs stay readable. Raises ValueError when
    the budget/floor/cap combination is infeasible for this many angles.
    """
    cap_units = _validate_inputs(priors, total_budget_units, floor, gamma, per_angle_cap_pct)
    angle_ids = list(priors)
    n = len(angle_ids)

    weights = [priors[a] ** gamma for a in angle_ids]
    extra = float(total_budget_units - n * floor)
    extra_caps = [float(cap_units - floor)] * n
    assigned_extra = _continuous_extra(weights, extra, extra_caps)
    reals = [floor + e for e in assigned_extra]
    units = _round_conserving(reals, total_budget_units, floor, cap_units, weights)

    return AllocationTable(
        kind=kind,
        total_budget_units=total_budget_units,
        floor=floor,
        gamma=gamma,
        per_angle_cap_pct=per_angle_cap_pct,
        rows=[
            AllocationRow(angle_id=a, relevance_prior=priors[a], units=u)
            for a, u in zip(angle_ids, units, strict=True)
        ],
    )


def reflow(
    priors: Mapping[str, float],
    critiques: Iterable[CardCritique],
    returned_units: int,
    gamma: float,
    redundancy_stop_pct: float = 40.0,
) -> AllocationTable | None:
    """Redistribute units returned by early-stopped scouts (design §5/S3).

    Eligible angles are those whose critic flagged missed options and did NOT
    trip the redundancy stop (a saturated angle gets no more budget even if its
    critic also named misses). The same formula applies, with floor 0 (the
    exploration guarantee was already spent in the initial pass) and cap 100%
    of the pool (the pool is small; re-concentration is the point).

    Returns None when there is nothing to redistribute or no eligible angle.
    """
    if returned_units <= 0:
        return None
    eligible = {
        c.angle_id
        for c in critiques
        if c.missed_options and c.redundancy_pct <= redundancy_stop_pct
    }
    flagged_priors = {a: r for a, r in priors.items() if a in eligible}
    if not flagged_priors:
        return None
    return allocate(
        flagged_priors,
        total_budget_units=returned_units,
        floor=0,
        gamma=gamma,
        per_angle_cap_pct=100.0,
        kind=AllocationKind.REFLOW,
    )
