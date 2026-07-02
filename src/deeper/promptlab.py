"""Prompt-lab: assemble, run, and validate one agent contract against a fixture.

Throwaway-quality iteration harness (design-doc M0), NOT the production dispatch
layer: `deeper-lab run <agent> --fixture f.yaml [--live|--mock]`. Mock (default)
prints the fully assembled contract; live sends it through a minimal
claude_agent_sdk query() and validates the yaml blocks in the reply against the
agent's declared schemas. Everything lands in promptlab-out/.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import sys
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from deeper.schemas.base import format_validation_error
from deeper.schemas.export import ARTIFACT_REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "agents"
SCHEMAS_DIR = REPO_ROOT / "schemas"
DEFAULT_OUT_DIR = REPO_ROOT / "promptlab-out"
DEFAULT_BUDGET = (
    "You have at most 8 web searches and one response in which to emit your "
    "artifact(s). Spend searches on primary sources first."
)
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
YAML_BLOCK_RE = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def _cli() -> None:
    """Prompt iteration harness for the agents/ library."""
    # Windows consoles default to cp1252, which chokes on the prompts' unicode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def parse_agent(path: Path) -> tuple[dict, str]:
    """Split an agents/*.md file into (frontmatter dict, prompt body)."""
    match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"{path.name}: missing '---' YAML frontmatter")
    return yaml.safe_load(match.group(1)), match.group(2)


def assemble(meta: dict, body: str, fixture: dict) -> str:
    """Role prompt + inlined JSON schemas + fixture inputs + budget line."""
    schemas = []
    for name in meta["output_schemas"]:
        text = (SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
        schemas.append(f"JSON Schema for artifact `{name}`:\n```json\n{text}```")
    contract = body.replace("{{schema}}", "\n\n".join(schemas))
    parts = [contract.rstrip(), "# INPUTS"]
    for name, content in (fixture.get("inputs") or {}).items():
        parts.append(f"### input: {name}\n```yaml\n{str(content).rstrip()}\n```")
    parts.append(f"# BUDGET\n{fixture.get('budget', DEFAULT_BUDGET)}")
    return "\n\n".join(parts) + "\n"


def validate_output(meta: dict, text: str) -> tuple[bool, str]:
    """Validate the reply's yaml blocks, in order, against the declared schemas."""
    blocks = YAML_BLOCK_RE.findall(text)
    expected = meta["output_schemas"]
    if len(blocks) < len(expected):
        return False, (
            f"expected {len(expected)} yaml block(s) for {expected}, found {len(blocks)}"
        )
    ok, lines = True, []
    for name, block in zip(expected, blocks, strict=False):
        model = ARTIFACT_REGISTRY[name]
        try:
            model.load_yaml(block)
            lines.append(f"{name}: OK")
        except yaml.YAMLError as err:
            ok = False
            lines.append(f"{name}: not parseable as YAML — {err}")
        except ValidationError as err:
            ok = False
            lines.append(f"{name}:\n{format_validation_error(err, model)}")
    return ok, "\n".join(lines)


async def _query_live(contract: str, model_class: str, research: bool) -> str:
    """Minimal one-shot claude_agent_sdk call; returns concatenated text output."""
    from claude_agent_sdk import ClaudeAgentOptions, query

    options = ClaudeAgentOptions(
        model=model_class,  # opus / sonnet / haiku aliases pass straight through
        allowed_tools=["WebSearch", "WebFetch"] if research else [],
        max_turns=30,
    )
    chunks: list[str] = []
    async for message in query(prompt=contract, options=options):
        for block in getattr(message, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


@app.command()
def run(
    agent: Annotated[str, typer.Argument(help="Agent role, e.g. 'scout' (agents/scout.md)")],
    fixture: Annotated[Path, typer.Option(help="Fixture YAML with 'inputs' (+ optional 'budget')")],
    live: Annotated[bool, typer.Option("--live/--mock", help="Actually call the API")] = False,
    out: Annotated[Path, typer.Option(help="Output directory")] = DEFAULT_OUT_DIR,
) -> None:
    """Assemble (and in --live mode run + validate) one agent contract."""
    meta, body = parse_agent(AGENTS_DIR / f"{agent}.md")
    fixture_data = yaml.safe_load(fixture.read_text(encoding="utf-8")) or {}
    contract = assemble(meta, body, fixture_data)

    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"{agent}-{stamp}-contract.md").write_text(contract, encoding="utf-8", newline="\n")

    if not live:
        typer.echo(contract)
        typer.echo(f"[mock] no API call made; contract saved under {out}")
        return

    reply = asyncio.run(
        _query_live(contract, meta.get("model_class", "sonnet"), bool(meta.get("research")))
    )
    (out / f"{agent}-{stamp}-output.md").write_text(reply, encoding="utf-8", newline="\n")
    ok, report = validate_output(meta, reply)
    report_text = f"agent: {agent}\nfixture: {fixture}\nvalid: {ok}\n{report}\n"
    (out / f"{agent}-{stamp}-validation.txt").write_text(
        report_text, encoding="utf-8", newline="\n"
    )
    typer.echo(report_text)
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
