"""Engine state-machine tests with a scripted stage registry.

The fakes write a trivial-but-valid artifact (GateADecision) into each stage's
canonical workspace location, so the engine's output validation, idempotency
shortcut, and rerun invalidation all operate on the real paths — only the
*content* of the stages is scripted."""

from __future__ import annotations

import pytest

from deeper.agents_runtime import AgentContract, AgentOutputInvalid
from deeper.config import RunConfig, SizeClass, profile_config
from deeper.orchestrator import Engine, Node, node_of
from deeper.schemas import (
    Criterion,
    GateADecision,
    GateName,
    GateStatus,
    PreferenceSlot,
    Rubric,
    RunStatus,
    Stage,
)
from deeper.stages import NotImplementedYet, StageBase
from deeper.workspace import Workspace

# One canonical output file per stage, inside the subtree rerun invalidation owns.
FAKE_OUTPUT: dict[Stage, str] = {
    Stage.S0: "brief.md",
    Stage.S1: "angles/map.yaml",
    Stage.S2: "allocation.yaml",
    Stage.S3: "options/cards.yaml",
    Stage.S4: "rubric.yaml",
    Stage.S5: "screening/scores.yaml",
    Stage.S6: "dossiers/winner.yaml",
    Stage.S7: "tournament/verdict.yaml",
    Stage.S8: "report/decision-report.md",
}

DUMMY = GateADecision(approved=False)
# S4's fake must write a REAL rubric: Gate B's apply step reads rubric.yaml to
# record the preference-slot weight, even on a bare approval.
DUMMY_RUBRIC = Rubric(
    criteria=[
        Criterion(
            id=f"crit-{i}",
            name=f"Criterion {i}",
            definition="what it measures",
            measurement_method="what evidence moves it",
            levels={n: f"level {n}" for n in range(1, 6)},
            weight=0.2,
            justification="traceable to the destination model",
        )
        for i in range(1, 6)
    ],
    preference_slot=PreferenceSlot(weight=0.2),
)


def fake_registry(record: list[str], failures: dict[Stage, list[Exception]] | None = None):
    """Stage classes that log executions, optionally raise scripted exceptions
    (consumed one per execution), then write their canonical dummy output."""
    failures = failures or {}

    def make(stage: Stage) -> type[StageBase]:
        artifact = DUMMY_RUBRIC if stage is Stage.S4 else DUMMY

        class Fake(StageBase):
            async def execute(self, ctx):
                record.append(stage.value)
                queue = failures.get(stage)
                if queue:
                    raise queue.pop(0)
                ctx.workspace.write_artifact(FAKE_OUTPUT[stage], artifact)

            def outputs(self, ctx):
                return [(FAKE_OUTPUT[stage], type(artifact))]

        Fake.stage = stage
        Fake.__name__ = f"Fake{stage.value}"
        return Fake

    return {stage: make(stage) for stage in Stage}


@pytest.fixture()
def workspace(tmp_path) -> Workspace:
    data = profile_config("quick").model_dump(mode="json")
    data["goal"] = "engine test goal"
    return Workspace.create(tmp_path / "run", RunConfig.model_validate(data))


def make_engine(workspace, record, failures=None, emitted=None):
    return Engine(
        workspace,
        stages=fake_registry(record, failures),
        emit=(emitted.append if emitted is not None else lambda _line: None),
    )


def approve(workspace: Workspace, gate: str, extra: str = "") -> None:
    workspace.path(f"gates/{gate}.yaml").write_text(f"approved: true\n{extra}", encoding="utf-8")


async def test_full_walk_to_done(workspace):
    record: list[str] = []
    engine = make_engine(workspace, record)

    assert await engine.run() is Node.GATE_A
    assert record == ["S0", "S1"]
    state = workspace.load_state()
    assert state.status is RunStatus.GATE_PENDING
    assert state.pending_gate is GateName.A
    assert state.gates[GateName.A] is GateStatus.PENDING
    assert workspace.path("gates/gate-a.yaml").is_file()

    approve(workspace, "gate-a")
    assert await engine.run() is Node.GATE_B
    assert record == ["S0", "S1", "S2", "S3", "S4"]

    approve(workspace, "gate-b", "preference_slot_weight: 0.2\n")
    assert await engine.run() is Node.GATE_C
    assert record == ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]

    approve(workspace, "gate-c")
    assert await engine.run() is Node.DONE
    assert record[-1] == "S8"
    state = workspace.load_state()
    assert state.status is RunStatus.DONE
    assert all(s is GateStatus.APPROVED for s in state.gates.values())
    # Running again is a no-op — safe to repeat any command.
    assert await engine.run() is Node.DONE
    assert record.count("S8") == 1
    subjects = workspace.history()
    assert "run complete" in subjects
    assert "gate-a: approved" in subjects


async def test_gate_template_prefilled_and_never_clobbered(workspace):
    record: list[str] = []
    engine = make_engine(workspace, record)
    await engine.run()
    gate_file = workspace.path("gates/gate-a.yaml")
    template = gate_file.read_text(encoding="utf-8")
    assert "approved: false" in template
    assert "rerun_hint" in template  # pre-filled with the schema's options
    assert "prior_adjustments" in template

    edited = template + "\n# my half-finished thoughts\n"
    gate_file.write_text(edited, encoding="utf-8")
    assert await engine.run() is Node.GATE_A  # undecided -> still paused
    assert gate_file.read_text(encoding="utf-8") == edited  # edit survived


async def test_gate_file_invalid_yaml_repauses_with_message(workspace):
    record: list[str] = []
    emitted: list[str] = []
    engine = make_engine(workspace, record, emitted=emitted)
    await engine.run()
    workspace.path("gates/gate-a.yaml").write_text("approved: [unclosed", encoding="utf-8")
    assert await engine.run() is Node.GATE_A
    assert any("not valid YAML" in m for m in emitted)
    assert workspace.load_state().status is RunStatus.GATE_PENDING


async def test_gate_file_schema_invalid_repauses_with_validation_message(workspace):
    record: list[str] = []
    emitted: list[str] = []
    engine = make_engine(workspace, record, emitted=emitted)
    await engine.run()
    workspace.path("gates/gate-a.yaml").write_text(
        'approved: true\nrerun_hint: "wider"\n', encoding="utf-8"
    )
    assert await engine.run() is Node.GATE_A
    assert any("not a valid decision" in m for m in emitted)
    assert record == ["S0", "S1"]  # nothing advanced


async def test_gate_undecided_repauses(workspace):
    record: list[str] = []
    emitted: list[str] = []
    engine = make_engine(workspace, record, emitted=emitted)
    await engine.run()
    assert await engine.run() is Node.GATE_A  # untouched template = undecided
    assert any("no decision yet" in m for m in emitted)


async def test_gate_a_rerun_hint_loops_back_through_s1(workspace):
    record: list[str] = []
    engine = make_engine(workspace, record)
    await engine.run()
    workspace.path("gates/gate-a.yaml").write_text(
        'approved: false\nrerun_hint: "look at adjacent fields"\n', encoding="utf-8"
    )
    assert await engine.run() is Node.GATE_A  # rerun S1, then pause at the gate again
    assert record == ["S0", "S1", "S1"]  # S0 untouched, S1 re-executed
    # The invalidation rewrote a fresh template (old decision file removed).
    assert 'rerun_hint: "look' not in workspace.path("gates/gate-a.yaml").read_text(
        encoding="utf-8"
    )
    subjects = workspace.history()
    assert any("rerun requested" in s for s in subjects)
    assert any(s.startswith("rerun S1") for s in subjects)


async def test_agent_output_invalid_pauses_for_attention_and_resumes(workspace):
    contract = AgentContract(
        role="scout",
        stage=Stage.S2,
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="n/a",
    )
    boom = AgentOutputInvalid(contract, errors="bad yaml everywhere", raw_output="...")
    record: list[str] = []
    emitted: list[str] = []
    engine = make_engine(workspace, record, failures={Stage.S2: [boom]}, emitted=emitted)
    await engine.run()
    approve(workspace, "gate-a")

    assert await engine.run() is Node.PAUSED_ATTENTION
    state = workspace.load_state()
    assert state.status is RunStatus.PAUSED_ATTENTION
    assert state.stage is Stage.S2
    assert any("paused for human attention" in m for m in emitted)

    # Plain run() reports the pause without re-entering; resume() re-enters.
    assert await engine.run() is Node.PAUSED_ATTENTION
    assert record.count("S2") == 1
    assert await engine.resume() is Node.GATE_B
    assert record.count("S2") == 2


async def test_notimplementedyet_exits_cleanly_and_state_untouched(workspace):
    record: list[str] = []
    emitted: list[str] = []

    class NotYet(StageBase):
        stage = Stage.S2

        async def execute(self, ctx):
            raise NotImplementedYet("S2 arrives later")

    stages = fake_registry(record)
    stages[Stage.S2] = NotYet
    engine = Engine(workspace, stages=stages, emit=emitted.append)
    await engine.run()
    approve(workspace, "gate-a")

    assert await engine.run() is Node.S2
    state = workspace.load_state()
    assert state.stage is Stage.S2
    assert state.status is RunStatus.RUNNING
    assert any("S2 arrives later" in m for m in emitted)
    # Retrying leaves no trace: same node, no new commits — resumable once built.
    before = workspace.history()
    assert await engine.run() is Node.S2
    assert workspace.history() == before


async def test_idempotent_reentry_skips_completed_stages(workspace):
    record: list[str] = []
    engine = make_engine(workspace, record)
    await engine.run()
    assert record == ["S0", "S1"]

    # Simulate a crash that lost the checkpoint but not the artifacts: point the
    # run back at S0 and resume — completed stages must not re-execute.
    state = workspace.load_state()
    state.status = RunStatus.RUNNING
    state.pending_gate = None
    state.stage = Stage.S0
    workspace.save_state(state)

    assert await engine.run() is Node.GATE_A
    assert record == ["S0", "S1"]  # no re-execution


async def test_node_of_derivation(workspace):
    state = workspace.load_state()
    assert node_of(state) is Node.S0
    state.stage = Stage.S6
    assert node_of(state) is Node.S6
    state.status = RunStatus.GATE_PENDING
    state.pending_gate = GateName.B
    assert node_of(state) is Node.GATE_B
    state.status = RunStatus.PAUSED_ATTENTION
    state.pending_gate = None
    assert node_of(state) is Node.PAUSED_ATTENTION
    state.status = RunStatus.DONE
    assert node_of(state) is Node.DONE
