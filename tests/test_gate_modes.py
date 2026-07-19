"""§11 gate-fatigue mitigation: per-gate `mode: gate | notify` in config.yaml.

'notify' auto-approves the gate's default decision (recorded in the gate file,
prominent summary emitted) instead of pausing; a human-edited decision file
always wins; every gate defaults to the hard 'gate' mode."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from deeper.config import RunConfig, profile_config
from deeper.orchestrator import Engine, Node
from deeper.schemas import GateName, GateStatus, Rubric, RunStatus

from .helpers import make_workspace

# -- config ---------------------------------------------------------------------


def test_gate_modes_default_to_gate_for_all_three() -> None:
    config = profile_config("quick")
    assert config.gate_modes == {g: "gate" for g in GateName}


def test_partial_gate_modes_override_keeps_the_rest_hard() -> None:
    data = profile_config("quick").model_dump(mode="json")
    data["gate_modes"] = {"gate-b": "notify"}
    config = RunConfig.model_validate(data)
    assert config.gate_modes[GateName.B] == "notify"
    assert config.gate_modes[GateName.A] == "gate"
    assert config.gate_modes[GateName.C] == "gate"


def test_invalid_gate_mode_rejected() -> None:
    data = profile_config("quick").model_dump(mode="json")
    data["gate_modes"] = {"gate-b": "auto"}
    with pytest.raises(ValidationError):
        RunConfig.model_validate(data)


def test_unknown_gate_name_rejected() -> None:
    data = profile_config("quick").model_dump(mode="json")
    data["gate_modes"] = {"gate-d": "notify"}
    with pytest.raises(ValidationError):
        RunConfig.model_validate(data)


# -- engine behavior ------------------------------------------------------------


async def test_notify_gate_b_auto_approves_with_defaults(tmp_path):
    ws = make_workspace(tmp_path, overrides={"gate_modes": {"gate-b": "notify"}})
    emitted: list[str] = []
    engine = Engine(ws, emit=emitted.append)

    assert await engine.run() is Node.GATE_A  # gate mode: still a hard pause
    ws.path("gates/gate-a.yaml").write_text("approved: true\n", encoding="utf-8")
    # Gate B never pauses: the walk continues S2..S4, auto-approves, and lands
    # at Gate C (still hard).
    assert await engine.run() is Node.GATE_C

    state = ws.load_state()
    assert state.gates[GateName.B] is GateStatus.APPROVED
    # The default decision was recorded in the gate file, marked auto-approved.
    gate_file = ws.path("gates/gate-b.yaml").read_text(encoding="utf-8")
    assert "auto-approved" in gate_file
    assert "approved: true" in gate_file
    # Gate B's apply ran for real: the profile-default slot weight is in the rubric.
    rubric = ws.read_artifact("rubric.yaml", Rubric)
    assert rubric.preference_slot.weight == ws.load_config().preference_slot_default_weight
    # The prominent summary named the mode, the review paths, and the weight.
    summary = next(m for m in emitted if "NOTIFY MODE" in m)
    assert "gate-b" in summary
    assert "rubric.yaml" in summary
    assert "preference-slot weight" in summary


async def test_notify_gate_honors_a_human_edited_decision(tmp_path):
    ws = make_workspace(tmp_path, overrides={"gate_modes": {"gate-b": "notify"}})
    emitted: list[str] = []
    engine = Engine(ws, emit=emitted.append)
    assert await engine.run() is Node.GATE_A
    ws.path("gates/gate-a.yaml").write_text("approved: true\n", encoding="utf-8")
    # A real decision already on file when the gate is reached: the template is
    # never overwritten, and notify must NOT clobber the human's choice.
    ws.path("gates/gate-b.yaml").write_text(
        "approved: true\npreference_slot_weight: 0.35\n", encoding="utf-8"
    )
    assert await engine.run() is Node.GATE_C
    assert ws.read_artifact("rubric.yaml", Rubric).preference_slot.weight == 0.35
    assert not any("NOTIFY MODE" in m for m in emitted)


async def test_all_gates_notify_runs_to_done_without_a_pause(tmp_path):
    ws = make_workspace(
        tmp_path,
        overrides={"gate_modes": {"gate-a": "notify", "gate-b": "notify", "gate-c": "notify"}},
    )
    emitted: list[str] = []
    engine = Engine(ws, emit=emitted.append)
    assert await engine.run() is Node.DONE
    state = ws.load_state()
    assert state.status is RunStatus.DONE
    assert all(state.gates[g] is GateStatus.APPROVED for g in GateName)
    assert sum("NOTIFY MODE" in m for m in emitted) == 3
    for gate in GateName:
        text = ws.path(f"gates/{gate.value}.yaml").read_text(encoding="utf-8")
        assert "auto-approved" in text
