# Deeper Research

A personal, decision-grade research system: given a goal, it maps the full space of
solution *angles*, populates each with *options*, scores them against a
destination-derived rubric, deep-dives the survivors, adversarially stress-tests the
winner, and produces a decision report — pausing at human review gates where your
judgment matters. It is built as a deterministic Python orchestrator (code owns the
process) driving LLM subagents (they own the content), with a plain-file workspace as
the single source of truth.

Full design: [`docs/deeper-research-design.md`](docs/deeper-research-design.md).
Build plan: [`docs/deeper-research-build-guide.md`](docs/deeper-research-build-guide.md).

## Current status

**Skeleton only (Phase A, Prompt 1 complete).** The Python package, tooling, and docs
exist and the test harness runs green. No pipeline logic, schemas, agents, or CLI are
implemented yet.

## Architecture map

The system is three layers (design §2): a deterministic **kernel** (orchestrator),
LLM **agents** with strict contracts, and a plain-file **workspace** that is the whole
state of a run. Modules are stubbed now and filled in later prompts:

| Path | Responsibility | Status |
|---|---|---|
| `src/deeper/schemas/` | Pydantic artifact models — the stage-to-stage contracts | stub (Prompt 2) |
| `src/deeper/agents_runtime/` | SDK dispatch, mock mode, enforcement hooks, cost accounting | stub (Prompt 5) |
| `src/deeper/stages/` | Per-stage logic S0–S8 | stub (Prompts 7–12) |
| `src/deeper/orchestrator/` | State machine, gates, CLI | stub (Prompt 6) |
| `agents/` | Versioned agent prompt files (one per role) | empty (Prompt 3) |
| `schemas/` | Exported JSON Schemas (inlined into prompts) | empty (Prompt 2) |
| `tests/` | Pytest suite | placeholder test only |
| `benchmarks/` | Eval question specs | empty (Prompt 14) |
| `runs/` | Per-run workspaces (gitignored) | created at runtime |

## How to run

There is no CLI yet — it arrives in Prompt 6 (`deeper new`, `status`, `resume`,
`rerun`, `report`). For now the project is a library skeleton.

Set up a virtual environment and install in editable mode:

```bash
python -m venv .venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# macOS/Linux:          source .venv/bin/activate
pip install -e ".[dev]"
```

## How to test

```bash
make test        # python -m pytest -q
make lint        # ruff check + ruff format --check
make typecheck   # lenient mypy
```

On Windows without GNU make installed, `make.bat` provides the same targets (typing
`make test` in PowerShell/cmd runs the shim). The canonical `Makefile` is used
wherever GNU make is available.

## Design deviations

- **Windows `make` shim.** GNU make is not standard on Windows, so a `make.bat` mirrors
  the `Makefile` targets (`test`, `lint`, `typecheck`, `schemas`) so the documented
  `make test` / `make lint` commands work here. The `Makefile` remains canonical.

## Roadmap position

Following the phases in the build guide:

- **Phase A — Foundation:** Prompt 1 (bootstrap) ✅ · Prompt 2 (schemas) · Prompt 3 (agent prompts + prompt-lab).
- **Phase B — Kernel happy path (M1):** workspace/config/allocation → dispatch layer → orchestrator/CLI → stages S0–S5.
- **Phase C — Depth & adversarial (M2):** deep dives, verifier, tournament, Gate C, synthesis.
- **Phase D — Evaluation & hardening (M3).**
- **Phase E — Viewer (M4, optional).**

**Next: Phase A, Prompt 2 — the artifact schema layer.**
