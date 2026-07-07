"""Contract assembly and artifact parsing (agents_runtime.contracts)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from deeper.agents_runtime import (
    AgentContract,
    ArtifactParseError,
    ContractError,
    assemble_prompt,
    parse_artifacts,
)
from deeper.config import SizeClass
from deeper.schemas import Stage
from deeper.schemas.options import OptionCardSet

SNAPSHOT = Path(__file__).parent / "fixtures" / "snapshots" / "scout-contract.md"
SCOUT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "mock_agents" / "scout" / "option-card-set.yaml"
)

# Inlined schema JSON churns whenever models change; the snapshot pins the
# assembly *structure* by eliding schema bodies down to a stable placeholder.
_SCHEMA_BODY_RE = re.compile(
    r"JSON Schema for artifact `(?P<name>[a-z0-9-]+)`:\n```json\n.*?```", re.DOTALL
)


def scout_contract(**overrides) -> AgentContract:
    fields = dict(
        role="scout",
        stage=Stage.S3,
        task_objective="Scout the interpretability-research angle.",
        input_artifacts={
            "brief": "goal: Choose the senior research project\nanswer_type: decision",
            "angle": "id: interpretability-research\nname: Mechanistic interpretability",
        },
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="You have 3 units (~36 searches). Enumerate first, then evidence.",
        context="interpretability-research",
        allowed_write_paths=("options/interpretability-research",),
    )
    fields.update(overrides)
    return AgentContract(**fields)


def normalize(prompt: str) -> str:
    return _SCHEMA_BODY_RE.sub(lambda m: "{{schema:" + m.group("name") + "}}", prompt)


def test_assemble_scout_snapshot() -> None:
    got = normalize(assemble_prompt(scout_contract()))
    assert got == SNAPSHOT.read_text(encoding="utf-8"), (
        "assembled contract drifted from the snapshot; if the change is intended, "
        "regenerate tests/fixtures/snapshots/scout-contract.md"
    )


def test_assemble_contains_all_sections_in_order() -> None:
    prompt = assemble_prompt(scout_contract())
    positions = [
        prompt.index("# OBJECTIVE"),
        prompt.index("JSON Schema for artifact `option-card-set`"),
        prompt.index("# TASK"),
        prompt.index("# INPUTS"),
        prompt.index("### input: brief"),
        prompt.index("### input: angle"),
        prompt.index("# BUDGET"),
    ]
    assert positions == sorted(positions)
    assert "{{schema}}" not in prompt


def test_assemble_multi_schema_role_inlines_all_schemas_in_order() -> None:
    contract = AgentContract(
        role="interviewer",
        stage=Stage.S0,
        output_schemas=("brief", "destination", "preferences"),
        size_class=SizeClass.L,
        budget_line="One artifact set.",
    )
    prompt = assemble_prompt(contract)
    positions = [
        prompt.index(f"JSON Schema for artifact `{name}`")
        for name in ("brief", "destination", "preferences")
    ]
    assert positions == sorted(positions)


def test_assemble_rejects_schema_drift() -> None:
    with pytest.raises(ContractError, match="drifted"):
        assemble_prompt(scout_contract(output_schemas=("rubric",)))


def test_assemble_rejects_unknown_role() -> None:
    with pytest.raises(ContractError, match="unknown role"):
        assemble_prompt(scout_contract(role="no-such-role"))


def _valid_card_set_text(marker: str = "option-card-set") -> str:
    body = SCOUT_FIXTURE.read_text(encoding="utf-8")
    return f"### artifact: {marker}\n```yaml\n{body}```\n"


def test_parse_artifacts_valid() -> None:
    artifacts = parse_artifacts(_valid_card_set_text(), ("option-card-set",))
    assert isinstance(artifacts["option-card-set"], OptionCardSet)
    assert artifacts["option-card-set"].angle_id == "interpretability-research"


def test_parse_ignores_stray_yaml_blocks() -> None:
    # An agent quoting an input back must not shift artifact matching —
    # promptlab's positional zip would break here; marker parsing must not.
    text = "Here is the input I used:\n```yaml\nnot: an-artifact\n```\n\n" + _valid_card_set_text()
    artifacts = parse_artifacts(text, ("option-card-set",))
    assert artifacts["option-card-set"].angle_id == "interpretability-research"


def test_parse_missing_marker_names_expected_artifact() -> None:
    with pytest.raises(ArtifactParseError) as err:
        parse_artifacts("no artifacts here\n```yaml\nangle_id: x\n```\n", ("option-card-set",))
    assert "### artifact: option-card-set" in err.value.report


def test_parse_last_marker_wins() -> None:
    first = "### artifact: option-card-set\n```yaml\nangle_id: wrong\ncards: []\n```\n"
    artifacts = parse_artifacts(first + "\n" + _valid_card_set_text(), ("option-card-set",))
    assert artifacts["option-card-set"].angle_id == "interpretability-research"


def test_parse_validation_failure_report_is_llm_feedback() -> None:
    bad = (
        "### artifact: option-card-set\n"
        "```yaml\nangle_id: interpretability-research\ncards: []\n```\n"
    )
    with pytest.raises(ArtifactParseError) as err:
        parse_artifacts(bad, ("option-card-set",))
    assert "failed validation against the OptionCardSet schema" in err.value.report


def test_contract_is_frozen() -> None:
    contract = scout_contract()
    with pytest.raises(ValidationError):
        contract.role = "other"  # type: ignore[misc]
