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

(Next findings from the restarted run go here — keep the M1 file's format:
what worked / findings by pain / final cost by stage from `deeper status`.)
