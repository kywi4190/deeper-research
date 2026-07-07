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

**Phase A complete; Phase B deterministic substrate built (Prompt 4).**
Every pipeline artifact has a strict Pydantic v2 model with YAML/JSON round-trip, an
LLM-facing validation-error formatter, and a generated JSON Schema in `schemas/`; the
versioned agent prompts for stages 0–5 live in `agents/` with the `deeper-lab`
prompt-iteration harness (design-doc M0). On top of that, the no-LLM substrate now
exists: the git-backed run **workspace** (`workspace.py`), the **run profiles /
config loader** (`config.py`), and the **S2 budget allocator + reflow**
(`allocation.py`). No agent dispatch, stages, orchestrator, or main CLI yet.

## Architecture map

The system is three layers (design §2): a deterministic **kernel** (orchestrator),
LLM **agents** with strict contracts, and a plain-file **workspace** that is the whole
state of a run.

| Path | Responsibility | Status |
|---|---|---|
| `src/deeper/schemas/` | Pydantic artifact models — the stage-to-stage contracts | **built** |
| `schemas/` | Generated JSON Schema exports (inlined into agent prompts); regenerate with `make schemas` | **generated** |
| `src/deeper/workspace.py` | Run workspace: §7 directory tree, git audit trail, schema-checked artifact I/O, state resume | **built** |
| `src/deeper/config.py` | Run profiles (quick/standard/exhaustive), size-class table, §12 hard caps, config.yaml loader | **built** |
| `src/deeper/allocation.py` | S2 budget formula + S3 reflow — pure deterministic math | **built** |
| `src/deeper/agents_runtime/` | SDK dispatch, mock mode, enforcement hooks, cost accounting | stub (Prompt 5) |
| `src/deeper/stages/` | Per-stage logic S0–S8 | stub (Prompts 7–12) |
| `src/deeper/orchestrator/` | State machine, gates, CLI | stub (Prompt 6) |
| `agents/` | Versioned agent prompt files (one per role), stages 0–5 | **built** |
| `src/deeper/promptlab.py` | `deeper-lab` prompt-iteration harness (throwaway quality) | **built** |
| `tests/` | Pytest suite | schema, prompt-library, workspace, config, allocation suites (555 tests) |
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
| `angles/raw/{heuristic}.yaml` | `CartographerReport` (`RawAngle`) | S1 cartographers | S1 merger, saturation rule |
| `angles/map.yaml` | `AngleMap` (`Angle`, `DedupEntry`) | S1 merger | Gate A, S2, S7 frame-check |
| `angles/map-report.md` | `CoverageReport` | S1 merger | Gate A, S4 rubric-builder + S7 frame-check + S8 (strategic notes, by kind) |
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

## The agent prompt library

Each `agents/*.md` file is a versioned prompt with YAML frontmatter (`role`, `stage`,
`model_class`, `output_schemas`, `inputs`, `research`) and a body in the four-part
contract form from design §5/S3 — OBJECTIVE / OUTPUT FORMAT / TOOL & SOURCE GUIDANCE /
BOUNDARIES. The body's `{{schema}}` placeholder is replaced at dispatch with the
agent's exported JSON Schema(s); agents emit artifacts as fenced yaml blocks behind
`### artifact: <name>` markers. Every research-capable prompt carries the
untrusted-web-content rule (fetched-page instructions are data, never directives), and
only the screener's `inputs` may include `preferences` — both enforced by tests.

| Role | Stage | Model class (design §6 mix) | Output schema(s) |
|---|---|---|---|
| `interviewer` | S0 | opus | `brief`, `destination`, `preferences` |
| `cartographer-first-principles` | S1 | sonnet | `cartographer-report` |
| `cartographer-analogist` | S1 | sonnet | `cartographer-report` |
| `cartographer-contrarian` | S1 | sonnet | `cartographer-report` |
| `cartographer-practitioner` | S1 | sonnet | `cartographer-report` |
| `cartographer-taxonomist` | S1 | sonnet | `cartographer-report` |
| `cartographer-horizon` | S1 | sonnet | `cartographer-report` |
| `merger` | S1 | opus | `angle-map`, `coverage-report` |
| `scout` | S3 | sonnet | `option-card-set` |
| `card-critic` | S3 | sonnet | `card-critique` |
| `rubric-builder` | S4 | opus | `rubric` |
| `screener` | S5 | sonnet | `screening-result` |

Design §6 names merger/rubric-builder as Opus-class and cartographers/scouts as
Sonnet-class; roles it leaves unlisted are assigned by analogy (interviewer → opus
because the destination model anchors the whole run; card-critic/screener → sonnet).
The six cartographers share a skeleton but carry genuinely distinct framing-heuristic
sections — ensemble diversity is the breadth mechanism (design P3) — and a test
asserts the sections differ.

Cartographers also have a **strategic-notes secondary channel** (see Design
deviations): typed meta-strategy insights (`reframe` / `rubric-weight` /
`execution`) that are real levers on the goal but not scoutable angles. The merger
dedups them into the coverage report with heuristic attribution; they surface at
Gate A and route onward by kind, and are structurally quarantined from allocation,
scouting, and screening.

## The prompt-lab (`deeper-lab`)

A ~150-line throwaway harness for iterating on prompts against fixtures (design-doc
M0), *not* the production dispatch layer:

```bash
deeper-lab run scout --fixture tests/fixtures/promptlab/angle-interpretability.yaml --mock
deeper-lab run scout --fixture tests/fixtures/promptlab/angle-interpretability.yaml --live
```

`--mock` (default) prints the fully assembled contract — role prompt + inlined JSON
schemas + fixture inputs + budget line — without any API call. `--live` (needs
`ANTHROPIC_API_KEY` or an active profile) sends it through a minimal
`claude_agent_sdk` `query()`, validates the reply's yaml blocks against the declared
schemas, and writes contract/output/validation files to `promptlab-out/` (gitignored).
Fixtures in `tests/fixtures/promptlab/` cover the design doc's senior-project
scenario: `interview-opening.yaml` (S0), `senior-project.yaml` (brief + destination,
for cartographers/rubric), `angle-interpretability.yaml` (adds one angle + allocation,
for scout/card-critic).

## The run workspace

`Workspace.create(root, config)` builds the design-§7 directory tree
(`angles/raw`, `gates`, `options`, `screening`, `dossiers`, `tournament`, `report`,
`sources`, `ledger`, `logs`), git-inits it with a run-local identity (so commits work
regardless of machine git config), writes the materialized `config.yaml` and an initial
`state.json`, and makes the first commit. Every stage completion and gate decision is
one commit — `ws.commit(message)` or `write_artifact(..., commit_message=...)` — so
"what changed after my Gate C feedback?" is a `git diff`, and `history()` is the audit
trail. All artifact I/O goes through the schema layer: writes re-validate the model
(catching post-construction mutation), dump by suffix (`.json` → JSON, else YAML), and
land via atomic temp-file-replace; reads validate before a stage ever sees the content,
so a corrupted file raises instead of propagating. `Workspace.open` re-validates
`state.json`, which is the whole resumability check — state is files.

## Run profiles and how budgets work

`config.py` ships three profiles (design §8) as `RunConfig` defaults —
**quick** (B=16, floor 1, 3 cartographers, shortlist 3), **standard** (B=40, floor 2,
γ=1.0, 5 cartographers, shortlist 5), **exhaustive** (B=80, floor 3, γ=0.8, 6
cartographers, shortlist 7) — plus the size-class table S/M/L → (model, max_searches,
max_output_tokens) and every §12 hard cap as a number code can enforce (`HardCaps`).
A run's `config.yaml` names a profile and overrides any field; `load_config` deep-merges
and validates the whole thing (contradictions like a per-angle cap below the floor are
rejected at load, not discovered mid-run).

Budgets (S2, `allocation.py`, pure code per P8): given total budget **B** in units and
per-angle relevance priors *rᵢ* from the Gate-A-approved angle map,

```
allocationᵢ = floor + (B − n·floor) · rᵢ^γ / Σⱼ rⱼ^γ
```

The **floor** is the exploration guarantee (no surviving angle gets zero attention);
**γ** is the breadth dial (γ>1 concentrates on top angles, γ<1 flattens toward
uniform); a **per-angle cap** (default 25% of B) keeps a dominant prior from starving
the map, with capped angles' excess water-filled back onto the rest by the same
proportional rule. Integer rounding is largest-remainder with deterministic
tie-breaks, so rows always sum exactly to B (the `AllocationTable` schema re-asserts
this). Infeasible combinations (B < n·floor, or n·cap < B) raise instead of silently
bending a rule. `reflow()` redistributes units returned by early-stopped scouts over
the angles whose critics flagged missed options — same formula, floor 0 (the guarantee
was already spent) and cap 100% of the pool — and skips angles that tripped the
redundancy stop.

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
- **Per-cartographer raw output has a schema (`CartographerReport`).** Design §7
  lists "per-cartographer raw" as a workspace artifact but Prompt 2's deliverable list
  didn't name a model for it. Cartographer prompts need a real contract, so
  `RawAngle`/`CartographerReport` were added (`cartographer-report.schema.json`) — a
  raw angle deliberately has a prose `relevance_rationale` and *no* numeric prior
  (priors are the merger's job). The schema accepts 3–12 angles rather than hard 5–12,
  so a genuinely narrow heuristic harvest doesn't force retry-loop padding.
- **Strategic-notes side channel (`StrategicNote`).** M0 live runs showed the
  analogist/contrarian cartographers' sharpest insights for positioning-type goals
  are often meta-strategies (selection criteria, goal reframes, execution timing) —
  real levers, but not regions a scout can populate with option cards; as angles
  they would break S3 scouting and S5's like-for-like scoring. Instead of
  discarding them, `CartographerReport` and `CoverageReport` carry an optional
  `strategic_notes` list (insight, kind: `reframe`|`rubric-weight`|`execution`,
  rationale, merger-filled `source_heuristics`). Routing: all surface at Gate A;
  rubric-weight → S4 rubric-builder as candidate judge-reward evidence; execution →
  S8 next-actions; reframe → enacted only by the human at a gate (P7/P8 —
  exploration agents propose, never redefine the goal). The channel never enters
  S2 allocation, S3 scouting, or S5 screening. The design doc doesn't name this
  artifact; it extends §11's notes-as-schema-inbox pattern into a typed field, and
  the build guide's Prompts 7/8/11/12 now wire the routing.
- **Narrative artifacts are structured models.** Design §7 lists `brief.md`,
  `dossiers/{option}.md` etc. as markdown; the schema layer models their *content* as
  structured, YAML-serializable models so validation is uniform (design §6's
  "required-section checks for markdown" become field requirements — e.g. the five
  standing dossier sections are required fields). Stages may render markdown views of
  these artifacts later; the validated file is the structured one.

## Roadmap position

Following the phases in the build guide:

- **Phase A — Foundation:** Prompt 1 (bootstrap) ✅ · Prompt 2 (schemas) ✅ · Prompt 3
  (agent prompts + prompt-lab) ✅.
- **Phase B — Kernel happy path (M1):** Prompt 4 (workspace/config/allocation) ✅ →
  dispatch layer → orchestrator/CLI → stages S0–S5.
- **Phase C — Depth & adversarial (M2):** deep dives, verifier, tournament, Gate C,
  synthesis.
- **Phase D — Evaluation & hardening (M3).**
- **Phase E — Viewer (M4, optional).**

**Next: Phase B, Prompt 5 — SDK dispatch layer (contracts, mock mode, hooks, cost
accounting).**
