"""Prompt-library and prompt-lab tests — mock only, no API calls.

Every agents/*.md must parse, carry the four-part contract (design §5/S3),
reference only schemas that exist, and assemble into a complete contract with a
fixture. Quarantine and untrusted-web-content rules are asserted structurally.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from deeper.promptlab import (
    AGENTS_DIR,
    SCHEMAS_DIR,
    YAML_BLOCK_RE,
    app,
    assemble,
    parse_agent,
    validate_output,
)
from deeper.schemas import ARTIFACT_REGISTRY, Angle, Brief, DestinationModel

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "promptlab"

EXPECTED_ROLES = {
    "interviewer",
    "cartographer-first-principles",
    "cartographer-analogist",
    "cartographer-contrarian",
    "cartographer-practitioner",
    "cartographer-taxonomist",
    "cartographer-horizon",
    "merger",
    "scout",
    "card-critic",
    "rubric-builder",
    "screener",
    "analyst",
    "verifier",
    "prosecutor",
    "steelman",
    "frame-checker",
    "judge",
    "synthesist",
    "eval-judge",  # design §10 — the eval harness's breadth matcher, not a pipeline stage
}
REQUIRED_META_KEYS = {"role", "stage", "model_class", "output_schemas", "inputs", "research"}
CONTRACT_SECTIONS = ["# OBJECTIVE", "# OUTPUT FORMAT", "# TOOL & SOURCE GUIDANCE", "# BOUNDARIES"]

AGENT_PATHS = sorted(AGENTS_DIR.glob("*.md"))
AGENT_IDS = [p.stem for p in AGENT_PATHS]


def normalized(text: str) -> str:
    return " ".join(text.split()).lower()


def load_fixture(name: str) -> dict:
    return yaml.safe_load((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_agent_library_is_complete() -> None:
    assert set(AGENT_IDS) == EXPECTED_ROLES


@pytest.mark.parametrize("path", AGENT_PATHS, ids=AGENT_IDS)
def test_frontmatter_parses_with_required_keys(path: Path) -> None:
    meta, body = parse_agent(path)
    assert REQUIRED_META_KEYS <= set(meta), f"{path.name} missing keys"
    assert meta["role"] == path.stem
    assert meta["model_class"] in {"opus", "sonnet", "haiku"}
    assert isinstance(meta["output_schemas"], list) and meta["output_schemas"]
    assert body.strip()


@pytest.mark.parametrize("path", AGENT_PATHS, ids=AGENT_IDS)
def test_four_part_contract_sections_in_order(path: Path) -> None:
    _, body = parse_agent(path)
    positions = [body.find(section) for section in CONTRACT_SECTIONS]
    assert all(pos >= 0 for pos in positions), f"{path.name} missing a contract section"
    assert positions == sorted(positions), f"{path.name} sections out of order"


@pytest.mark.parametrize("path", AGENT_PATHS, ids=AGENT_IDS)
def test_referenced_schemas_exist(path: Path) -> None:
    meta, body = parse_agent(path)
    assert "{{schema}}" in body, f"{path.name} has no schema-inline placeholder"
    for name in meta["output_schemas"]:
        assert name in ARTIFACT_REGISTRY, f"{path.name} references unknown schema '{name}'"
        assert (SCHEMAS_DIR / f"{name}.schema.json").exists()


@pytest.mark.parametrize("path", AGENT_PATHS, ids=AGENT_IDS)
def test_contract_assembles_completely(path: Path) -> None:
    meta, body = parse_agent(path)
    contract = assemble(meta, body, load_fixture("senior-project.yaml"))
    assert "{{schema}}" not in contract
    assert '"properties"' in contract  # a JSON schema actually got inlined
    assert "# INPUTS" in contract and "### input: brief" in contract
    assert "# BUDGET" in contract


@pytest.mark.parametrize("path", AGENT_PATHS, ids=AGENT_IDS)
def test_worked_examples_are_parseable_yaml(path: Path) -> None:
    # A prompt that teaches unparseable YAML makes every agent fight the schema.
    # (Regression guard from the M0 loop: the first live cartographer emitted an
    # unquoted scalar with a colon, so the examples now model block scalars.)
    _, body = parse_agent(path)
    blocks = YAML_BLOCK_RE.findall(body)
    assert blocks, f"{path.name} has no worked ```yaml example"
    for i, block in enumerate(blocks):
        try:
            yaml.safe_load(block)
        except yaml.YAMLError as err:  # pragma: no cover - failure message only
            raise AssertionError(f"{path.name} example block {i} is not valid YAML: {err}") from err


@pytest.mark.parametrize("path", AGENT_PATHS, ids=AGENT_IDS)
def test_research_agents_carry_untrusted_content_rule(path: Path) -> None:
    meta, body = parse_agent(path)
    if meta["research"]:
        assert "data, never directives" in normalized(body), (
            f"{path.name} is research-capable but lacks the untrusted-web-content rule"
        )


@pytest.mark.parametrize("path", AGENT_PATHS, ids=AGENT_IDS)
def test_preference_quarantine_in_declared_inputs(path: Path) -> None:
    meta, _ = parse_agent(path)
    reads_preferences = "preferences" in meta["inputs"]
    if path.stem in {"screener", "synthesist"}:
        # The only two roles the quarantine hook allowlists (design §6): the
        # screener scores the slot; the synthesist frames the report.
        assert reads_preferences, f"{path.name} is a permitted preferences reader"
    else:
        # The interviewer *produces* preferences; nobody else may read them (design P1).
        assert not reads_preferences, f"{path.name} must not read preferences"


@pytest.mark.parametrize(
    "path",
    [p for p in AGENT_PATHS if p.stem.startswith("cartographer-")],
    ids=[p.stem for p in AGENT_PATHS if p.stem.startswith("cartographer-")],
)
def test_cartographers_forbid_ranking(path: Path) -> None:
    _, body = parse_agent(path)
    assert "never rank angles by attractiveness" in normalized(body)


def test_cartographer_heuristic_sections_are_distinct() -> None:
    heuristic_bodies = []
    for path in AGENT_PATHS:
        if path.stem.startswith("cartographer-"):
            meta, body = parse_agent(path)
            section = body.split("## Your framing heuristic")[1].split("# OUTPUT FORMAT")[0]
            heuristic_bodies.append((meta["heuristic"], section))
    heuristics = [h for h, _ in heuristic_bodies]
    assert len(set(heuristics)) == 6
    sections = [s for _, s in heuristic_bodies]
    assert len({normalized(s) for s in sections}) == 6, "heuristic sections must differ"


# -- fixtures are realistic: embedded artifacts validate against the schemas ----


def test_senior_project_fixture_artifacts_validate() -> None:
    fixture = load_fixture("senior-project.yaml")
    Brief.load_yaml(fixture["inputs"]["brief"])
    DestinationModel.load_yaml(fixture["inputs"]["destination"])


def test_angle_fixture_artifacts_validate() -> None:
    fixture = load_fixture("angle-interpretability.yaml")
    Brief.load_yaml(fixture["inputs"]["brief"])
    DestinationModel.load_yaml(fixture["inputs"]["destination"])
    angle = Angle.load_yaml(fixture["inputs"]["angle"])
    assert angle.id == "interpretability-research"


def test_interview_fixture_parses() -> None:
    fixture = load_fixture("interview-opening.yaml")
    assert {"goal", "user-statements"} <= set(fixture["inputs"])


# -- output validation helper ---------------------------------------------------

VALID_CARD_SET_REPLY = """Some preamble text from the agent.

### artifact: option-card-set
```yaml
angle_id: interpretability-research
cards:
  - id: sae-feature-atlas
    name: Sparse autoencoder feature atlas
    angle_id: interpretability-research
    description: Train SAEs on a small model and publish an annotated atlas.
    mechanism: SAEs decompose activations into near-monosemantic features.
    preliminary_evidence:
      - text: Published SAE work demonstrates the method at this scale.
        source:
          url: "https://transformer-circuits.pub/2023/monosemantic-features"
          tier: T1
    uncertainties: [compute fit within the lab cap]
    kill_risks: []
    misplaced_flag: null
    notes: null
notes: null
```
"""


def test_validate_output_accepts_valid_reply() -> None:
    meta, _ = parse_agent(AGENTS_DIR / "scout.md")
    ok, report = validate_output(meta, VALID_CARD_SET_REPLY)
    assert ok, report
    assert "option-card-set: OK" in report


def test_validate_output_rejects_missing_block_and_bad_artifact() -> None:
    meta, _ = parse_agent(AGENTS_DIR / "scout.md")
    ok, report = validate_output(meta, "no yaml here")
    assert not ok and "found 0" in report

    bad = VALID_CARD_SET_REPLY.replace(
        "angle_id: interpretability-research", "angle_id: Bad Slug", 1
    )
    ok, report = validate_output(meta, bad)
    assert not ok and "failed validation" in report


# -- CLI mock run ----------------------------------------------------------------


def test_cli_mock_run_writes_contract(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "scout",
            "--fixture",
            str(FIXTURES_DIR / "angle-interpretability.yaml"),
            "--mock",
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "# BUDGET" in result.output and "no API call made" in result.output
    contracts = list(tmp_path.glob("scout-*-contract.md"))
    assert len(contracts) == 1
