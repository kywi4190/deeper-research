"""S2 allocation math: sum conservation, floor/cap invariants, gamma behavior,
degenerate cases, and reflow (design §5/S2, §5/S3)."""

from __future__ import annotations

import math
import random

import pytest

from deeper.allocation import allocate, reflow
from deeper.schemas import AllocationKind, AllocationTable, CardCritique


def units_of(table: AllocationTable) -> dict[str, int]:
    return {row.angle_id: row.units for row in table.rows}


def critique(angle_id: str, missed: list[str] | None = None, redundancy: float = 0.0):
    return CardCritique(
        angle_id=angle_id,
        redundancy_pct=redundancy,
        missed_options=missed or [],
    )


# -- property-style sweep -------------------------------------------------------


def _random_cases(count: int = 300):
    rng = random.Random(20260706)
    for _ in range(count):
        n = rng.randint(1, 15)
        style = rng.choice(["random", "equal", "spiky", "zeros"])
        if style == "equal":
            priors = {f"a{i}": 0.5 for i in range(n)}
        elif style == "spiky":
            priors = {f"a{i}": (1.0 if i == 0 else rng.uniform(0, 0.1)) for i in range(n)}
        elif style == "zeros":
            priors = {f"a{i}": (rng.uniform(0, 1) if rng.random() < 0.5 else 0.0) for i in range(n)}
        else:
            priors = {f"a{i}": rng.uniform(0, 1) for i in range(n)}
        floor = rng.randint(0, 3)
        gamma = rng.choice([0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 4.0])
        cap_pct = rng.choice([25.0, 40.0, 60.0, 100.0])
        budget = max(1, n * floor + rng.randint(0, 40))
        cap_units = math.floor(cap_pct * budget / 100)
        if cap_units < max(floor, 1) or n * cap_units < budget:
            cap_pct = 100.0  # keep the case feasible; infeasibility is tested separately
        yield priors, budget, floor, gamma, cap_pct


@pytest.mark.parametrize("case", list(_random_cases()))
def test_invariants_hold_across_random_cases(case):
    priors, budget, floor, gamma, cap_pct = case
    table = allocate(priors, budget, floor, gamma, cap_pct)
    cap_units = math.floor(cap_pct * budget / 100)

    assert sum(r.units for r in table.rows) == budget, "budget must be conserved exactly"
    assert all(r.units >= floor for r in table.rows), "floor is the exploration guarantee"
    assert all(r.units <= cap_units for r in table.rows), "per-angle cap must hold"
    assert [r.angle_id for r in table.rows] == list(priors), "row order = input order"


def test_deterministic():
    priors = {"a": 0.7, "b": 0.7, "c": 0.31, "d": 0.0}
    one = allocate(priors, 17, 1, 1.3, 40.0)
    two = allocate(priors, 17, 1, 1.3, 40.0)
    assert one == two
    assert one.dump_yaml() == two.dump_yaml()


# -- floor and cap ---------------------------------------------------------------


def test_zero_prior_angle_still_gets_floor():
    table = allocate({"hot": 1.0, "cold": 0.0, "cold2": 0.0}, 10, 2, 1.0, 100.0)
    got = units_of(table)
    assert got["cold"] == 2 and got["cold2"] == 2
    assert got["hot"] == 6


def test_cap_binds_dominant_angle_and_excess_redistributes():
    priors = {"top": 1.0, "b": 0.01, "c": 0.01, "d": 0.01, "e": 0.01}
    table = allocate(priors, 20, 1, 1.0, 25.0)  # cap = 5 units
    got = units_of(table)
    assert got["top"] == 5
    assert sum(got.values()) == 20
    assert all(v >= 1 for v in got.values())


def test_huge_gamma_concentrates_up_to_cap():
    table = allocate({"top": 0.9, "b": 0.1, "c": 0.1, "d": 0.1}, 20, 1, 8.0, 25.0)
    assert units_of(table)["top"] == 5  # pinned at the cap


# -- gamma is the breadth dial ----------------------------------------------------


def test_higher_gamma_concentrates():
    priors = {"a": 0.9, "b": 0.5, "c": 0.2, "d": 0.1}
    tops, spreads = [], []
    for gamma in (0.5, 1.0, 2.0, 4.0):
        got = units_of(allocate(priors, 40, 1, gamma, 100.0))
        tops.append(got["a"])
        spreads.append(max(got.values()) - min(got.values()))
    assert tops == sorted(tops), "top angle's share must not shrink as gamma rises"
    assert spreads == sorted(spreads), "dispersion must not shrink as gamma rises"


def test_gamma_below_one_flattens_toward_uniform():
    priors = {"a": 0.9, "b": 0.3, "c": 0.1}
    sharp = units_of(allocate(priors, 30, 1, 1.0, 100.0))
    flat = units_of(allocate(priors, 30, 1, 0.2, 100.0))
    assert flat["a"] <= sharp["a"]
    assert max(flat.values()) - min(flat.values()) <= max(sharp.values()) - min(sharp.values())


# -- degenerate cases --------------------------------------------------------------


def test_single_angle_gets_entire_budget():
    table = allocate({"only": 0.4}, 12, 2, 1.0, 100.0)
    assert units_of(table) == {"only": 12}


def test_equal_priors_split_evenly():
    got = units_of(allocate({f"a{i}": 0.6 for i in range(4)}, 21, 1, 1.0, 100.0))
    assert sum(got.values()) == 21
    assert max(got.values()) - min(got.values()) <= 1


def test_all_zero_priors_split_evenly():
    got = units_of(allocate({"a": 0.0, "b": 0.0, "c": 0.0}, 9, 1, 1.0, 100.0))
    assert sum(got.values()) == 9
    assert max(got.values()) - min(got.values()) <= 1


def test_budget_exactly_n_times_floor():
    got = units_of(allocate({"a": 0.9, "b": 0.1}, 4, 2, 1.0, 100.0))
    assert got == {"a": 2, "b": 2}


def test_awkward_fractions_still_conserve():
    got = units_of(allocate({"a": 0.5, "b": 0.3, "c": 0.2}, 10, 1, 1.0, 100.0))
    assert sum(got.values()) == 10


# -- infeasible / invalid inputs ----------------------------------------------------


def test_budget_below_n_times_floor_raises():
    with pytest.raises(ValueError, match="infeasible"):
        allocate({"a": 0.5, "b": 0.5, "c": 0.5}, 5, 2, 1.0, 100.0)


def test_cap_below_floor_raises():
    with pytest.raises(ValueError, match="cap"):
        allocate({"a": 0.5, "b": 0.5}, 10, 3, 1.0, 25.0)  # cap = 2 < floor 3


def test_cap_too_tight_to_absorb_budget_raises():
    with pytest.raises(ValueError, match="infeasible"):
        allocate({"a": 0.5, "b": 0.5, "c": 0.5}, 100, 1, 1.0, 25.0)  # 3 × 25 < 100


@pytest.mark.parametrize(
    "kwargs",
    [
        {"priors": {}, "total_budget_units": 10},
        {"priors": {"a": 1.5}, "total_budget_units": 10},
        {"priors": {"a": -0.1}, "total_budget_units": 10},
        {"priors": {"a": 0.5}, "total_budget_units": 0},
        {"priors": {"a": 0.5}, "total_budget_units": 10, "floor": -1},
        {"priors": {"a": 0.5}, "total_budget_units": 10, "gamma": 0.0},
        {"priors": {"a": 0.5}, "total_budget_units": 10, "gamma": -1.0},
        {"priors": {"a": 0.5}, "total_budget_units": 10, "per_angle_cap_pct": 0.0},
        {"priors": {"a": 0.5}, "total_budget_units": 10, "per_angle_cap_pct": 101.0},
    ],
)
def test_invalid_inputs_raise(kwargs):
    defaults = {"floor": 1, "gamma": 1.0, "per_angle_cap_pct": 100.0}
    with pytest.raises(ValueError):
        allocate(**{**defaults, **kwargs})


# -- the artifact ---------------------------------------------------------------------


def test_allocation_table_round_trips():
    table = allocate({"a": 0.8, "b": 0.2}, 10, 2, 1.0, 60.0)
    assert table.kind is AllocationKind.INITIAL
    assert AllocationTable.load_yaml(table.dump_yaml()) == table


# -- reflow (S3 saturation returns) -----------------------------------------------------


def test_reflow_redistributes_only_to_flagged_angles():
    priors = {"a": 0.8, "b": 0.5, "c": 0.3}
    critiques = [
        critique("a", missed=["missed-one"]),
        critique("b"),  # nothing missed — no reflow
        critique("c", missed=["missed-two", "missed-three"]),
    ]
    table = reflow(priors, critiques, returned_units=6, gamma=1.0)
    assert table is not None
    assert table.kind is AllocationKind.REFLOW
    got = units_of(table)
    assert set(got) == {"a", "c"}
    assert sum(got.values()) == 6


def test_reflow_excludes_saturated_angles():
    critiques = [
        critique("a", missed=["x"], redundancy=60.0),  # saturated: no more budget
        critique("b", missed=["y"], redundancy=10.0),
    ]
    table = reflow({"a": 0.9, "b": 0.2}, critiques, returned_units=4, gamma=1.0)
    assert table is not None and units_of(table) == {"b": 4}


def test_reflow_none_when_pool_empty_or_nothing_flagged():
    priors = {"a": 0.5}
    assert reflow(priors, [critique("a", missed=["x"])], returned_units=0, gamma=1.0) is None
    assert reflow(priors, [critique("a")], returned_units=5, gamma=1.0) is None
    assert (
        reflow(priors, [critique("a", missed=["x"], redundancy=90.0)], returned_units=5, gamma=1.0)
        is None
    )


def test_reflow_ignores_critiques_for_unknown_angles():
    table = reflow({"a": 0.5}, [critique("ghost", missed=["x"])], returned_units=3, gamma=1.0)
    assert table is None


def test_reflow_pool_smaller_than_flagged_angles_uses_no_floor():
    critiques = [critique("a", missed=["x"]), critique("b", missed=["y"])]
    table = reflow({"a": 0.5, "b": 0.5}, critiques, returned_units=1, gamma=1.0)
    assert table is not None
    got = units_of(table)
    assert sum(got.values()) == 1
    assert sorted(got.values()) == [0, 1]
