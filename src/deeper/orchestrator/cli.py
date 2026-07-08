"""`deeper` — the CLI over the orchestrator (design §8 CLI surface).

Every command is safe to run repeatedly: `run`/`resume` re-derive the machine
node from state.json and re-enter idempotently; `status`/`report` are
read-only; `new` refuses to touch an existing run directory. Pauses (gates,
attention, not-built-yet stages) exit 0 with instructions; only genuine errors
exit 1.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from deeper.agents_runtime import BillingAuthError
from deeper.config import ConfigError, RunConfig, profile_config
from deeper.schemas import GateName, Stage
from deeper.workspace import Workspace, WorkspaceError

from .engine import Engine, Node, node_of
from .gates import GATE_SPECS
from .rerun import RerunError, invalidate

app = typer.Typer(
    name="deeper",
    help="Gated multi-agent research pipeline: deterministic kernel, LLM subagents.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

DEFAULT_RUNS_DIR = Path("runs")


def _fail(message: str) -> typer.Exit:
    console.print(f"[red]error:[/red] {message}")
    return typer.Exit(code=1)


def _slugify(goal: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "run"


def _open_run(run: str) -> Workspace:
    """Resolve RUN as a literal path first, then as a name under runs/."""
    for candidate in (Path(run), DEFAULT_RUNS_DIR / run):
        if candidate.is_dir():
            try:
                return Workspace.open(candidate)
            except WorkspaceError as err:
                raise _fail(str(err)) from err
    raise _fail(f"no run found at {run!r} (tried ./{run} and {DEFAULT_RUNS_DIR / run})")


def _emit(line: str) -> None:
    # markup=False: engine messages are plain text and may contain [brackets]
    # (gate names, YAML fragments) that Rich would otherwise eat as markup.
    console.print(line, markup=False)


def _terminal_ask_user() -> Callable[[str], str] | None:
    """The S0 interview/confirmation channel: prints the agent's question and
    reads one line back. None when stdin is not a terminal — stages then run
    non-interactively (the interviewer finalizes without questions)."""
    if not sys.stdin.isatty():
        return None

    def ask(question: str) -> str:
        console.print()
        console.print(question, markup=False)
        try:
            return input("> ").strip()
        except EOFError:  # stdin looked like a tty but closed: decline, don't crash
            return ""

    return ask


def _run_engine(workspace: Workspace, *, resume: bool = False) -> Node:
    try:
        engine = Engine(workspace, emit=_emit, ask_user=_terminal_ask_user())
    except BillingAuthError as err:  # billing: api without a key — before any work
        raise _fail(str(err)) from err
    return asyncio.run(engine.resume() if resume else engine.run())


RunArg = Annotated[str, typer.Argument(help="Run directory or name under runs/.")]


@app.command()
def new(
    goal: Annotated[str, typer.Argument(help="The research question or decision to work.")],
    profile: Annotated[str, typer.Option(help="quick | standard | exhaustive.")] = "standard",
    runs_dir: Annotated[
        Path, typer.Option(help="Directory runs are created under.")
    ] = DEFAULT_RUNS_DIR,
    live: Annotated[
        bool, typer.Option("--live", help="Dispatch real agents (default: mock).")
    ] = False,
    max_spend_usd: Annotated[
        float | None,
        typer.Option(
            "--max-spend-usd",
            help="USD spend guard: the run pauses when the ledger crosses this "
            "(default: the profile's, e.g. 5.0 for quick).",
        ),
    ] = None,
) -> None:
    """Create a run, execute S0, and advance until the first gate."""
    try:
        base = profile_config(profile)
    except ConfigError as err:
        raise _fail(str(err)) from err
    data = base.model_dump(mode="json")
    data["goal"] = goal
    data["mode"] = "live" if live else "mock"
    if max_spend_usd is not None:
        data["max_spend_usd"] = max_spend_usd
    config = RunConfig.model_validate(data)

    slug = _slugify(goal)
    stamp = datetime.now().strftime("%Y-%m-%d")  # local date: run ids are human labels
    root = runs_dir / f"{stamp}-{slug}"
    n = 2
    while root.exists():
        root = runs_dir / f"{stamp}-{slug}-{n}"
        n += 1
    try:
        workspace = Workspace.create(root, config)
    except WorkspaceError as err:
        raise _fail(str(err)) from err
    console.print(
        f"created run [bold]{workspace.root}[/bold] (profile={profile}, mode={config.mode})"
    )
    _run_engine(workspace)


@app.command()
def status(run: RunArg) -> None:
    """Where the run is: node, gates, spend by stage."""
    workspace = _open_run(run)
    state = workspace.load_state()
    config = workspace.load_config()
    node = node_of(state)

    table = Table(title=f"run {state.run_id}", show_header=False)
    table.add_row("goal", config.goal or "—")
    table.add_row("profile / mode", f"{state.profile} / {config.mode}")
    table.add_row("node", node.value)
    table.add_row("stage", state.stage.value)
    table.add_row("status", state.status.value)
    table.add_row("gates", "  ".join(f"{g.value}: {s.value}" for g, s in state.gates.items()))
    table.add_row("updated", state.updated_at.isoformat())
    console.print(table)

    spend = Table(title="agent spend")
    spend.add_column("stage")
    spend.add_column("usd", justify="right")
    for stage_name, usd in sorted(state.spend_by_stage().items()):
        spend.add_row(stage_name, f"${usd:.4f}")
    spend.add_row("[bold]total[/bold]", f"[bold]${state.total_usd():.4f}[/bold]")
    console.print(spend)

    if state.retry_counts:
        retries = ", ".join(f"{k}×{v}" for k, v in state.retry_counts.items())
        console.print(f"schema retries: {retries}")
    if state.pending_gate is not None:
        spec = GATE_SPECS[GateName(state.pending_gate)]
        console.print(
            f"pending: [bold]{spec.name.value}[/bold] — review "
            f"{', '.join(spec.review_paths)}, edit {spec.relpath}, then "
            f"`deeper resume {workspace.root}`"
        )


@app.command()
def resume(
    run: RunArg,
    max_spend_usd: Annotated[
        float | None,
        typer.Option(
            "--max-spend-usd",
            help="Rewrite the run's USD spend guard before resuming (the way past "
            "a spend-cap pause).",
        ),
    ] = None,
) -> None:
    """Continue a run from wherever it paused (gate, attention, or crash)."""
    workspace = _open_run(run)
    if max_spend_usd is not None:
        old = workspace.load_config()
        try:
            config = RunConfig.model_validate(
                {**old.model_dump(mode="json"), "max_spend_usd": max_spend_usd}
            )
        except ValidationError as err:
            raise _fail(f"--max-spend-usd {max_spend_usd} is not a valid cap: {err}") from err
        workspace.write_artifact(
            "config.yaml",
            config,
            commit_message=f"spend cap {old.max_spend_usd:g} -> {max_spend_usd:g} USD",
        )
        console.print(f"spend cap: ${old.max_spend_usd:g} -> ${max_spend_usd:g}")
    _run_engine(workspace, resume=True)


@app.command()
def rerun(
    run: RunArg,
    stage: Annotated[str, typer.Option("--stage", help="Stage to re-execute, e.g. S3.")],
    angle: Annotated[
        str | None, typer.Option("--angle", help="Scope an S3 rerun to one angle (id or name).")
    ] = None,
) -> None:
    """Surgically re-execute a stage: invalidate its outputs (git-tracked) and
    everything downstream, then run forward again."""
    workspace = _open_run(run)
    try:
        target = Stage(stage.upper())
    except ValueError as err:
        raise _fail(
            f"unknown stage {stage!r}; expected one of {', '.join(s.value for s in Stage)}"
        ) from err
    try:
        removed = invalidate(workspace, target, angle)
    except RerunError as err:
        raise _fail(str(err)) from err
    console.print(f"invalidated: {', '.join(removed) if removed else 'nothing to remove'}")
    _run_engine(workspace)


@app.command()
def doctor() -> None:
    """Preflight checks for a live run: auth, SDK, config, prompts, schemas.

    Exit 1 only on a genuine failure; auth findings are informational
    (subscription auth cannot be fully verified without dispatching).
    """
    import os

    from deeper.agents_runtime.contracts import AGENTS_DIR, SCHEMAS_DIR, ContractError, load_role
    from deeper.config import PROFILES
    from deeper.schemas.export import ARTIFACT_REGISTRY
    from deeper.schemas.export import check as check_schemas

    rows: list[tuple[str, str, str]] = []  # (check, status, detail)

    # Live runs bill the Claude Code login by default (billing: subscription);
    # a metered ANTHROPIC_API_KEY is used only when config.yaml opts into
    # billing: api. The credentials file is the CLI's login marker on
    # Windows/Linux (macOS may hold it in the Keychain instead — hence warn,
    # never fail, when neither is visible).
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "") or (Path.home() / ".claude"))
    cli_login = (config_dir / ".credentials.json").is_file()
    if key:
        rows.append(
            (
                "Auth",
                "ok",
                f"ANTHROPIC_API_KEY present ({len(key)} chars) — note: under the "
                "default billing: subscription live subagents ignore it (the "
                "dispatcher blanks it and uses the Claude Code login); it is "
                "billed only when config.yaml sets billing: api",
            )
        )
    elif cli_login:
        rows.append(
            (
                "Auth",
                "ok",
                "subscription auth (the default): Claude Code CLI login found at "
                f"{config_dir / '.credentials.json'} — live runs meter your Claude "
                "plan, not an API account",
            )
        )
    else:
        rows.append(
            (
                "Auth",
                "warn",
                "no Claude Code CLI login found and ANTHROPIC_API_KEY not set — "
                "run `claude` once to log in (subscription billing, the default), "
                "or export ANTHROPIC_API_KEY and set billing: api in the run config",
            )
        )

    try:
        import claude_agent_sdk  # noqa: F401 — importability is the check

        try:
            from importlib.metadata import version

            sdk_version = version("claude-agent-sdk")
        except Exception:  # noqa: BLE001 — version is cosmetic; import is the check
            sdk_version = "unknown version"
        rows.append(("Agent SDK", "ok", f"claude-agent-sdk {sdk_version} importable"))
    except ImportError as err:
        rows.append(("Agent SDK", "fail", f"claude_agent_sdk not importable: {err}"))

    config_problems = []
    for name in PROFILES:
        try:
            profile_config(name)
        except (ConfigError, ValidationError) as err:
            config_problems.append(f"{name}: {err}")
    if config_problems:
        rows.append(("Config profiles", "fail", "; ".join(config_problems)))
    else:
        rows.append(("Config profiles", "ok", f"{', '.join(PROFILES)} all validate"))

    prompt_problems = []
    prompt_files = sorted(AGENTS_DIR.glob("*.md")) if AGENTS_DIR.is_dir() else []
    if not prompt_files:
        prompt_problems.append(f"no prompt files found under {AGENTS_DIR}")
    for path in prompt_files:
        try:
            meta, body = load_role(path.stem, AGENTS_DIR)
        except (ContractError, yaml.YAMLError) as err:
            prompt_problems.append(f"{path.name}: {err}")
            continue
        if meta.get("role") != path.stem:
            prompt_problems.append(
                f"{path.name}: frontmatter role {meta.get('role')!r} != filename"
            )
        if "{{schema}}" not in body:
            prompt_problems.append(f"{path.name}: missing the {{{{schema}}}} placeholder")
        for schema in meta.get("output_schemas") or []:
            if schema not in ARTIFACT_REGISTRY:
                prompt_problems.append(f"{path.name}: unknown output schema {schema!r}")
            elif not (SCHEMAS_DIR / f"{schema}.schema.json").is_file():
                prompt_problems.append(f"{path.name}: schema export {schema}.schema.json missing")
    if prompt_problems:
        rows.append(("Agent prompts", "fail", "; ".join(prompt_problems)))
    else:
        rows.append(("Agent prompts", "ok", f"{len(prompt_files)} prompts parse"))

    schema_problems = check_schemas()
    if schema_problems:
        rows.append(
            ("Schema exports", "fail", f"run `make schemas` — {'; '.join(schema_problems)}")
        )
    else:
        rows.append(("Schema exports", "ok", f"{len(ARTIFACT_REGISTRY)} exports fresh"))

    table = Table(title="deeper doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    style = {"ok": "green", "warn": "yellow", "fail": "red"}
    for name, status, detail in rows:
        table.add_row(name, f"[{style[status]}]{status}[/{style[status]}]", detail)
    console.print(table)
    if any(status == "fail" for _, status, _ in rows):
        raise typer.Exit(code=1)


@app.command()
def report(run: RunArg) -> None:
    """Print the decision report's path once S8 has produced it."""
    workspace = _open_run(run)
    target = workspace.path("report/decision-report.md")
    if target.is_file():
        console.print(str(target))
    else:
        console.print("no report yet — the run has not completed S8 synthesis (Prompt 12)")


if __name__ == "__main__":
    app()
