# M1 live smoke run — triage notes (feed to Prompt 13)

Run: `runs/2026-07-07-which-vector-store-should-a-solo-develop` (quick profile,
live). Question: which vector store should a solo developer adopt for a
local-first personal RAG assistant on Windows (~1M chunks, Python, no cloud,
low-maintenance hobby project). Supervised end-to-end by Claude; the run
workspace itself is the full audit trail (gitignored, kept locally).

## What worked well (keep; don't regress)

- **S0 non-interactive quality.** From a rich one-line goal, the interviewer
  produced a correct brief, quarantined preferences, and an evidence-backed
  destination model (live WebSearch during S0 worked). Best single behavior:
  it *flagged the ambiguity it couldn't ask about* — "no cloud dependency
  preferred" recorded as a hard requirement with the reasoning and the
  downgrade option documented in `notes` for Gate A review.
- **Strategic-notes channel earned its place.** The rubric-weight notes
  (Windows-wheel availability, export-format-as-abandonment-insurance, Lindy
  maturity signal) and the reframe note ("~1M vectors is a few GB — the scale
  reward over-weights scale-solving engineering") were decision-grade insights.
- **Spend guard fired three times ($5 → $20 → $40), each a clean pause** with
  the spend table in `logs/`, exit 0, and `resume --max-spend-usd` continuing
  exactly where it left off. In-flight calls land in the ledger after the trip,
  so expect overshoot of roughly concurrency × per-call cost (observed $5.62 on
  a $5.00 cap).
- **No-crash hardening validated by a real SDK failure.** A scout dispatch
  died with `Exception: Claude Code returned an error result: success` (SDK
  stream quirk, not our code). The run paused with the full traceback at
  `logs/attention-*-S3-scout.md`; resume re-entered and re-ran only the
  missing sub-work.
- **Idempotent re-entry across pauses.** After each pause the engine skipped
  completed cartographers/merges/scout angles ("already valid — skipping",
  "merged map already covers all raw reports — skipping").
- **Live schema retries recovered silently** (interviewer ×1, merger ×2) —
  the retry loop works against real model output.
- **Gate A actions at scale.** A 30-removal + 1-addition + 1-prior-adjustment
  decision applied cleanly in one commit, every removal echoed with its reason.

## Findings to fix (ordered by pain)

1. **Cartography explodes on design-space decomposition.** The
   first-principles cartographer maps the *engineering design space* (index
   families, WAL durability, quantization, metadata filtering) instead of the
   *adoption space* (regions a scout can populate with adoptable options).
   The merger then accepts nearly all of it as distinct: 13 angles after pass
   one, 33 after two, **41 at Gate A** — third-pass novelty was still 1.00,
   so saturation never triggered and cartography ran to the 8-invocation hard
   cap. ~$13 spent before Gate A; 30 of 41 angles had to be removed by hand.
   Fix in prompts (`agents/cartographer-*.md`, `agents/merger.md`): define an
   angle as "a region an adopter could *choose*, populated by nameable
   options"; tell the merger to fold feature/criterion dimensions into
   strategic notes (rubric-weight kind) instead of keeping them as angles;
   consider a soft map-size target (5–15) in the merger prompt.
2. **An infeasible approved map crashes S2.** With 41 angles × floor 1 >
   B=16, `allocate()` raises and the engine has no handler — an unhandled
   traceback, violating the pause-don't-crash rule. Only my Gate A pruning
   avoided it. Fix in code: S2 should catch infeasibility and pause with
   "remove angles or raise total_budget_units", and/or Gate A approval should
   warn when the post-edit map is infeasible for the run's budget.
3. **The `error result: success` SDK failure was output-token exhaustion,
   and it was invisible.** Three S5 dispatch failures in a row reported only
   `Exception: Claude Code returned an error result: success` — the real
   cause (found in the subagent's CLI session log: `stop_reason: max_tokens`,
   then "response exceeded the 32000 output token maximum, set
   CLAUDE_CODE_MAX_OUTPUT_TOKENS") never reached our transcript. Two fixes:
   (a) **landed during the run**: `LiveDispatcher` now passes the size-class
   `max_output_tokens` as `CLAUDE_CODE_MAX_OUTPUT_TOKENS` into the subagent
   env (`_live_options`, tested) — the knob existed in config since Prompt 4
   but was never plumbed; (b) **for Prompt 13**: wrap dispatch failures with
   the CLI result subtype/detail when available, and add retry-with-backoff
   for the genuinely transient class (one S3 scout failure of the same shape
   succeeded on plain re-resume). Keep the pause as the final fallback.
3b. **Single-call screening does not scale — fixed during the run.** 52
   options × 7 criteria needs a >64k-token reply (hit the model output
   ceiling even after the env fix, then the CLI hung for 50 minutes); one
   mega-call also re-pays the whole prompt on every retry. **Landed:** S5 now
   dispatches one screener batch per angle (mirroring S3's fan-out), verifies
   integrity per batch, filters over-scoped options, and merges in code with
   a cross-angle duplicate-id guard; mock full-fixture fallback handled by
   the filter. Remaining for Prompt 13: see finding 7 (persist batches).
4. **Quick-profile cost reality vs the $5 default cap.** This run cost ~$13
   to Gate A and ~$31+ through S3 — the saturation loop multiplied
   cartography (8 invocations, and the merger re-reads *all* raw reports each
   expansion pass: $0.84 → $1.62 per merge). Either quick should constrain
   expansion (e.g. cap cartography passes at 1 expansion for quick), shrink
   size-class search budgets, or ship a more honest default cap (~$25–40).
   Revisit after the cartography prompt fix — a sane map may halve total cost.
5. **Failed-but-recovered retry attempts leave no artifact.** Retry counts
   persist, but the invalid outputs that triggered them are discarded, so
   there's nothing to study for prompt iteration (the §10 quality metric
   wants schema-failure rates *and* causes). Log every failed attempt to
   `logs/retries/` even when the retry succeeds.
5b. **Failed dispatches are not ledgered.** A dispatch that dies in flight
   (all three S5 failures, each a full ~50-card prompt) records no
   SpendEntry — the tokens were consumed but the ledger undercounts them.
   Record a SpendEntry (estimated or zero-cost marker) for failed attempts
   too, so the audit trail shows the money went somewhere.
6. **Cross-angle option-id collisions are real.** Two scouts independently
   carded `sqlite-vec` (it genuinely belongs to both the SQLite-extension and
   pure-Python angles); the S5 merge guard (added during the run) caught it
   and paused. Resolved by hand-renaming one card id. Fix for S3: make the
   option-id namespace global — auto-suffix duplicates at cards.yaml write
   time (deterministic code), and/or note in the S5 guard message that
   renaming a card id is the cheap fix (not a full angle re-scout). Overlap
   like this is also signal the two angles partially overlap — worth
   surfacing to Gate A.
7. **S5 batch results are not persisted per angle.** All 12 screening
   batches completed and were paid for, then the merge-time collision threw
   everything away — resume re-paid all 12. Persist per-angle batch results
   (like S3's per-angle cards) and skip valid ones on re-entry.
8. **Windows `isatty` quirk:** `deeper new < NUL` still looks interactive
   (NUL is a character device), asks the confirm question, and declines on
   EOF. Harmless but surprising; a `--non-interactive` flag would make the
   mode explicit instead of stdin-shape-dependent.
9. **Console mojibake:** em-dashes render as `�` under cp1252 consoles
   (git-bash default). Cosmetic; consider ASCII-safe punctuation in emit
   strings or forcing UTF-8 output.

10. **ADDRESSED — The shortlist rule does not concentrate: 26 "finalists"
   out of 52.** Quick profile, shortlist_size 3 — and 26 options advanced,
   all via `ucb-above-threshold`; only 2 options fell below the 3.5 bar. Two
   compounding causes: (a) *screener optimism/band inflation* — average band
   half-width 0.816 on a 1–5 scale, so nearly every survivor's UCB clears the
   threshold (the dark-horse mechanism degenerates into "advance everyone"
   when every band is wide); (b) *by design nothing truncates to k*, and with
   12 angles × cap 3 the angle cap barely binds. S6 would build 26 dossiers.
   **Fixed on both sides:** the screener prompt now demands the anchored
   levels as written (most options 2–3 on most criteria), bands as genuine
   screening uncertainty (±0.25 when evidence is clear, wide only when truly
   thin — uniformly wide bands defeat the dark-horse mechanism), and carries
   an S6 calibration-accountability line; and `stages/shortlist.py` now
   advances the top `shortlist_size` by UCB plus any option within
   `shortlist_dark_horse_margin` (0.25) of the k-th finalist's UCB, floored
   at the absolute threshold and hard-capped at `caps.max_finalists` (new cut
   cause `below-cutoff`; recorded as a Design deviation in the README).

Final cost: **$65.20** total — S0 $0.75, S1 $12.34 (41-angle bloat), S3
$21.99, S4 $1.32, S5 $20.06 (~$12 of which was re-paid discarded batches),
plus unledgered failed dispatches (finding 5b). A quick-profile run with the
cartography fix and batch persistence should land nearer $20–25.

**ADDRESSED — Billing.** This run authorized through the Claude Code CLI's
stored login (no ANTHROPIC_API_KEY in the environment), so the dollar figures
above are the SDK's API-equivalent estimates metering plan usage — but that
was accidental: a key in the environment would silently have taken precedence
and billed a metered API account. Now guaranteed: `RunConfig.billing`
defaults to `subscription` — the dispatcher blanks ANTHROPIC_API_KEY in every
subagent env (the CLI treats empty as unset and falls back to the login) and
refuses dispatch if a subagent reports any other auth source; `billing: api`
opts into metered billing and fails fast without a key. `deeper doctor`
describes subscription auth as the default path.

## Shortlist judgment (M1 exit question)

Design §9 asks: does a full run produce *an auditable shortlist you trust
more than a single Deep Research pass*?

**On content: yes, clearly.** The top of the ranking is terrain-correct and
non-obvious: `sqlite-fts5` (lexical-only, no vectors at all) ranked #1 at
point 4.76 — the run independently concluded that a personal notes/code
corpus may not need ANN at all, exactly the contrarian challenger a plain
"best vector DB" search never surfaces (note: that angle entered at Gate A
as a human addition — the gate mechanism, not luck). The rest of the top 10
is coherent boring-technology: flat brute-force rebuild, numpy memmap,
faiss-cpu, sqlite-vec — all defensible at ~1M vectors ≈ a few GB, which the
run's own reframe note established. The kill list is the strongest part:
20 options eliminated on *confirmed, specific, checkable* facts (milvus-lite
has no native Windows support; duckdb-vss persistence is experimentally
gated; libSQL's vendor pivoted) — several with UCBs that would have ranked
top-5. A single Deep Research pass would have recommended one of the very
products this run kill-listed, without the receipt.

**On form: no — 26 finalists is not a shortlist** (finding 10). The audit
trail is decision-grade; the concentration step is not yet. Verdict: the
kernel earns M1 (every mechanism worked live, failures paused instead of
crashed, gates changed the outcome), with finding 10 as the first
prompt-quality target before S6 exists to pay 26-dossier costs — since
addressed (see finding 10 above): the screener is calibrated and the
shortlist rule concentrates to at most `caps.max_finalists` regardless of
band width.
