# M2 live run — triage notes (feed to Prompt 14)

The Prompt 13 post-verification run: one full standard-profile live run,
all three gates worked for real, report read end-to-end against ground
truth. First attempt (2026-07-13, "map the space of first hands-on AI/ML
research projects") aborted in S0 — see finding 1 — run wiped (~$0.52
spent), to be restarted.

## Findings

1. **ADDRESSED — A multi-line paste into the S0 interview auto-answers later
   questions.** The interview read one line per answer (`input()`), so
   pasting a multi-line answer sent only its first line; the remaining lines
   sat buffered on stdin and each subsequent `input()` silently consumed one
   as the "answer" to a question the user never meant to answer that way —
   the interview derailed within two turns and the interviewer got visibly
   confused reconciling the disconnected answers. **Landed (2026-07-13):**
   the CLI's `ask_user` channel now drains input already buffered on stdin
   into the SAME answer (`_read_answer` in `cli.py`: `msvcrt.kbhit` on
   Windows / zero-timeout `select` elsewhere, with a 50 ms grace window for
   chunked paste delivery). A human cannot type a further full line inside
   the polling window, so buffered-input-after-Enter is reliably a paste;
   typed answers still submit on a single Enter. Interior blank lines
   (multi-paragraph pastes) are preserved; EOF mid-drain keeps what was
   read. Unit-tested with injected fakes in `test_orchestrator_cli.py`.

2. **ADDRESSED — A near-cap angle's single screener batch overflows the size
   class's output ceiling.** Restarted run (standard profile), S5: 88 cards
   across 16 angles. One angle's batch reply exceeded the M class's 16k
   output-token maximum — per-angle batching (the M1 finding-3b fix) bounds
   the *number* of batches, but a single angle allocated near the 25% cap can
   carry ~20 cards (~2× units), and 20 cards × every criterion × bands +
   evidence pointers is legitimately >16k tokens of YAML on any profile.
   Three Prompt 13 mechanisms visibly worked on the way down, worth keeping:
   the run paused cleanly (no crash), the enriched dispatch error showed the
   REAL cause in the terminal (`result: API Error: Claude's response exceeded
   the 16000 output token maximum` — in M1 this identical failure was an
   opaque `error result: success`; note the CLI reported `subtype='success'`
   WITH `is_error=true`, a shape worth remembering), and the persisted
   batches meant resume re-paid only the failed angle, not all 16.
   *Immediate mitigation used mid-run:* raise the M class's
   `max_output_tokens` 16000 → 32000 in the run's `config.yaml`, resume.
   **Landed (2026-07-14), the durable fix:** an angle with more than
   `screener_batch_max_cards` (new config knob, default 10) cards is
   screened in balanced sub-batches (`chunk_cards`: fewest chunks of ≤ max,
   near-equal sizes, deterministic), each integrity-checked against exactly
   its own cards and persisted at `screening/batches/{angle}.partN.yaml` the
   moment it passes, then merged in code into the same settled
   `screening/batches/{angle}.yaml` — reply size is bounded by construction
   instead of racing per-profile ceilings, and the on-disk contract is
   unchanged (a mid-flight run's already-settled batches stay valid). A
   crash mid-angle resumes from the completed parts.

3. **ADDRESSED — Even a sub-batched (≤10-card) screener reply overflows a
   16k M-class ceiling.** Restarted run, S5 resume (2026-07-15): with the
   finding-2 sub-batching active (terminal confirmed "angles over 10 cards
   sub-batched"), a screener dispatch still died on `Claude's response
   exceeded the 16000 output token maximum`. So 10 cards × every rubric
   criterion × bands + evidence pointers does not reliably fit in 16k when
   the cards are verbose — the finding-2 estimate ("~20 cards > 16k") was
   optimistic by ~2×. Also confirmed the run had never picked up the 32k
   mitigation: the run's own `config.yaml` (the only source the dispatcher
   reads — `_live_options` injects it as `CLAUDE_CODE_MAX_OUTPUT_TOKENS`)
   still carried M=16000; the mid-M2 hand-edit applied to the aborted first
   attempt's workspace, and the shipped `standard` profile still defaulted
   to 16000. *Mitigation used mid-run:* same hand-edit, M 16000 → 32000 in
   this run's `config.yaml`, resume. **Landed (2026-07-15), the durable
   fix:** live profiles now ship M-class `max_output_tokens` = 32000
   (`standard` 16000 → 32000, `exhaustive` 24000 → 32000 so the deeper
   profile is never below standard; `quick` deliberately untouched — its
   tight ceilings are the sanity-pass trade-off). The ceiling is a runaway
   guard like the spend cap: output tokens cost nothing unless produced.
   Pinned by `test_live_profile_m_class_ceiling_fits_a_full_screener_batch`.
   Existing runs keep their own `config.yaml` — a mid-flight run wanting the
   new ceiling needs the one-line hand-edit.

4. **ADDRESSED — The S6 re-score demands an `angle_id` its inputs never
   state.** Restarted run, S6 round 1 (2026-07-16): the
   'attention-backprop-hand-derivation' re-score failed the coherence check
   with `carries angle_id 'theory-derivation' but its card belongs to
   angle 'from-scratch-build'` and paused the run. Root cause is a contract
   gap, not a flaky agent: the re-score inputs are brief + destination +
   rubric + dossier + preferences, and NONE of them carry the option's
   angle — the dossier schema has no angle field — while the screener
   prompt's own rule said "every angle_id must match the cards" in a mode
   with no cards. The model had to guess, and guessed the semantically
   obvious angle for a hand-derivation option. (Correction while triaging
   finding 5: the pause message's "failed 3 times" was the retry cap, not a
   count — a stage-level coherence check paused after ONE dispatch, so one
   wasted M-class dispatch, not three.) The other six
   finalists passed by luck: their names telegraph their angles.
   **Landed (2026-07-16):** the angle is S3 bookkeeping code already knows
   (`finalist.baseline.angle_id`), so per P8 it is never the agent's to
   decide — `_rescore` now states it in the task objective, and a
   mismatched echo is corrected in code with an emitted notice instead of
   pausing the run; `agents/screener.md` re-score mode says to echo the
   objective's angle, not infer one. Covered by
   `test_rescore_corrects_an_agent_guessed_angle_id` (stub dispatcher
   returns a plausible-wrong angle; the re-score survives, corrected).
   Both the round-loop and the post-revision final re-score share the
   `_rescore` path, so one fix covers both.

5. **ADDRESSED — Coherence checks pause the run on the first miss; the
   feedback retry loop never sees them.** Restarted run, S6 verification
   (2026-07-16): the 'implicit-bias-max-margin' verifier adjudicated 13 of
   its 14 sampled claims — high-quality work, two `unsupported` verdicts
   catching a real mis-citation, one `contradicted` — but silently dropped
   'c-fanpu-sketch-scope' (its own counts said 12+2=14, so it believed it
   was done) and the run paused. The structural gap: the dispatcher's
   retry-with-feedback loop (`_RETRY_TEMPLATE`) covered only
   `parse_artifacts` schema failures, while every stage-level coherence
   check raised `AgentOutputInvalid` straight to the engine after a single
   dispatch — the one kind of failure where feedback ("you missed exactly
   this claim") is most likely to fix the reply on the next attempt was the
   one kind that never got a retry. The pause message ("failed schema
   validation 3 times") printed the cap, not the truth.
   **Landed (2026-07-16):** `run_agent` takes an optional `validate`
   callback — a stage-owned coherence check returning an LLM-facing error
   report or None — run INSIDE the retry loop, so semantic misses get the
   same corrective feedback (previous output + errors) as schema failures,
   every attempt ledgered and logged to `logs/retries/` as before. The S6
   verifier's sampling-assignment check is wired through it.
   `AgentOutputInvalid` now carries `attempts` and the engine's pause
   message reports the true count. Covered by the dispatch-level
   validate-callback tests and
   `test_verifier_missing_sampled_claim_is_retried_with_feedback` (first
   reply missing one sampled claim, exactly the live shape; second attempt
   sees the feedback and completes). *Candidate follow-up:* wire the other
   single-shot coherence checks (S5 screener batches, S6 dossier checks,
   S7 prosecution/steelman, S8 report, Gate-C/redivergence) through
   `validate` too — deliberately not done in the same change, to keep the
   mid-flight run's blast radius to the stage it is actually in.

6. **OPEN (Prompt 14) — Review the verifier report itself: the analyst
   mis-cited a load-bearing source and built on it.** Same verification as
   finding 5 (`dossiers/implicit-bias-max-margin-verification.md` in the
   run workspace once the resume settles it; the last attempted report is
   in `logs/attention-20260716T165724Z-S6-verifier.md`). The verifier's
   content is exactly what P5 pays for and worth reading end to end:
   - Two load-bearing claims (`c-ji-telgarsky-proof`,
     `c-tight-rate-ji-telgarsky`) cite arxiv.org/abs/1806.01796 as "Ji &
     Telgarsky" — it is actually a Nacson/Srebro/Soudry SGD paper. The
     verifier's notes say the error propagates through every dossier
     section resting on those claims (instrumentation-preregistrability,
     completion-confidence, failure_modes, self-directed-initiative,
     comparable_cases). Only `contradicted` triggers the one targeted
     revision (`s6_deepdive.py` filters on `Verdict.CONTRADICTED`), and S8
     hands the synthesist only the aggregate `pass_rate_table` — so an
     `unsupported` verdict on a LOAD-BEARING claim reaches the report as
     nothing but a lowered percentage. The wrong citation survives into
     S7/S8 unless a human intervenes at Gate C.
   - One `contradicted` (`c-cold-email-evidence-norm`) and one
     half-`unsupported` (`c-theory-checklist-sanity-check`: the source
     never says what the claim's second half asserts).
   For Prompt 14: (a) the analyst prompt should demand citation-identity
   checks when a claim names authors — title/author match against the
   fetched source, not just a live URL; (b) consider whether `unsupported`
   verdicts on LOAD-BEARING claims deserve more than silent survival —
   e.g. the revision leg, a Gate-C surfacing, or at minimum a forced note
   in the S8 residual-uncertainty register; (c) the verifier also worked
   around a 403 (CACM) via a corroborating lookup and an unreadable PDF
   via Read on the cached binary — prompt guidance that already works,
   worth keeping verbatim.

7. **ADDRESSED — "Fatal error in message reader": the SDK's 1MB
   stdout-message buffer kills a dispatch when one streamed tool result
   exceeds it.** Restarted run, S6 (2026-07-16 ~18:00Z): two scary `Fatal
   error in message reader: ... JSON message exceeded maximum buffer size
   of 1048576 bytes` lines mid-run. The line itself is just the SDK's
   `logger.error` leaking to the terminal; the Prompt 13 machinery did its
   job — each death became a transient `LiveDispatchError`, was ledgered as
   a zero-cost failed marker, backoff-retried, and the run never paused
   (the other parallel finalists' progress kept printing in between, which
   is why it looked interleaved and alarming). Root cause: the SDK reads
   the CLI's stdout as newline-delimited JSON with a 1MB per-message buffer
   (`_DEFAULT_MAX_BUFFER_SIZE`), and a research agent's WebFetch of a big
   page/PDF flows through as one such message. The rub is in the ledger:
   the `implicit-bias-max-margin` verifier died on the SAME >1MB fetch
   twice in a row (18:01, 18:04) before attempt 3 hit the session limit —
   the failure is near-deterministic when the agent re-walks the same
   source, so "transient" backoff retries mostly re-pay the same death.
   **Landed (2026-07-16):** `_live_options` now passes
   `max_buffer_size=MAX_SDK_MESSAGE_BYTES` (32MB) to `ClaudeAgentOptions` —
   clears any plausible tool result, still bounds a runaway stream.
   Asserted in `test_live_options_enforce_size_class_budgets`. For Prompt
   14's context: nothing needs prompt work here, but when reading ledgers
   remember failed markers at the same context minutes apart are usually
   ONE cause retried, not independent flakes.

8. **ADDRESSED — A live session-limit notice slipped the detector and wore
   the wrong pause.** Restarted run, S6 (2026-07-16T18:07Z, attempt 3 of
   the finding-7 verifier): the CLI said `You've hit your session limit ·
   resets 2pm (America/Denver)` — a plan-limit pause — but the run paused
   as a generic `dispatch failed — inspect the saved transcript, fix the
   cause`, because `_LIMIT_TEXT_RE` knew "usage limit hit", "session limit
   reached", and "reached your session limit", and this third live shape
   says "HIT your session limit". Consequences were mild but real: no
   reset-time instruction in the pause message, and had the notice arrived
   one attempt earlier it would have burned backoff retries rediscovering
   the limit. This is exactly the drift M1 finding 11 predicted when it
   made detection "deliberately broad over the known families" — the
   families keep growing. **Landed (2026-07-16):** the third family
   (`(?:reached|hit) your (?:usage|session|plan|weekly|daily) limit`) added
   to `_LIMIT_TEXT_RE`; `_LIMIT_RESET_RE` stops at `]` so a reset time
   embedded in the enriched LiveDispatchError's bracketed CLI detail comes
   out clean (`2pm (America/Denver)`, not `2pm (America/Denver)]`).
   Live-confirmed shape pinned in
   `test_usage_limit_notice_recognizes_known_shapes`. Also observed in the
   same window, NOT actionable: a burst of `Event loop is closed` /
   `unclosed transport` tracebacks after the pause message — CPython's
   Windows Proactor teardown racing subprocess-transport GC at interpreter
   exit, cosmetic, prefixed "Exception ignored in:" (Python is saying it
   ignored them).

9. **ADDRESSED — Bucket 3: subprocess lifecycle & observability (the
   terminal-noise class).** Holistic triage of the run's error history
   sorted everything into three buckets: (1) content contracts (findings
   2/4/5 — closed, `validate=` is the mechanism), (2) infrastructure
   ceilings & error taxonomy (findings on 16k, 7, 8 — closed), and (3) the
   one still open: everything alarming the terminal showed that was NOT a
   pipeline failure. Three structural causes, all violating
   pipeline-as-kernel/UI-as-viewer: the CLI subprocess *inherited the
   operator's stderr* (the minified-JS "Error in hook callback … Stream
   closed" dumps — the CLI complaining its pending hook control-requests
   died when an abandoned sibling's stream was yanked); the project
   configured no logging, so SDK records ("Fatal error in message reader")
   fell to logging's lastResort handler = the terminal; and all 9 stage
   fan-outs were bare `asyncio.gather`, which abandons siblings when one
   child raises a pause — their transports then closed by GC luck at loop
   shutdown ("Event loop is closed" Proactor spew). **Landed (2026-07-17,
   commits e4cf77d/b054e49/5f8d832 + the finding-5 follow-up in
   ab96db7/this):** `gather_strict` (deeper/aio.py) cancels-and-drains
   siblings and re-raises ONE deterministic exception (UsageLimitReached,
   then SpendCapExceeded, outrank per-dispatch deaths — the actionable
   pause wins the race); `_invoke` acloses the query() generator in a
   finally (PEP 533); subagent stderr is piped per attempt, persisted to
   `logs/stderr/` on failure with the tail in the enriched error;
   SDK loggers route to `logs/sdk.log` (WARNING+); win32 gets a 0.2s
   Proactor teardown grace; and every per-dispatch coherence check now
   rides `validate=` (S3, S5, S6, S7, S8, gate-C, redivergence — S8's
   bespoke one-citation-retry replaced by the shared budget, recorded as a
   README design deviation), with merge-level checks staying pause-on-fire
   by stated taxonomy. Operator's view from here on: emits only; every
   diagnostic lands in the run's `logs/`.

## Run complete (2026-07-17) — the M2 exit test

Winner `relu-depth-width-expressivity`, rank 1 on BOTH boards, no
inversions, stable across the full preference sweep 0-40%; dissent
UNREBUTTED (carried faithfully from the winner's prosecution); citation
pass clean over 105 indexed claim ids; frame-check PASS with no proposal;
judge applied zero updates with per-thread reasoning. Gate C approved
after review — rationale recorded in the run's `gates/gate-c.yaml`
(notable: the winner's prosecution was the strongest artifact in the pack,
built entirely from the verifier's own findings; the finding-6 mis-cited
claims sat at rank 5 and were noted, not re-challenged).

**What worked end-to-end:** every failure across the run paused resumably
and no paid work was re-paid (persisted batches, part files, round logs
all honored on resume); the adversarial layer produced genuinely decisive
material (a verifier contradiction forced a dossier walk-back that moved
the winner's initiative score; rademacher dropped 3.98 → 3.86 after its
3-claim revision); the final resume after the finding-9 hardening showed
an emit-only terminal.

**Final cost by stage** (`deeper status`; subscription plan-usage
API-equivalents, cap was raised 100 → 125 mid-run):

| stage | usd |
|---|---|
| S0 | $1.71 |
| S1 | $5.32 |
| S3 | $24.05 |
| S4 | $3.18 |
| S5 | $35.46 |
| S6 | $35.11 |
| S7 | $3.90 |
| S8 | $1.69 |
| **total** | **$110.43** |

Schema-retry hotspots for Prompt 14: S3 scouts (12 retries across
initial/revision/top-up passes) and S5 screeners (8) dominate;
S6/S7 nearly clean (3 combined); S8 zero. The remaining human leg of the
M2 exit test: read `report/decision-report.md` end-to-end against ground
truth.

## User triage through Gate B (F1–F12), merged and closed out post-run

The user kept independent triage notes through Gate B with **withheld
ground truth** (4 known probe candidates; expected winner: a from-scratch
nanoGPT *fork test* with compression framing, engineered to measure the
build/understand axis by revealed choice). Deduped against findings 1–9
above; post-run verdicts filled in from the completed workspace.

**Prompt-iteration targets (the heart of Prompt 14's loop):**

- **F3 — Cartography ground-truth miss: agent/LLM-systems region.** 3 of 4
  withheld probes had map homes; the scratchpad+memory agent build
  (build×empirics) did not — all five personas shared the frame "probe =
  study/train/adapt a model"; none produced systems-composed-around-a-model.
  The human added `llm-agent-systems-build` at Gate A; it was scouted and
  produced finalist #6. Cartographer-persona iteration target; add the
  region to the benchmark's reference angle union as the
  "practitioner-obvious miss" test case. (Horizon-scanner absent at
  standard — expected; note in the reference union.)
- **F9 — Rubric-builder defect: purity ≠ discrimination.** The S4 rubric
  was destination-faithful and quarantine-clean, but the build/understand
  criterion credited only pole purity and would have scored a *designed
  fork* as ambiguity (~2 on the axis it was engineered to measure). Fixed
  live at Gate B via `edited_criteria` (that gate path is now exercised).
  Iteration target: the rubric-builder must recognize
  revealed-choice-at-a-designed-fork as a measurement mechanism.
- **F6 — Critic revision rate 100% (16/16 angles).** Baseline for the
  quality metric; either scouts under-deliver on pass 1 or the critic
  over-triggers. Post-run texture: S5 kill-risk checks did real work (the
  two sampler options died on summed-hours math), so the critic is not
  uniformly noise — validate a sample of critiques against cards before
  choosing which prompt to iterate.
- **F7 — Allocation floor dominance.** 16 angles × floor 2 = 32 of 40
  units; only 8 proportional, so γ was inert (priors 0.85 and 0.65 both →
  3 units). Expect weak informedness rank-correlation in the eval;
  knob-tuning candidate: floor/γ/budget vs angle count at standard.

**Trap 2 verdict — the ground-truth divergence (the eval headline).** The
expected winner never existed as an option card: no scout produced the
compression-framed fork test ("compression" appears in zero cards; the
nearest neighbors are plain nanoGPT builds). Those neighbors screened
mid-field and were cut honestly by the finalist cap — every seated
finalist's S5-time UCB (≥ 4.3705) beat the best nanoGPT cut
(scaling-law-fit, 4.165); cut-reason texts verified against the S5
scoreboard. The actual winner is the theory-side
relu-depth-width-expressivity, robust across the full sensitivity sweep.
So the divergence is a *scouting/cartography breadth miss at the option
level*, not a scoring error — the F9 rubric fix made the fork axis
measurable, but no card embodied the fork mechanism. For Prompt 14: the
breadth metric should check option-level ground truth, not only
angle-level; adjudicating whether the fork-test prior or the run's answer
is right is the human's ground-truth reading.

**Closed post-run:**

- **F10 — preference slot 0.2 → 0.15 propagated.** Verified: the decision
  matrix scores the slot at 0.15 and the S8 sweep pivots on slot weight
  0.15.
- **F11 — hard-floors-as-weights did not bite.** No private-by-design
  option survived screening: twin-track and diagonal-pair cut by the cap,
  both samplers by confirmed kill-risks. The design question stays open
  (constraint enforcement is screener prompt + kill-risk checks, not code),
  but this run gives no evidence it needs code yet.
- **F12 / Trap 3 — quarantine grep done.** One hit total across
  angles/ + rubric artifacts: the practitioner cartographer's raw report
  suggests "a structured AI-safety fellowship such as MATS or SPAR" —
  plausibly generic advice for the field, but the one line worth the
  user's eye; map priors and rubric justifications show no
  compression/safety shaping.
- **F5 — session-limit detection: since fixed.** The mid-S3 untyped
  fall-through is the same class findings 3/8 above closed (enriched CLI
  detail + the three limit-notice families); S3's batch persistence
  correctly re-paid nothing on resume. Superseded, no further action.
- **S6 depth stats** (for the depth metric's baseline): rounds to
  convergence 1/1/2/2/2/2/3, all CONVERGED, zero BUDGET-CAPPED; verifier
  pass rates 33%–91% (rademacher 3/9 and context-strategy 5/10 at the low
  end); 5 of 7 finalists took the one targeted revision; 10 contradictions
  ledgered, all revised or open-by-design in the report.
- **S7**: zero rank inversions (so the inversion-docket path ran empty —
  only the runner-up steelman exercised); frame-check PASS explicitly
  re-examined the logged Gate-A removal and upheld it.

**What worked, recorded as tested (F1/F2/F4):** S0 filed both floors as
hard constraints and kept tastes out of the brief — but the
preference-stated-as-constraint pushback never fired: record as
*untested*, not passed. Over-decomposition fix confirmed (16 angles vs
M1's 41; merger self-flagged with a full dedup trail; saturation novelty
0.90/0.38/0.33/0.09/0.00 → trailing-2 mean 0.05 < 0.2, stop). All three
Gate-A edit types (add/remove/prior-adjust) applied and logged, removal
reason preserved for the frame-checker (which used it).

**Still unexercised live (coverage gaps for eval/benchmarks):** the S3
reflow path (redundancy 0–25%, no early stops → no-op); ALL Gate-C loop
actions (the gate was approved directly — zero preference-feedback
re-scores, zero evidence challenges, no re-divergence; mock-tested only);
S8 citation-pass retries (zero — first dispatch clean); the S0 pushback
trap (above).

## Benchmark seed (for benchmarks/ in Prompt 14)

**Expanded into [`docs/benchmark-seeds.md`](benchmark-seeds.md)** — four
complete, pre-authored specs (this run as the known-ground-truth spec, the
M1 vector-store run as the technical selection, plus two fresh questions
derived from this run's own artifacts). Prompt 14's step 1 transcribes that
file verbatim; no TODO-USER placeholders remain. The summary below stands
as the short form.

- Type: personal decision, known ground truth. Question text = this run's
  goal string; the run's brief/destination/preferences artifacts are
  reusable as fixtures.
- Reference angle union: this run's 16-angle map + `llm-agent-systems-build`
  (the F3 miss; flag as the practitioner-obvious-miss test case) +
  horizon-scanner-shaped frontier coverage (absent at standard).
- Expected outcome (user's withheld prior): fork test (from-scratch
  nanoGPT, compression framing) at/near the top of the destination-only
  board; grokking/induction-heads interp as understand-side runner-up.
  Actual outcome: the fork test was never carded (see Trap 2 verdict);
  winner relu-depth-width-expressivity, interp probes #2/#3.
- Cost calibration: S0–S1 ≈ $7.04, S0–S4 ≈ $34.27, final $110.43 at
  standard (ledger above) — use as the README cost row and the eval's
  spend baseline.
