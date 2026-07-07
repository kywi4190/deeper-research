"""S1 cartography tests: the parallel ensemble over the canned scenario, the
saturation rule end-to-end (expansion passes against contrived fixtures, hard
cap), Gate-A hint injection/consumption, and idempotent re-entry."""

from __future__ import annotations

from pathlib import Path

from deeper.schemas import AngleMap, CoverageReport, Heuristic
from deeper.stages import CartographyStage

from .helpers import (
    RecordingMockDispatcher,
    angle_map_yaml,
    cartographer_report_yaml,
    coverage_report_yaml,
    make_ctx,
    make_workspace,
    write_s0_artifacts,
)

FP = Heuristic.FIRST_PRINCIPLES
AN = Heuristic.ANALOGIST
CO = Heuristic.CONTRARIAN


async def test_standard_ensemble_saturates_on_the_canned_scenario(tmp_path):
    """The shipped fixtures: 5 cartographers with heavy overlap; the trailing
    window (practitioner 1/3, taxonomist 0/4) means saturation with no
    expansion pass — and a map of >=8 angles from >=4 heuristics."""
    ws = make_workspace(tmp_path, profile="standard")
    write_s0_artifacts(ws)
    emitted: list[str] = []
    ctx = make_ctx(ws, emitted=emitted)
    stage = CartographyStage()
    await stage.execute(ctx)

    raw = sorted(p.name for p in ws.path("angles/raw").glob("*.yaml"))
    assert raw == [
        "analogist.yaml",
        "contrarian.yaml",
        "first-principles.yaml",
        "practitioner.yaml",
        "taxonomist.yaml",
    ]  # exactly the initial ensemble — no expansion pass
    angle_map = ws.read_artifact("angles/map.yaml", AngleMap)
    ws.read_artifact("angles/map-report.md", CoverageReport)
    assert len(angle_map.angles) >= 8
    contributing = set()
    for angle in angle_map.angles:
        contributing.update(angle.contributing_heuristics)
    assert len(contributing - {Heuristic.HUMAN}) >= 4
    assert any("saturated" in line for line in emitted)
    assert any("novelty" in line for line in emitted)
    assert stage.is_complete(ctx)


def _expansion_fixtures(fixtures: Path) -> None:
    """Contrived scenario forcing one expansion round under the quick profile
    (initial: first-principles, analogist, contrarian):

      fp A1-A3 -> a1-a3 (novelty 3/3)      analogist B1-B3 -> b1,b2,a1 (2/3)
      contrarian C1-C3 -> c1-c3 (3/3)      trailing mean 0.83 -> expand
      most novel: fp(3) and contrarian(3, tie -> later)  -> spawn *-2 passes
      fp-2 D1-D3 and contrarian-2 E1-E3 all fold into existing -> mean 0 -> stop
    """
    reports = {
        ("cartographer-first-principles", "cartographer-report.yaml"): (FP, ["A1", "A2", "A3"]),
        ("cartographer-analogist", "cartographer-report.yaml"): (AN, ["B1", "B2", "B3"]),
        ("cartographer-contrarian", "cartographer-report.yaml"): (CO, ["C1", "C2", "C3"]),
        ("cartographer-first-principles", "cartographer-report.first-principles-2.yaml"): (
            FP,
            ["D1", "D2", "D3"],
        ),
        ("cartographer-contrarian", "cartographer-report.contrarian-2.yaml"): (
            CO,
            ["E1", "E2", "E3"],
        ),
    }
    for (role, filename), (heuristic, names) in reports.items():
        target = fixtures / role / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(cartographer_report_yaml(heuristic, names), encoding="utf-8")
    dedup = [
        (FP, "A1", "a1"),
        (FP, "A2", "a2"),
        (FP, "A3", "a3"),
        (AN, "B1", "b1"),
        (AN, "B2", "b2"),
        (AN, "B3", "a1"),
        (CO, "C1", "c1"),
        (CO, "C2", "c2"),
        (CO, "C3", "c3"),
        (FP, "D1", "a1"),
        (FP, "D2", "a2"),
        (FP, "D3", "a3"),
        (CO, "E1", "c1"),
        (CO, "E2", "c2"),
        (CO, "E3", "c3"),
    ]
    merger = fixtures / "merger"
    merger.mkdir(parents=True, exist_ok=True)
    (merger / "angle-map.yaml").write_text(
        angle_map_yaml(["a1", "a2", "a3", "b1", "b2", "c1", "c2", "c3"], dedup),
        encoding="utf-8",
    )
    (merger / "coverage-report.yaml").write_text(coverage_report_yaml(), encoding="utf-8")


async def test_high_novelty_spawns_the_most_novel_heuristics(tmp_path):
    fixtures = tmp_path / "fixtures"
    _expansion_fixtures(fixtures)
    ws = make_workspace(tmp_path, profile="quick")
    write_s0_artifacts(ws)
    emitted: list[str] = []
    ctx = make_ctx(ws, emitted=emitted, fixtures_dir=fixtures)
    stage = CartographyStage()
    await stage.execute(ctx)

    raw = sorted(p.name for p in ws.path("angles/raw").glob("*.yaml"))
    assert raw == [
        "analogist.yaml",
        "contrarian-2.yaml",
        "contrarian.yaml",
        "first-principles-2.yaml",
        "first-principles.yaml",
    ]
    assert any("spawning first-principles-2, contrarian-2" in line for line in emitted)
    assert any("saturated" in line for line in emitted)
    assert stage.is_complete(ctx)


async def test_expansion_respects_the_hard_cap(tmp_path):
    fixtures = tmp_path / "fixtures"
    _expansion_fixtures(fixtures)
    ws = make_workspace(tmp_path, profile="quick", caps={"max_cartographers": 4})
    write_s0_artifacts(ws)
    emitted: list[str] = []
    ctx = make_ctx(ws, emitted=emitted, fixtures_dir=fixtures)
    stage = CartographyStage()
    await stage.execute(ctx)

    raw = sorted(p.name for p in ws.path("angles/raw").glob("*.yaml"))
    # budget for extras = min(2, cap 4 - 3 dispatched) = 1 -> only the top heuristic
    assert raw == [
        "analogist.yaml",
        "contrarian.yaml",
        "first-principles-2.yaml",
        "first-principles.yaml",
    ]
    assert any("hard cap" in line for line in emitted)
    assert stage.is_complete(ctx)


async def test_expansion_pass_prompt_names_already_mapped_angles(tmp_path):
    fixtures = tmp_path / "fixtures"
    _expansion_fixtures(fixtures)
    ws = make_workspace(tmp_path, profile="quick")
    write_s0_artifacts(ws)
    dispatcher = RecordingMockDispatcher(ws, ws.load_config(), fixtures_dir=fixtures)
    await CartographyStage().execute(make_ctx(ws, dispatcher=dispatcher))
    second_pass = [p for role, c, p in dispatcher.invocations if c == "first-principles-2"]
    assert len(second_pass) == 1
    assert "EXPANSION PASS" in second_pass[0]
    assert "- a1" in second_pass[0]  # already-mapped angle names listed


async def test_gate_a_hint_injected_into_every_cartographer_and_consumed(tmp_path):
    ws = make_workspace(tmp_path, profile="quick")
    write_s0_artifacts(ws)
    hint = "explore hardware and infrastructure angles"
    ws.path("gates/gate-a-hint.txt").write_text(hint + "\n", encoding="utf-8")
    dispatcher = RecordingMockDispatcher(ws, ws.load_config())
    stage = CartographyStage()
    ctx = make_ctx(ws, dispatcher=dispatcher)
    assert not stage.is_complete(ctx)  # a pending hint always means another pass
    await stage.execute(ctx)

    cartographer_prompts = [
        prompt for role, _, prompt in dispatcher.invocations if role.startswith("cartographer-")
    ]
    assert len(cartographer_prompts) == 3
    assert all(hint in prompt for prompt in cartographer_prompts)
    merger_prompts = [prompt for role, _, prompt in dispatcher.invocations if role == "merger"]
    assert all("GATE A RERUN HINT" not in prompt for prompt in merger_prompts)
    assert not ws.path("gates/gate-a-hint.txt").exists()  # applied to one pass only


async def test_reentry_skips_completed_raw_reports_and_merger(tmp_path):
    ws = make_workspace(tmp_path, profile="quick")
    write_s0_artifacts(ws)
    await CartographyStage().execute(make_ctx(ws))
    dispatcher = RecordingMockDispatcher(ws, ws.load_config())
    stage = CartographyStage()
    ctx = make_ctx(ws, dispatcher=dispatcher)
    assert stage.is_complete(ctx)
    await stage.execute(ctx)  # even a forced re-execute dispatches nothing
    assert dispatcher.invocations == []
