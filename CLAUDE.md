# Deeper Research — project rules

Gated multi-agent research pipeline per `docs/deeper-research-design.md` (Option 3:
pipeline-as-kernel, UI-as-viewer). A deterministic orchestrator decides *process*
(stage sequencing, budgets, stop rules, gates); LLM subagents decide *content*
(mapping, scouting, scoring, arguing). A plain-file research workspace is the
complete state of a run — inspectable, diffable, resumable.

## File map
- `docs/deeper-research-design.md` — authoritative design (numbered §sections).
- `docs/deeper-research-build-guide.md` — the ordered prompt/build plan (phases A–E).
- `docs/m1-live-run-notes.md`, `docs/m2-live-run-notes.md` — live-run triage
  findings and the benchmark seed; required reading for Prompt 14 (eval) and
  ongoing prompt iteration.
- `src/deeper/schemas/` — Pydantic artifact models (stage contracts).
- `src/deeper/agents_runtime/` — SDK dispatch, mock mode, enforcement hooks, cost accounting.
- `src/deeper/stages/` — per-stage logic S0–S8.
- `src/deeper/orchestrator/` — deterministic state machine, gates, CLI.
- `agents/` — versioned agent prompt files (one markdown per role).
- `schemas/` — exported JSON Schemas (inlined into agent prompts).
- `tests/` — pytest suite; `runs/` — per-run workspaces (gitignored); `benchmarks/` — eval specs.

## Standing rules
- **TDD.** Write or extend tests with every feature. Run `make test` before claiming
  done. Never claim done with red tests.
- **README is living context, not a changelog.** After every task, rewrite `README.md`
  so it describes the system *as it now is* (architecture, module responsibilities,
  how to run, what is not yet built). Rewrite stale sections; never append history.
- **Commit** at the end of every task with a conventional-commit message.
- **The design doc is authoritative.** If the implementation must deviate, record the
  deviation and its reason in README's "Design deviations" section.
- **Deterministic spine (P8).** Orchestration logic is code. Never have an LLM decide
  stage sequencing, budgets, or stop rules — only content.

## Commands
- `make test` — run the pytest suite (`python -m pytest -q`).
- `make lint` — `ruff check` + `ruff format --check`.
- `make typecheck` — lenient `mypy`.
- On Windows without GNU make, `make.bat` shims the same targets in PowerShell/cmd.
- CLI (`deeper …`) does not exist yet; it arrives in Prompt 6.

## Compaction
When compacting, preserve: the list of modified files, the current failing tests,
and the active design-doc §section being implemented.
