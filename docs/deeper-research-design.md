# Deeper Research — Design & Implementation Plan

**A personal, decision-grade research system that optimizes informed breadth, per-angle quality, and per-option depth over token efficiency and speed.**

Version 1.0 — July 2026

---

## 1. Executive summary

Deeper Research is a gated, multi-agent research pipeline for a single user. Given a goal, it maps the full space of *angles* (general solution areas), populates each angle with *options* (specific solutions), scores those options against a destination-derived rubric, deep-dives the survivors, adversarially stress-tests the winner, and produces a decision report with sensitivity analysis — pausing at human review gates where your judgment matters and your preferences are deliberately quarantined until the scoring stage.

The recommended implementation (the "Option 3" you asked for) is **pipeline-as-kernel, UI-as-viewer**: a headless Python orchestrator built on the Claude Agent SDK, where a deterministic state machine owns the pipeline and LLM agents do the cognitive work *inside* each stage. All state lives in a plain-file research workspace that is the single source of truth. Human gates are first-class pause states in the state machine, not chat turns. A thin local web viewer is added last, as a convenience layer over the files — never as the system's brain.

This beats your Option 1 (a series of Claude Code prompts) because a prompt series makes *you* the orchestrator: budgets, stopping rules, and stage contracts live in your head and erode under fatigue; runs aren't reproducible; and nothing enforces that preferences stay quarantined. It beats your Option 2 (a self-contained UI application) because the UI is the least valuable and most expensive part: it front-loads weeks of interface engineering that improve zero of your three target properties (breadth, quality, depth), and it couples research logic to presentation, making the pipeline harder to iterate on. Option 3 gets you Option 2's experience eventually — but earns it incrementally, with the research engine testable and useful from week one.

**Core architectural bets, each mapped to a target property:**

| Target property | Mechanism |
|---|---|
| Informed breadth | Ensemble angle cartography (diverse heuristic personas) + relevance-proportional budget allocation with an exploration floor |
| Quality per angle | Explicit subagent contracts (objective, output schema, source guidance, boundaries) + independent critic pass per artifact |
| Depth per option | Dossier saturation loop with a score-stability stopping rule + independent claim verification agent |
| Anti-overfitting | Preference quarantine until scoring; destination-derived rubric; adversarial tournament; frame-check with re-divergence trigger |
| Diminishing returns | Every expansion loop (angles, options, evidence) has a measurable marginal-novelty or score-stability stop condition and a hard budget cap |

---

## 2. The architecture decision: three options analyzed

### Option 1 — Prompt-driven Claude Code project

A folder plus a set of slash commands / skills you invoke in sequence, reviewing markdown between steps.

*Strengths:* zero infrastructure; fastest to first result; naturally file-based; the human gates are free (you're already in the loop).

*Fatal weaknesses for this project's goals:* the orchestration logic — how many cartographers to run, when an angle is saturated, whether the verifier passed, whether budgets are exhausted — is either re-prompted every run (drift, inconsistency) or lives in your discipline (erodes). Parallelism is awkward and manual. There is no enforcement layer: nothing *stops* a scout from skipping the schema, nothing *blocks* synthesis from starting before verification passes. Anthropic's own postmortem of their multi-agent research system is blunt that agent behavior degrades without explicit delegation contracts and effort-scaling rules baked into the harness — in an interactive prompt series, you are that harness, every time.

Verdict: correct as **v0 scaffolding** (it's the cheapest way to iterate on the agent prompts themselves), wrong as the destination.

### Option 2 — Self-contained UI application

A desktop/web app: define a goal, watch agents map the space, review contenders in cards, give feedback, iterate — everything in one interface.

*Strengths:* best ergonomics for the gate interactions (comparing contender cards side-by-side, dragging weight sliders, tagging feedback); a satisfying artifact.

*Weaknesses:* for a personal tool, the UI is 60–70% of the engineering effort and 0% of the research quality. Real-time agent streaming into a UI, run persistence, interactive state sync — all of it is plumbing that competes with the actual hard problems (breadth allocation, stopping rules, verification). Worse, building UI-first tempts you to make the UI the state holder, which destroys inspectability and resumability — the two properties that made the project-folder approach superior to single-chat Deep Research in the first place.

Verdict: correct as a **final layer**, wrong as the foundation.

### Option 3 (recommended) — Pipeline-as-kernel, UI-as-viewer

A three-layer system:

1. **Kernel** — a deterministic Python orchestrator (a small state machine, ~500 lines of real logic) built on the Claude Agent SDK. It owns: stage sequencing, budget accounting, gate pausing/resuming, concurrency limits, retry policy, and enforcement hooks. It contains no research intelligence.
2. **Agents** — specialized subagents (cartographers, scouts, rubric-builder, analysts, verifier, advocate/prosecutor, frame-checker, synthesist), each defined by a versioned prompt file with a strict contract: objective, output schema, tool allowlist, source guidance, and boundaries. All cognitive work happens here, in isolated context windows, in parallel where the task decomposes.
3. **Workspace** — a plain-file directory (markdown + YAML/JSON) that is the *complete* state of a run. Every stage reads its inputs from files and writes its outputs to files. You can inspect, hand-edit, diff, or version-control any artifact at any gate. If the orchestrator dies, the workspace *is* the checkpoint.

The interface starts as CLI + your editor (gates are "open this file, edit the decision block, run `resume`"), and graduates to a thin local web viewer (read the workspace, render cards and matrices, write gate-decision files) once the pipeline is proven.

Why this wins: it puts the deterministic things (budgets, sequencing, enforcement) in deterministic code and the cognitive things (mapping, judging, arguing) in LLMs — the inverse assignment is the root cause of most agent-system failures. It is reproducible (same config + same goal → same pipeline shape), resumable, inspectable, cheap to iterate (edit a prompt file, rerun one stage), and it grows into Option 2's experience without ever depending on it.

---

## 3. Design principles

**P1 — Decouple divergence from convergence, structurally.** Exploration stages (angle mapping, option scouting) are executed by agents that *never see* your preferences and are prompt-forbidden from ranking. Convergence stages (scoring, shortlisting) run later, against artifacts that already exist. Overfitting to stated preferences becomes architecturally impossible rather than merely discouraged.

**P2 — Artifacts are contracts.** Every stage boundary is a schema-validated file. A stage cannot start until its input artifacts validate; a stage's output is rejected (and retried with the validation error) until it validates. This is where quality is enforced mechanically instead of hoped for.

**P3 — Breadth through ensemble diversity, not one big brainstorm.** A single agent asked to "list all angles" produces one distribution — its most probable completion. Several agents with *different framing heuristics* produce overlapping-but-distinct distributions; the union minus duplicates is measurably broader. Diversity is engineered via personas, not temperature.

**P4 — Informed breadth through proportional allocation with a floor.** Budget per angle ∝ estimated relevance, but every surviving angle gets a minimum exploration allocation. Density concentrates where relevance is high (informed) without zeroing out the periphery (breadth). This is a soft explore/exploit allocation, deliberately biased toward explore relative to what a naive optimizer would choose, per your priorities.

**P5 — Quality through adversarial redundancy.** Nothing important is asserted once. Options get a critic pass. Dossier claims get an independent verifier. The leading contender gets a prosecutor. The *frame itself* gets a dedicated skeptic. Redundancy costs tokens; you have explicitly priced that in.

**P6 — Depth until stability, not until exhaustion.** Deep dives loop (research → update dossier → re-score) until the option's score stops moving and no high-impact unknowns remain, or the per-option budget caps out. Depth is defined by *information sufficiency for the decision*, not by page count.

**P7 — Human gates where judgment lives, automation everywhere else.** Three gates: the angle map (is the frame right?), the rubric (are we optimizing the right thing?), the contenders (what does the evidence make you feel?). Everything between gates runs unattended.

**P8 — Deterministic spine, stochastic muscles.** The orchestrator never asks an LLM "what stage should we do next?" Stage order, budgets, retries, and stop-rule evaluation are code. LLMs decide *content*, code decides *process*.

**P9 — Late-binding preferences, visible influence.** Preferences enter only at scoring, as a separate, visible score component with its own weight. The report always shows both the destination-only ranking and the preference-adjusted ranking, and flags every rank inversion between them. You see exactly what your preferences cost you.

**P10 — Everything measurable.** Marginal novelty, score stability, verification pass rates, per-stage token spend — all logged per run, so the diminishing-returns knobs can be tuned against evidence rather than vibes.

---

## 4. Conceptual model and vocabulary

- **Goal** — the user's research question or decision, e.g. "Which senior research project best positions me for X?" or "What's the best approach to Y technical problem?"
- **Destination model** — an explicit description of what success looks like *from the target's perspective*: the reward function of the admissions committee, the hiring pipeline, the production environment, the reader. Derived in Stage 0; this, not your tastes, anchors the rubric.
- **Angle** — a general solution area; a region of the space. ("Interpretability research," "systems-for-ML," "managed database," "build-vs-buy.")
- **Option** — a specific solution within an angle. ("Join lab X on project Y," "TimescaleDB on RDS," "vendor Z.")
- **Option card** — the schema'd artifact representing an option at screening depth: description, mechanism, preliminary evidence, key uncertainties, kill-risks.
- **Dossier** — the deep-research artifact for a finalist: evidence by rubric criterion, sources with tiers, failure modes, costs, second-order effects, confidence-tagged claims, open questions.
- **Rubric** — weighted criteria derived from the destination model, plus a separate preference component. Every criterion has a definition, a measurement method, and anchored score levels (what a 2 vs a 4 means).
- **Gate** — a pipeline pause requiring a human decision, expressed as a gate file the human edits (or a UI action that writes it).
- **Budget unit** — one subagent invocation of a given size class (S/M/L), the currency of allocation. Token costs are tracked per unit via the SDK's per-result cost reporting.

---

## 5. Pipeline specification

The pipeline has nine stages and three gates. Stages 1, 3, and 6 are the parallel, token-heavy ones. Every stage lists its agents, artifacts, and stop conditions.

```
S0 Intake ──► S1 Angle cartography ──► [GATE A: frame] ──► S2 Allocation
──► S3 Option scouting ──► S4 Rubric construction ──► [GATE B: values]
──► S5 Screening ──► S6 Deep dives ──► S7 Tournament ──► [GATE C: contenders]
──► S8 Synthesis                    ▲                        │
        ▲                           └── re-divergence ◄──────┘
        └────────────── re-score loop ◄─────────────────────┘
```

### Stage 0 — Intake & destination modeling

**Agent:** `interviewer` (one, interactive — the only conversational agent in the system).

**Process:** A short structured interview (capped at ~8 questions) that produces three strictly separated artifacts:

1. `brief.md` — the goal, restated; scope boundaries; the *type* of answer wanted (a decision, a landscape, a recommendation with fallbacks); constraints that are facts about the world (deadline, budget, hard requirements).
2. `destination.md` — the destination model: who/what ultimately judges the outcome, and what that judge rewards. For a decision like "pick a research project," this is admissions/hiring reward functions. For a technical question like "choose a vector database," it's the production environment's actual demands. For advice questions, it's the concrete situation's success conditions. The interviewer does light web research here if the destination is external and verifiable (e.g., what do target MS programs actually weight?).
3. `preferences.yaml` — everything that is a *taste* rather than a fact: interests, aesthetics, risk appetite, soft dislikes. **This file is quarantined**: the orchestrator's hooks deny read access to it for every agent except the Stage-5 scorer and Stage-8 synthesist.

The interviewer's prompt explicitly instructs it to classify each user statement as constraint / destination-fact / preference and to push back when the user states a preference as if it were a constraint ("must be ML" — is that a hard constraint or a strong preference?). This classification step is itself a major anti-overfitting lever.

**Stop condition:** artifacts validate; user confirms the brief.

### Stage 1 — Angle cartography (divergence)

**Agents:** an ensemble of 4–6 `cartographer` subagents run in parallel, each with the same objective but a *different framing heuristic* baked into its persona prompt:

- **First-principles decomposer** — derive angles from the structure of the problem itself (what are the independent dimensions along which solutions can vary?).
- **Adjacent-field analogist** — how do neighboring domains solve the isomorphic problem? Import their solution families as angles.
- **Contrarian / inverter** — assume the obvious framing is wrong; what angles exist if the goal is achieved indirectly, partially, or by dissolving the problem?
- **Practitioner** — what do people who actually face this decision in the wild choose, including the unglamorous defaults? (Grounded in web research: forums, postmortems, surveys.)
- **Taxonomist** — find existing published taxonomies/surveys of this space and extract their categorization (grounded in web research).
- *(Optional 6th, for forward-looking questions)* **Horizon scanner** — angles that are marginal today but rising.

Each cartographer returns 5–12 candidate angles in a strict schema: name, definition, why-it's-a-distinct-region, 2–3 example options (existence proofs only, not endorsements), and an estimated relevance rationale. Cartographers never see `preferences.yaml` and are prompt-forbidden from ranking angles by attractiveness.

**Merger:** a single `merger` agent (larger model) deduplicates, resolves near-synonyms, builds a two-level taxonomy (angle → sub-angle where warranted), and assigns each angle a **relevance prior** (0–1) with a one-paragraph justification grounded *only* in `brief.md` + `destination.md`. It also emits a **coverage self-report**: which framing heuristics contributed which angles, and where the map feels thin.

**Saturation rule (diminishing returns on breadth):** after the initial ensemble, the orchestrator computes marginal novelty = (new distinct angles contributed by the last cartographer) / (its total angles). If marginal novelty across the last two cartographers < 0.2, cartography is saturated. If ≥ 0.2, the orchestrator spawns up to 2 additional cartographers with the heuristics that produced the most novel angles. Hard cap: 8 cartographers.

**Artifact:** `angles/map.yaml` + `angles/map-report.md`.

### Gate A — Frame review

You review the angle map. Actions available (edited into `gates/gate-a.yaml`): approve; add an angle (with a note — a scout will be assigned); remove an angle (with reason, logged); adjust a relevance prior; request another cartography pass with a hint. **This is the single highest-leverage gate**: five minutes here prevents the classic failure where a beautifully executed pipeline optimizes inside the wrong map.

### Stage 2 — Budget allocation (informed breadth, formalized)

Pure code, no agents. Given total option-scouting budget **B** (in budget units, from the run profile) and per-angle relevance priors *rᵢ*:

```
allocationᵢ = floor + (B − n·floor) · rᵢ^γ / Σⱼ rⱼ^γ
```

- **floor** = minimum units per surviving angle (default 2) — the exploration guarantee; no angle you kept at Gate A gets zero attention.
- **γ** (default 1.0) — concentration exponent. γ>1 concentrates budget on top angles (sharper "informed"), γ<1 flattens toward uniform (broader). Exposed in config; this single knob is your breadth-vs-density dial.
- Per-angle cap (default 25% of B) prevents one angle from starving the map even if its prior is dominant.

The allocation table is written to `allocation.yaml` and included in the final report — the breadth decision is itself an inspectable artifact.

### Stage 3 — Option scouting (parallel, per-angle)

**Agents:** one `scout` per angle, in parallel (concurrency-limited by the orchestrator, default 4 simultaneous). Each scout receives a contract in exactly the four-part form Anthropic found necessary to prevent subagent drift: **objective** (enumerate the strongest distinct options within this angle, target count = allocated units × ~2), **output format** (the option-card schema), **tool/source guidance** (prefer primary sources: docs, papers, official pages, first-hand practitioner accounts; note source tier per claim), and **boundaries** (this angle only; do not rank against other angles; do not recommend; flag options that actually belong to another angle rather than absorbing them).

Option cards are deliberately shallow-but-structured (~300–500 words): what it is, mechanism in two sentences, preliminary evidence for viability, 2–3 key uncertainties, and any *kill-risks* (single facts that, if true, eliminate it — these get checked first in screening, cheaply).

**Critic pass (quality mechanism):** a `card-critic` agent reviews each angle's card set against a checklist — schema completeness, source-tier honesty, distinctness (are two cards really one option?), and *coverage within the angle* (name up to 3 plausible options the scout missed). Scouts get one revision round against the critique. This is the per-angle quality enforcement your requirement #2 asks for: every angle is not just visited but *thoughtfully* populated, with a second pair of eyes attesting to it.

**Within-angle saturation:** if a scout's last N cards are minor variants of earlier cards (the critic flags redundancy > 40%), the scout stops early and the unused units return to a global pool, redistributed by the same allocation formula to the angles whose critics flagged missed options.

**Artifact:** `options/{angle}/cards.yaml`, `options/{angle}/critique.md`.

### Stage 4 — Rubric construction

**Agent:** `rubric-builder` (large model). Reads `destination.md` and the option cards (to know what dimensions actually differentiate this space) — **not** `preferences.yaml`.

Produces `rubric.yaml`: 5–9 criteria, each with a definition, a measurement method ("what evidence would move this score"), anchored levels 1–5 with concrete descriptions, and a weight with a written justification traceable to the destination model. It also emits a `preference-slot`: a reserved, separately-weighted component (default weight 15–25%, set at Gate B) where preferences will later enter — visible, bounded, and singular.

**Artifact:** `rubric.yaml` + `rubric-rationale.md`.

### Gate B — Values review

You review the rubric: adjust weights, edit criteria, and — critically — set the preference-slot weight, which is the one number that says how much your tastes are allowed to bend the destination-optimal answer. The report will later show the sensitivity of the final ranking to this weight.

### Stage 5 — Screening & shortlist

**Agents:** `screener` (the first agent permitted to read `preferences.yaml`) plus code.

Every option card is scored against the rubric at *screening confidence*: each criterion gets a score **and an uncertainty band** (the screener must widen the band when evidence is thin — the prompt anchors this with examples). Preferences are scored into the preference slot only.

**Shortlist rule — optimism under uncertainty:** an option advances if its **upper confidence bound** clears the shortlist threshold, not its point estimate. This deliberately advances under-researched dark horses (wide bands) alongside well-documented favorites — under-information should trigger *more research*, not elimination. Kill-risks flagged in Stage 3 are checked first with single cheap lookups; a confirmed kill-risk eliminates regardless of score.

Default shortlist size: 4–7 finalists, with a guardrail: no more than 3 finalists from a single angle, and if the top-k all come from ≤2 angles, the highest-UCB option from each unrepresented top-half angle is added (breadth insurance at the moment it is most at risk).

**Artifact:** `screening/scores.yaml`, `screening/shortlist.md` (with one-paragraph "why advanced / why cut" notes for *every* option — cuts are auditable).

### Stage 6 — Deep dives (parallel, per-finalist)

**Agents:** one `analyst` per finalist, in parallel. Each builds a dossier structured *by rubric criterion* (so evidence maps directly onto the decision), plus standing sections: failure modes & prerequisites, total cost of adoption (time/money/optionality), second-order effects, strongest published criticism, and comparable cases (who chose this and what happened).

Every claim in a dossier carries an inline confidence tag (`[high|med|low]`) and a source reference with a tier (T1 primary/official, T2 reputable secondary, T3 forum/anecdote). Analysts are instructed to *seek disconfirming evidence explicitly* — at least one search per criterion phrased to find problems ("X limitations", "X postmortem", "migrating away from X").

**Verification pass (quality mechanism):** an independent `verifier` agent samples each dossier — all load-bearing claims (those that move a criterion score by ≥1 point) plus a random 20% of the rest — re-fetches sources, and marks each claim verified / unsupported / contradicted. Contradicted claims trigger one targeted analyst revision. Verification results are appended to the dossier; the final report includes each finalist's verification pass rate. This is the citation-agent pattern from Anthropic's system, extended from attribution to *adjudication*.

**Depth stopping rule (diminishing returns on depth):** each analyst works in rounds (research → update dossier → re-score the option against the rubric). Stop when: (a) the option's weighted score has moved < 0.15 (on the 1–5 scale) across the last round, **and** (b) no remaining `low`-confidence claim is load-bearing; **or** (c) the per-option budget cap is hit — in which case the dossier is stamped `BUDGET-CAPPED` with its open questions listed, so unfinished depth is visible rather than silent.

**Artifact:** `dossiers/{option}.md`, `dossiers/{option}-verification.md`.

### Stage 7 — Adversarial tournament

Three distinct adversarial roles, run against the post-deep-dive ranking:

1. **Prosecutor** — one per top-3 finalist: the strongest good-faith case *against* it, using only dossier evidence plus up to 3 new targeted searches. Must produce the "most likely way choosing this leads to regret."
2. **Steelman** — for the runner-up (and any option whose destination-only rank differs from its preference-adjusted rank): the strongest case that it should win. Rank inversions between the two scoreboards are the tournament's priority docket — they are exactly where preference-overfitting would hide.
3. **Frame-checker** — the anti-overfitting backstop. It sees the *original brief*, the angle map, and the final ranking — and answers one question: "Is there a plausible answer to the brief that this map could not have produced?" It checks: angles removed at Gate A whose removal now looks consequential; option-card critiques that flagged missed options which were never scouted; and whether the winner's dominance is an artifact of the rubric (would a defensible alternative weighting change the winner?). If it finds a credible gap, it emits a **re-divergence proposal** (a specific new angle or scouting task with an estimated cost), which the orchestrator surfaces at Gate C rather than auto-executing.

A `judge` agent then updates scores where the tournament surfaced decisive material, logging every score change with its cause.

**Artifact:** `tournament/{option}-prosecution.md`, `tournament/steelman.md`, `tournament/frame-check.md`, `tournament/score-updates.yaml`.

### Gate C — Contender review & feedback loop

You read the contender pack: dossier summaries, prosecution/steelman highlights, the dual ranking (destination-only vs preference-adjusted), and any re-divergence proposal. Your actions, written to `gates/gate-c.yaml`:

- **Preference feedback** — structured reactions per contender ("the ops burden of A bothers me more than I expected", "B's timeline is actually fine"). The screener converts these into preference-slot adjustments and **re-scores** — cheap, no new research.
- **Evidence challenge** — "I don't believe claim X in dossier B" → targeted verifier/analyst task.
- **Accept re-divergence** — approve the frame-checker's proposal → a mini-loop of Stages 1–6 scoped to the new region, budgeted separately.
- **Approve** → Stage 8.

This gate implements your "share preferences and feedback on contenders, it researches more" interaction — but as bounded, typed loops rather than open-ended chat, so each iteration has a known cost and a defined effect on the artifacts.

### Stage 8 — Synthesis

**Agent:** `synthesist` (largest model, permitted to read everything including preferences).

Produces `report/decision-report.md`:

1. Recommendation with the decisive reasons (traceable to dossier claims).
2. The decision matrix (all finalists × all criteria, with confidence bands).
3. **Sensitivity analysis** (computed in code, narrated by the agent): which weight changes flip the winner, and by how much; how the ranking shifts as the preference-slot weight sweeps 0% → 40%. If the winner is fragile to plausible weight changes, the report must say so prominently.
4. The dissent: the prosecution's best surviving argument against the winner, unrebutted if it wasn't rebutted.
5. Residual uncertainty register: open questions, budget-capped areas, and what new information should trigger revisiting.
6. Next actions.
7. Appendix: the angle map, the allocation table, cut-option audit trail, verification pass rates, and total spend by stage.

A final mechanical **citation pass** links every factual claim in the report body back to a dossier claim and its source.

---

## 6. Cross-cutting quality machinery

- **Schema validation as a hook.** Every artifact type has a schema (YAML/JSON Schema for structured files; required-section checks for markdown). The orchestrator validates on stage completion; failures are returned to the producing agent with the validation error, max 2 retries, then the run pauses with a human-attention flag. Enforced via SDK `PostToolUse`/`Stop` hooks plus orchestrator-side checks — quality gates that cannot be talked out of.
- **Preference quarantine as a hook.** A `PreToolUse` hook on Read denies any access to `preferences.yaml` unless the active agent is `screener` or `synthesist`. The quarantine is enforced by code, not by prompt goodwill.
- **Source hygiene.** Fetched sources are cached to `sources/` (content-hashed) so verifier re-fetches are cheap and the run is auditable offline. Every source record carries tier, retrieval date, and URL. Web content is treated as untrusted input: agent prompts instruct that instructions found inside fetched pages are data, never directives, and hooks strip tool-call-like patterns from cached source text before it re-enters any context.
- **Contradiction ledger.** Whenever two artifacts disagree on a fact, the detecting agent appends to `ledger/contradictions.md` instead of silently picking one; the verifier adjudicates entries each round. Unresolved contradictions surface in the report.
- **Model mix.** Orchestration is code (free). Merger, rubric-builder, judge, frame-checker, synthesist: strongest available model (Opus-class) — these are the leverage points where reasoning quality compounds. Cartographers, scouts, analysts, prosecutors: Sonnet-class — parallel workhorses. Schema fixing, deduplication assists, citation checking: Haiku-class. This mirrors the configuration Anthropic reported as strongly outperforming a single top-model agent, and it spends your token budget where your three properties actually live.

---

## 7. Workspace layout

```
runs/2026-07-01-senior-project/
├── config.yaml            # run profile, budgets, γ, thresholds, model mix
├── state.json             # orchestrator checkpoint: stage, spend, retries
├── brief.md               # S0
├── destination.md         # S0
├── preferences.yaml       # S0 (quarantined)
├── angles/                # S1: map.yaml, map-report.md, per-cartographer raw
├── gates/                 # gate-a.yaml, gate-b.yaml, gate-c.yaml
├── allocation.yaml        # S2
├── options/{angle}/       # S3: cards.yaml, critique.md
├── rubric.yaml            # S4 (+ rubric-rationale.md)
├── screening/             # S5: scores.yaml, shortlist.md
├── dossiers/              # S6: {option}.md, {option}-verification.md
├── tournament/            # S7
├── report/                # S8: decision-report.md
├── sources/               # cached fetched content, content-addressed
├── ledger/                # contradictions.md, score-updates log
└── logs/                  # per-agent transcripts, spend per invocation
```

The workspace is a git repo per run: every gate decision and agent output is a commit, giving free diffing ("what changed after my Gate C feedback?") and rollback.

---

## 8. Technical implementation

**Substrate: Claude Agent SDK (Python).** It provides exactly the primitives this design needs — programmatic subagents with isolated fresh contexts (the prompt string is the only parent→child channel, which *enforces* the artifact-as-contract discipline: agents literally receive file contents, not conversation history), lifecycle hooks for the quarantine/validation/audit enforcement, per-invocation cost reporting for budget accounting, session resumability, and built-in WebSearch/WebFetch/Read/Write tools. The alternative — raw Messages API plus hand-rolled tool loop — rebuilds all of this for no benefit; heavier frameworks (LangGraph etc.) add abstraction without adding any capability this design uses.

**Orchestrator shape.** A single `orchestrator.py` implementing an explicit state machine over the stage enum. Per stage: load + validate inputs → construct agent contracts (prompt template + injected file contents + budget) → dispatch via `query()`/`ClaudeSDKClient` with an asyncio semaphore for concurrency (default 4) → validate outputs → evaluate stop rules → write artifacts + `state.json` → advance or pause. Gates are just a paused state; `deeper resume <run>` re-validates the gate file and continues. Crash recovery is trivial because state is files.

**Agent definitions.** One directory, `agents/`, with a markdown prompt file per role (versioned in git — prompt iteration is the main ongoing tuning activity, per Anthropic's experience that prompt engineering dominates multi-agent behavior). The orchestrator assembles each contract as: role prompt + task-specific objective + schema (inlined) + injected inputs + explicit budget statement ("you have ~N searches; spend them on X first").

**Budget accounting.** Each subagent result's reported cost/token usage is logged against its stage and angle/option. The orchestrator refuses to dispatch past a stage's cap and reports spend at each gate. Size classes (S/M/L) map to (model, max searches, max output) tuples in `config.yaml`.

**Run profiles.** `quick` (≈ sanity pass: 3 cartographers, floor 1, shortlist 3, 1 dossier round), `standard` (the defaults above), `exhaustive` (6 cartographers, floor 3, γ=0.8, shortlist 7, deeper dossier caps). Expect a `standard` run on a meaty question to cost the same order as ~15–30 heavy Deep Research queries; that is the deliberate trade you specified, and the profiles are how you keep it proportionate to the stakes of the question.

**CLI surface (v0–v1).**
`deeper new "<goal>"` → runs S0 interactively → pauses at Gate A.
`deeper status <run>` / `deeper resume <run>` / `deeper rerun <run> --stage S3 --angle "x"` (surgical re-execution) / `deeper report <run>`.

**Viewer (v2, optional).** A single-process FastAPI app that reads the workspace and renders: angle map (tree + priors), option cards (grid, filter by angle), rubric editor, contender comparison (side-by-side dossier summaries + dual ranking + sensitivity chart), and gate action forms that simply write the gate YAML files. No database, no state of its own — close the tab and nothing is lost. This is where your Option 2's ergonomics arrive, ~200 lines of Python + one page of HTMX/React, after the engine already works.

---

## 9. Development roadmap

**M0 — Prompt-lab in Claude Code (2–4 evenings).** Before writing the orchestrator, prototype the *agents* as Claude Code slash commands against a real question (use the senior-project decision). Goal: converge on the angle-map schema, option-card schema, and cartographer personas — the artifacts are the hard design surface, and iterating them is 10x faster interactively. Deliverable: `agents/` prompt files + `schemas/` that the real system will import unchanged.

**M1 — Kernel happy path (1–2 weekends).** Orchestrator state machine, S0→S5 with gates A/B as file-edits, schema validation, budget accounting, resumability. No tournament, no verifier yet. Exit test: a full run on a mid-size question produces an auditable shortlist you actually trust more than a single Deep Research pass.

**M2 — Depth & adversarial layer (1–2 weekends).** S6 analysts with the stability stopping rule, verifier, S7 tournament, frame-checker, Gate C loops, S8 synthesis with code-computed sensitivity analysis.

**M3 — Hardening & evaluation (ongoing, light).** The eval harness (below), spend dashboards per stage, prompt iteration against observed failure modes — Anthropic's core lesson is that this iteration loop, not the architecture, is where multi-agent quality actually comes from; expect several rounds.

**M4 — Viewer (optional, 1 weekend).** Only after M1–M3 prove the pipeline earns its cost.

---

## 10. Evaluation plan

You can't tune breadth/quality/depth knobs without measurement. Keep a small benchmark set (4–6 questions with different shapes: a personal decision, a technical selection, an open advice question, one where you already know the ground truth well). After each significant prompt/knob change, run `quick` profile and grade with an LLM-judge rubric scored per property:

- **Breadth:** count of distinct defensible angles vs a reference union (built once per benchmark question by an exhaustive manual + multi-tool pass); penalty for missing any angle a domain practitioner would consider obvious.
- **Informedness:** rank-correlation between allocation and post-hoc angle value (did budget go where the finalists came from — without collapsing the floor?).
- **Quality:** critic-pass revision rate and schema-failure rate per angle (falling over time = prompts improving).
- **Depth:** verifier pass rate; fraction of load-bearing claims at `high` confidence; count of `BUDGET-CAPPED` dossiers.
- **Anti-overfit:** does the destination-only ranking differ from preference-adjusted, and did the tournament examine every inversion?

Also A/B occasionally against plain Deep Research on the same question — the system must visibly beat it on these axes to justify its cost, and if it doesn't, the eval tells you which stage to fix.

---

## 11. Risks & mitigations

- **Subagent sprawl / runaway cost** — the community's loudest failure mode. Mitigated structurally: agent *count* is derived from allocation math, never chosen by an LLM; hard caps per stage; semaphore-limited concurrency; spend visible at every gate.
- **Orchestrator drift** — avoided by P8: no LLM ever controls sequencing.
- **Prompt injection via fetched web content** — untrusted-input framing in every research agent's prompt, source-cache sanitization, and no research agent has Write access outside its own artifact directory; none has Bash.
- **Gate fatigue** — three gates is the ceiling, and Gate B collapses to a 60-second weight check for most runs. If a gate consistently gets rubber-stamped, demote it to a notification in config.
- **Schema rigidity strangling agents** — schemas constrain *structure*, not content length or reasoning; each schema has a free-form `notes` field so agents can flag what the schema didn't anticipate, and those notes feed schema revisions.
- **Over-engineering (the meta-risk)** — the roadmap is ordered so the system is useful after M1; every later milestone must pay for itself on the benchmark before you build the next.

## 12. Diminishing-returns policy (summary)

| Loop | Expansion driver | Stop condition | Hard cap |
|---|---|---|---|
| Cartography | Heuristic ensemble | Marginal novelty < 0.2 over last 2 agents | 8 cartographers |
| Scouting per angle | Allocation units | Critic redundancy flag > 40% | Allocation + reflow pool |
| Screening | — | Fixed shortlist size w/ angle-diversity guardrail | 7 finalists |
| Deep dive per option | Research rounds | Δscore < 0.15 AND no load-bearing low-confidence claims | Per-option unit cap |
| Tournament | Rank inversions & top-3 | Fixed roles, 3 new searches each | 1 judge update round |
| Re-divergence | Frame-checker proposal | Human approval at Gate C only | 1 mini-loop per run |
| Gate C feedback | User | Re-score is free; new research is typed & budgeted | 3 loops, then decide |
