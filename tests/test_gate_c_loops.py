"""Gate C loop tests (design §5 Gate C, §12): each typed action end-to-end in
mock through the real engine — preference feedback re-scores the
preference-adjusted board only; an evidence challenge fires one scoped
verifier contract and feeds the contradiction ledger; the accepted
re-divergence mini-loop runs on its own budget, respects the 1-per-run cap,
and reruns S7 over the merged finalists; the §12 iteration cap flips the gate
template to approve-only."""

from __future__ import annotations

from deeper.contradictions import load_ledger
from deeper.orchestrator import Node
from deeper.orchestrator.gates import GATE_SPECS, read_decision
from deeper.schemas import (
    DeepDiveRoundLog,
    DeepDiveStatus,
    Dossier,
    GateName,
    GateStatus,
    OptionCardSet,
    Rubric,
    RunState,
    RunStatus,
    ScreeningResult,
    Shortlist,
    ShortlistOutcome,
    VerificationReport,
)
from deeper.sensitivity import dual_scoreboards
from deeper.workspace import Workspace

from .helpers import walk_engine_to_gate_c

ADJ_WINNER = "sae-feature-atlas"
NEW_OPTION = "clinical-imaging-collaboration"
GAP_ANGLE = "applied-domain-collaboration"

FEEDBACK_DECISION = """\
approved: false
preference_feedback:
  - option_id: sae-feature-atlas
    reaction: "the ops burden of the atlas bothers me more than I expected"
    direction: negative
  - option_id: backdoor-probe-study
    reaction: "the probe study's scoped ambition is actually fine"
    direction: positive
"""

CHALLENGE_DECISION = """\
approved: false
evidence_challenges:
  - option_id: sae-feature-atlas
    claim_id: c-sae-compute
    challenge: "I don't believe the sweeps fit with 2x headroom"
"""

REDIVERGENCE_DECISION = """\
approved: false
accept_redivergence: true
"""


def spend_count(state: RunState, role: str, context: str | None = None) -> int:
    return sum(
        1 for e in state.spend if e.role == role and (context is None or e.context == context)
    )


def boards(ws: Workspace, path: str = "tournament/scores.yaml"):
    scores = ws.read_artifact(path, ScreeningResult)
    rubric = ws.read_artifact("rubric.yaml", Rubric)
    return dual_scoreboards(scores, rubric)


# -- preference feedback ---------------------------------------------------------------


async def test_preference_feedback_rescores_the_adjusted_board_only(tmp_path):
    ws, engine, emitted = await walk_engine_to_gate_c(tmp_path)
    dest_before, adj_before = boards(ws)
    screener_before = spend_count(ws.load_state(), "screener")

    ws.path("gates/gate-c.yaml").write_text(FEEDBACK_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_C  # loop applied, gate reopened

    state = ws.load_state()
    assert state.gate_c_iterations == 1
    assert state.status is RunStatus.GATE_PENDING
    assert state.gates[GateName.C] is GateStatus.PENDING
    # Exactly one screener dispatch, no research roles fired: a free re-score.
    assert spend_count(state, "screener") == screener_before + 1
    assert spend_count(state, "screener", "gate-c-feedback") == 1
    assert not any(e.context and e.context.startswith("challenge-") for e in state.spend)
    assert not any(e.context and e.context.startswith("redivergence") for e in state.spend)

    # CODE re-scored both scoreboards: destination-only provably identical,
    # preference-adjusted moved (sae slot 4.5 -> 4.4, backdoor 4.0 -> 4.25).
    dest_after, adj_after = boards(ws)
    assert dest_after == dest_before
    assert adj_after != adj_before
    assert adj_after[0].option_id == ADJ_WINNER  # modest movement — no flip
    scores = ws.read_artifact("tournament/scores.yaml", ScreeningResult)
    slots = {o.option_id: o.preference_score.score for o in scores.options}
    assert slots["sae-feature-atlas"] == 4.4
    assert slots["backdoor-probe-study"] == 4.25
    # dossiers/scores.yaml carries the same slots, so a later S7 rerun keeps them.
    dive = ws.read_artifact("dossiers/scores.yaml", ScreeningResult)
    dive_slots = {o.option_id: o.preference_score.score for o in dive.options}
    assert dive_slots["sae-feature-atlas"] == 4.4

    # Audit: decision archived, fresh template written, one loop commit.
    assert ws.path("gates/gate-c.1.yaml").is_file()
    decision, problem = read_decision(ws, GATE_SPECS[GateName.C])
    assert problem is None and decision is not None and not decision.approved
    assert not decision.preference_feedback  # a fresh undecided template
    assert any(s.startswith("gate-c loop 1:") for s in ws.history())
    assert any("preference slot of 'sae-feature-atlas' 4.5 -> 4.4" in m for m in emitted)


# -- evidence challenges -----------------------------------------------------------------


async def test_evidence_challenge_fires_one_scoped_verifier_contract(tmp_path):
    ws, engine, emitted = await walk_engine_to_gate_c(tmp_path)
    ws.path("gates/gate-c.yaml").write_text(CHALLENGE_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_C

    state = ws.load_state()
    assert state.gate_c_iterations == 1
    assert spend_count(state, "verifier", "challenge-sae-feature-atlas-c-sae-compute") == 1
    report = ws.read_artifact(
        "gates/challenge-1-sae-feature-atlas-c-sae-compute.yaml", VerificationReport
    )
    assert report.results[0].claim_id == "c-sae-compute"
    ledger = load_ledger(ws)
    assert any(e.id == "gate-c-sae-feature-atlas-c-sae-compute" for e in ledger.entries)
    assert any("CONTRADICTED" in m for m in emitted)
    # No scores moved: a re-score is the human's next loop, not a side effect.
    dest, adj = boards(ws)
    assert adj[0].option_id == ADJ_WINNER
    assert ws.path("gates/gate-c.1.yaml").is_file()


async def test_unresolvable_decision_is_refused_without_consuming_an_iteration(tmp_path):
    ws, engine, emitted = await walk_engine_to_gate_c(tmp_path)
    verifier_before = spend_count(ws.load_state(), "verifier")
    ws.path("gates/gate-c.yaml").write_text(
        CHALLENGE_DECISION.replace("c-sae-compute", "c-invented"), encoding="utf-8"
    )
    assert await engine.run() is Node.GATE_C
    state = ws.load_state()
    assert state.gate_c_iterations == 0  # refused whole, nothing consumed
    assert spend_count(state, "verifier") == verifier_before  # nothing dispatched
    assert not ws.path("gates/gate-c.1.yaml").exists()  # not archived
    assert any("cannot be applied" in m and "c-invented" in m for m in emitted)


# -- the re-divergence mini-loop ------------------------------------------------------------


async def test_mini_loop_runs_on_its_own_budget_and_reruns_s7(tmp_path):
    # The canned scenario seats 7 finalists — exactly the §12 hard cap — so
    # give the mini-loop one free seat; the at-cap refusal has its own test.
    ws, engine, emitted = await walk_engine_to_gate_c(tmp_path, caps={"max_finalists": 8})
    finalists_before = ws.read_artifact("screening/shortlist.md", Shortlist).finalist_ids

    ws.path("gates/gate-c.yaml").write_text(REDIVERGENCE_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_C  # mini-loop -> S7 rerun -> gate reopens

    state = ws.load_state()
    assert state.redivergence_runs == 1
    assert state.gate_c_iterations == 1
    assert spend_count(state, "scout", f"redivergence-{GAP_ANGLE}") == 1
    assert spend_count(state, "screener", "redivergence") == 1

    # The proposed miss was scouted, merged, screened, and seated.
    cards = ws.read_artifact(f"options/{GAP_ANGLE}/cards.yaml", OptionCardSet)
    assert NEW_OPTION in {c.id for c in cards.cards}
    assert len(cards.cards) == 4  # the 3 originals survived the merge
    shortlist = ws.read_artifact("screening/shortlist.md", Shortlist)
    assert shortlist.finalist_ids == [*finalists_before, NEW_OPTION]
    champion = next(d for d in shortlist.decisions if d.option_id == NEW_OPTION)
    assert champion.decision is ShortlistOutcome.ADVANCED
    assert "mini-loop" in champion.reason
    assert NEW_OPTION in {
        o.option_id for o in ws.read_artifact("screening/scores.yaml", ScreeningResult).options
    }

    # Its OWN budget: proposal estimates 2 units -> dive cap 1 (the quick
    # profile's own cap is 2), so the moving score lands BUDGET-CAPPED at
    # exactly one analyst round.
    log = ws.read_artifact(f"dossiers/{NEW_OPTION}-rounds.yaml", DeepDiveRoundLog)
    assert len(log.rounds) == 1
    assert log.status is DeepDiveStatus.BUDGET_CAPPED
    dossier = ws.read_artifact(f"dossiers/{NEW_OPTION}.md", Dossier)
    assert dossier.budget_capped and dossier.open_questions

    # Merged finalists reached S7's rerun: the new option is on both score
    # files and the rerun tournament produced a fresh scoreboard around it.
    dive = ws.read_artifact("dossiers/scores.yaml", ScreeningResult)
    assert NEW_OPTION in {o.option_id for o in dive.options}
    tournament = ws.read_artifact("tournament/scores.yaml", ScreeningResult)
    assert NEW_OPTION in {o.option_id for o in tournament.options}
    dest, adj = boards(ws)
    assert adj[0].option_id == ADJ_WINNER  # 4th on both boards: no new inversions
    assert any(s.startswith("gate-c loop 1: re-divergence accepted") for s in ws.history())
    assert any(s.startswith("rerun S7") for s in ws.history())

    # ---- the 1-per-run cap (§12): a second acceptance is refused ---------------
    scout_before = spend_count(ws.load_state(), "scout")
    ws.path("gates/gate-c.yaml").write_text(REDIVERGENCE_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_C
    state = ws.load_state()
    assert state.redivergence_runs == 1  # unchanged
    assert state.gate_c_iterations == 1  # refused decisions consume nothing
    assert spend_count(state, "scout") == scout_before
    assert any("1 mini-loop per run" in m for m in emitted)


async def test_mini_loop_respects_the_hard_finalist_cap(tmp_path):
    """With the default caps the scenario's 7 finalists already fill the §12
    cap: the mini-loop scouts and screens, but refuses to seat its champion —
    no deep dive, no S7 rerun, and the cut is audited in shortlist.md."""
    ws, engine, emitted = await walk_engine_to_gate_c(tmp_path)
    ws.path("gates/gate-c.yaml").write_text(REDIVERGENCE_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_C
    state = ws.load_state()
    assert state.redivergence_runs == 1  # the one mini-loop was spent trying
    assert spend_count(state, "analyst", f"{NEW_OPTION}-r1") == 0  # no dive
    shortlist = ws.read_artifact("screening/shortlist.md", Shortlist)
    assert NEW_OPTION not in shortlist.finalist_ids
    cut = next(d for d in shortlist.decisions if d.option_id == NEW_OPTION)
    assert cut.decision is ShortlistOutcome.CUT and "hard finalist cap" in cut.reason
    assert NEW_OPTION not in {
        o.option_id for o in ws.read_artifact("tournament/scores.yaml", ScreeningResult).options
    }
    assert not any(s.startswith("rerun S7") for s in ws.history())


async def test_accepting_without_a_proposal_is_refused(tmp_path):
    ws, engine, emitted = await walk_engine_to_gate_c(tmp_path)
    ws.path("tournament/frame-check.md").write_text(
        """\
verdict: pass
removals_check:
  finding: Nothing consequential was removed.
  consequential: false
missed_options_check:
  finding: Every critique-flagged miss was scouted.
  consequential: false
rubric_fragility_check:
  finding: No in-range reweighting flips the winner.
  consequential: false
proposal: null
notes: null
""",
        encoding="utf-8",
    )
    ws.path("gates/gate-c.yaml").write_text(REDIVERGENCE_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_C
    assert ws.load_state().gate_c_iterations == 0
    assert any("nothing to accept" in m for m in emitted)


# -- the §12 iteration cap -------------------------------------------------------------


async def test_loop_cap_flips_the_template_to_approve_only(tmp_path):
    ws, engine, emitted = await walk_engine_to_gate_c(tmp_path, caps={"max_gate_c_loops": 1})
    ws.path("gates/gate-c.yaml").write_text(FEEDBACK_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_C
    assert ws.load_state().gate_c_iterations == 1

    # The fresh template is the capped variant: approve-only, with the note.
    template = ws.path("gates/gate-c.yaml").read_text(encoding="utf-8")
    assert "ALL GATE-C FEEDBACK LOOPS ARE USED" in template
    assert "preference_feedback" not in template

    # A further loop decision is refused at the cap...
    ws.path("gates/gate-c.yaml").write_text(CHALLENGE_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_C
    state = ws.load_state()
    assert state.gate_c_iterations == 1
    assert not ws.path("gates/gate-c.2.yaml").exists()
    assert any("then decide" in m for m in emitted)

    # ...and approval still advances all the way through S8.
    ws.path("gates/gate-c.yaml").write_text("approved: true\n", encoding="utf-8")
    assert await engine.run() is Node.DONE
    assert ws.path("report/decision-report.md").is_file()


async def test_sequential_loops_archive_numbered_decisions(tmp_path):
    ws, engine, _ = await walk_engine_to_gate_c(tmp_path)
    ws.path("gates/gate-c.yaml").write_text(FEEDBACK_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_C
    ws.path("gates/gate-c.yaml").write_text(CHALLENGE_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_C
    state = ws.load_state()
    assert state.gate_c_iterations == 2
    assert ws.path("gates/gate-c.1.yaml").is_file()
    assert ws.path("gates/gate-c.2.yaml").is_file()
    assert any(s.startswith("gate-c loop 2:") for s in ws.history())
