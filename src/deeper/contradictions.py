"""The contradiction ledger (design §6): when two artifacts disagree on a fact,
the detecting stage appends here instead of silently picking one. This is the
shared helper every detector uses — S6 verifier verdicts today, S7 tournament
material and S8 report surfacing later.

Appends are idempotent by entry id: callers derive deterministic ids (e.g.
``{option_id}--{claim_id}``) so a crash-resume replay of the same detection
never duplicates an entry.
"""

from __future__ import annotations

from deeper.schemas import ContradictionEntry, ContradictionLedger
from deeper.workspace import Workspace, WorkspaceError

LEDGER_PATH = "ledger/contradictions.md"


def load_ledger(workspace: Workspace) -> ContradictionLedger:
    """The run's ledger; empty (not an error) when nothing has been detected yet."""
    if not workspace.path(LEDGER_PATH).is_file():
        return ContradictionLedger()
    return workspace.read_artifact(LEDGER_PATH, ContradictionLedger)


def append_contradictions(
    workspace: Workspace, entries: list[ContradictionEntry]
) -> ContradictionLedger:
    """Append new entries to ledger/contradictions.md, skipping ids already
    present. Returns the ledger as persisted (unchanged input writes nothing)."""
    ledger = load_ledger(workspace)
    known = {e.id for e in ledger.entries}
    fresh = [e for e in entries if e.id not in known]
    dupes = sorted({e.id for e in entries if sum(1 for x in entries if x.id == e.id) > 1})
    if dupes:
        raise WorkspaceError(f"contradiction entries passed in one batch collide on ids: {dupes}")
    if not fresh:
        return ledger
    ledger = ContradictionLedger(entries=[*ledger.entries, *fresh])
    workspace.write_artifact(LEDGER_PATH, ledger)
    return ledger
