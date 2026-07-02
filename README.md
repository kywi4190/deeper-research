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

**Schema layer complete (Phase A, Prompt 2).** Every pipeline artifact — S0 intake
through S8-adjacent run state — has a strict Pydantic v2 model with YAML/JSON
round-trip, an LLM-facing validation-error formatter, and a generated JSON Schema in
`schemas/`. No agents, stages, orchestrator, or CLI yet.

## Architecture map

The system is three layers (design §2): a deterministic **kernel** (orchestrator),
LLM **agents** with strict contracts, and a plain-file **workspace** that is the whole
state of a run.

| Path | Responsibility | Status |
|---|---|---|
| `src/deeper/schemas/` | Pydantic artifact models — the stage-to-stage contracts | **built** |
| `schemas/` | Generated JSON Schema exports (inlined into agent prompts); regenerate with `make schemas` | **generated** |
| `src/deeper/agents_runtime/` | SDK dispatch, mock mode, enforcement hooks, cost accounting | stub (Prompt 5) |
| `src/deeper/stages/` | Per-stage logic S0–S8 | stub (Prompts 7–12) |
| `src/deeper/orchestrator/` | State machine, gates, CLI | stub (Prompt 6) |
| `agents/` | Versioned agent prompt files (one per role) | empty (Prompt 3) |
| `tests/` | Pytest suite | schema suite (85 tests) |
| `benchmarks/` | Eval question specs | empty (Prompt 14) |
| `runs/` | Per-run workspaces (gitignored) | created at runtime |

## The schema layer

Artifacts are contracts (design P2): a stage cannot start until its inputs validate,
and agent outputs are rejected-and-retried against validation errors until they do.
The layer lives in `src/deeper/schemas/`, one module per artifact family, all
subclassing `ArtifactModel` (`base.py`), which provides:

- **Strict validation** — `extra="forbid"` everywhere; each agent-produced artifact
  instead carries an explicit free-form `notes` field (design §11's schema-revision
  inbox).
- **YAML/JSON round-trip** — `dump_yaml()`/`load_yaml()` (and file variants) preserve
  field declaration order so gate-time diffs stay readable; `RunState` uses the JSON
  variants for `state.json`.
- **`format_validation_error()`** — renders a `ValidationError` as per-field
  instructions (path + what's wrong + what's expected) that are fed back verbatim to
  the producing agent on retry.
- **JSON Schema exports** — `make schemas` (= `python -m deeper.schemas`) regenerates
  `schemas/*.schema.json`; `--check` and a test fail when exports are stale. Field
  `description`s carry the design semantics into the schemas, which later get inlined
  into agent prompts.

Cross-artifact referential integrity (e.g. a screening score's `criterion_id`
existing in the rubric) is deliberately an orchestrator concern, not a schema one:
each model validates a single file.

### Artifact → model → stage map

| Workspace file (design §7) | Model | Produced by | Consumed by |
|---|---|---|---|
| `brief.md` | `Brief` | S0 interviewer | S1, S7 frame-check, S8 |
| `destination.md` | `DestinationModel` | S0 interviewer | S1 merger, S4 rubric-builder, S8 |
| `preferences.yaml` | `Preferences` | S0 interviewer | S5 screener, S8 synthesist (quarantined) |
| `angles/map.yaml` | `AngleMap` (`Angle`, `DedupEntry`) | S1 merger | Gate A, S2, S7 frame-check |
| `angles/map-report.md` | `CoverageReport` | S1 merger | Gate A, S7 frame-check |
| `allocation.yaml` | `AllocationTable` | S2 (pure code) | S3, report appendix |
| `options/{angle}/cards.yaml` | `OptionCardSet` (`OptionCard`, `KillRisk`) | S3 scout | S4, S5, S6 |
| `options/{angle}/critique.md` | `CardCritique` | S3 card-critic | S3 revision/reflow, S7 frame-check |
| `rubric.yaml` | `Rubric` (`Criterion`, `PreferenceSlot`) | S4 rubric-builder | Gate B, S5–S8 |
| `screening/scores.yaml` | `ScreeningResult` (`OptionScreening`) | S5 screener | shortlist code, S6 re-scoring |
| `screening/shortlist.md` | `Shortlist` (`ShortlistDecision`) | S5 (code) | S6, report appendix |
| `dossiers/{option}.md` | `Dossier` (`Claim`, `DossierSection`) | S6 analyst | verifier, S7, S8 |
| `dossiers/{option}-verification.md` | `VerificationReport` | S6 verifier | S8 report |
| `tournament/{option}-prosecution.md` | `Prosecution` | S7 prosecutor | Gate C, S8 |
| `tournament/steelman.md` | `Steelman` | S7 steelman | Gate C, S8 |
| `tournament/frame-check.md` | `FrameCheck` (`RedivergenceProposal`) | S7 frame-checker | Gate C |
| `tournament/score-updates.yaml` | `ScoreUpdateLog` | S7 judge | S8 |
| `gates/gate-{a,b,c}.yaml` | `GateADecision` / `GateBDecision` / `GateCDecision` | human (or viewer) | orchestrator resume |
| `sources/` records | `SourceRecord` | any research agent | verifier, audit |
| `ledger/contradictions.md` | `ContradictionLedger` | any detecting agent | verifier, S8 |
| `state.json` | `RunState` (`SpendEntry`) | orchestrator | orchestrator, CLI |

Notable schema-level invariants (each mirrors a design rule): allocation rows must sum
exactly to the budget; anchored rubric levels must be exactly 1–5 and criterion
weights must sum to 1.0 (the preference slot is weighted separately, per P9); a
screening score must lie inside its uncertainty band; a `gap-found` frame-check must
carry a re-divergence proposal; a `BUDGET-CAPPED` dossier must list its open
questions; Gate C approval excludes the loop actions.

## How to run

There is no CLI yet — it arrives in Prompt 6 (`deeper new`, `status`, `resume`,
`rerun`, `report`). For now the project is a library.

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
make schemas     # regenerate schemas/*.schema.json from the models
```

On Windows without GNU make installed, `make.bat` provides the same targets. The
canonical `Makefile` is used wherever GNU make is available.

## Design deviations

- **Windows `make` shim.** GNU make is not standard on Windows, so a `make.bat` mirrors
  the `Makefile` targets (`test`, `lint`, `typecheck`, `schemas`). The `Makefile`
  remains canonical.
- **Narrative artifacts are structured models.** Design §7 lists `brief.md`,
  `dossiers/{option}.md` etc. as markdown; the schema layer models their *content* as
  structured, YAML-serializable models so validation is uniform (design §6's
  "required-section checks for markdown" become field requirements — e.g. the five
  standing dossier sections are required fields). Stages may render markdown views of
  these artifacts later; the validated file is the structured one.

## Roadmap position

Following the phases in the build guide:

- **Phase A — Foundation:** Prompt 1 (bootstrap) ✅ · Prompt 2 (schemas) ✅ · Prompt 3
  (agent prompts + prompt-lab).
- **Phase B — Kernel happy path (M1):** workspace/config/allocation → dispatch layer →
  orchestrator/CLI → stages S0–S5.
- **Phase C — Depth & adversarial (M2):** deep dives, verifier, tournament, Gate C,
  synthesis.
- **Phase D — Evaluation & hardening (M3).**
- **Phase E — Viewer (M4, optional).**

**Next: Phase A, Prompt 3 — the agent prompt library and prompt-lab harness.**
