"""`deeper` — the CLI over the orchestrator (design §8 CLI surface).

Every command is safe to run repeatedly: `run`/`resume` re-derive the machine
node from state.json and re-enter idempotently; `status`/`report` are
read-only; `new` refuses to touch an existing run directory. Pauses (gates,
attention, not-built-yet stages) exit 0 with instructions; only genuine errors
exit 1.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import re
import sys
import time
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
from deeper.workspace import (
    LOG_ROTATE_BACKUPS,
    LOG_ROTATE_MAX_BYTES,
    Workspace,
    WorkspaceError,
)

from .engine import Engine, Node, node_of
from .gates import GATE_SPECS
from .rerun import RerunError, invalidate


def _force_utf8_output() -> None:
    """No emit string may ever crash the console (M1 finding 9: S6's 'Δ' emit
    killed a cp1252 stdout mid-stage with UnicodeEncodeError — an unhandled
    traceback, violating pause-don't-crash). reconfigure mutates the stream in
    place, so Rich's Console picks it up; errors='replace' is the floor —
    worst case mojibake, never a crash."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # detached/closed stream: nothing to fix
                pass


_force_utf8_output()


def _configure_logging(workspace: Workspace) -> None:
    """Route SDK loggers to the run workspace (M2 finding 9): with no handlers
    configured, python logging's lastResort prints records like the SDK's
    "Fatal error in message reader" straight to the operator's stderr — but
    the terminal is a viewer of pipeline emits only. Configures by logger NAME
    so nothing here imports claude_agent_sdk (mock mode stays SDK-free).
    Idempotent per workspace; a different workspace re-points the handler
    (and closes the old one — Windows holds file locks)."""
    logger = logging.getLogger("claude_agent_sdk")
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    target = str(workspace.path("logs/sdk.log"))
    for handler in list(logger.handlers):
        if getattr(handler, "_deeper_sdk_log", False):
            if getattr(handler, "baseFilename", None) == target:
                return
            logger.removeHandler(handler)
            handler.close()
    workspace.path("logs").mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        target,
        encoding="utf-8",
        delay=True,
        maxBytes=LOG_ROTATE_MAX_BYTES,
        backupCount=LOG_ROTATE_BACKUPS,
    )
    handler._deeper_sdk_log = True  # type: ignore[attr-defined]  # idempotency tag
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)


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


def _pending_stdin_input() -> bool:
    """True when unread input is already buffered on stdin. A human cannot type
    a whole further line inside the polling window, so pending input right
    after a line was read is the signature of a multi-line paste."""
    try:
        if sys.platform == "win32":
            import msvcrt

            return bool(msvcrt.kbhit())
        import select

        ready, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(ready)
    except (OSError, ValueError):  # exotic stdin: fall back to single-line reads
        return False


def _paste_pending(has_pending: Callable[[], bool], grace_s: float) -> bool:
    """Poll for buffered input for up to grace_s — the grace absorbs the
    terminal delivering a paste in chunks (conpty), while a typed answer
    (empty buffer) submits after at most one short window."""
    deadline = time.monotonic() + grace_s
    while True:
        if has_pending():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _read_answer(
    read_line: Callable[[], str],
    has_pending: Callable[[], bool] = _pending_stdin_input,
    *,
    grace_s: float = 0.05,
) -> str:
    """One interview answer, multi-line-paste safe (M2 live-run finding 1):
    lines already buffered when a line is read belong to the SAME answer.
    Before this, a pasted multi-line answer sent only its first line — the
    rest sat on stdin and were consumed one per subsequent question, each
    auto-answering a question the user never saw, deranging the interview.
    EOF while draining keeps what was read; EOF on the first line propagates
    (the caller treats it as a decline)."""
    lines = [read_line()]
    while _paste_pending(has_pending, grace_s):
        try:
            lines.append(read_line())
        except EOFError:
            break
    return "\n".join(lines).strip()


def _terminal_ask_user() -> Callable[[str], str] | None:
    """The S0 interview/confirmation channel: prints the agent's question and
    reads one answer back — a multi-line paste is one answer, not a queue of
    future ones. None when stdin is not a terminal — stages then run
    non-interactively (the interviewer finalizes without questions)."""
    if not sys.stdin.isatty():
        return None

    def ask(question: str) -> str:
        console.print()
        console.print(question, markup=False)
        prompts = iter(["> "])  # continuation lines were pasted: no re-prompt
        try:
            return _read_answer(lambda: input(next(prompts, "")))
        except EOFError:  # stdin looked like a tty but closed: decline, don't crash
            return ""

    return ask


def _run_engine(
    workspace: Workspace, *, resume: bool = False, non_interactive: bool = False
) -> Node:
    _configure_logging(workspace)
    ask_user = None if non_interactive else _terminal_ask_user()
    try:
        engine = Engine(workspace, emit=_emit, ask_user=ask_user)
    except BillingAuthError as err:  # billing: api without a key — before any work
        raise _fail(str(err)) from err

    async def _main() -> Node:
        try:
            return await (engine.resume() if resume else engine.run())
        finally:
            if sys.platform == "win32":
                # CPython's Proactor loop needs a few iterations after
                # subprocess transports close before the loop itself closes,
                # or their GC spews "Event loop is closed" (removable belt;
                # gather_strict + generator aclose do the real teardown).
                await asyncio.sleep(0.2)

    return asyncio.run(_main())


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
            "(default: the profile's, e.g. 30.0 for quick).",
        ),
    ] = None,
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            help="Skip all terminal questions (S0 interview, confirmations) even "
            "when stdin looks like a terminal — the explicit form of piping from "
            "a non-tty (on Windows, `< NUL` still LOOKS interactive).",
        ),
    ] = False,
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
    _run_engine(workspace, non_interactive=non_interactive)


def _stage_sort_key(stage_name: str) -> tuple[bool, str]:
    return (stage_name == "EVAL", stage_name)  # pipeline order, EVAL last


def _spend_matrix(state) -> Table:
    """The stage×agent spend table (design §11 ops): USD per (stage, role)
    cell with the attempt count, plus row and column totals."""
    cells: dict[tuple[str, str], float] = {}
    counts: dict[tuple[str, str], int] = {}
    for e in state.spend:
        key = (e.stage.value, e.role)
        cells[key] = cells.get(key, 0.0) + e.usd
        counts[key] = counts.get(key, 0) + 1
    stages = sorted({s for s, _ in cells}, key=_stage_sort_key)
    roles = sorted({r for _, r in cells})
    table = Table(title="agent spend by stage x role (usd, x attempts)")
    table.add_column("stage")
    for role in roles:
        table.add_column(role, justify="right", overflow="fold")
    table.add_column("total", justify="right")
    for stage_name in stages:
        row = [stage_name]
        for role in roles:
            key = (stage_name, role)
            row.append(f"${cells[key]:.4f} x{counts[key]}" if key in cells else "-")
        row.append(f"${sum(usd for (s, _), usd in cells.items() if s == stage_name):.4f}")
        table.add_row(*row)
    totals = ["[bold]total[/bold]"]
    for role in roles:
        totals.append(f"${sum(usd for (_, r), usd in cells.items() if r == role):.4f}")
    totals.append(f"[bold]${sum(cells.values()):.4f}[/bold]")
    table.add_row(*totals)
    return table


@app.command()
def status(
    run: RunArg,
    spend: Annotated[
        bool,
        typer.Option(
            "--spend",
            help="Also print the stage x agent spend matrix (usd and attempt "
            "counts per cell, from the state.json audit trail).",
        ),
    ] = False,
) -> None:
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

    by_stage = Table(title="agent spend")
    by_stage.add_column("stage")
    by_stage.add_column("usd", justify="right")
    ordered = sorted(state.spend_by_stage().items(), key=lambda i: _stage_sort_key(i[0]))
    for stage_name, usd in ordered:
        by_stage.add_row(stage_name, f"${usd:.4f}")
    by_stage.add_row("[bold]total[/bold]", f"[bold]${state.total_usd():.4f}[/bold]")
    console.print(by_stage)
    if spend:
        console.print(_spend_matrix(state))

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
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            help="Skip all terminal questions even when stdin looks like a terminal.",
        ),
    ] = False,
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
    _run_engine(workspace, resume=True, non_interactive=non_interactive)


@app.command()
def rerun(
    run: RunArg,
    stage: Annotated[str, typer.Option("--stage", help="Stage to re-execute, e.g. S3.")],
    angle: Annotated[
        str | None, typer.Option("--angle", help="Scope an S3 rerun to one angle (id or name).")
    ] = None,
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            help="Skip all terminal questions even when stdin looks like a terminal.",
        ),
    ] = False,
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
    _run_engine(workspace, non_interactive=non_interactive)


def _doctor_hooks_check() -> tuple[str, str, str]:
    """§11: verify the enforcement hooks actually deny, not just exist. A dummy
    scout contract attempts the forbidden reads/writes through the same gate
    functions the SDK hooks call — and, when the SDK is importable, through the
    registered PreToolUse callbacks themselves."""
    import tempfile
    from types import SimpleNamespace

    from deeper.agents_runtime.contracts import AgentContract
    from deeper.agents_runtime.hooks import build_hooks, quarantine_gate, write_scope_gate
    from deeper.config import SizeClass

    probe_root = Path(tempfile.gettempdir())  # gates are pure path logic: any root works
    dummy = AgentContract(
        role="scout",
        stage=Stage.S3,
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="doctor probe — never dispatched",
    )
    problems = []
    if quarantine_gate(dummy.role, "Read", {"file_path": "preferences.yaml"}, probe_root) is None:
        problems.append("quarantine gate ALLOWED a scout to read preferences.yaml")
    if quarantine_gate("screener", "Read", {"file_path": "preferences.yaml"}, probe_root):
        problems.append("quarantine gate denied the screener (allowlist broken)")
    if write_scope_gate("Write", {"file_path": "state.json"}, probe_root, (".",)) is None:
        problems.append("write gate allowed an agent to write state.json")
    if write_scope_gate("Write", {"file_path": "notes.md"}, probe_root, ()) is None:
        problems.append("write gate allowed a write with no granted subtree")

    wired = "pure gates verified; SDK hook wiring not checked (SDK not importable)"
    try:
        import claude_agent_sdk  # noqa: F401 — presence gates the wiring probe
    except ImportError:
        pass
    else:
        hooks = build_hooks(dummy, SimpleNamespace(root=probe_root))  # type: ignore[arg-type]
        forbidden_read = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "preferences.yaml"},
        }

        async def _probe() -> bool:
            for matcher in hooks.get("PreToolUse", []):
                for callback in matcher.hooks:
                    result = await callback(forbidden_read, None, None)
                    decision = (result or {}).get("hookSpecificOutput", {})
                    if decision.get("permissionDecision") == "deny":
                        return True
            return False

        if not hooks.get("PreToolUse") or not hooks.get("PostToolUse"):
            problems.append("build_hooks did not register PreToolUse + PostToolUse matchers")
        elif not asyncio.run(_probe()):
            problems.append(
                "a forbidden preferences.yaml Read through the registered PreToolUse "
                "hooks was NOT denied"
            )
        wired = "forbidden preferences.yaml read denied through the registered SDK hooks"

    if problems:
        return ("Enforcement hooks", "fail", "; ".join(problems))
    return (
        "Enforcement hooks",
        "ok",
        f"dummy-contract probe: quarantine + write-scope gates deny correctly; {wired}",
    )


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

    rows.append(_doctor_hooks_check())

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


@app.command("eval")
def eval_cmd(
    run: Annotated[
        str | None, typer.Argument(help="Run to evaluate (omit when using --compare).")
    ] = None,
    against: Annotated[
        str | None,
        typer.Option(
            "--against",
            help="Benchmark spec (an id under benchmarks/, or a path) whose reference "
            "union the breadth metric scores against.",
        ),
    ] = None,
    compare: Annotated[
        tuple[str, str] | None,
        typer.Option(
            "--compare",
            help="Two runs whose persisted eval reports to diff (before/after a "
            "knob or prompt change). Run `deeper eval <run>` on each first.",
        ),
    ] = None,
    compare_baseline: Annotated[
        bool,
        typer.Option(
            "--compare-baseline",
            help="Also judge the benchmark's pasted plain-Deep-Research answer "
            "against the same reference union (requires --against and a pasted "
            "baseline file).",
        ),
    ] = False,
) -> None:
    """Score a completed run on the design-§10 property metrics (breadth,
    informedness, quality, depth, anti-overfit) and write eval/eval-report.md
    — or, with --compare, diff two runs' persisted reports."""
    from deeper.eval import (
        EVAL_MD_PATH,
        EVAL_YAML_PATH,
        EvalError,
        compare_path,
        load_benchmark,
        render_compare,
    )
    from deeper.schemas import EvalReport

    if compare is not None:
        if run is not None or against is not None or compare_baseline:
            raise _fail("--compare takes exactly two runs and no other arguments")
        ws_a, ws_b = _open_run(compare[0]), _open_run(compare[1])
        reports = []
        for ws in (ws_a, ws_b):
            try:
                reports.append(ws.read_artifact(EVAL_YAML_PATH, EvalReport))
            except WorkspaceError as err:
                raise _fail(
                    f"{ws.run_id} has no persisted eval report ({err}); "
                    f"run `deeper eval {ws.root}` first"
                ) from err
        markdown = render_compare(reports[0], reports[1])
        target = ws_b.path(compare_path(reports[0].run_id))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8", newline="\n")
        ws_b.commit(f"deeper eval: compared against {reports[0].run_id}")
        console.print(markdown, markup=False, soft_wrap=True)
        console.print(f"\ncompare report written to {target}", markup=False, soft_wrap=True)
        return

    if run is None:
        raise _fail("give a run to evaluate, or --compare <runA> <runB>")
    if compare_baseline and against is None:
        raise _fail("--compare-baseline needs --against <benchmark> for the reference union")
    workspace = _open_run(run)
    _configure_logging(workspace)
    spec = spec_path = None
    if against is not None:
        try:
            spec, spec_path = load_benchmark(against)
        except EvalError as err:
            raise _fail(str(err)) from err

    async def _main():
        from deeper.eval import evaluate_run

        return await evaluate_run(
            workspace,
            spec,
            spec_path,
            include_baseline=compare_baseline,
            emit=_emit,
        )

    from deeper.agents_runtime import (
        AgentDispatchFailed,
        AgentOutputInvalid,
        SpendCapExceeded,
        UsageLimitReached,
    )

    try:
        eval_report = asyncio.run(_main())
    except EvalError as err:
        raise _fail(str(err)) from err
    except (
        AgentDispatchFailed,
        AgentOutputInvalid,
        SpendCapExceeded,
        UsageLimitReached,
        BillingAuthError,
    ) as err:
        # Judge dispatch failures never touch the run's pipeline state — the
        # eval writes nothing partial; report the cause and stop. A spend-cap
        # refusal means the completed run sits at its cap: raise max_spend_usd
        # in the run's config.yaml to fund the judge.
        raise _fail(f"eval judge dispatch failed: {err}") from err

    console.print(f"eval report written to {workspace.path(EVAL_MD_PATH)}", soft_wrap=True)
    console.print(f"structured report at   {workspace.path(EVAL_YAML_PATH)}", soft_wrap=True)
    headline = []
    if eval_report.breadth is not None and eval_report.breadth.reference_total:
        b = eval_report.breadth
        headline.append(f"breadth {len(b.hits)}/{b.reference_total}")
        if b.practitioner_obvious_misses:
            headline.append(f"OBVIOUS MISSES {len(b.practitioner_obvious_misses)}")
    if eval_report.informedness is not None:
        sp = eval_report.informedness.spearman
        headline.append(f"informedness {'n/a' if sp is None else f'{sp:.2f}'}")
    if eval_report.quality is not None:
        headline.append(f"revision rate {eval_report.quality.revision_rate:.0%}")
    if eval_report.depth is not None:
        headline.append(f"pass rate {eval_report.depth.overall_pass_rate:.0%}")
    if eval_report.anti_overfit is not None:
        headline.append(
            f"{len(eval_report.anti_overfit.inversions)} inversion(s)"
            + ("" if eval_report.anti_overfit.inversions_steelmanned else " UNSTEELMANNED")
        )
    if headline:
        console.print("headline: " + " · ".join(headline), markup=False, soft_wrap=True)
    if eval_report.skipped:
        console.print("skipped: " + "; ".join(eval_report.skipped), markup=False, soft_wrap=True)
    console.print(f"eval judge spend: ${eval_report.eval_usd:.4f}")


@app.command()
def view(
    run: RunArg,
    host: Annotated[str, typer.Option(help="Bind address (keep it local).")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to serve on.")] = 8177,
) -> None:
    """Launch the workspace viewer (design §8 v2) on localhost: read-only pages
    over the run's files, plus gate forms that write ONLY gates/gate-*.yaml —
    the same files, through the same schemas, `deeper resume` validates."""
    import uvicorn

    from deeper.viewer import create_app

    workspace = _open_run(run)  # validates the run before anything binds a port
    runs_root = workspace.root.parent
    console.print(
        f"viewer: http://{host}:{port}/run/{workspace.root.name} "
        f"(all runs under {runs_root} at http://{host}:{port}/)"
    )
    uvicorn.run(create_app(runs_root), host=host, port=port, log_level="warning")


@app.command()
def report(run: RunArg) -> None:
    """The decision report's path plus a terminal summary: winner, both
    scoreboards, the top sensitivity flag, verification pass rates, spend."""
    from deeper.report import strip_annotations, top_sensitivity_flag
    from deeper.schemas import DecisionReport, Rubric, ScreeningResult, VerificationReport
    from deeper.sensitivity import dual_scoreboards, preference_sweep, weight_sensitivity
    from deeper.stages.s6_deepdive import verification_path
    from deeper.stages.s7_tournament import UPDATED_SCORES_PATH
    from deeper.stages.s8_synthesis import REPORT_MD_PATH, REPORT_YAML_PATH

    workspace = _open_run(run)
    md_target = workspace.path(REPORT_MD_PATH)
    if not md_target.is_file() or not workspace.path(REPORT_YAML_PATH).is_file():
        console.print("no report yet — the run has not completed S8 synthesis")
        return
    decision = workspace.read_artifact(REPORT_YAML_PATH, DecisionReport)
    rubric = workspace.read_artifact("rubric.yaml", Rubric)
    scores = workspace.read_artifact(UPDATED_SCORES_PATH, ScreeningResult)
    weights = {c.id: c.weight for c in rubric.criteria}
    slot_weight = rubric.preference_slot.weight
    dest, adjusted = dual_scoreboards(scores, rubric)
    flag = top_sensitivity_flag(
        weight_sensitivity(scores.options, weights, slot_weight),
        preference_sweep(scores.options, weights),
        slot_weight,
    )

    console.print(str(md_target), markup=False, soft_wrap=True)
    first_sentence = strip_annotations(decision.recommendation).split(". ")[0].rstrip(".")
    winner_line = f"winner: [bold]{decision.winner_option_id}[/bold] — {first_sentence}."
    if decision.dissent_unrebutted:
        winner_line += " [red]DISSENT UNREBUTTED[/red]"
    console.print(winner_line, soft_wrap=True)

    boards = Table(title="the two scoreboards")
    boards.add_column("option")
    boards.add_column("destination-only", justify="right")
    boards.add_column("preference-adjusted", justify="right")
    dest_by_id = {row.option_id: row for row in dest}
    for row in adjusted:
        d = dest_by_id[row.option_id]
        boards.add_row(row.option_id, f"{d.score:g} (#{d.rank})", f"{row.score:g} (#{row.rank})")
    console.print(boards)
    console.print(f"sensitivity: {flag}", markup=False, soft_wrap=True)

    checks = Table(title="verification pass rates")
    checks.add_column("option")
    checks.add_column("pass rate", justify="right")
    for row in adjusted:
        target = workspace.path(verification_path(row.option_id))
        if target.is_file():
            vr = workspace.read_artifact(verification_path(row.option_id), VerificationReport)
            checks.add_row(row.option_id, f"{vr.pass_rate:.0%}")
        else:
            checks.add_row(row.option_id, "-")
    console.print(checks)
    console.print(f"agent spend: ${workspace.load_state().total_usd():.4f}")


if __name__ == "__main__":
    app()
