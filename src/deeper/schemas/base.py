"""Base artifact model: strict validation, YAML/JSON round-trip, LLM-facing errors.

Every stage boundary in the pipeline is a schema-validated file (design P2).
Artifacts subclass ArtifactModel; agent outputs that fail validation are retried
with the exact string produced by ``format_validation_error``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

# Cross-artifact reference keys (angle/option/criterion/claim ids) double as
# workspace directory names (options/{angle}/), so they share one format.
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"

Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
Probability = Annotated[float, Field(ge=0, le=1)]
Score = Annotated[float, Field(ge=1, le=5)]


class ArtifactModel(BaseModel):
    """Base class for every pipeline artifact.

    Unknown fields are rejected: anything a schema did not anticipate belongs in
    the artifact's explicit ``notes`` field (design §11), which feeds schema
    revisions instead of silently loosening validation.
    """

    model_config = ConfigDict(extra="forbid")

    # -- YAML (workspace artifacts) -------------------------------------------

    def dump_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json"),
            sort_keys=False,  # keep field declaration order: gate diffs stay readable
            allow_unicode=True,
            width=100,
        )

    def to_yaml_file(self, path: str | Path) -> None:
        Path(path).write_text(self.dump_yaml(), encoding="utf-8", newline="\n")

    @classmethod
    def load_yaml(cls, text: str) -> Self:
        return cls.model_validate(yaml.safe_load(text))

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> Self:
        return cls.load_yaml(Path(path).read_text(encoding="utf-8"))

    # -- JSON (state.json) -----------------------------------------------------

    def dump_json_str(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2) + "\n"

    def to_json_file(self, path: str | Path) -> None:
        Path(path).write_text(self.dump_json_str(), encoding="utf-8", newline="\n")

    @classmethod
    def load_json(cls, text: str) -> Self:
        return cls.model_validate(json.loads(text))

    @classmethod
    def from_json_file(cls, path: str | Path) -> Self:
        return cls.load_json(Path(path).read_text(encoding="utf-8"))


def _format_loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "(top level)"


def _short(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _explain(error: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return (what's wrong, what's expected) in language an agent can act on."""
    kind = error["type"]
    ctx = error.get("ctx") or {}
    msg: str = error["msg"]
    if kind == "missing":
        return "required field is missing", "add this field"
    if kind == "extra_forbidden":
        return (
            "this field is not part of the schema",
            "remove it; put anything the schema did not anticipate in the 'notes' field",
        )
    if kind == "greater_than_equal":
        return "value is below the minimum", f"a number >= {ctx.get('ge')}"
    if kind == "less_than_equal":
        return "value is above the maximum", f"a number <= {ctx.get('le')}"
    if kind == "greater_than":
        return "value is too small", f"a number > {ctx.get('gt')}"
    if kind == "less_than":
        return "value is too large", f"a number < {ctx.get('lt')}"
    if kind in ("enum", "literal_error"):
        return "value is not an allowed choice", f"one of: {ctx.get('expected', msg)}"
    if kind == "string_pattern_mismatch":
        return (
            "string is not in the required format",
            f"a lowercase kebab-case slug matching {ctx.get('pattern')}",
        )
    if kind == "too_short":
        return "too few items or characters", f"at least {ctx.get('min_length')}"
    if kind == "too_long":
        return "too many items or characters", f"at most {ctx.get('max_length')}"
    if kind == "value_error":
        # Custom validator messages are written to be self-explanatory.
        return msg.removeprefix("Value error, "), None
    if kind.endswith("_type") or kind.endswith("_parsing"):
        return "wrong type", msg.removeprefix("Input should be ")
    return msg, None


def format_validation_error(error: ValidationError, model: type[BaseModel] | None = None) -> str:
    """Render a ValidationError as instructions an LLM agent can follow verbatim.

    Each line: field path + what's wrong (with the offending input) + what's
    expected. This string is fed back to the producing agent on retry.
    """
    name = model.__name__ if model is not None else error.title
    lines = [
        f"Your output failed validation against the {name} schema. "
        "Fix exactly the fields below, keep everything else unchanged, "
        "and resubmit the complete artifact:"
    ]
    for e in error.errors():
        path = _format_loc(e["loc"])
        problem, expected = _explain(e)
        line = f"- {path}: {problem}"
        if e["type"] != "missing" and "input" in e:
            line += f" (got {_short(e['input'])})"
        if expected:
            line += f". Expected: {expected}"
        lines.append(line + ".")
    return "\n".join(lines)
