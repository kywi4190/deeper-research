"""The shared contradiction-ledger helper (design §6): append-only, schema-
checked, and idempotent by entry id — a resume replay of the same detection
must not duplicate entries."""

from __future__ import annotations

import pytest

from deeper.contradictions import LEDGER_PATH, append_contradictions, load_ledger
from deeper.schemas import ContradictionEntry, ContradictionLedger, ContradictionStatement
from deeper.workspace import WorkspaceError

from .helpers import make_workspace


def entry(entry_id: str, statement: str = "the dossier says X") -> ContradictionEntry:
    return ContradictionEntry(
        id=entry_id,
        statement_a=ContradictionStatement(artifact="dossiers/opt.md", statement=statement),
        statement_b=ContradictionStatement(
            artifact="dossiers/opt-verification.md", statement="the source says not-X"
        ),
        detected_by="verifier",
    )


def test_load_ledger_is_empty_before_any_detection(tmp_path):
    ws = make_workspace(tmp_path)
    assert load_ledger(ws).entries == []
    assert not ws.path(LEDGER_PATH).is_file()  # loading never creates the file


def test_append_creates_the_file_and_round_trips(tmp_path):
    ws = make_workspace(tmp_path)
    ledger = append_contradictions(ws, [entry("opt-claim-a"), entry("opt-claim-b")])
    assert [e.id for e in ledger.entries] == ["opt-claim-a", "opt-claim-b"]
    on_disk = ws.read_artifact(LEDGER_PATH, ContradictionLedger)
    assert on_disk.model_dump() == ledger.model_dump()


def test_append_accumulates_and_dedups_by_id(tmp_path):
    ws = make_workspace(tmp_path)
    append_contradictions(ws, [entry("opt-claim-a")])
    # A replay of the same detection plus one genuinely new entry: only the new
    # entry lands, and the original's content is not overwritten.
    ledger = append_contradictions(
        ws, [entry("opt-claim-a", statement="a DIFFERENT restatement"), entry("opt-claim-b")]
    )
    assert [e.id for e in ledger.entries] == ["opt-claim-a", "opt-claim-b"]
    assert ledger.entries[0].statement_a.statement == "the dossier says X"


def test_pure_replay_writes_nothing(tmp_path):
    ws = make_workspace(tmp_path)
    append_contradictions(ws, [entry("opt-claim-a")])
    before = ws.path(LEDGER_PATH).read_text(encoding="utf-8")
    append_contradictions(ws, [entry("opt-claim-a")])
    assert ws.path(LEDGER_PATH).read_text(encoding="utf-8") == before


def test_colliding_ids_in_one_batch_are_a_caller_bug(tmp_path):
    ws = make_workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="collide"):
        append_contradictions(ws, [entry("opt-claim-a"), entry("opt-claim-a")])
