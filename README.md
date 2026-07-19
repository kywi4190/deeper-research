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

**M2 complete (including its exit test), and the M3 eval harness is built.**
One full standard-profile live run
(`2026-07-13-map-the-space-of-first-hands-on-ai-ml-re`, $110.43) worked all
three gates for real and produced a citation-linked decision report with a
clean pass over 105 claims. The complete triage for that run — findings 1–9
(infrastructure, all addressed), the user's through-Gate-B findings F1–F12
with post-run verdicts, the ground-truth divergence (the expected fork-test
winner was never carded: an option-level scouting miss, not a scoring
error), the S6 depth stats, and the still-unexercised paths — lives in
[`docs/m2-live-run-notes.md`](docs/m2-live-run-notes.md) (with
[`docs/m1-live-run-notes.md`](docs/m1-live-run-notes.md)); it is the
prompt-iteration evidence the eval loop consumes.

**Prompt 14 has landed the measurement layer** (design §10, P10): four
seeded benchmark specs in `benchmarks/` (transcribed from
[`docs/benchmark-seeds.md`](docs/benchmark-seeds.md), no placeholders),
`src/deeper/eval/` computing the five property metrics over any run's
workspace, a Haiku-class `eval-judge` dispatched through the same agent
runtime as the pipeline (mockable, cost-tracked under ledger stage `EVAL`),
and `deeper eval` / `eval --compare` / `eval --compare-baseline` — see
"Evaluation" below. **The Prompt 14 verify step has been run**: scoring the
M2 run against `probe-space-mapping` matched the triage on informedness
(0.32, floor 80% — F7), quality (100% revision rate, S3/S5 retry hotspots —
F6), depth (73%, rademacher 33%), and anti-overfit (no inversions), and
exposed two eval-layer defects, both fixed: the breadth metric now reads
angle provenance (a Gate-A rescue reports as **human-rescued**, so the
F3 practitioner-obvious check fires on ensemble coverage — the M2 run
honestly scores 15/16 ensemble, 16/16 map), and an option-check term match
renders as **TERM MATCH — confirm by hand**, never as a pass (the run's
"compression" match was a context-compression card, not the fork test —
Trap 2's NOT CARDED verdict stands).

**Prompt 15 has closed the design-§11 risk register** — every mitigation the
doc lists is now implemented *and pinned by tests*: the prompt-injection
bullet is complete (hidden-text sanitizer patterns — HTML comments and
invisible Unicode — plus adversarial fixture pages proven inert on cache
re-injection, and `tests/test_policy_s11.py` turning the no-Bash /
no-write-scope / quarantine-allowlist claims into executable policy); §6's
"unresolved contradictions surface in the report" is wired into S8's
residual-uncertainty register; the ops layer gained structured JSONL
invocation logs with per-run rotation, `deeper status --spend` (stage×agent
matrix), and a doctor check that probes hook denial through a dummy
contract; and §11's gate-fatigue mitigation is a real config dial
(`gate_modes: {gate-b: notify}` auto-approves with defaults + a prominent
summary; all gates default to hard). "When things go wrong" below is the
matching failure-mode runbook.

The full pipeline S0–S8 runs end-to-end in mock (proven live end-to-end by
the run above), from `deeper new` to a citation-linked decision report, and
the Prompt 13 pre-live hardening pass has landed: every M1 triage finding is
addressed, and every live failure path — agent timeout, network error, SDK
exception, schema double-failure, spend cap, plan usage-limit exhaustion —
lands in a resumable `PAUSED_ATTENTION` with a transcript, never a crash. The
extended M1/M2 walk is proven by `tests/test_e2e_mock.py`: one test drives a
full mock run S0 → Gate A (approved with a prior adjustment) → S2 → S3 → S4 →
Gate B (weight override) → S5 → S6 deep dives → S7 tournament → Gate C (a
preference-feedback loop that moves the preference-adjusted board and provably
not the destination-only one, then approval) → S8 synthesis → DONE, asserting
every artifact validates, the git log carries one commit per stage, gate, and
Gate-C loop, the spend ledger is populated, under the cap, and reconciles
(per-role = per-stage = total, in USD and tokens), and `rerun --stage S3
--angle X` invalidates exactly that subtree before reconverging.
Every pipeline artifact has a strict Pydantic v2 model with YAML/JSON round-trip,
an LLM-facing validation-error formatter, and a generated JSON Schema in
`schemas/`; the versioned agent prompts for stages 0–8 live in `agents/`. The
kernel is live-run hardened: `deeper doctor` preflights the environment, every
run carries a `max_spend_usd` guard the dispatcher enforces before each
invocation, every raw invocation runs under a wall-clock timeout, and every
agent failure path (schema-retry exhaustion, SDK/network error, hung call,
spend cap, plan usage limit) lands in a resumable `PAUSED_ATTENTION` with a
transcript in `logs/` — never a crash. The stages themselves: S0 is an interactive terminal
interview with a confirm-before-write step; S1 runs the parallel cartographer
ensemble plus the §5/S1 saturation rule; Gate A applies all five review actions
to the map on resume; S2 prints the allocation table at the gate exit; S3 runs
one scout per angle in parallel, a card-critic per angle, one revision round, the
redundancy early-stop, and budget reflow onto critic-flagged misses; S4 derives
the rubric from the destination model plus the cards (never preferences) and
Gate B applies weight/criterion edits and the preference-slot weight on resume;
S5 screens every card with uncertainty bands and pure code applies the shortlist
rule with a written reason per option; S6 deep-dives every finalist in analyst
rounds under the design's three-clause stability stopping rule, re-scoring each
round with the S5 screening machinery pointed at the dossier, then an
independent verifier adjudicates every load-bearing claim (plus a seeded 20% of
the rest) — contradictions land in the §6 ledger and trigger exactly one
targeted revision; S7 computes the two scoreboards (destination-only vs
preference-adjusted) and their rank inversions in code, runs prosecutors
(top 3), steelmen (runner-up + every inversion), and the frame-checker in
parallel with the code-computed rubric-sensitivity tables as evidence, then a
judge whose every score change lands in a cause-logged ledger and is applied
by code — any re-divergence proposal is surfaced at Gate C, never
auto-executed. Gate C's four typed actions are live: preference feedback (one
screener dispatch converts reactions to preference-slot adjustments; code
re-scores both scoreboards — free, no new research), evidence challenges (one
scoped verifier task per challenged claim; contradicted claims enter the §6
ledger, scores never move), accept-re-divergence (a mini-loop S1→S6 scoped to
the proposed region on its OWN budget, then S7 reruns over the merged
finalists — 1 per run, §12), and approve — with iterations capped at 3, after
which the gate template only offers approve. S8's synthesist (the only agent
besides the screener permitted preferences) narrates the code-computed
boards, matrix, and sensitivity tables into the seven-component decision
report; a mechanical citation pass then resolves every inline `[[claim-id]]`
annotation against the dossiers (one retry on failure) before code renders
`report/decision-report.md` with claims linked into an appendix index.
`deeper report <run>` prints the path plus a terminal summary: winner, both
scoreboards, the top sensitivity flag, verification pass rates, spend. A full
mock run walks `deeper new` → Gate A → Gate B → the shortlist → seven settled
dossiers → the tournament → Gate C feedback → the decision report in seconds,
offline.

The **first supervised live run** (quick profile, a real vector-store
selection question) exercised the whole kernel: three spend-cap pauses each
resumed cleanly, real SDK failures paused-not-crashed, gates materially
changed the outcome (a 41→12 angle prune at Gate A, a weight swap at Gate B),
and the shortlist's kill list carried specific checkable receipts. Its triage
findings live in [`docs/m1-live-run-notes.md`](docs/m1-live-run-notes.md),
and **every finding is now addressed**: the screener is calibrated and the
shortlist rule (top-k by UCB + a dark-horse margin) concentrates regardless
of band width; billing is pinned to the Claude Code login by default
(`billing: subscription`), never a stray ANTHROPIC_API_KEY; the cartography
prompts map the *adoption space* instead of the engineering design space
(the 41-angle blowup); an infeasible approved map pauses S2 with the fix
named instead of crashing; dispatch failures carry the CLI's own result
detail; failed dispatch attempts are ledgered and recovered schema retries
logged to `logs/retries/`; option ids are globally unique by construction;
S5 batches persist per angle and are never re-paid; `--non-interactive`
makes the stdin shape explicit; the CLI forces UTF-8 output (the cp1252 'Δ'
crash); and plan usage-limit exhaustion is its own pause cause with the
reset time in the message and no auto-resume. The terminal is emit-only:
subagent stderr is captured per attempt (failures persist it to
`logs/stderr/` and carry the tail), SDK loggers write to `logs/sdk.log`,
and stage fan-outs use `gather_strict` (deeper/aio.py) — on first failure
siblings are cancelled and drained so subprocess teardown happens on a
live loop, and one deterministic exception (usage limit and spend cap
outrank per-dispatch deaths) names the pause.

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
| `src/deeper/contradictions.py` | The §6 contradiction ledger's shared append helper (idempotent by entry id) | **built** |
| `src/deeper/sensitivity.py` | S7/S8 scoreboard + rubric-sensitivity math: dual boards, rank inversions, steelman docket, criterion weight-flip deltas, preference-slot sweep — pure deterministic code | **built** |
| `src/deeper/report.py` | S8 report machinery: the mechanical citation pass over `[[claim-id]]` annotations, decision-matrix/appendix table rendering, the top-sensitivity-flag line, markdown assembly — pure deterministic code | **built** |
| `src/deeper/agents_runtime/` | SDK dispatch, mock mode, enforcement hooks, cost accounting | **built** |
| `src/deeper/stages/` | Per-stage logic S0–S8 (`StageBase` protocol + registry); `saturation.py` (S1 rule), `shortlist.py` (S5 rule + screening arithmetic), and `depth.py` (S6 stopping rule + verifier sampling) are pure math | **S0–S8 built** |
| `src/deeper/orchestrator/` | State machine (`engine.py`), gates (`gates.py`), Gate-C loop actions (`gate_c_loops.py`), the re-divergence mini-loop (`redivergence.py`), rerun invalidation (`rerun.py`), `deeper` CLI (`cli.py`) | **built** |
| `src/deeper/eval/` | Design-§10 measurement layer: pure metric math (`metrics.py`), benchmark loading (`benchmarks.py`), the eval-judge dispatch (`judge.py`), the per-run eval runner (`runner.py`), report/compare renders (`report.py`) | **built** |
| `agents/` | Versioned agent prompt files (one per role), stages 0–8 | **built** |
| `src/deeper/promptlab.py` | `deeper-lab` prompt-iteration harness (throwaway quality) | **built** |
| `tests/` | Pytest suite | schema, prompt-library, workspace, config, allocation, sensitivity, report, agents-runtime (incl. adversarial injection fixtures), §11 policy, gate modes, orchestrator, stage (S0/S1/S3/S4/S5/S6/S7/S8, saturation, shortlist, depth, contradiction ledger, Gates A/B, Gate-C loops), end-to-end mock run, live guards, doctor, eval suites (1056 tests) |
| `benchmarks/` | Eval question specs + baseline-answer slots (`baselines/`) | **seeded — 4 specs** |
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
| `dossiers/{option}-rounds.yaml` | `DeepDiveRoundLog` (`DeepDiveRound`) | S6 (pure code) | S6 resume, S8 metrics |
| `dossiers/scores.yaml` | `ScreeningResult` | S6 (code-merged re-scores) | S7 scoreboards, S8 |
| `tournament/{option}-prosecution.md` | `Prosecution` | S7 prosecutor | judge, Gate C, S8 |
| `tournament/{option}-steelman.md` | `Steelman` | S7 steelman | judge, Gate C, S8 |
| `tournament/frame-check.md` | `FrameCheck` (`RedivergenceProposal`) | S7 frame-checker | judge, Gate C |
| `tournament/score-updates.yaml` | `ScoreUpdateLog` | S7 judge | S8 |
| `tournament/scores.yaml` | `ScreeningResult` | S7 (code-applied judge updates) | Gate C, S8 |
| `gates/gate-{a,b,c}.yaml` | `GateADecision` / `GateBDecision` / `GateCDecision` | human (or viewer) | orchestrator resume |
| `gates/gate-c.{n}.yaml` | `GateCDecision` | orchestrator (archives each applied Gate-C loop) | audit trail |
| `gates/challenge-{n}-{option}-{claim}.yaml` | `VerificationReport` | Gate-C evidence challenge (verifier) | human, audit |
| `report/decision-report.yaml` | `DecisionReport` | S8 synthesist (validated artifact) | citation pass, `deeper report` |
| `report/decision-report.md` | rendered view | S8 (code, from the validated artifact + code-computed tables) | the human — the deliverable |
| `sources/` records | `SourceRecord` | any research agent | verifier, audit |
| `ledger/contradictions.md` | `ContradictionLedger` | any detecting stage, via `contradictions.append_contradictions` | verifier, S8 |
| `state.json` | `RunState` (`SpendEntry`) | orchestrator | orchestrator, CLI |
| `benchmarks/<id>.yaml` (repo, not run) | `BenchmarkSpec` (`ReferenceAngle`, `OptionCheck`) | human (seeded from the live-run notes) | `deeper eval --against` |
| (judge reply, not persisted) | `AngleMatchReport` | eval-judge | `eval.metrics.breadth` |
| `eval/eval-report.yaml` (+ `.md` render) | `EvalReport` | `deeper eval` | the human; `eval --compare` |

Notable schema-level invariants (each mirrors a design rule): allocation rows must sum
exactly to the budget; anchored rubric levels must be exactly 1–5 and criterion
weights must sum to 1.0 (the preference slot is weighted separately, per P9); a
screening score must lie inside its uncertainty band; a `gap-found` frame-check must
carry a re-divergence proposal; a `BUDGET-CAPPED` dossier must list its open
questions; a deep-dive round log's rounds must be contiguous from 1 and its
final re-score can exist only after a completed revision; Gate C approval
excludes the loop actions.

## The agent prompt library

Each `agents/*.md` file is a versioned prompt with YAML frontmatter (`role`, `stage`,
`model_class`, `output_schemas`, `inputs`, `research`) and a body in the four-part
contract form from design §5/S3 — OBJECTIVE / OUTPUT FORMAT / TOOL & SOURCE GUIDANCE /
BOUNDARIES. The body's `{{schema}}` placeholder is replaced at dispatch with the
agent's exported JSON Schema(s); agents emit artifacts as fenced yaml blocks behind
`### artifact: <name>` markers. Every research-capable prompt carries the
untrusted-web-content rule (fetched-page instructions are data, never directives), and
only the screener's and synthesist's `inputs` may include `preferences` (the §6
quarantine's exact allowlist) — both enforced by tests.

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
| `screener` | S5 (+ S6 re-scores) | sonnet | `screening-result` |
| `analyst` | S6 | sonnet | `dossier` |
| `verifier` | S6 | sonnet | `verification-report` |
| `prosecutor` | S7 | sonnet | `prosecution` |
| `steelman` | S7 | sonnet | `steelman` |
| `frame-checker` | S7 | opus | `frame-check` |
| `judge` | S7 | opus | `score-update-log` |
| `synthesist` | S8 | opus | `decision-report` |
| `eval-judge` | EVAL (the harness, not a pipeline stage) | haiku | `angle-match-report` |

Design §6 names merger/rubric-builder/judge/frame-checker as Opus-class and
cartographers/scouts/analysts/prosecutors as Sonnet-class; roles it leaves unlisted
are assigned by analogy (interviewer → opus because the destination model anchors the
whole run; card-critic/screener → sonnet; steelman → sonnet, the prosecutor's mirror;
verifier → sonnet, because adjudication is more than §6's Haiku-class citation
checking and the S size class's search budget could not re-fetch a full sample).
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
     │ valid → stage's validate= callback,     │ invalid (schema OR callback)
     │ if any (coherence checks: sampling      ▼
     ▼ assignments, cross-artifact linkage)   re-invoke with the errors AND the
AgentResult                                   previous output as feedback, up to
(validated models,                            caps.max_schema_retries times →
 cost, retries_used)                          then raise AgentOutputInvalid
                                              (orchestrator pauses run, message
                                              carries the true attempt count)
```

A stage passes its coherence check as `run_agent(contract, validate=...)` so a
schema-valid but semantically wrong reply (the M2 live run's verifier dropped
one of its 14 sampled claims) gets corrective feedback naming exactly what is
wrong instead of pausing the run on the first miss. Every per-dispatch
coherence check rides this loop — scout/critic angle assignments (S3,
redivergence), screener chunk and re-score coherence (S5, S6, gate-C
mini-loop and preference feedback), analyst dossier round/sections (S6),
verifier sampling assignments and challenges (S6, gate-C),
prosecutor/steelman/frame-check/judge (S7), and the synthesist's board
coherence + mechanical citation pass (S8). The taxonomy: a check over ONE
agent's reply vs its assignment retries with feedback; a merge-level or
cross-dispatch check (S5's cross-angle id collisions, the merged sub-batch
belt) still raises straight to a pause — firing means a code bug or a
hand-edited workspace, not retryable agent output. Sites that normalize a
reply before checking (dropping foreign options, correcting a stray
angle_id) use pure `normalize_*` functions shared by the callback and the
post-success path, and their notices emit once, for the accepted attempt.

**The quarantine guarantee** (design §6): a `PreToolUse` hook denies any
Read/Grep/Glob whose target *or search root* covers `preferences.yaml` unless the
contract's role is in `{screener, synthesist}` — an allowlist in code, not prompt
goodwill. A second hook fences writes to the contract's declared subtrees (and
hard-denies `state.json`/`config.yaml`/`preferences.yaml` for every agent); a
`PostToolUse` hook caches every WebFetch content-addressed into `sources/` (with a
`SourceRecord` and a `logs/web-audit.jsonl` line) after `sanitize_source_text`
strips tool-call-like, instruction-injection, and hidden-text patterns —
system-reminder spans, fake tool-call tags, role-prefix lines,
ignore-previous-instructions lines, tool-traffic JSON keys, HTML comments
(invisible in any rendered view — the classic hidden-directive carrier), and
invisible Unicode (zero-width/bidi/tag-block characters, removed *first* so a
directive spelled in them cannot reassemble past the visible patterns).
Sanitization protects *re-injection from the cache* into later verifier/analyst
contexts; the fetching agent's own defense is the prompt-level untrusted-web
rule. Adversarial fixture pages under `tests/fixtures/adversarial/` prove each
family arrives inert, and `tests/test_policy_s11.py` pins the whole §11 posture
as executable policy: Bash/Task/Agent stay disallowed, the research-role set is
frozen, no contract in `src/` grants a write path (agents emit artifacts
in-reply; the orchestrator writes files), the preference-reader allowlist is
exactly {screener, synthesist}, and every research prompt carries the
untrusted-web rule — loosening any of these fails the suite. Live dispatch is
additionally fenced by `permission_mode="dontAsk"` + per-role `allowed_tools`
(research roles get WebSearch/WebFetch/Read/Write; no research agent gets Bash
or subagents) + `setting_sources=[]` + `cwd` pinned to the run workspace.

**Billing enforcement** (`RunConfig.billing`, default `subscription`): the
SDK builds each subagent's env as `{**os.environ, **options.env}` — an
override can't *remove* a key — so under subscription billing `_live_options`
sets `ANTHROPIC_API_KEY=""`, which the CLI treats as unset (verified against
the bundled CLI: its init message reports `apiKeySource: "none"`) and falls
back to the stored Claude Code login; a non-empty key would silently take
precedence over that login and meter the API account. A runtime belt backs
the env suspenders: if a subagent's init message ever reports a real
`apiKeySource` under subscription billing, the dispatch raises
`BillingAuthError` (never retried — it's deterministic) and the run pauses.
Under `billing: api` the key passes through untouched and its absence fails
fast at dispatcher construction.

Besides one-shot `run_agent`, the dispatcher exposes `run_interview` — the S0
conversational loop (the design's single conversational agent). It shares the same
semaphore, ledger, and schema-retry discipline; only the turn protocol differs: a
reply without artifact markers is a question routed to the terminal, and the
question budget is enforced by code (an over-budget question is fed back as a
violation, like a schema failure).

**Mock mode** (`config.yaml mode: mock`, the default) substitutes only the network
call: `MockDispatcher` renders canned fixtures from
`tests/fixtures/mock_agents/<role>/<schema>[.<context>].yaml` (a coherent
senior-project scenario covering all 20 roles, including per-round analyst
dossiers, deep-dive re-scores, verifier reports that exercise all three S6
termination paths, and a tournament with an engineered rank inversion and
frame-check gap) into the same marker+fenced-yaml
text a live agent emits, then flows through the identical parse/validate/retry/ledger
path — the whole pipeline runs offline with zero SDK imports (asserted by a
fresh-interpreter test). `scripted_responses` lets tests inject invalid-then-valid
sequences to exercise the retry loop.

**Spend accounting**: every attempt lands a `SpendEntry` (stage, role, angle/option
context, usd, tokens) in `state.json` immediately; `SpendLedger.spend_so_far(stage)`
is what gates report and the orchestrator's cap checks read. Every raw attempt
also lands one structured line in `logs/agents.jsonl` — timestamp, stage, role,
context, a 16-hex contract hash (grouping retries of the same contract), attempt
number, outcome (`ok` / `invalid` / `question` / `dispatch-error`), usd, tokens,
duration, session id — the machine-greppable twin of the transcripts. Per-run
logs (`agents.jsonl`, `web-audit.jsonl`, `sdk.log`) rotate at 5MB × 3 backups
(`workspace.append_log_line`), so a pathological run cannot grow one file
without bound. A dispatch attempt
that dies in flight lands a zero-cost marker entry (`failed: <error>`) — the
tokens it consumed are unknowable, but the audit trail shows the attempt.
Retry counts persist in `RunState.retry_counts` keyed `stage:role:context`,
and every schema-invalid attempt's raw output + validation errors are saved to
`logs/retries/` even when the retry then succeeds — the §10 prompt-iteration
evidence. The USD figures are the SDK's API-equivalent estimates: under the
default subscription billing they meter plan usage (and still drive the spend
guard), not dollars charged.

**The spend guard and the no-crash rule**: every raw invocation passes through one
guarded chokepoint. Before dispatch, the ledger total is checked against
`config.max_spend_usd` (quick 30 / standard 100 / exhaustive 200 by default —
calibrated against the M1 ledger, see "Cost expectations"; `--max-spend-usd`
overrides at `deeper new`, and `deeper resume --max-spend-usd` rewrites it,
committed) — crossing it raises `SpendCapExceeded`. Each `_invoke` runs under
`config.dispatch_timeout_s` (default 20 min), so a hung SDK call (observed
live: 50 minutes) becomes an ordinary transient failure. A transient
infrastructure exception (SDK stream error, network hiccup, timeout) is
retried on a short backoff schedule (`DISPATCH_RETRY_BACKOFF_S`, 2s/8s +
jitter) before being wrapped as `AgentDispatchFailed` with the cause attached
— and a live failure is first enriched with the CLI's own result
subtype/text (`LiveDispatchError`), because the SDK's wrapper alone can be
actively misleading (M1's `error result: success` was output-token
exhaustion). Two failure classes are deterministic and skip the backoff:
`BillingAuthError`, and `UsageLimitReached` — the Claude plan's session
limit, detected whether it surfaces as an SDK exception or as a short
limit-notice reply (which would otherwise burn the schema-retry loop as a
bogus parse failure). All of them, like `AgentOutputInvalid`, pause the run
as `PAUSED_ATTENTION` with a transcript in `logs/` — a live run always ends
in a resumable pause, never a crash or lost work. The usage-limit pause
states the reset time when the notice carries one and instructs `deeper
resume` at that time; resuming is always an explicit human action, never an
automatic wait. Live subagents also get their size-class output budget
enforced, not just stated: `_live_options` passes `max_output_tokens` as
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` into the subagent's environment (the CLI's
default 32k ceiling silently killed long artifact replies before this), and
raises the SDK's per-message stdout buffer to `MAX_SDK_MESSAGE_BYTES` (32MB —
the default 1MB kills a dispatch whenever one streamed WebFetch tool result
exceeds it, near-deterministically when the agent re-walks the same big
source on retry).

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
The live profiles ship M-class output ceilings of 32k: the M2 live run showed even a
sub-batched (≤10-card) screener reply can overflow 16k, and the ceiling is a runaway
guard, not a target — output tokens cost nothing unless produced. (Existing runs keep
the table snapshotted in their own `config.yaml`; raising a mid-flight run's ceiling
is a one-line edit there.)
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
              ▲      │ rerun_hint                                     ▲       │ feedback /
              └──────┘                                                │       │ challenges (≤3 loops)
                                                                      └───────┤
                                                                              │ re-divergence
                                                          (mini-loop S1→S6, then S7 rerun; 1/run)
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
in `STAGES`. The built stages:

- **S0 Intake** drives the interviewer through the dispatch layer's
  `run_interview` loop — the system's only multi-turn dispatch. Each turn
  re-invokes the agent with the transcript; a reply without artifact markers is a
  question printed to the terminal (answer captured via the CLI's `ask_user`
  channel — a multi-line paste is one answer: lines already buffered on stdin
  are drained into it, never leaked as auto-answers to later questions), a
  reply with markers is the final emission, validated with the same
  schema-retry discipline as any dispatch. The question budget
  (`caps.max_interview_questions`, default 8) is enforced by code: an over-budget
  question never reaches the user — it is fed back as a violation. The stage
  prints a brief summary and requires explicit confirmation before writing
  `brief.md` / `destination.md` / `preferences.yaml`; declining discards
  everything and leaves the run resumable at S0. Non-interactive sessions (no
  tty; every mock test walk) skip questions and confirmation — the interviewer
  is told every turn is final. In live mode the interviewer may WebSearch to
  verify destination-facts.
- **S1 Cartography** dispatches the profile's initial ensemble in parallel
  (one `asyncio.gather` through the dispatcher semaphore), then the merger, then
  applies the **saturation rule** (design §5/S1; pure math in
  `stages/saturation.py`): marginal novelty per cartographer = new distinct
  merged angles / its total raw angles, computed from the merger's dedup map;
  while the trailing-window mean (window 2) stays ≥ 0.2, it spawns up to 2 more
  passes of the heuristics that produced the most novel angles (raw output at
  `angles/raw/{heuristic}-{n}.yaml`, prompt lists the already-mapped angles),
  re-merges, and re-measures — hard cap 8 invocations. All thresholds come from
  `HardCaps`, never an LLM. `is_complete` replays the deterministic expansion
  plan against the files on disk, so a crash mid-expansion resumes mid-plan.
- **S2 Allocation** runs the budget formula over the post-Gate-A map and prints
  the full table (angle, prior, units, share) at the gate exit — the user sees
  where budget will go before S3 spends any of it. An approved map the formula
  cannot satisfy (angles × floor > B, or angles × cap < B) pauses cleanly with
  the exact fix named — remove angles or raise `total_budget_units` — and Gate
  A already warns at apply time when a decision produces such a map.
- **S3 Scouting** runs one pipeline per allocated angle, angles in parallel:
  scout (contract carries "you have ~N units ≈ target 2N option cards" from
  allocation.yaml) → card-critic → one revision round against the critique when
  it reports completeness/distinctness issues. If the critic's `redundancy_pct`
  exceeds `caps.scout_redundancy_stop_pct` (40), the angle stops early — no
  revision — and its unused units return to a global pool. After all angles
  finish, `reflow()` redistributes the pool over angles whose critics named
  `missed_options` (redundancy-stopped angles excluded), a top-up scout per
  target angle is dispatched with those specific misses as its task, and new
  cards merge into `options/{angle}/cards.yaml`. Every cards.yaml write passes
  through one chokepoint that keeps card ids **globally unique**: a cross-angle
  collision (two scouts can genuinely card the same thing — M1's `sqlite-vec`)
  is auto-suffixed `id-{angle}` deterministically at write time, because
  option ids key scores, dossiers, and the tournament. Within an angle, critique.md is
  written after any revision (its presence marks the angle settled), and
  `options/reflow.yaml` is written after the top-ups merge — so `is_complete`
  can replay the deterministic pool computation against the files on disk.
- **S4 Rubric** dispatches the rubric-builder (Opus-class) with the destination
  model, every angle's cards, and the coverage report's rubric-weight strategic
  notes — never preferences (structurally: the file is not in the contract, and
  the quarantine hook denies tool access). Code then overwrites the preference
  slot with `preference_slot_default_weight` (0.2) — the weight is a process
  knob, not agent content — and renders `rubric-rationale.md` from the
  validated rubric.
- **S5 Screening** dispatches one screener batch per angle in parallel
  (mirroring S3's fan-out — the M1 live run proved a full-map single call
  exceeds even a 64k output-token ceiling), each with the Gate-B-approved
  rubric, that angle's cards, and — uniquely in the pipeline —
  `preferences.yaml`. An angle with more than `screener_batch_max_cards` (10)
  cards is itself split into balanced sub-batches — per-angle batching bounds
  the *number* of calls, not one call's reply, and the M2 live run overflowed
  the M class's 16k output ceiling on a single near-cap ~20-card angle — each
  sub-batch persisted as a part file the moment it passes (a crash mid-angle
  resumes from the completed parts; the deterministic chunker recomputes the
  same boundaries), then merged in code into the angle's settled batch file. Each batch is integrity-checked against its own angle's
  cards (every card scored on every criterion, ids resolvable; over-scoped
  options dropped; incoherence pauses the run) and **persisted the moment it
  passes** at `screening/batches/{angle}.yaml` — a later failure or crash
  never re-pays a paid batch, and re-entry re-dispatches only batches that
  are missing or no longer cohere with the current rubric and cards. Code
  then merges the batches (a
  cross-angle duplicate option id pauses with instructions — hand-renaming
  one card id is the fix, not a re-scout), the weighted
  aggregates are recomputed in code from the rubric weights, and then pure code
  (`stages/shortlist.py`) applies the shortlist rule: a confirmed kill-risk
  eliminates regardless of score; survivors are ranked by their weighted
  **upper confidence bound** and the top `shortlist_size` advance, plus any
  option whose UCB is within `shortlist_dark_horse_margin` (0.25) of the
  k-th finalist's — dark horses with wide bands advance by design, but band
  inflation can no longer advance the whole field. Two absolute rails bound
  the relative rule (nothing advances below `shortlist_threshold`; nothing
  past `caps.max_finalists`), at most `caps.max_finalists_per_angle` (3)
  finalists come from one angle, and if the top-k finalists span ≤ 2 angles,
  the highest-UCB option from each unrepresented top-half angle (by prior) is
  added. Every option gets a one-paragraph advanced/cut reason in
  `screening/shortlist.md` — cuts are auditable.
- **S6 Deep dives** runs one analyst per finalist, all finalists in parallel,
  each in a round loop the orchestrator (never the analyst) terminates:

  ```
  baseline = the option's S5 record            (round 0 for the delta)
  ROUND r = 1, 2, … ≤ deep_dive_unit_cap       (1 round = 1 unit)
    analyst researches → dossiers/{opt}.md     (claims: confidence+tier+load_bearing;
    │                                           ≥1 disconfirming search per criterion)
    screener re-scores from the DOSSIER        (S5 machinery: integrity check +
    │                                           code-recomputed aggregates; the
    │                                           angle_id is stated in the task and
    │                                           corrected in code if mis-echoed —
    │                                           the dossier carries no angle)
    Δ = |weighted_point − last round's|; re-score diff cross-checks load_bearing
    │  tags (a ≥1-point criterion move promotes its section's claims, tagged or not)
    STOP if Δ < 0.15 AND no low-confidence load-bearing claim  → CONVERGED
    else if r = cap → dossier stamped BUDGET-CAPPED, open questions listed
  VERIFY: independent verifier re-checks ALL load-bearing claims + seeded 20% of
    the rest → verified/unsupported/contradicted with evidence quotes
    contradicted → ledger/contradictions.md + exactly ONE targeted analyst
    revision (those claims only) → final re-score
  ```

  Every step lands in the code-owned round log (`dossiers/{option}-rounds.yaml`),
  so a resume never re-dispatches a recorded round, and the finalists' final
  re-scores merge into `dossiers/scores.yaml` — the post-deep-dive scoreboard S7
  ranks from. **What BUDGET-CAPPED means for the report:** unfinished depth is
  visible, never silent — the stamp travels on the dossier itself with the open
  questions the budget could not close, S8's residual-uncertainty register lists
  them, and the count of capped dossiers is one of the design-§10 depth metrics.
  A capped option is not penalized on the merits; its score simply carries wider
  honest uncertainty into the tournament.
- **S7 Tournament** starts with pure code (`sensitivity.py`) over the
  post-deep-dive scores: the **destination-only** scoreboard (preference-slot
  weight forced to 0) and the **preference-adjusted** one (slot weight as set
  at Gate B), their **rank inversions** (a pair whose strict order flips
  between the boards — a tie refined by the other board is not an inversion),
  and the rubric-sensitivity tables (per-criterion weight delta that ties
  ranks 1–2 under Gate B's own pin-and-rescale semantics, plus the
  preference-slot sweep 0→40%). Inversions are the priority docket — exactly
  where preference-overfitting would hide. Then three adversarial roles run in
  parallel: a **prosecutor** per top-3 finalist (strongest good-faith case
  against, from dossier evidence + at most `caps.tournament_new_searches`
  targeted searches, with the mandatory most-likely-regret path), a
  **steelman** per docket entry (the runner-up plus every inversion-demoted
  option — never the current winner), and the **frame-checker** (Opus-class),
  which sees the original brief, the map with Gate A's removals log, every
  critique's missed options against an index of what was actually scouted,
  the reframe strategic notes, the final ranking, and the code-computed
  sensitivity tables, and answers one question: is there a plausible answer
  to the brief this map could not have produced? Its three checks are
  consequential Gate-A removals, critiqued-but-never-scouted missed options,
  and rubric fragility; a credible gap yields a **re-divergence proposal**
  that is persisted and surfaced at Gate C — never auto-executed. Finally the
  **judge** (Opus-class) updates criterion scores only where tournament
  material is decisive; code verifies every update (real option, rubric
  criterion — never the preference slot — and an `old_score` matching the
  ledger), applies it (widening a band the new score falls outside), logs it
  with its cause in `tournament/score-updates.yaml`, and writes the
  post-tournament scoreboard to `tournament/scores.yaml` (written last — it
  marks the stage settled). Each adversarial artifact is written the moment
  its dispatch completes, so a resume re-dispatches only what is missing.
- **S8 Synthesis** starts with pure code over `tournament/scores.yaml`: both
  scoreboards, the criterion flip deltas, the 0→40% preference sweep, the
  finalists×criteria decision matrix with confidence bands, per-finalist
  verifier pass rates, and spend by stage. The **synthesist** (Opus-class, the
  only agent besides the screener the quarantine hook allowlists for
  preferences) narrates all of it — plus the dossiers, prosecutions, steelmen,
  frame-check, shortlist, contradiction ledger, and the coverage report's
  execution-kind strategic notes — into the validated `decision-report`
  artifact carrying the design's seven components: recommendation with
  decisive reasons, matrix narration, sensitivity narration (its prompt says
  verbatim: if the winner is fragile to plausible weight changes, say so
  prominently), the dissent (the best surviving prosecution argument, marked
  explicitly when unrebutted; code checks it comes from the winner's
  prosecution), the residual-uncertainty register (open questions,
  BUDGET-CAPPED areas, revisit triggers — and, code-rendered beneath the
  narration, every still-`open` contradiction-ledger entry with both statements
  and their artifacts, per §6's "unresolved contradictions surface in the
  report"; the full ledger, adjudicated entries included, renders in the
  appendix), next actions (execution strategic
  notes folded in), and appendix commentary. Code cross-checks the winner
  against the adjusted board's rank 1, then runs the **mechanical citation
  pass** (`src/deeper/report.py`, code not LLM): every inline `[[claim-id]]`
  (or `[[option-id:claim-id]]` where a bare id is ambiguous across dossiers)
  must resolve to a dossier claim, and the recommendation must carry at least
  one. Both checks ride the dispatcher's feedback retry loop
  (`caps.max_schema_retries`); exhaustion pauses the run with the exact
  unresolvable-annotation list. On success code
  renders `report/decision-report.md`: seven numbered sections, the
  code-computed tables embedded verbatim, every annotation linked to an
  appendix claims index carrying each claim's text, source, and tier, plus
  the angle map, allocation table, cut-option audit trail (screening
  decisions + Gate-A removals), verification pass rates, and spend by stage.
  `report/decision-report.yaml` is written last — the settled marker.

**Gates are file-edit pause states.** Entering a gate writes a commented template
(`gates/gate-{a,b,c}.yaml`) whose body already parses as a *valid but undecided*
decision (`approved: false`), prints what to review and edit, and exits. The template
is never overwritten once it exists — a half-edited decision survives resume. On
`deeper resume`, the file is validated: YAML/schema errors or a still-undecided body
re-pause with the exact problem; `approved: true` advances (gate marked in
`state.json`, one commit). **Gate A's five actions are applied on resume**, before
the state transition, so their writes ride the gate commit: removals drop the angle
and its dedup entries (reason echoed to the terminal and preserved in the kept
decision file + map diff — the S7 frame-checker re-examines them); additions enter
the map as `human`-provenance angles with prior 0.5 (adjustable in the same
decision via the name's kebab-case slug), which queues them for allocation and a
scout; prior adjustments overwrite priors; referential mistakes (unknown angle id,
colliding addition) re-pause the gate with every problem listed and nothing
written. **Gate B's edits are applied the same way**: its template puts
`preference_slot_weight` at the very top — the one number that says how much your
tastes may bend the destination-optimal answer (0–0.4, the report's sensitivity
sweep range) — and on approval the slot weight is always written into
rubric.yaml, criterion edits are full replacements, and weight overrides pin the
named criteria while untouched ones rescale proportionally so criterion weights
still sum to 1.0; referential problems re-pause with nothing written.
`rerun_hint` records the hint at `gates/gate-a-hint.txt` (where S1
invalidation can't delete it), loops back through S1 via the same invalidation
machinery as `rerun`, and the S1 pass injects it into every cartographer prompt
then consumes the file — one pass, exactly. **Gate C is the loop gate** (design
§5): `approved: true` proceeds to S8, and its three loop actions are bounded,
typed loops rather than open-ended chat — each submission is one Gate-C
iteration (counted in `state.json`, capped at `caps.max_gate_c_loops` = 3 per
§12, after which the engine writes an approve-only template with a note
explaining why). A loop decision is validated whole and up-front (Gate A
style): an unknown option/claim id, a second re-divergence, or acceptance with
no proposal on file refuses the entire decision, consumes no iteration, and
dispatches nothing. Applied loops archive the decision to `gates/gate-c.N.yaml`
and commit as `gate-c loop N`. *Preference feedback* dispatches the screener
in its Gate-C mode; code copies ONLY the returned preference slots (criterion
drift is discarded — P9 by construction) into both `tournament/scores.yaml`
and `dossiers/scores.yaml`, recomputes the aggregates, and prints both
scoreboards — the destination-only board cannot move. *Evidence challenges*
each fire one scoped verifier contract carrying the human's challenge
verbatim; the verdict lands at `gates/challenge-N-{option}-{claim}.yaml`, a
contradicted claim enters the contradiction ledger, and scores never move — a
re-score is the human's next loop, taken deliberately. *Accept re-divergence*
runs the mini-loop (`orchestrator/redivergence.py`) on the proposal's own
`estimated_cost_units` — 1 unit buys one targeted scout pass over the proposed
region (a `new-angle` proposal first enters the map with human provenance),
the remainder caps the deep dive via a scoped config copy of
`deep_dive_unit_cap` — then one screener batch scores the new cards and the
threshold rule seats at most one new finalist (the §12 `max_finalists` cap
still binds; every new option's advance/cut reason is appended to
`shortlist.md`), the real S6 machinery dives the champion, its final re-score
merges into `dossiers/scores.yaml`, and S7 is invalidated so the tournament
reruns over the merged finalists before Gate C reopens. **Every agent failure is a pause, not a crash**: schema-retry
exhaustion (`AgentOutputInvalid`), a dispatch/SDK failure or timeout
(`AgentDispatchFailed`), the spend guard (`SpendCapExceeded`), or the Claude
plan's usage limit (`UsageLimitReached`) all land the
run in `PAUSED_ATTENTION` — the failure transcript (validation errors + raw
output, or the traceback, or the spend table, or the limit notice) is saved to
`logs/attention-<timestamp>-<stage>-<role>.md` and rides the pause commit;
`deeper resume` re-enters the stage after you fix the cause (for a spend-cap
pause: `deeper resume <run> --max-spend-usd <higher>`; for a usage-limit
pause: just resume at the reset time stated in the message — never
automatic).

**Gate fatigue has a config dial** (design §11): `config.yaml` may set
`gate_modes: {gate-b: notify}` per gate. A `notify` gate never pauses — when
the machine reaches it with the template still untouched, the engine writes the
gate's *default* decision into the gate file (marked auto-approved, riding the
normal gate commit), applies it through the identical interpret/apply path
(Gate B still writes the profile-default preference-slot weight into the
rubric), and prints a prominent banner naming what was auto-approved, the
after-the-fact review paths, and spend so far. A decision file the human
already edited — any real decision — always wins over the auto-approval, and
every gate defaults to the hard `gate` mode; demote only a gate you
consistently rubber-stamp (Gate B is §11's candidate).

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
offline against fixtures). What a run looks like, from `deeper new` to the
allocation table:

```bash
deeper new "which vector database should we adopt" --profile quick
#   1. creates runs/<date>-<goal-slug> (a git repo; every transition is a commit)
#   2. S0 interviews you in the terminal — the agent's questions stream in, you
#      answer inline, capped at 8 questions (--live lets it also web-verify
#      destination-facts). It then prints a brief summary and asks you to CONFIRM
#      before brief.md / destination.md / preferences.yaml are written. In a
#      non-interactive session (pipe, mock test) it skips straight to artifacts.
#   3. S1 dispatches the cartographer ensemble in parallel, merges, and prints
#      the saturation math per cartographer:
#        S1 saturation: first-principles novelty 1.00 (6/6 new)
#        S1 saturation: contrarian novelty 0.33 (1/3 new)
#        S1 saturation: saturated (trailing-2 mean novelty 0.17 < 0.2)
#      (had novelty stayed >= 0.2 it would spawn up to 2 extra passes, cap 8)
#   4. pauses at Gate A telling you exactly what to review and which file to edit

deeper status <run>          # node, gate statuses, spend by stage, pending-gate hint
deeper status <run> --spend  # + the stage x agent spend matrix (usd, attempt counts)

# edit runs/<...>/gates/gate-a.yaml — approve, optionally with actions:
#   approved: true
#   prior_adjustments: [{angle_id: evaluation-science, new_prior: 0.9}]
# (other actions: added_angles [{name, note}] queues a human-added angle for
#  scouting; removed_angles [{angle_id, reason}] drops one, reason logged)

deeper resume <run>
#   applies your edits to angles/map.yaml (echoing each action), S2 prints the
#   allocation table before S3 spends anything:
#     gate-a: prior of 'evaluation-science' 0.7 -> 0.9
#     S2: allocation — 16 units over 8 angles (floor 1, gamma 1.0, cap 25%):
#       angle                         prior  units  share
#       ...
#   then S3 scouts every angle in parallel, critiques, revises, and reflows:
#     S3: interpretability-research scout done (4 cards)
#     S3: interpretability-research critique found issues — one scout revision round
#     S3: training-efficiency redundancy 55% > 40% — early stop, returning 1 unit(s)…
#     S3: reflow — 2 returned unit(s) over 2 angle(s) whose critics named missed
#         options: interpretability-research +1, evaluation-science +1
#   S4 writes rubric.yaml + rubric-rationale.md and pauses at Gate B

# edit runs/<...>/gates/gate-b.yaml — the preference-slot weight sits at the top:
#   preference_slot_weight: 0.2
#   approved: true
#   weight_overrides: {letter-strength: 0.35}   # untouched criteria rescale to sum 1.0

deeper resume <run>
#   applies the rubric edits, then S5 screens every card and pure code applies
#   the shortlist rule — every advance/cut gets a written reason:
#     S5 shortlist (top-3 by UCB + dark-horse margin 0.25, floor 3.5): 7 finalists, 15 cut
#     S5:   finalist cot-hurts-small-models        <- dark horse: wide band, within margin
#     S5:   finalist model-collapse-dynamics [breadth-guardrail add]
#     S5:   cut (kill-risk-confirmed): 1           <- the top scorer, killed anyway
#     S5:   cut (below-cutoff): 1                  <- above the floor, not near the top-k
#   then S6 deep-dives the finalists in parallel — every option ends in one of
#   three visible ways:
#     S6: sae-feature-atlas round 2 re-score 4.5 (delta 0)
#     S6: sae-feature-atlas converged after 2 round(s) — delta 0 < 0.15 and no
#         low-confidence load-bearing claims
#     S6: contamination-robust-benchmark BUDGET-CAPPED after 2 round(s) — 2 open
#         question(s) listed in the dossier
#     S6: backdoor-probe-study contradicted claim(s) ['c-back-transfer'] — one
#         targeted analyst revision
#     S6: backdoor-probe-study final re-score after revision: 3.2 (was 3.6)
#   then S7 computes both scoreboards in code and runs the tournament:
#     S7: rank inversion — destination-only ranks 'contamination-robust-benchmark'
#         above 'sae-feature-atlas'; the preference-adjusted board reverses them
#     S7: tournament — 3 prosecution(s) (top-3 by preference-adjusted rank: ...),
#         1 steelman(s) (docket: contamination-robust-benchmark [rank-inversion]),
#         frame-check
#     S7: frame-check found a credible gap — re-divergence proposal [scout-task]
#         (target angle: applied-domain-collaboration), estimated cost 2 unit(s): ...
#     S7: the proposal is NOT auto-executed — review tournament/frame-check.md and
#         approve or decline it at Gate C
#     S7: judge — sae-feature-atlas 'momentum-by-deadline' 4.5 -> 4.25 (cause: ...)
#   and pauses at Gate C (the contender review)

# edit runs/<...>/gates/gate-c.yaml — approve, or submit a typed loop first:
#   preference_feedback:
#     - option_id: sae-feature-atlas
#       reaction: "the ops burden bothers me more than I expected"
#       direction: negative
deeper resume <run>
#   the screener converts the reactions to preference-slot adjustments and CODE
#   re-scores both scoreboards (free — no new research), then the gate reopens:
#     gate-c: preference slot of 'sae-feature-atlas' 4.5 -> 4.4
#     gate-c: code re-scored both scoreboards (the destination-only board cannot
#             move — the slot weighs 0 there): ...
#     gate-c: loop 1 of 3 applied (1 preference reaction(s); decision archived to
#             gates/gate-c.1.yaml) — review the updated artifacts, then decide again
#   (evidence_challenges fire scoped verifier tasks; accept_redivergence: true
#    runs the mini-loop on the proposal's own budget, then S7 reruns)

# edit runs/<...>/gates/gate-c.yaml — approved: true proceeds to S8
deeper resume <run>
#   S8 synthesizes the report and the run completes:
#     S8: synthesist drafting the decision report — adjusted-board winner '...'
#     S8: decision report written — report/decision-report.md (winner
#         'sae-feature-atlas', dissent UNREBUTTED; citation pass clean over 28
#         indexed claim ids)
#     run <id> is complete — see report/decision-report.md

deeper rerun <run> --stage S1            # invalidate S1 + downstream, rewalk
deeper rerun <run> --stage S3 --angle x  # scoped to one angle's scout outputs
deeper eval <run> --against <benchmark>  # design-§10 property metrics (below)
deeper report <run>
#   the report's path + a terminal summary: winner (first decisive sentence,
#   with a red DISSENT UNREBUTTED marker when it stands), both scoreboards
#   with ranks, the top sensitivity flag (e.g. "FRAGILE: the winner depends on
#   the preference-slot weight — ... between slot weights 0.1 and 0.15"),
#   per-finalist verification pass rates, and total agent spend
```

Setting `rerun_hint: "<hint>"` instead of approving loops cartography once with the
hint injected into every cartographer prompt. `<run>` is a path or a name under
`runs/`. Every command is safe to repeat: pauses exit 0 with instructions,
`status`/`report` are read-only, and re-entering a run never re-executes completed
work.

## Running your first live run

Mock mode is the default everywhere; live dispatch is opt-in per run.

**Billing: subscription by default.** Live runs authorize through the Claude
Code CLI's stored login and meter your Claude plan — never a metered
`ANTHROPIC_API_KEY`, even if one sits in your environment (the dispatcher
overrides it to an empty string in every subagent's env, which the CLI treats
as unset, and refuses to dispatch if a subagent reports it authenticated any
other way). To bill an API account instead, set `billing: api` in the run's
`config.yaml`; that mode requires `ANTHROPIC_API_KEY` and fails fast without
it. One consequence to know: the spend ledger's USD figures are the SDK's
**API-equivalent estimates** — under subscription billing they meter plan
usage (and drive the spend guard), they are not dollars charged.

Before the first live run:

```bash
deeper doctor
```

checks the six things a live run needs: auth (a Claude Code CLI login for the
default subscription billing — a present `ANTHROPIC_API_KEY` is noted as
ignored unless the run sets `billing: api`; warning only, since a macOS
Keychain login isn't visible as a file), `claude-agent-sdk` importable (its
version is printed), the shipped config profiles validating, every `agents/*.md`
prompt parsing (frontmatter, `{{schema}}` placeholder, declared schemas known and
exported), `schemas/` exports fresh, and the enforcement hooks actually denying:
a dummy scout contract attempts a forbidden `preferences.yaml` read and a
`state.json` write through the same gate functions the SDK hooks call — and,
when the SDK is importable, through the registered `PreToolUse` callbacks
themselves — expecting denial each time. It exits 1 on genuine failures only.

Then start small — the `quick` profile is sized as a sanity pass (3 cartographers,
floor 1, shortlist 3, budget B=16, cartography capped at one expansion pass)
and carries a $30 spend guard by default (see "Cost expectations" below):

```bash
deeper new "your mid-size question" --profile quick --live
```

`--max-spend-usd 10` tightens the guard at creation; `--non-interactive` skips
the S0 interview and confirmations explicitly (useful for scripted runs — on
Windows, piping from `NUL` still *looks* interactive). The run behaves exactly
like the mock walkthrough above — S0 interviews you in the terminal (live
agents may also web-search), gates pause for your file edits — with three
live-specific rails:

- **The spend guard.** The dispatcher checks the ledger before every invocation;
  crossing `max_spend_usd` pauses the run with the spend table saved to `logs/`.
  Nothing is lost — raise the cap and continue:
  `deeper resume <run> --max-spend-usd 40`. In-flight calls still land in the
  ledger after the trip, so expect overshoot of roughly concurrency ×
  per-call cost.
- **No crash on agent failure.** Schema-retry exhaustion, SDK/network errors,
  hung calls (per-invocation timeout, default 20 min), and
  the spend cap all land in `PAUSED_ATTENTION` with a transcript at
  `logs/attention-<timestamp>-<stage>-<role>.md`; `deeper status <run>` shows
  where it stopped, and `deeper resume <run>` re-enters the stage once you've
  fixed the cause.
- **The plan usage limit is its own pause.** Under subscription billing a long
  run can exhaust the Claude plan's session limit mid-stage; the run pauses
  with a message that says exactly that, includes the reset time when the
  CLI's notice carries one, and tells you to `deeper resume <run>` at that
  time. It never burns backoff or schema retries rediscovering the limit, and
  it never auto-resumes — resuming is your explicit action, like every pause.

## When things go wrong

Everything below starts the same way: `deeper status <run>` (add `--spend` for
the stage×agent cost matrix) to see where the run is, then the newest
`logs/attention-*.md` for the failure transcript. A run can only be in one of
four states — running, gate-pending, `PAUSED_ATTENTION`, done — and a pause is
always resumable: completed work is on disk, validated, and skipped on
re-entry.

**Triage for a `PAUSED_ATTENTION` run.** The transcript's first line names the
stage, role, and cause; the pause commit (`git log -1` in the run) repeats it.
By cause:

- *Schema retries exhausted* (`agent 'X' output invalid after N time(s)`): the
  transcript carries the last validation errors and raw output; the earlier
  invalid attempts are in `logs/retries/`. Usually a prompt or schema defect —
  fix `agents/<role>.md` (or the fixture, in mock), then `deeper resume`.
  The retry pattern across runs is what `deeper eval`'s quality metric trends.
- *Dispatch failed*: an SDK/network/CLI failure that survived the backoff
  schedule. The transcript has the traceback, enriched with the CLI's own
  result detail and the stderr tail (full capture in `logs/stderr/`);
  `logs/sdk.log` has the SDK's own logging. Transient causes (network blips,
  a hung call that hit the 20-minute timeout) need nothing but
  `deeper resume`; a deterministic cause (a broken CLI install) needs the fix
  first.
- *Spend cap crossed*: not an error — the guard did its job. The transcript
  shows spend by stage; `deeper resume <run> --max-spend-usd <higher>`
  continues exactly where it stopped, or accept the partial run.
- *Plan usage limit reached*: also not an error. Resume at the reset time the
  message states (never automatic). Nothing was lost.
- *Billing mismatch* (`BillingAuthError`): the run's `billing:` setting and
  the actual auth path disagree — fix the config (or log in / set the key)
  before resuming; this one is never retried because it would meter the wrong
  account.

**Rerun surgery.** When an *artifact* is bad rather than a dispatch —
a scout misread an angle, a rubric criterion is nonsense, you edited a prompt
and want its stage re-run — don't hand-edit downstream files:
`deeper rerun <run> --stage S3 --angle <id>` deletes exactly that subtree plus
everything downstream (one commit, so `git revert` undoes the surgery), moves
the run pointer back, and rewalks. Spend entries are never touched — the
ledger stays a complete audit trail. Scoping rules worth knowing: an
angle-scoped S3 rerun keeps `options/reflow.yaml` and replays the persisted
top-up decision for the re-scouted angle; any rerun also invalidates `eval/`
(a stale eval must never look like evidence); gate decision files downstream
of the target are deleted with their stages, but Gate-C iteration counters
never reset (the §12 cap is per run).

**Resuming mid-S6** (the longest stage — a crash or pause here looks scariest
and is the most mechanical to resume). Per finalist, S6's progress is the
code-owned round log `dossiers/<option>-rounds.yaml`: every completed analyst
round, re-score, delta, the verification record, and whether the one targeted
revision ran. `deeper resume` replays that log against the files on disk and
re-dispatches only what is missing — recorded rounds are never re-paid, other
finalists' settled dossiers are untouched, and a finalist that already
converged (or hit BUDGET-CAPPED) is skipped entirely. The same holds inside
S5 (paid screener batches persist at `screening/batches/`) and S7 (each
adversarial artifact is written the moment its dispatch completes). If a
dossier looks *wrong* rather than incomplete, that's rerun surgery instead:
`deeper rerun <run> --stage S6` rebuilds every dossier from the shortlist.

## Cost expectations

One supervised live quick run has been measured (the M1 vector-store run,
before the hardening fixes); the ledger's stage split and the fixes' expected
effect are the basis for these numbers. USD figures are the SDK's
API-equivalent estimates — under the default subscription billing they meter
plan usage, not dollars charged.

| Profile | Default cap | Measured | Expected per run (post-fixes) |
|---|---|---|---|
| quick | $30 | **$65.20** total — S0 $0.75, S1 $12.34, S3 $21.99, S4 $1.32, S5 $20.06 | **~$20–25** |
| standard | $100 | not yet run | ~$60–90 (extrapolated: 2.5× budget units, larger size classes) |
| exhaustive | $200 | not yet run | ~$120–180 (extrapolated) |

The measured $65.20 carried roughly $20 of waste the hardening pass removed:
the 41-angle cartography blowup (S1 ran to the 8-invocation cap and the merger
re-read every raw report each pass — fixed by the adoption-space prompts plus
quick's one-expansion cap) and ~$12 of re-paid screening batches (fixed by
per-angle batch persistence), on top of unledgered failed dispatches (now
zero-cost marker entries). S6–S8 costs are not yet measured live — the run
predates them; expect the quick estimate to shift after the first full live
run and this table to be updated from its ledger. The caps are runaway
guards, not targets: a spend-cap pause is cheap (resume with a higher cap
continues exactly where it stopped).

## Evaluation (design §10 — the measurement layer)

You can't tune breadth/quality/depth knobs without measurement, so every
prompt or knob change is judged by `deeper eval`, never by vibes (P10).

**Running an eval:**

```bash
deeper eval <run>                                  # the four un-judged metrics
deeper eval <run> --against probe-space-mapping    # + breadth vs the reference union
deeper eval <run> --against probe-space-mapping --compare-baseline
deeper eval --compare <runA> <runB>                # diff two persisted eval reports
```

`eval` writes `eval/eval-report.yaml` (machine-readable `EvalReport`) and
`eval/eval-report.md` (the human view) into the run workspace, committed. A
partial run evaluates partially — each metric a run hasn't earned yet is
skipped with the reason named (an M1-shaped S0–S5 run still gets
informedness and quality). The five metrics:

- **Breadth** — the run's distinct-angle count vs the benchmark's reference
  union. The semantic matching is the eval's only LLM call: the Haiku-class
  `eval-judge` (`agents/eval-judge.md`) goes through the *same* dispatch
  layer as every pipeline agent — schema-retried with a coherence callback,
  every attempt ledgered under stage `EVAL`, fixture-answered in mock mode.
  The report lists hits, misses, **practitioner-obvious misses** (the
  design's penalty flag — e.g. `llm-agent-systems-build`, the angle the M2
  ensemble missed and a human added at Gate A), and novel run angles
  (candidate union additions). Matching is provenance-aware: a reference
  angle whose only match carries `[human]` provenance (a Gate-A addition)
  is a **human-rescued** hit — in the map, but not ensemble coverage, and
  its practitioner-obvious flag still fires, so a gate rescue can never
  hide an ensemble miss. Where the spec carries option-level ground truth
  (`option_checks`), a mechanical term scan over every option card reports
  whether the option was plausibly carded — a match renders as **TERM
  MATCH — confirm by hand**, never as a pass (word overlap is evidence,
  not proof: the M2 run's "compression" hit was a context-compression
  card, not the fork test). The M2 Trap-2 divergence is exactly the miss
  the angle-level metric cannot see.
- **Informedness** — Spearman rank correlation between allocation units and
  post-hoc angle value (an angle's share of the finalists), with
  floor-compliance and the **floor's budget share**: near 100% means γ was
  inert (M2 F7: 16 angles × floor 2 consumed 32 of 40 units), and a
  no-variance allocation reports `n/a`, not a fake 0.
- **Quality** — critic revision rate per angle (computed with S3's own
  `needs_revision` rule, so the metric cannot drift from the pipeline) plus
  schema/coherence retry counts from the ledger, by stage and by angle —
  falling over time = prompts improving. The raw causes stay in
  `logs/retries/`. M2 baselines: revision rate 100% (F6), S3/S5 the retry
  hotspots (12/8).
- **Depth** — verifier pass rate (verified/sampled over all reports), the
  share of load-bearing claims at high confidence, and the BUDGET-CAPPED
  dossier count, per finalist and overall. M2 baseline: pass rates 33–91%,
  zero capped.
- **Anti-overfit** — asserted from the tournament artifacts: do the
  destination-only and preference-adjusted boards differ, what are the rank
  inversions (recomputed with the same `sensitivity.py` code S7 used), and
  does every inversion-demoted option have a `rank-inversion` steelman on
  file — missing steelmen are named.

**Reading the report:** the markdown leads each section with the headline
number, then the per-angle/per-finalist table. Bold uppercase markers
(**PRACTITIONER-OBVIOUS MISSES**, **NOT CARDED**, **STEELMAN MISSING**,
floor **VIOLATED**) are the things that demand action; everything else is
trend data. The spend table and the eval's own judge cost close the report.

**The A/B scaffold:** each benchmark has a slot under `benchmarks/baselines/`
to paste a plain Deep Research answer to the same question.
`--compare-baseline` has the same judge score that answer's angle coverage
against the same reference union and prints the side-by-side — the system
must visibly beat it to justify its cost, and when it doesn't, the miss
lists name the stage to fix (angle misses → S1 cartography personas; carded
misses → S3 scouts). A still-empty placeholder is refused loudly.

**The tuning loop** (this is the ongoing M3 activity):

1. `deeper eval <run> --against <benchmark>` — find the weak property.
2. Edit the responsible `agents/*.md` prompt or config knob (γ, floor,
   shortlist margin, stability Δ…) — one change at a time.
3. Rerun the benchmark question on the **quick** profile.
4. `deeper eval <new-run> --against <benchmark>`, then
   `deeper eval --compare <old-run> <new-run>` — did the change move the
   property it was aimed at, and did anything else regress? The compare
   lands at `eval/compare-vs-<old>.md` in the new run.

`deeper rerun` invalidates a run's `eval/` reports along with everything
else — a stale eval can never masquerade as evidence. The four seeded specs
and the spec format live in [`benchmarks/README.md`](benchmarks/README.md).

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
- **S8's citation pass uses the shared retry budget, not its own "one retry".**
  The build guide gave the mechanical citation pass a bespoke single retry;
  it now rides the same `validate=` feedback loop as every other
  per-dispatch coherence check (strictly more forgiving — up to
  `caps.max_schema_retries` — same pause on exhaustion, one retry
  discipline instead of two).
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
- **`Heuristic.HUMAN` marks Gate-A-added angles.** The design's "add an angle" action
  needs the addition to live in the same map every downstream stage reads, but
  `Angle.contributing_heuristics` requires provenance. A `human` member of the
  heuristic enum records it honestly; a human-added angle also carries placeholder
  example options ("(to be scouted…)") because existence proofs are exactly what the
  assigned scout will establish. The saturation math never sees these (they have no
  dedup entries).
- **Interview cap and turn semantics are config, not prose.** Design §5/S0 says
  "capped at ~8 questions"; `HardCaps.max_interview_questions` (default 8) makes the
  cap enforceable code. "Streaming" is per-question: each question prints the moment
  the agent's turn completes (token-level streaming inside a turn is not wired).
  Declining the confirmation discards the interview entirely — a revision loop
  (feed the objection back and re-emit) is deliberately out of scope for now.
- **The Gate A rerun hint lives at `gates/gate-a-hint.txt`.** The design doesn't say
  where the hint travels; S1 invalidation wipes `angles/` and the gate decision
  file, so the hint is written beside the gate file (which invalidation preserves)
  and deleted by the S1 pass that used it — "another pass with a hint" is exactly
  one pass.
- **Same-heuristic expansion passes get suffixed workspace names.** Design §7 lists
  one raw file per cartographer; the saturation rule can re-run a heuristic, so
  expansion output lands at `angles/raw/{heuristic}-{n}.yaml` and dedup entries are
  attributed to a pass by (heuristic, raw angle name). "Novelty across the last two"
  is read as the mean over the trailing window (`cartography_novelty_window`).
- **Returned-units currency for the S3 early stop.** Design §5/S3 says an
  early-stopped scout's "unused units return to a global pool" without
  quantifying "unused". Here (pure code): distinct cards = `n − floor(n·pct/100)`,
  units consumed = `ceil(distinct / 2)` (the allocation's own 1-unit ≈ 2-cards
  currency), returned = allocated − consumed, floored at 0. Only
  redundancy-stopped angles return units — those angles never receive revisions
  or top-ups, so the pool computation is stable across crash re-entry.
- **The revision round is conditional, and misses don't trigger it.** Design
  §5/S3 grants "one revision round against the critique"; here it runs only
  when the critique reports completeness or distinctness issues, and never for
  a redundancy-stopped angle (early stop trumps revision). `missed_options`
  route to the reflow pool, not the revising scout — a revision fixes defects,
  reflow buys new coverage.
- **The reflow table persists at `options/reflow.yaml`.** The design doesn't
  name a home for the redistribution decision; it is an allocation artifact
  like any other, so it is written (kind `reflow`) after the top-up cards
  merge, making the redistribution auditable and marking the stage settled.
- **The preference-slot weight is code-owned end to end.** Design §5/S4 has
  the rubric-builder emit the slot "default weight 15–25%, set at Gate B"; here
  the number is process, not content (P8): S4 overwrites whatever the agent
  emitted with `RunConfig.preference_slot_default_weight` (0.2), and Gate B's
  decision always rewrites it.
- **Gate B weight edits renormalize the untouched criteria.** The design says
  "adjust weights" but the schema requires criterion weights to sum to exactly
  1.0. Rather than forcing the human to hand-normalize the whole vector,
  overridden/edited criteria are pinned at their stated weights and the
  untouched ones rescale proportionally (with the factor echoed); pinning
  everything requires an exact sum, and infeasible combinations re-pause the
  gate with nothing written.
- **S5 recomputes the screener's aggregates.** The screener stores
  `weighted_point`/`weighted_ucb`, but code re-derives both from the criterion
  scores and the Gate-B-approved rubric weights before persisting (drift beyond
  0.05 is reported) — arithmetic is the orchestrator's job (P8), and the gate
  may have changed the weights after the fixture-or-agent computed its numbers.
- **The shortlist rule is relative (top-k + dark-horse margin), not the
  design's bare absolute threshold.** §5/S5 reads as "advances if its upper
  confidence bound clears the shortlist threshold", with nothing truncating
  to k. The M1 live run showed that rule does not concentrate when the
  screener's bands inflate: 52 options, average band half-width 0.816 on the
  1–5 scale, 50 of 52 UCBs above the 3.5 bar — 26 "finalists", every one via
  `ucb-above-threshold`, where S6 would have built 26 dossiers. The rule is
  now: advance the top `shortlist_size` by UCB **plus** any option whose UCB
  is within `shortlist_dark_horse_margin` (default 0.25) of the k-th
  finalist's — which preserves the design's dark-horse property (a wide band
  still lifts an under-researched option into the shortlist) while making
  concentration independent of band calibration. The absolute threshold
  survives as a floor, `caps.max_finalists` is a hard ceiling, and the
  kill-first / angle-cap / breadth-guardrail clauses are unchanged ("top-half
  angle" for the guardrail still means the top `ceil(n/2)` map angles by
  relevance prior). Cuts by the new mechanism get their own cause
  (`below-cutoff`, "nothing here eliminates the option on the merits")
  distinct from `below-threshold`.
- **Run-level `billing` mode.** The design doc never addresses *whose money*
  a live run spends. The M1 run happened to authorize via the Claude Code
  CLI's login; `RunConfig.billing` makes that guaranteed rather than
  accidental: `subscription` (default) blanks `ANTHROPIC_API_KEY` in every
  subagent env (the CLI treats empty as unset and uses the stored login) and
  refuses dispatch if a subagent reports any other auth source; `api` passes
  the key through and fails fast without one.
- **`rubric-rationale.md` is a rendered view.** The rubric-builder emits only
  `rubric` (the design's rationale file has no schema of its own); S4 renders
  the rationale markdown from the validated rubric's definitions, measurement
  methods, and weight justifications, so the two files cannot disagree.
- **S5 screening is batched per angle, and oversized angles are
  sub-batched.** Design §5/S5 reads as one screener pass over all cards; the
  M1 live run (52 options × 7 criteria) needed a reply beyond even a 64k
  output-token ceiling and hung the CLI. S5 now dispatches one batch per
  angle and merges in code — like-for-like scoring is preserved because
  every batch carries the same rubric and anchored levels; the per-batch
  integrity checks union to the design's full-map check. The M2 live run
  then showed per-angle batching still leaves one batch's size unbounded (a
  25%-cap angle's ~20-card reply overflowed the M class's 16k ceiling), so
  an angle over `screener_batch_max_cards` (default 10) splits into
  balanced sub-batches — bounded replies by construction instead of chasing
  per-profile token ceilings — each persisted as a part file until the
  angle's settled batch file supersedes them.
- **Dispatch retry-with-backoff arrived early.** The build guide schedules
  exponential backoff for Prompt 13; the M1 live run hit repeated one-off SDK
  stream errors, each costing a human resume, so the minimal schedule (2
  retries, 2s/8s + jitter) landed with the M1-exit hardening. The rest of the
  failure-path work (per-invocation timeout, CLI result-detail enrichment,
  failed-attempt ledgering, usage-limit detection) landed with Prompt 13 as
  planned.
- **Plan usage-limit exhaustion is a distinct, non-retried pause
  (`UsageLimitReached`).** The design doc never contemplates subscription
  billing hitting a plan limit mid-run. Detection is deliberately broad (the
  `usage limit reached|<epoch>` marker and the CLI's prose notices, matched in
  both exception text and short artifact-free replies) because the exact live
  shape is unconfirmed — the M1 triage's planned probe at the limit boundary
  remains worth doing to tighten the matchers. Per the recorded scope
  decision: the pause message with the reset time is the whole feature; there
  is deliberately NO automatic sleep-and-resume.
- **A failed dispatch attempt is a ledgered `SpendEntry`.** The tokens a
  dispatch that died in flight consumed are unknowable from our side, so the
  entry is a zero-cost marker carrying the error text in a new `failed` field
  — the audit trail shows every attempt without pretending to know its cost.
- **`logs/retries/` preserves recovered failures.** Design §10 wants
  schema-failure rates *and causes*; retry counts alone lose the causes when
  the retry succeeds, so every invalid attempt's raw output + validation
  errors are persisted even on recovery.
- **The option-id namespace is global, enforced at S3 write time.** The design
  scopes nothing about card-id uniqueness across angles, but scores, dossiers,
  and tournament artifacts key on bare option ids. Cross-angle collisions are
  deterministically auto-suffixed (`id-{angle}`) at the cards.yaml write
  chokepoint; the S5 merge guard survives as a backstop for hand-edited
  workspaces.
- **`screening/batches/{angle}.yaml` persists paid screener batches.** Not a
  design §7 artifact: it exists so a merge-time failure or crash never
  re-pays completed batches (M1 re-paid all 12). A persisted batch is trusted
  only while it still coheres with the current rubric and cards.
- **Quick caps cartography at one expansion pass, and the default spend caps
  are ledger-calibrated.** §12's global 8-invocation cap let the M1 quick run
  spend ~$13 before Gate A; quick now ships `caps.max_cartographers: 4`
  (3 initial + 1 expansion), and the profile caps (30/100/200) reflect the
  measured cost reality instead of the original optimistic 5/25/60 — see
  "Cost expectations".
- **Run-level `max_spend_usd` guard.** Design §8 speaks of per-stage caps and
  spend visible at gates; the runaway-cost mitigation here is (additionally) one
  whole-run USD cap the dispatcher checks before every invocation, because a
  single number the human sets at `deeper new` is the guard that actually
  matches how a supervised live run is budgeted. Per-stage caps can layer on
  later without changing the chokepoint.
- **An angle-scoped S3 rerun replays the persisted reflow decision.** `rerun
  --stage S3 --angle X` deletes `options/X/` but keeps `options/reflow.yaml`
  (the settled redistribution decision, which is not angle-scoped). If that
  table granted X units, X's top-up merge was invalidated with its cards — so
  S3 re-entry re-dispatches the top-up for freshly re-scouted angles from the
  persisted table (never recomputed), leaving other angles' settled top-ups
  untouched. Without this, the re-scouted angle silently loses the coverage the
  reflow decision bought it.
- **The S6 round log is a new code-owned artifact
  (`dossiers/{option}-rounds.yaml`).** Design §5/S6 names only the dossier and
  verification files, but the stopping rule's evidence — each round's re-score,
  delta, and remaining low-confidence load-bearing claims, the verification
  sample, and whether the one revision ran — must live somewhere for resume to
  be honest (P8: replayed rounds are never re-dispatched). It lives under
  `dossiers/` so the existing S6 rerun invalidation covers it.
- **`dossiers/scores.yaml` is S6's merged output.** The design has S7 rank "the
  post-deep-dive" scores without naming their home; S6 merges every finalist's
  final re-score into one `ScreeningResult` (written last — it marks the stage
  settled, like S3's reflow table).
- **Round 0 for the stability delta is the S5 screening score.** "Moved < 0.15
  across the last round" needs a round-1 comparison point; the option's
  `screening/scores.yaml` record (already recomputed under the Gate-B rubric) is
  it. An option whose first deep-dive round confirms its screening — and leaves
  no low-confidence load-bearing claim — legitimately converges in one round.
- **1 round = 1 unit.** The design's "per-option budget cap is hit" is
  quantified in the allocation's own currency: a research round consumes one
  unit, so `deep_dive_unit_cap` (quick 2 / standard 4 / exhaustive 6) is the
  maximum round count. Stability is checked before the cap: a final budgeted
  round that also stabilizes is CONVERGED, not BUDGET-CAPPED.
- **Effective load-bearing is the union of tag and re-score diff.** The design
  says the analyst tags load-bearing claims and defines them by the ≥ 1-point
  criterion move; here the cross-check has teeth: any criterion whose re-score
  moved ≥ 1 point promotes every claim in its dossier section into the
  load-bearing set (for clause (b) and verifier sampling) whether tagged or
  not, and under-tagging is reported. An analyst cannot shrink the verifier's
  mandatory sample by under-tagging.
- **The verifier's "random 20%" is a seeded draw.** `ceil(0.2 · n)` of the
  non-load-bearing claims, drawn by an RNG seeded on the option id — resume
  re-samples identically (and mock fixtures can be authored against the exact
  sample). The verifier is Sonnet-class by analogy (§6 leaves it unlisted;
  adjudication outgrows Haiku-class citation checking, and the S size class's
  search budget could not re-fetch a full sample).
- **The BUDGET-CAPPED stamp is code's, with derived open questions.** The
  design stamps the dossier when the cap hits; here the orchestrator sets
  `budget_capped` (and re-asserts it after the revision round, so an agent
  re-emission cannot launder it away). The final budgeted round's contract
  demands honest `open_questions`; if the analyst still left none, code derives
  them from the remaining low-confidence load-bearing claims — the schema
  requires a capped dossier to list them.
- **Steelmen are per-option files (`tournament/{option}-steelman.md`).** Design
  §7 lists a single `tournament/steelman.md`, but the docket it defines — the
  runner-up *plus every rank inversion* — can hold several options, each owed
  its own steelman (schema `Steelman` models exactly one case). One file per
  docket entry keeps the naming parallel with prosecutions.
- **A rank inversion is a strict pairwise flip, and the docket excludes the
  winner.** "Any option whose destination-only rank differs from its
  preference-adjusted rank" is read as: a pair the destination-only board
  strictly orders one way and the preference-adjusted board strictly reverses
  (a tie refined by the other board contradicts nothing, and dense ranks mean
  tie-break jitter never manufactures an inversion). Steelmen go to the
  *demoted* option of each inverted pair — the destination model's preference
  that tastes overrode is exactly where overfitting hides — plus the adjusted
  runner-up; the adjusted winner is never steelmanned (the case for it is the
  status quo the prosecutors attack), and an option that is both runner-up
  and inverted carries the sharper `rank-inversion` trigger.
- **The judge emits the ledger; code applies it (`tournament/scores.yaml`).**
  Design §5/S7 has the judge "update scores"; here (P8) the judge's artifact
  is only the cause-logged `ScoreUpdateLog` — code verifies every entry (real
  option, rubric criterion, never the preference slot, `old_score` matching
  the scoreboard as it stands), applies the changes (widening an uncertainty
  band the new score falls outside), recomputes the weighted aggregates, and
  persists the post-tournament scoreboard to `tournament/scores.yaml` (a
  fifth tournament artifact the design doesn't name — S8 needs the updated
  scores without replaying the ledger; written last, it marks the stage
  settled).
- **Narrative artifacts are structured models.** Design §7 lists `brief.md`,
  `dossiers/{option}.md` etc. as markdown; the schema layer models their *content* as
  structured, YAML-serializable models so validation is uniform (design §6's
  "required-section checks for markdown" become field requirements — e.g. the five
  standing dossier sections are required fields). Stages may render markdown views of
  these artifacts later; the validated file is the structured one.
- **`decision-report.md` is a code-rendered view of a validated artifact.** The
  design names one report file; here the synthesist emits the structured
  `report/decision-report.yaml` (`DecisionReport` — seven component fields, so
  the "all seven sections" contract is schema-checkable) and code renders the
  human deliverable `report/decision-report.md` around it, embedding the
  code-computed scoreboards, sensitivity tables, matrix, and appendix tables
  verbatim (P8: the agent narrates arithmetic, never produces it) and turning
  every `[[claim-id]]` annotation into a link to the appendix claims index.
  The yaml is written last and is the stage's declared output.
- **The citation pass validates inline annotations, not raw sentences.** "Every
  factual claim in the report body links back to a dossier claim" is made
  mechanical by having the synthesist annotate each factual sentence with
  `[[claim-id]]` (qualified `[[option-id:claim-id]]` when the bare id exists in
  more than one dossier — claim ids are unique only per dossier); code verifies
  every annotation resolves and that the recommendation carries at least one.
  Sentence-level coverage is the prompt's contract; resolution is code's.
- **Gate-C preference feedback amends `dossiers/scores.yaml` too.** The re-score
  naturally lands in `tournament/scores.yaml` (S8's input), but a later
  re-divergence mini-loop invalidates the tournament and reranks from
  `dossiers/scores.yaml` — so the human's slot adjustments are applied to both
  files (same rubric, aggregates recomputed in code); the gate-loop commit is
  the audit trail for the S6-owned file changing at a gate.
- **The mini-loop compresses S1→S6.** "A mini-loop of Stages 1–6 scoped to the
  new region" runs, on the proposal's own `estimated_cost_units`: a map edit
  (new-angle proposals only; human provenance like Gate-A additions) → ONE
  targeted scout pass with no critic round (a critique would double a
  1–6-unit budget) → one screener batch + the threshold seat rule (at most one
  new finalist, `max_finalists` still binding, all decisions appended to
  `shortlist.md`) → the real `DeepDiveStage` round/verification machinery under
  a scoped `deep_dive_unit_cap`. S4 is deliberately not re-run — the
  Gate-B-approved rubric is fixed for the run.
- **Evidence challenges dispatch the verifier only, and never move scores.**
  The design says "targeted verifier/analyst task"; adjudicating "I don't
  believe claim X" against its source is exactly the verifier's existing skill
  and schema, so no analyst rewrite is spawned. The verdict is surfaced
  (gates/challenge artifact, ledger entry when contradicted, terminal message);
  any score consequence is the human's next deliberate loop (preference
  feedback, or approving informed) — a challenge is a question, not an edit.
- **Gate-loop bookkeeping lives in `RunState`, spend under stage S7.**
  `gate_c_iterations` and `redivergence_runs` are new run-state counters
  (defaults 0, so old state.json still validates); they are never reset by
  rerun invalidation — the §12 caps are per *run*. Gate-loop agent dispatches
  ledger their spend under S7 (the gate's preceding stage; `SpendEntry` has no
  gate stages) with `gate-c-feedback` / `challenge-*` / `redivergence*`
  contexts, so the audit trail still separates them.
- **`Stage.EVAL` is a ledger/contract stage, not a pipeline node.** The §10
  judge must be cost-tracked through the same `SpendEntry` machinery as every
  agent, and `SpendEntry.stage`/`AgentContract.stage` are typed `Stage` — so
  the enum gains an `EVAL` member that `RunState.stage` never takes, the
  engine never dispatches on, and `deeper rerun` refuses as a target (with
  the pointer to re-run `deeper eval` instead). Its one rerun-machinery role:
  any invalidation also deletes the run's `eval/` subtree, because an eval
  report is stale the moment upstream artifacts change.
- **Option-level ground-truth checks extend §10's angle-level breadth.**
  Design §10 measures breadth only against a reference *angle* union; the M2
  ground-truth divergence was an option-level scouting miss inside a
  correctly-mapped angle ("compression" appeared in zero cards), invisible
  to that metric. Benchmark specs may therefore carry `option_checks` —
  case-insensitive evidence terms scanned mechanically over every option
  card, with matches listed as candidates for the human to confirm (a term
  match is evidence, not proof). Adjudicating what the answer *should* have
  been stays the user's; the check only answers "was it ever carded".
- **The eval never settles ground truth.** Benchmark `ground_truth` fields
  record the withheld prior, the actual outcome, and exactly what remains
  the user's adjudication, verbatim from the live-run notes; the eval report
  surfaces them but no metric scores "was the winner right" — per the M2
  triage, that reading is the human's.

## Roadmap position

Following the phases in the build guide:

- **Phase A — Foundation:** Prompt 1 (bootstrap) ✅ · Prompt 2 (schemas) ✅ · Prompt 3
  (agent prompts + prompt-lab) ✅.
- **Phase B — Kernel happy path (M1):** Prompt 4 (workspace/config/allocation) ✅ →
  Prompt 5 (dispatch layer) ✅ → Prompt 6 (orchestrator/CLI) ✅ → Prompt 7 (real
  S0–S2) ✅ → Prompt 8 (S3 scouts + critic + reflow, S4 rubric, Gate B applied
  edits, S5 screening with the shortlist rule) ✅ → Prompt 9 (M1 exit: end-to-end
  integration test, `deeper doctor`, spend guard, live hardening) ✅. **M1 done.**
- **Phase C — Depth & adversarial (M2):** Prompt 10 (S6 deep dives: analyst
  rounds under the stability stopping rule, verifier pass, contradiction
  ledger) ✅ → Prompt 11 (S7 tournament: prosecutor, steelman, frame-checker,
  judge + the code-computed rubric sensitivity in `sensitivity.py`) ✅ →
  Prompt 12 (Gate C loops: preference feedback, evidence challenges, the
  re-divergence mini-loop; S8 synthesis with the mechanical citation pass;
  `deeper report`) ✅. **M2 done — the full pipeline S0→S8 is built.**
- **Phase D — Evaluation & hardening (M3):** Prompt 13 (pre-live hardening:
  every M1 triage finding addressed, the failure-path audit — timeout,
  network, SDK, schema double-failure, usage limit — all pausing resumably,
  ledger reconciliation asserted end-to-end) ✅ → Prompt 14 (the eval
  harness: 4 seeded benchmark specs, the five §10 property metrics, the
  `eval-judge`, `deeper eval`/`--compare`/`--compare-baseline`) ✅ →
  Prompt 15 (security/ops hardening: the §11 mitigation audit closed —
  hidden-text sanitizer patterns + adversarial fixtures, executable §11
  policy tests, unresolved-contradiction surfacing in S8, structured JSONL
  invocation logs + per-run rotation, `status --spend`, the doctor
  hook-denial probe, per-gate `gate_modes` notify) ✅. **M3's build items are
  done — what remains is the ongoing tuning loop.**
- **Phase E — Viewer (M4, optional).**

**Next: the ongoing M3 tuning loop (eval-driven prompt iteration against the
seeded benchmarks), then optionally the Phase E viewer. Still worth doing
sometime: a live probe at the plan-limit boundary to pin down how the SDK
surfaces the usage-limit condition (the detector is deliberately broad until
then).**
