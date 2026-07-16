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

(Next findings from the restarted run go here — keep the M1 file's format:
what worked / findings by pain / final cost by stage from `deeper status`.)
