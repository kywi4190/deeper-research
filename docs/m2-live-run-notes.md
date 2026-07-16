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

(Next findings from the restarted run go here — keep the M1 file's format:
what worked / findings by pain / final cost by stage from `deeper status`.)
