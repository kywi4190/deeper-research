"""Run profiles and config.yaml loading (design §8, §12)."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from deeper.config import (
    DEFAULT_PROFILE,
    PROFILES,
    ConfigError,
    RunConfig,
    SizeClass,
    load_config,
    profile_config,
)


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_shipped_profiles_validate(name):
    cfg = profile_config(name)
    assert cfg.profile == name
    assert set(cfg.size_classes) == set(SizeClass)
    assert all(spec.model for spec in cfg.size_classes.values())


def test_profile_knobs_match_design_section_8():
    quick, standard, exhaustive = (
        profile_config("quick"),
        profile_config("standard"),
        profile_config("exhaustive"),
    )
    # quick ≈ sanity pass: 3 cartographers, floor 1, shortlist 3
    assert (quick.initial_cartographers, quick.floor, quick.shortlist_size) == (3, 1, 3)
    # standard = the design defaults: floor 2, gamma 1.0, cap 25%
    assert (standard.floor, standard.gamma, standard.per_angle_cap_pct) == (2, 1.0, 25.0)
    # exhaustive: 6 cartographers, floor 3, gamma 0.8, shortlist 7, deeper dossier cap
    assert (exhaustive.initial_cartographers, exhaustive.floor) == (6, 3)
    assert (exhaustive.gamma, exhaustive.shortlist_size) == (0.8, 7)
    assert exhaustive.deep_dive_unit_cap > standard.deep_dive_unit_cap > quick.deep_dive_unit_cap


def test_hard_caps_match_design_section_12():
    caps = profile_config("standard").caps
    assert caps.max_cartographers == 8
    assert caps.cartography_novelty_threshold == 0.2
    assert caps.scout_redundancy_stop_pct == 40.0
    assert caps.max_finalists == 7
    assert caps.max_finalists_per_angle == 3
    assert caps.deep_dive_delta_score_stop == 0.15
    assert caps.tournament_new_searches == 3
    assert caps.max_judge_update_rounds == 1
    assert caps.max_redivergence_loops == 1
    assert caps.max_gate_c_loops == 3
    assert caps.max_schema_retries == 2


def test_unknown_profile_name_raises():
    with pytest.raises(ConfigError, match="unknown profile"):
        profile_config("heroic")


def test_config_round_trips_through_yaml():
    cfg = profile_config("standard")
    assert RunConfig.load_yaml(cfg.dump_yaml()) == cfg


# -- load_config: profile + overrides ------------------------------------------------


def test_load_config_merges_overrides_over_profile(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "profile: standard\n"
        "gamma: 2.0\n"
        "caps:\n"
        "  max_gate_c_loops: 2\n"
        "size_classes:\n"
        "  M:\n"
        "    max_searches: 20\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.gamma == 2.0  # overridden
    assert cfg.floor == 2  # inherited from standard
    assert cfg.caps.max_gate_c_loops == 2  # nested override
    assert cfg.caps.max_cartographers == 8  # nested default survives
    assert cfg.size_classes[SizeClass.M].max_searches == 20  # per-class deep merge
    assert cfg.size_classes[SizeClass.M].model  # other M fields survive
    assert cfg.size_classes[SizeClass.S] == profile_config("standard").size_classes[SizeClass.S]


def test_load_config_defaults_to_standard_profile(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("concurrency: 2\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.profile == DEFAULT_PROFILE
    assert cfg.concurrency == 2


@pytest.mark.parametrize(
    "text,match",
    [
        ("profile: heroic\n", "unknown profile"),
        ("profile: quick\nnot_a_knob: 3\n", "not part of the schema"),
        ("profile: quick\ngamma: 0\n", "gamma"),
        ("- just\n- a\n- list\n", "mapping"),
        ("profile: quick\nshortlist_size: 9\n", "hard cap"),
        ("profile: quick\nper_angle_cap_pct: 1\n", "floor"),
    ],
)
def test_load_config_rejects_bad_files(tmp_path, text, match):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_config(path)


# -- RunConfig coherence validators ---------------------------------------------------


def _standard_dict():
    return copy.deepcopy(PROFILES["standard"])


def test_missing_size_class_rejected():
    raw = _standard_dict()
    del raw["size_classes"]["M"]
    with pytest.raises(ValidationError, match="missing.*M"):
        RunConfig.model_validate(raw)


def test_cap_below_floor_rejected():
    raw = _standard_dict()
    raw["per_angle_cap_pct"] = 1.0  # 1% of 40 units = 0 < floor 2
    with pytest.raises(ValidationError, match="floor"):
        RunConfig.model_validate(raw)


def test_cartographers_above_hard_cap_rejected():
    raw = _standard_dict()
    raw["initial_cartographers"] = 9
    with pytest.raises(ValidationError, match="hard cap"):
        RunConfig.model_validate(raw)


def test_mode_defaults_to_mock_and_rejects_junk():
    assert profile_config("quick").mode == "mock"
    raw = _standard_dict()
    raw["mode"] = "yolo"
    with pytest.raises(ValidationError):
        RunConfig.model_validate(raw)
