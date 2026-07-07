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

**Phase A complete; Phase B substrate, dispatch layer, and orchestrator built
(Prompt 6).** Every pipeline artifact has a strict Pydantic v2 model with YAML/JSON
round-trip, an LLM-facing validation-error formatter, and a generated JSON Schema in
`schemas/`; the versioned agent prompts for stages 0–5 live in `agents/` with the
`deeper-lab` prompt-iteration harness (design-doc M0). The no-LLM substrate exists —
the git-backed run **workspace** (`workspace.py`), the **run profiles / config loader**
(`config.py`), the **S2 budget allocator + reflow** (`allocation.py`) — plus the
**agent dispatch layer** (`agents_runtime/`) and now the **deterministic orchestrator**
(`orchestrator/`): an explicit state machine over S0–S8 with gates as file-edit pause
states, crash-safe idempotent resume, surgical rerun invalidation, and the `deeper`
CLI. Stages S0–S2 have provisional mock-walkable implementations (real versions arrive
in Prompt 7); S3–S8 are registered stubs that report cleanly.

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
| `src/deeper/agents_runtime/` | SDK dispatch, mock mode, enforcement hooks, cost accounting | **built** |
| `src/deeper/stages/` | Per-stage logic S0–S8 (`StageBase` protocol + registry) | S0–S2 provisional, S3–S8 stubs (Prompts 7–12) |
| `src/deeper/orchestrator/` | State machine (`engine.py`), gates (`gates.py`), rerun invalidation (`rerun.py`), `deeper` CLI (`cli.py`) | **built** |
| `agents/` | Versioned agent prompt files (one per role), stages 0–5 | **built** |
| `src/deeper/promptlab.py` | `deeper-lab` prompt-iteration harness (throwaway quality) | **built** |
| `tests/` | Pytest suite | schema, prompt-library, workspace, config, allocation, agents-runtime, orchestrator suites (658 tests) |
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

## The agent runtime (`src/deeper/agents_runtime/`)

The single chokepoint through which every LLM invocation flows. An `AgentContract`
names a role (→ `agents/<role>.md`), the stage, the declared output schemas, a size
class, a budget line, the input artifacts *as content* (never paths — the prompt
string is the only parent→child channel, which enforces artifact-as-contract), and
the workspace subtrees the agent may write.

```
AgentContract
     │ assemble_prompt(): role prompt + inlined JSON schema(s)
     │                    + # TASK + # INPUTS + # BUDGET
     │ (drift between contract and prompt frontmatter fails HERE, before any spend)
     ▼
┌ semaphore (config.concurrency) ┐
│  _invoke():                    │   mode: live → claude_agent_sdk query()
│    live SDK call | mock fixture│   mode: mock → tests/fixtures/mock_agents/<role>/
└────────────────────────────────┘
     │ SpendEntry → state.json          (EVERY attempt, success or not)
     ▼
parse `### artifact: <name>` markers → validate via ARTIFACT_REGISTRY
     │ valid                                  │ invalid
     ▼                                        ▼
AgentResult                    re-invoke with format_validation_error()
(validated models,             feedback appended, up to caps.max_schema_retries
 cost, retries_used)           times → then raise AgentOutputInvalid
                               (orchestrator pauses run: human-attention flag)
```

**The quarantine guarantee** (design §6): a `PreToolUse` hook denies any
Read/Grep/Glob whose target *or search root* covers `preferences.yaml` unless the
contract's role is in `{screener, synthesist}` — an allowlist in code, not prompt
goodwill. A second hook fences writes to the contract's declared subtrees (and
hard-denies `state.json`/`config.yaml`/`preferences.yaml` for every agent); a
`PostToolUse` hook caches every WebFetch content-addressed into `sources/` (with a
`SourceRecord` and a `logs/web-audit.jsonl` line) after `sanitize_source_text`
strips tool-call-like and instruction-injection patterns. Sanitization protects
*re-injection from the cache*; the fetching agent's own defense is the prompt-level
untrusted-web rule. Live dispatch is additionally fenced by
`permission_mode="dontAsk"` + per-role `allowed_tools` (research roles get
WebSearch/WebFetch/Read/Write; no research agent gets Bash or subagents) +
`setting_sources=[]` + `cwd` pinned to the run workspace.

**Mock mode** (`config.yaml mode: mock`, the default) substitutes only the network
call: `MockDispatcher` renders canned fixtures from
`tests/fixtures/mock_agents/<role>/<schema>[.<context>].yaml` (a coherent
senior-project scenario covering all 12 Phase-A roles) into the same marker+fenced-yaml
text a live agent emits, then flows through the identical parse/validate/retry/ledger
path — the whole pipeline runs offline with zero SDK imports (asserted by a
fresh-interpreter test). `scripted_responses` lets tests inject invalid-then-valid
sequences to exercise the retry loop.

**Spend accounting**: every attempt lands a `SpendEntry` (stage, role, angle/option
context, usd, tokens) in `state.json` immediately; `SpendLedger.spend_so_far(stage)`
is what gates report and the orchestrator's cap checks read. Retry counts persist in
`RunState.retry_counts` keyed `stage:role:context`.

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

## The orchestrator (`src/deeper/orchestrator/`)

The deterministic spine (design P8, §8): code decides *process* — stage sequencing,
budgets, stop rules, gates — and the only path to an LLM is a stage calling the
dispatch layer. The state machine's nodes:

```
new ─► S0 ─► S1 ─► GATE_A ─► S2 ─► S3 ─► S4 ─► GATE_B ─► S5 ─► S6 ─► S7 ─► GATE_C ─► S8 ─► DONE
              ▲      │ rerun_hint                                            │ (loops: Prompts 11/12)
              └──────┘
any stage ──AgentOutputInvalid──► PAUSED_ATTENTION ──resume──► same stage
```

Nothing new is persisted for the nodes: `RunState` already encodes every one as the
`(stage, status, pending_gate)` triple, and `node_of()` derives the node from it. Each
`Engine.run()` step loads state, dispatches on the node, and commits the transition —
so **crash safety is free**: after any interruption, `deeper resume` re-derives where
it was and re-enters. Idempotency has two levels: the engine skips a stage whose
declared outputs all exist and validate, and each stage's `execute()` skips
already-completed sub-work before dispatching (e.g. a per-cartographer raw report that
validates is not re-run).

**Stages** (`src/deeper/stages/`) are classes over a small protocol —
`validate_inputs()` (schema-check required artifacts), `execute(ctx)` (may dispatch
agents), `evaluate_stop_rules(ctx)`, `outputs(ctx)`, `is_complete(ctx)` — registered
in `STAGES`. S0 (mock-only interview), S1 (fixed ensemble, no saturation rule yet),
and S2 (real allocation math) are provisional; S3–S8 raise `NotImplementedYet`, which
the engine reports cleanly, leaving state untouched and resumable.

**Gates are file-edit pause states.** Entering a gate writes a commented template
(`gates/gate-{a,b,c}.yaml`) whose body already parses as a *valid but undecided*
decision (`approved: false`), prints what to review and edit, and exits. The template
is never overwritten once it exists — a half-edited decision survives resume. On
`deeper resume`, the file is validated: YAML/schema errors or a still-undecided body
re-pause with the exact problem; `approved: true` advances (gate marked in
`state.json`, one commit); Gate A's `rerun_hint` loops back through S1 via the same
invalidation machinery as `rerun`. An agent that exhausts its schema retries
(`AgentOutputInvalid`) pauses the run as `PAUSED_ATTENTION` with the validation
errors; `deeper resume` re-enters the stage after you fix the cause.

**Surgical rerun** (`deeper rerun <run> --stage S3 [--angle x]`) is git-tracked
deletion: the target stage's output subtree (angle-scoped for S3) plus everything
downstream — stage outputs, gate files, gate statuses — is removed in one commit, the
run pointer moves back, and the machine walks forward again. Deletion (not a stale
flag) is what keeps the idempotency checks honest; spend entries are never touched,
and recovery is one `git revert` away.

## How to run

Set up a virtual environment and install in editable mode (registers the `deeper`
and `deeper-lab` entry points):

```bash
python -m venv .venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# macOS/Linux:          source .venv/bin/activate
pip install -e ".[dev]"
```

Then drive a run through the CLI (mock mode is the default — the whole pipeline runs
offline against fixtures):

```bash
deeper new "which vector database should we adopt" --profile quick
#   creates runs/<date>-<goal-slug>, executes S0+S1, pauses at Gate A telling you
#   exactly what to review and which file to edit
#   (--live dispatches real agents; S0's interactive interview arrives in Prompt 7)

deeper status <run>          # node, gate statuses, spend by stage, pending-gate hint
# edit runs/<...>/gates/gate-a.yaml -> approved: true
deeper resume <run>          # validates the gate file, runs S2, continues to S3
deeper rerun <run> --stage S1            # invalidate S1 + downstream, rewalk
deeper rerun <run> --stage S3 --angle x  # scoped to one angle's scout outputs
deeper report <run>          # decision-report path (S8, not built yet)
```

`<run>` is a path or a name under `runs/`. Every command is safe to repeat: pauses
exit 0 with instructions, `status`/`report` are read-only, and re-entering a run never
re-executes completed work.

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
- **Schema-retry count is config-driven, not the build guide's "once".** The build
  guide's Prompt 5 says re-invoke once then fail; design §6 says "max 2 retries" and
  `HardCaps.max_schema_retries` already defaults to 2. The design doc wins: the retry
  loop runs up to `caps.max_schema_retries` re-invocations before raising
  `AgentOutputInvalid`.
- **Per-dispatcher semaphore, not module-level.** An `asyncio.Semaphore` binds to the
  running event loop, so a literal module-level semaphore breaks across loops (pytest
  creates one per test). The semaphore lives on the dispatcher instance, created
  lazily; one dispatcher per run keeps the concurrency limit globally binding.
- **Hook-written `SourceRecord.tier` defaults to T3.** The source-cache hook cannot
  judge source quality, so it records the conservative floor; agents carry their own
  tier judgments in per-claim `SourceRef`s and the verifier adjudicates later.
- **The goal lives in `RunConfig`.** `deeper new` must persist the user's goal before
  S0 exists to restate it into `brief.md`, so `RunConfig` carries an optional `goal`
  field — `config.yaml` is the materialized per-run input record. The design doc
  doesn't name a home for the raw goal text.
- **Undecided-gate semantics.** The design says resume "re-validates the gate file and
  continues" but doesn't define a not-yet-decided file. Here the written template
  parses as a valid decision with `approved: false` and no actions; resume treats that
  as "no decision recorded" and re-pauses, so an accidental resume can never advance a
  gate. Gate templates are never overwritten once written.
- **S0–S2 are provisional until Prompt 7.** So the state machine is walkable
  end-to-end today (the build guide's Prompt 6 exit test), S0 dispatches the
  interviewer non-interactively in mock mode only, S1 runs a fixed ensemble without
  the saturation rule, and Gate A records but does not yet apply edit actions or
  inject the rerun hint. S2's allocation is the real math.
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
  Prompt 5 (dispatch layer) ✅ → Prompt 6 (orchestrator/CLI) ✅ → stages S0–S5.
- **Phase C — Depth & adversarial (M2):** deep dives, verifier, tournament, Gate C,
  synthesis.
- **Phase D — Evaluation & hardening (M3).**
- **Phase E — Viewer (M4, optional).**

**Next: Phase B, Prompt 7 — real S0–S2: interactive intake, cartography ensemble with
the saturation rule, Gate A action application, allocation at the gate exit.**
