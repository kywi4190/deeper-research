# Benchmark seeds — pre-authored content for Prompt 14's `benchmarks/`

Prompt 14 step 1 defines the benchmark spec format and seeds four specs per
design §10's shapes. The content below is pre-authored so **no TODO-USER
placeholders remain**: every question is real (drawn from the two live runs
and the user's own run artifacts — nothing invented), and the ground-truth
notes record what is actually known vs. what the user must still adjudicate.
Transcribe each seed into the spec format Prompt 14 defines; keep the
distinction between *organic* and *human-added* reference angles — it is
load-bearing for the breadth metric.

---

## Seed 1 — `probe-space-mapping` (type: personal-decision-known-ground-truth)

The design's "one where I know ground truth" shape; doubles as the personal
decision that has already been run end-to-end.

- **Question (verbatim goal string):** "Map the space of first hands-on
  AI/ML research 'probe' projects runnable over the next few weeks
  (evenings/weekends, ~3-4 weekends each), and recommend which to run
  first — where the chosen first probe must do double duty: serve as an
  instrumented diagnostic on the user's own research-direction uncertainty,
  and produce a linkable artifact that seeds/aligns with the undergraduate
  research project he will propose to CU Boulder faculty this fall."
- **Profile:** standard. **Fixtures:** the completed run
  `runs/2026-07-13-map-the-space-of-first-hands-on-ai-ml-re` (kept locally,
  gitignored) — its brief.md / destination.md / preferences.yaml are
  reusable as S0 fixtures; the full workspace is the comparison artifact.
- **Reference angle union** (16 = 15 organic + 1 human-added):
  mechanistic-interpretability, from-scratch-build, paper-reproduction,
  empirical-phenomenon-study, theory-proof-note,
  theory-empirics-verification, fine-tuning-adaptation, rl-probe,
  eval-artifact-construction, explainer-exposition,
  open-source-contribution, competition-leaderboard,
  curriculum-anchored-extension, repurpose-existing-work,
  diagnostic-sampler, **llm-agent-systems-build** (human-added at Gate A —
  the "practitioner-obvious miss" test case: a fresh run's map should be
  scored on producing it WITHOUT human help; M2 notes F3). Also flag:
  frontier/speculative framings are expected to be under-covered at
  standard (no horizon-scanner in the 5-persona ensemble).
- **Ground truth (user's withheld prior):** expected winner = a
  from-scratch nanoGPT *fork test* with compression framing (engineered to
  measure the build/understand axis by revealed choice at a designed
  fork); grokking/induction-heads interpretability as understand-side
  runner-up.
- **Actual M2 outcome:** the fork test was never carded (option-level
  scouting miss — "compression" appears in zero cards; M2 notes Trap 2
  verdict); winner relu-depth-width-expressivity (theory proof note),
  interp probes #2/#3, robust across the full sensitivity sweep.
- **Eval checks this seed supports:** (a) breadth at ANGLE level vs the
  union above; (b) breadth at OPTION level — was a compression-framed
  fork-test option carded at all?; (c) ranking — whether the fork-test
  prior or the theory-side answer is right is the **user's adjudication**,
  not a settled fact; the spec should record both.
- **Cost calibration (standard):** S0–S1 ≈ $7.04, S0–S4 ≈ $34.27, complete
  ≈ $110.43.
- **Baseline slot:** empty — the user may later paste a plain Deep
  Research answer to the same question for `eval --compare-baseline`.

## Seed 2 — `vector-store-selection` (type: technical-selection)

- **Question (verbatim from the M1 run):** "Which vector store should a
  solo developer adopt for a local-first personal RAG assistant on Windows
  (~1M chunks, Python, no cloud, low-maintenance hobby project)?"
- **Profile:** quick. **Fixtures:** the M1 run
  `runs/2026-07-07-which-vector-store-should-a-solo-develop` (kept locally;
  **pre-M2 artifact formats** — use for the angle union and qualitative
  comparison, do not expect current schemas to load every artifact).
- **Reference angle union** (12, the post-Gate-A map after the 41→12
  prune): embedded-purpose-built-vector-db,
  embedded-general-db-vector-extension, server-dbms-vector-extension,
  local-server-vector-db, bare-ann-index-library,
  exact-brute-force-search, hybrid-lexical-vector-search,
  pure-python-no-build-toolchain, ephemeral-rebuild-from-source,
  turnkey-personal-ai-appliance, hand-rolled-minimal-dependency-index,
  lexical-only-retrieval.
- **Ground truth:** partial. M1 ran S0→S5 only (kernel milestone): there is
  an audited shortlist with kill receipts but no tournament winner. The
  M1 strategic notes are quality anchors a good run should rediscover:
  Windows-wheel availability as a rubric weight, export-format as
  abandonment insurance, the "~1M vectors is a few GB — scale reward
  over-weights scale-solving engineering" reframe.
- **Baseline slot:** empty (as above).

## Seed 3 — `faculty-targeting-decision` (type: personal-decision, no ground truth, NOT yet run)

Derived from the M2 run's own second-order-effects finding: probe #1
naturally narrows faculty targeting, and the brief says targeting must NOT
narrow at the input stage — making the targeting choice a real, live
decision the user faces after probe #1 completes.

- **Question:** "Given a completed first research probe (a scoped
  theory/proof-note artifact) and an early-August outreach deadline: decide
  the faculty-targeting strategy for the CU Boulder undergraduate research
  proposal — target theory/learning-theory labs aligned with probe #1's
  artifact, target interpretability/empirics labs to counterweight it, or
  stay subfield-open and filter on rigor + undergrad track record only —
  and recommend how many faculty to approach in the first wave."
- **Profile:** quick (it is a bounded decision, not a mapping question).
- **Reference angle union:** none yet (first run of this question seeds
  it).
- **Ground truth:** none — score on property metrics only (breadth,
  informedness, quality, depth, anti-overfit).
- **Baseline slot:** empty.

## Seed 4 — `cold-email-advice` (type: open-advice)

Grounded in the M2 run's own evidence trail (the cold-email evidence-norm
claims in the implicit-bias-max-margin dossier, one of which the verifier
contradicted — so the pipeline has already touched this question's
evidence base once, badly, which makes it a good open-advice probe).

- **Question:** "How should a CS + applied-math undergraduate approach
  cold-emailing faculty about joining undergraduate research — timing
  relative to the semester, what evidence/artifacts to attach or link,
  how to frame prior self-directed work, and how many emails to send —
  when the goal is a research position with a rigor-focused advisor?"
- **Profile:** quick.
- **Reference angle union:** none yet (first run seeds it).
- **Ground truth:** none — open-advice shape; property metrics only. Note
  for the judge: the M2 run's contradicted claim
  (gate: cold-email evidence norms, "attach vs. don't attach" sources
  disagree) is a known live disagreement in the evidence base — a good run
  should surface it as residual uncertainty, not resolve it by fiat.
- **Baseline slot:** empty.
