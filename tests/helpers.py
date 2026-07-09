"""Shared helpers for stage-level tests (S0–S2): workspace factories over the
canned mock-fixture scenario, a prompt-recording dispatcher, and builders for
contrived cartography fixtures used by the saturation tests."""

from __future__ import annotations

from pathlib import Path

from deeper.agents_runtime import MockDispatcher
from deeper.config import RunConfig, profile_config
from deeper.schemas import (
    Angle,
    AngleMap,
    Brief,
    CartographerReport,
    CoverageReport,
    DedupEntry,
    DestinationModel,
    Heuristic,
    Preferences,
    RawAngle,
)
from deeper.stages import StageContext
from deeper.workspace import Workspace

FIXTURES = Path(__file__).parent / "fixtures" / "mock_agents"


def make_workspace(
    tmp_path: Path,
    profile: str = "quick",
    goal: str = "pick a senior research project",
    caps: dict | None = None,
) -> Workspace:
    data = profile_config(profile).model_dump(mode="json")
    data["goal"] = goal
    if caps:
        data["caps"].update(caps)
    return Workspace.create(tmp_path / "run", RunConfig.model_validate(data))


def write_s0_artifacts(ws: Workspace) -> None:
    """Materialize the interviewer fixtures as the run's S0 outputs."""
    ws.write_artifact("brief.md", Brief.from_yaml_file(FIXTURES / "interviewer" / "brief.yaml"))
    ws.write_artifact(
        "destination.md",
        DestinationModel.from_yaml_file(FIXTURES / "interviewer" / "destination.yaml"),
    )
    ws.write_artifact(
        "preferences.yaml",
        Preferences.from_yaml_file(FIXTURES / "interviewer" / "preferences.yaml"),
    )


def write_s1_s2_artifacts(ws: Workspace) -> None:
    """Materialize the merger fixtures as S1 output and run the real S2 formula
    over them, so S3+ tests start from the canonical post-Gate-A state."""
    from deeper.allocation import allocate

    ws.write_artifact(
        "angles/map.yaml", AngleMap.from_yaml_file(FIXTURES / "merger" / "angle-map.yaml")
    )
    ws.write_artifact(
        "angles/map-report.md",
        CoverageReport.from_yaml_file(FIXTURES / "merger" / "coverage-report.yaml"),
    )
    config = ws.load_config()
    angle_map = ws.read_artifact("angles/map.yaml", AngleMap)
    ws.write_artifact(
        "allocation.yaml",
        allocate(
            {a.id: a.relevance_prior for a in angle_map.angles},
            total_budget_units=config.total_budget_units,
            floor=config.floor,
            gamma=config.gamma,
            per_angle_cap_pct=config.per_angle_cap_pct,
        ),
    )


def make_ctx(
    ws: Workspace,
    dispatcher=None,
    emitted: list[str] | None = None,
    ask_user=None,
    **mock_kwargs,
) -> StageContext:
    config = ws.load_config()
    if dispatcher is None:
        dispatcher = MockDispatcher(ws, config, **mock_kwargs)
    return StageContext(
        workspace=ws,
        config=config,
        dispatcher=dispatcher,
        emit=(emitted.append if emitted is not None else lambda _line: None),
        ask_user=ask_user,
    )


async def walk_engine_to_gate_c(
    tmp_path,
    caps: dict | None = None,
    emitted: list[str] | None = None,
    **mock_kwargs,
):
    """Drive the real engine over the canned scenario to the Gate C pause:
    S0 -> Gate A (plain approval) -> S4 -> Gate B (slot 0.2) -> S7 -> Gate C.
    Returns (workspace, engine, emitted)."""
    from deeper.orchestrator import Engine, Node

    ws = make_workspace(tmp_path, caps=caps)
    emitted = emitted if emitted is not None else []
    engine = Engine(ws, emit=emitted.append, mock_kwargs=mock_kwargs or None)
    assert await engine.run() is Node.GATE_A
    ws.path("gates/gate-a.yaml").write_text("approved: true\n", encoding="utf-8")
    assert await engine.run() is Node.GATE_B
    ws.path("gates/gate-b.yaml").write_text(
        "approved: true\npreference_slot_weight: 0.2\n", encoding="utf-8"
    )
    assert await engine.run() is Node.GATE_C
    return ws, engine, emitted


class RecordingMockDispatcher(MockDispatcher):
    """Mock dispatcher that records every (role, context, prompt) it sends."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.invocations: list[tuple[str, str | None, str]] = []

    async def _invoke(self, prompt, contract, attempt):
        self.invocations.append((contract.role, contract.context, prompt))
        return await super()._invoke(prompt, contract, attempt)


def interviewer_artifacts_text() -> str:
    """The interviewer fixtures as one marker+fenced-yaml response — what the
    agent's final artifact-emission turn looks like."""
    blocks = []
    for schema in ("brief", "destination", "preferences"):
        text = (FIXTURES / "interviewer" / f"{schema}.yaml").read_text(encoding="utf-8")
        blocks.append(f"### artifact: {schema}\n```yaml\n{text.rstrip()}\n```")
    return "\n\n".join(blocks) + "\n"


# -- contrived cartography fixtures (saturation/expansion tests) ---------------


def cartographer_report_yaml(heuristic: Heuristic, names: list[str]) -> str:
    return CartographerReport(
        heuristic=heuristic,
        angles=[
            RawAngle(
                name=name,
                definition=f"{name} definition",
                distinctness_rationale="a distinct region",
                example_options=["an existence proof"],
                relevance_rationale="fits the brief",
            )
            for name in names
        ],
    ).dump_yaml()


def merged_angle(angle_id: str) -> Angle:
    return Angle(
        id=angle_id,
        name=angle_id,
        definition=f"{angle_id} definition",
        distinctness_rationale="a distinct region",
        example_options=["an existence proof"],
        relevance_prior=0.5,
        prior_justification="grounded in the brief",
        contributing_heuristics=[Heuristic.FIRST_PRINCIPLES],
    )


def angle_map_yaml(angle_ids: list[str], dedup: list[tuple[Heuristic, str, str]]) -> str:
    return AngleMap(
        angles=[merged_angle(a) for a in angle_ids],
        dedup_map=[
            DedupEntry(heuristic=h, raw_name=raw, merged_into=into) for h, raw, into in dedup
        ],
    ).dump_yaml()


def coverage_report_yaml() -> str:
    return CoverageReport(contributions={Heuristic.FIRST_PRINCIPLES: ["a1"]}).dump_yaml()
