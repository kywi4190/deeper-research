"""Agent contracts: assembly and output parsing (design §5, §8).

An AgentContract is everything one subagent invocation receives — role prompt,
inlined JSON schemas, input artifact *contents* (never paths: the prompt string
is the only parent→child channel, which enforces the artifact-as-contract
discipline), and an explicit budget statement. This module is pure: it never
imports claude_agent_sdk, so contract assembly and output validation are
testable — and mock mode runnable — without the SDK.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from deeper.config import SizeClass
from deeper.schemas import ArtifactModel, Stage, format_validation_error
from deeper.schemas.export import ARTIFACT_REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "agents"
SCHEMAS_DIR = REPO_ROOT / "schemas"

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
# An artifact is a `### artifact: <name>` marker line followed by a fenced yaml
# block (the convention every agents/*.md prompt mandates). Parsing by marker —
# not by yaml-fence position — survives agents quoting input yaml back at us.
ARTIFACT_BLOCK_RE = re.compile(
    r"^### artifact:[ \t]*(?P<name>[a-z0-9-]+)[ \t]*\n+```yaml[ \t]*\n(?P<body>.*?)^```",
    re.DOTALL | re.MULTILINE,
)


class ContractError(Exception):
    """A contract that cannot be assembled (unknown role, schema drift)."""


class ArtifactParseError(Exception):
    """Agent output that fails marker parsing, YAML parsing, or schema
    validation. ``report`` is the LLM-facing feedback fed back on retry."""

    def __init__(self, report: str) -> None:
        super().__init__(report)
        self.report = report


class AgentOutputInvalid(Exception):
    """Schema retries exhausted — the orchestrator pauses the run with a
    human-attention flag (design §6)."""

    def __init__(self, contract: AgentContract, errors: str, raw_output: str) -> None:
        super().__init__(f"agent '{contract.role}' output still invalid after retries:\n{errors}")
        self.contract = contract
        self.errors = errors
        self.raw_output = raw_output


class AgentContract(BaseModel):
    """One subagent invocation, fully specified. A runtime object, not a
    workspace artifact — plain pydantic, frozen so hooks can trust it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    stage: Stage
    task_objective: str = ""
    input_artifacts: dict[str, str] = {}
    output_schemas: tuple[str, ...]
    size_class: SizeClass
    budget_line: str
    context: str | None = None
    allowed_write_paths: tuple[str, ...] = ()


class AgentResult(BaseModel):
    """What one invocation produced and what it cost."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    role: str
    artifacts: dict[str, ArtifactModel]
    raw_text: str
    usd: float
    input_tokens: int
    output_tokens: int
    num_turns: int
    duration_ms: int
    session_id: str | None
    retries_used: int


def load_role(role: str, agents_dir: Path = AGENTS_DIR) -> tuple[dict, str]:
    """Parse agents/<role>.md into (frontmatter dict, prompt body)."""
    path = agents_dir / f"{role}.md"
    if not path.is_file():
        raise ContractError(f"unknown role {role!r}: no prompt file at {path}")
    match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if not match:
        raise ContractError(f"{path.name}: missing '---' YAML frontmatter")
    return yaml.safe_load(match.group(1)), match.group(2)


def assemble_prompt(
    contract: AgentContract,
    *,
    agents_dir: Path = AGENTS_DIR,
    schemas_dir: Path = SCHEMAS_DIR,
) -> str:
    """Role prompt + inlined JSON schemas + task + inputs + budget (design §8).

    Fails fast — before any spend — when the contract's declared schemas drift
    from the role prompt's frontmatter, or a schema export is missing/stale.
    """
    meta, body = load_role(contract.role, agents_dir)
    declared = tuple(meta.get("output_schemas") or ())
    if contract.output_schemas != declared:
        raise ContractError(
            f"contract for {contract.role!r} declares output_schemas "
            f"{list(contract.output_schemas)} but agents/{contract.role}.md declares "
            f"{list(declared)} — contract and prompt library have drifted"
        )
    schema_blocks = []
    for name in contract.output_schemas:
        if name not in ARTIFACT_REGISTRY:
            raise ContractError(f"output schema {name!r} is not in ARTIFACT_REGISTRY")
        schema_path = schemas_dir / f"{name}.schema.json"
        if not schema_path.is_file():
            raise ContractError(f"schema export missing: {schema_path} — run `make schemas`")
        text = schema_path.read_text(encoding="utf-8")
        schema_blocks.append(f"JSON Schema for artifact `{name}`:\n```json\n{text}```")
    prompt = body.replace("{{schema}}", "\n\n".join(schema_blocks))

    parts = [prompt.rstrip()]
    if contract.task_objective:
        parts.append(f"# TASK\n{contract.task_objective.rstrip()}")
    parts.append("# INPUTS")
    for name, content in contract.input_artifacts.items():
        parts.append(f"### input: {name}\n```yaml\n{content.rstrip()}\n```")
    parts.append(f"# BUDGET\n{contract.budget_line.rstrip()}")
    return "\n\n".join(parts) + "\n"


def parse_artifacts(text: str, output_schemas: tuple[str, ...]) -> dict[str, ArtifactModel]:
    """Extract and validate every declared artifact from agent output text.

    Blocks are matched by their `### artifact: <name>` marker; when an agent
    re-emits a corrected artifact, the last occurrence wins. Any missing
    marker, YAML error, or schema violation raises ArtifactParseError whose
    report is fed back verbatim on retry.
    """
    found = {m.group("name"): m.group("body") for m in ARTIFACT_BLOCK_RE.finditer(text)}
    problems: list[str] = []
    artifacts: dict[str, ArtifactModel] = {}
    for name in output_schemas:
        model = ARTIFACT_REGISTRY[name]
        if name not in found:
            problems.append(
                f"- artifact `{name}` is missing. Emit a line `### artifact: {name}` "
                "followed by a fenced ```yaml block containing the artifact."
            )
            continue
        try:
            artifacts[name] = model.load_yaml(found[name])
        except yaml.YAMLError as err:
            problems.append(
                f"- artifact `{name}` is not parseable as YAML: {err}\n"
                "  Re-emit the complete artifact as valid YAML."
            )
        except ValidationError as err:
            problems.append(f"- artifact `{name}`:\n{format_validation_error(err, model)}")
    if problems:
        expected = ", ".join(output_schemas)
        raise ArtifactParseError(
            f"Your response must contain exactly these artifacts: {expected}.\n"
            + "\n".join(problems)
        )
    return artifacts
