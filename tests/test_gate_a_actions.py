"""Gate A action-application tests (design §5 Gate A): each of the five actions
mutates the map/queue correctly, referential problems re-pause instead of
advancing, and the S2 allocation at the gate exit reflects the edits."""

from __future__ import annotations

from deeper.orchestrator import Engine, Node
from deeper.orchestrator.gates import apply_gate_a_actions, record_rerun_hint
from deeper.schemas import (
    AllocationTable,
    AngleAddition,
    AngleMap,
    AngleRemoval,
    GateADecision,
    Heuristic,
    PriorAdjustment,
    RunStatus,
    Stage,
)
from deeper.stages import STAGES, NotImplementedYet, StageBase

from .helpers import FIXTURES, make_workspace

MAP_FIXTURE = FIXTURES / "merger" / "angle-map.yaml"


class _StubS3(StageBase):
    """Halts the walk after S2: human-added angles have no mock fixtures, and
    these tests are about Gate A's edits, not scouting."""

    stage = Stage.S3

    async def execute(self, ctx):
        raise NotImplementedYet("S3 halted for this test")


def ws_with_map(tmp_path):
    ws = make_workspace(tmp_path)
    ws.write_artifact("angles/map.yaml", AngleMap.from_yaml_file(MAP_FIXTURE))
    return ws


def decision(**kwargs) -> GateADecision:
    return GateADecision(approved=True, **kwargs)


# -- apply_gate_a_actions unit tests --------------------------------------------


def test_removal_drops_angle_and_its_dedup_entries_and_logs_reason(tmp_path):
    ws = ws_with_map(tmp_path)
    messages, problem = apply_gate_a_actions(
        ws,
        decision(
            removed_angles=[
                AngleRemoval(angle_id="training-efficiency", reason="crowded space, weak fit")
            ]
        ),
    )
    assert problem is None
    assert any("crowded space, weak fit" in m for m in messages)  # reason in the audit trail
    angle_map = ws.read_artifact("angles/map.yaml", AngleMap)
    assert "training-efficiency" not in {a.id for a in angle_map.angles}
    assert all(e.merged_into != "training-efficiency" for e in angle_map.dedup_map)


def test_infeasible_post_edit_map_warns_but_still_applies(tmp_path):
    """Finding 2: a 41-angle map × floor 1 > B crashed S2 unhandled on the M1
    run. Gate A now warns at apply time (S2 pauses on the same check); the
    decision itself still lands — the human may intend to fix config next."""
    ws = ws_with_map(tmp_path)
    additions = [
        AngleAddition(name=f"Filler angle {i}", note="breadth stress") for i in range(20)
    ]  # map fixture has 8 angles; 28 × floor 1 > B=16 (quick)
    messages, problem = apply_gate_a_actions(ws, decision(added_angles=additions))
    assert problem is None  # warn, never block
    assert any("WARNING" in m and "infeasible" in m for m in messages)
    angle_map = ws.read_artifact("angles/map.yaml", AngleMap)
    assert len(angle_map.angles) == 28  # the edits were applied regardless


async def test_infeasible_map_pauses_s2_cleanly(tmp_path):
    """S2 catches allocate() infeasibility and interrupts with the exact fix
    instead of an unhandled traceback; state stays resumable at S2."""
    import pytest

    from deeper.stages import StageInterrupted
    from deeper.stages.s2_allocation import AllocationStage

    from .helpers import make_ctx

    ws = ws_with_map(tmp_path)
    angle_map = ws.read_artifact("angles/map.yaml", AngleMap)
    filler = [
        angle_map.angles[0].model_copy(update={"id": f"filler-{i}", "name": f"Filler {i}"})
        for i in range(20)
    ]
    ws.write_artifact(
        "angles/map.yaml",
        AngleMap(
            angles=[*angle_map.angles, *filler],
            dedup_map=angle_map.dedup_map,
            notes=angle_map.notes,
        ),
    )
    ctx = make_ctx(ws)
    with pytest.raises(StageInterrupted) as excinfo:
        await AllocationStage().execute(ctx)
    message = str(excinfo.value)
    assert "infeasible" in message
    assert "total_budget_units" in message  # the fix is named
    assert "deeper resume" in message
    assert not ws.path("allocation.yaml").exists()  # nothing half-written


def test_addition_enters_map_with_human_provenance_and_default_prior(tmp_path):
    ws = ws_with_map(tmp_path)
    messages, problem = apply_gate_a_actions(
        ws,
        decision(
            added_angles=[
                AngleAddition(
                    name="Theory of Deep Learning",
                    note="scout theory-only projects feasible without experiments",
                )
            ]
        ),
    )
    assert problem is None
    angle_map = ws.read_artifact("angles/map.yaml", AngleMap)
    added = next(a for a in angle_map.angles if a.id == "theory-of-deep-learning")
    assert added.relevance_prior == 0.5
    assert added.contributing_heuristics == [Heuristic.HUMAN]
    assert "theory-only projects" in (added.notes or "")  # scout guidance travels
    assert any("queued for scouting" in m for m in messages)


def test_prior_adjustment_including_on_a_just_added_angle(tmp_path):
    ws = ws_with_map(tmp_path)
    _, problem = apply_gate_a_actions(
        ws,
        decision(
            added_angles=[AngleAddition(name="Theory of Deep Learning", note="see above")],
            prior_adjustments=[
                PriorAdjustment(angle_id="evaluation-science", new_prior=0.9),
                PriorAdjustment(angle_id="theory-of-deep-learning", new_prior=0.25),
            ],
        ),
    )
    assert problem is None
    by_id = {a.id: a for a in ws.read_artifact("angles/map.yaml", AngleMap).angles}
    assert by_id["evaluation-science"].relevance_prior == 0.9
    assert by_id["theory-of-deep-learning"].relevance_prior == 0.25


def test_referential_problems_reported_together_and_nothing_written(tmp_path):
    ws = ws_with_map(tmp_path)
    before = ws.path("angles/map.yaml").read_text(encoding="utf-8")
    messages, problem = apply_gate_a_actions(
        ws,
        decision(
            removed_angles=[AngleRemoval(angle_id="not-an-angle", reason="typo")],
            prior_adjustments=[PriorAdjustment(angle_id="also-missing", new_prior=0.4)],
        ),
    )
    assert messages == []
    assert problem is not None
    assert "not-an-angle" in problem and "also-missing" in problem
    assert ws.path("angles/map.yaml").read_text(encoding="utf-8") == before


def test_added_name_colliding_with_existing_id_is_a_problem(tmp_path):
    ws = ws_with_map(tmp_path)
    _, problem = apply_gate_a_actions(
        ws,
        decision(added_angles=[AngleAddition(name="Supervisor Pipeline", note="dup")]),
    )
    assert problem is not None and "collides" in problem


def test_removing_every_angle_is_a_problem(tmp_path):
    ws = ws_with_map(tmp_path)
    all_ids = [a.id for a in ws.read_artifact("angles/map.yaml", AngleMap).angles]
    _, problem = apply_gate_a_actions(
        ws,
        decision(removed_angles=[AngleRemoval(angle_id=i, reason="no") for i in all_ids]),
    )
    assert problem is not None and "cannot be emptied" in problem


def test_record_rerun_hint_survives_under_gates(tmp_path):
    ws = make_workspace(tmp_path)
    messages, problem = record_rerun_hint(ws, "look at adjacent fields")
    assert problem is None and messages
    assert ws.path("gates/gate-a-hint.txt").read_text(encoding="utf-8").strip() == (
        "look at adjacent fields"
    )


# -- engine-level integration -----------------------------------------------------


async def walk_to_gate_a(tmp_path, **caps):
    ws = make_workspace(tmp_path, profile="quick", caps=caps or None)
    emitted: list[str] = []
    engine = Engine(ws, stages={**STAGES, Stage.S3: _StubS3}, emit=emitted.append)
    assert await engine.run() is Node.GATE_A
    return ws, engine, emitted


async def test_gate_a_edits_are_applied_before_allocation(tmp_path):
    ws, engine, emitted = await walk_to_gate_a(tmp_path)
    ws.path("gates/gate-a.yaml").write_text(
        "approved: true\n"
        "added_angles:\n"
        '  - {name: "Theory of deep learning", note: "scout theory-only projects"}\n'
        "removed_angles:\n"
        '  - {angle_id: training-efficiency, reason: "crowded space"}\n'
        "prior_adjustments:\n"
        "  - {angle_id: evaluation-science, new_prior: 0.9}\n",
        encoding="utf-8",
    )
    node = await engine.run()
    assert node is Node.S3  # S2 done, S3 reports not-built-yet

    ids = {a.id for a in ws.read_artifact("angles/map.yaml", AngleMap).angles}
    assert "theory-of-deep-learning" in ids and "training-efficiency" not in ids
    table = ws.read_artifact("allocation.yaml", AllocationTable)
    row_ids = {r.angle_id for r in table.rows}
    assert row_ids == ids  # the allocation ran over the post-edit map
    assert any("removed angle 'training-efficiency'" in line for line in emitted)
    assert any(line.startswith("S2: allocation —") for line in emitted)  # table at gate exit
    assert "gate-a: approved (1 added, 1 removed, 1 prior(s) adjusted)" in ws.history()


async def test_gate_a_bad_edit_repauses_without_advancing(tmp_path):
    ws, engine, emitted = await walk_to_gate_a(tmp_path)
    ws.path("gates/gate-a.yaml").write_text(
        "approved: true\nremoved_angles:\n  - {angle_id: no-such-angle, reason: oops}\n",
        encoding="utf-8",
    )
    assert await engine.run() is Node.GATE_A
    assert ws.load_state().status is RunStatus.GATE_PENDING
    assert any("cannot be applied" in line for line in emitted)
    assert not ws.path("allocation.yaml").exists()


async def test_gate_a_rerun_hint_loops_s1_once_with_hint_then_consumes_it(tmp_path):
    ws, engine, emitted = await walk_to_gate_a(tmp_path)
    ws.path("gates/gate-a.yaml").write_text(
        'approved: false\nrerun_hint: "look wider than software"\n', encoding="utf-8"
    )
    assert await engine.run() is Node.GATE_A  # S1 re-ran and re-paused at a fresh gate
    assert not ws.path("gates/gate-a-hint.txt").exists()  # consumed by the S1 pass
    template = ws.path("gates/gate-a.yaml").read_text(encoding="utf-8")
    assert "look wider" not in template and "approved: false" in template
    assert any("rerun requested" in s for s in ws.history())
    assert any("rerun hint active" in line for line in emitted)
