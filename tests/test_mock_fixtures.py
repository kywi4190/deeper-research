"""Mock fixture library: every Phase-A role answers with schema-valid artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from deeper.agents_runtime import (
    DEFAULT_FIXTURES_DIR,
    AgentContract,
    AgentDispatchFailed,
    MockDispatcher,
)
from deeper.agents_runtime.mock import MockFixtureMissing
from deeper.config import SizeClass, profile_config
from deeper.schemas import Stage
from deeper.schemas.export import ARTIFACT_REGISTRY
from deeper.workspace import Workspace

AGENTS_DIR = Path(__file__).parent.parent / "agents"
AGENT_PATHS = sorted(AGENTS_DIR.glob("*.md"))
AGENT_IDS = [p.stem for p in AGENT_PATHS]


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---")[1])


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.create(tmp_path / "run", profile_config("quick"))


@pytest.mark.parametrize("agent_path", AGENT_PATHS, ids=AGENT_IDS)
def test_every_declared_schema_has_a_valid_fixture(agent_path: Path) -> None:
    meta = frontmatter(agent_path)
    role = meta["role"]
    # Prompt files are named after their role; the fixture dir mirrors that.
    assert agent_path.stem == role or role.startswith(agent_path.stem.split("-")[0])
    for schema in meta["output_schemas"]:
        fixture = DEFAULT_FIXTURES_DIR / agent_path.stem / f"{schema}.yaml"
        assert fixture.is_file(), f"missing mock fixture {fixture}"
        ARTIFACT_REGISTRY[schema].from_yaml_file(fixture)  # raises if invalid


@pytest.mark.parametrize("agent_path", AGENT_PATHS, ids=AGENT_IDS)
async def test_mock_dispatch_returns_all_artifacts(ws: Workspace, agent_path: Path) -> None:
    meta = frontmatter(agent_path)
    contract = AgentContract(
        role=agent_path.stem,
        stage=Stage(meta["stage"]),
        output_schemas=tuple(meta["output_schemas"]),
        size_class=SizeClass.M,
        budget_line="Mock budget.",
    )
    result = await MockDispatcher(ws, ws.load_config()).run_agent(contract)
    assert set(result.artifacts) == set(meta["output_schemas"])
    assert result.retries_used == 0
    assert result.usd == 0.0


async def test_context_specific_fixture_beats_default(ws: Workspace, tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    role_dir = fixtures / "scout"
    role_dir.mkdir(parents=True)
    default = (DEFAULT_FIXTURES_DIR / "scout" / "option-card-set.yaml").read_text(encoding="utf-8")
    role_dir.joinpath("option-card-set.yaml").write_text(default, encoding="utf-8")
    role_dir.joinpath("option-card-set.special-angle.yaml").write_text(
        default.replace("angle_id: interpretability-research", "angle_id: special-angle"),
        encoding="utf-8",
    )

    def contract(context: str | None) -> AgentContract:
        return AgentContract(
            role="scout",
            stage=Stage.S3,
            output_schemas=("option-card-set",),
            size_class=SizeClass.M,
            budget_line="b",
            context=context,
        )

    dispatcher = MockDispatcher(ws, ws.load_config(), fixtures_dir=fixtures)
    specific = await dispatcher.run_agent(contract("special-angle"))
    assert specific.artifacts["option-card-set"].angle_id == "special-angle"
    fallback = await dispatcher.run_agent(contract("other-angle"))
    assert fallback.artifacts["option-card-set"].angle_id == "interpretability-research"


async def test_missing_fixture_raises(ws: Workspace, tmp_path: Path) -> None:
    """A missing fixture is an infrastructure failure: the dispatcher wraps it
    in AgentDispatchFailed so the orchestrator pauses the run instead of
    crashing (same discipline as a live SDK error)."""
    dispatcher = MockDispatcher(ws, ws.load_config(), fixtures_dir=tmp_path / "empty")
    contract = AgentContract(
        role="scout",
        stage=Stage.S3,
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="b",
    )
    with pytest.raises(AgentDispatchFailed) as excinfo:
        await dispatcher.run_agent(contract)
    assert isinstance(excinfo.value.cause, MockFixtureMissing)


def test_fixture_cross_references_cohere() -> None:
    """screener ↔ rubric ↔ scout ↔ critic ↔ merger fixtures form one consistent
    multi-angle scenario: every screening option maps to a card in a per-angle
    scout fixture (initial, revision, or top-up), every angle in the map has a
    scout and a critic fixture, and every criterion id lives in the rubric."""
    load = lambda role, schema: ARTIFACT_REGISTRY[schema].from_yaml_file(  # noqa: E731
        DEFAULT_FIXTURES_DIR / role / f"{schema}.yaml"
    )
    angle_map = load("merger", "angle-map")
    rubric = load("rubric-builder", "rubric")
    screening = load("screener", "screening-result")
    angle_ids = {a.id for a in angle_map.angles}

    card_angle: dict[str, str] = {}
    for path in sorted((DEFAULT_FIXTURES_DIR / "scout").glob("option-card-set.*.yaml")):
        card_set = ARTIFACT_REGISTRY["option-card-set"].from_yaml_file(path)
        assert card_set.angle_id in angle_ids, f"{path.name} names an unmapped angle"
        for card in card_set.cards:
            card_angle[card.id] = card_set.angle_id
    for angle_id in angle_ids:
        assert (DEFAULT_FIXTURES_DIR / "scout" / f"option-card-set.{angle_id}.yaml").is_file()
        critique_path = DEFAULT_FIXTURES_DIR / "card-critic" / f"card-critique.{angle_id}.yaml"
        critique = ARTIFACT_REGISTRY["card-critique"].from_yaml_file(critique_path)
        assert critique.angle_id == angle_id

    criterion_ids = {c.id for c in rubric.criteria}
    for option in screening.options:
        assert option.option_id in card_angle, f"screened option {option.option_id} has no card"
        assert option.angle_id == card_angle[option.option_id]
        assert {s.criterion_id for s in option.criterion_scores} == criterion_ids

    # Coverage-report contributions point at real angle ids.
    coverage = load("merger", "coverage-report")
    for contributed in coverage.contributions.values():
        assert set(contributed) <= angle_ids
