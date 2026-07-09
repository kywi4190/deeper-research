"""M1-exit test extended through S7 (design §9): one full mock run through the
real stage registry.

A single test drives `deeper new`-equivalent state S0 → Gate A (approved with a
prior adjustment) → S2 → S3 → S4 → Gate B (one weight override) → S5 → S6 deep
dives → S7 tournament (engineered rank inversion + frame-check gap) → Gate C →
the S8 stub, then surgically reruns one angle's scouting — asserting the
workspace is exactly what the design promises at every step: validated
artifacts, one commit per stage and gate, a populated spend ledger under the
cap, resumable state, and precisely-scoped invalidation.
"""

from __future__ import annotations

import pytest

from deeper.allocation import allocate
from deeper.orchestrator import Engine, Node
from deeper.orchestrator.rerun import invalidate
from deeper.schemas import (
    AllocationTable,
    AngleMap,
    Brief,
    CardCritique,
    CartographerReport,
    ContradictionLedger,
    CoverageReport,
    DecisionReport,
    DeepDiveRoundLog,
    DeepDiveStatus,
    DestinationModel,
    Dossier,
    FrameCheck,
    FrameCheckVerdict,
    GateName,
    GateStatus,
    OptionCardSet,
    Preferences,
    Prosecution,
    Rubric,
    RunState,
    RunStatus,
    ScoreUpdateLog,
    ScreeningResult,
    Shortlist,
    Stage,
    Steelman,
    SteelmanTrigger,
    VerificationReport,
)
from deeper.sensitivity import dual_scoreboards
from deeper.stages.s7_tournament import prosecution_path, steelman_path
from deeper.workspace import Workspace

from .helpers import make_workspace

ADJUSTED_ANGLE = "evaluation-science"  # prior 0.7 in the merger fixture
RERUN_ANGLE = "interpretability-research"

GATE_A_DECISION = f"""\
approved: true
prior_adjustments:
  - angle_id: {ADJUSTED_ANGLE}
    new_prior: 0.9
"""

GATE_B_DECISION = """\
approved: true
preference_slot_weight: 0.25
weight_overrides:
  letter-strength: 0.35
"""

GATE_C_FEEDBACK = """\
approved: false
preference_feedback:
  - option_id: sae-feature-atlas
    reaction: "the ops burden of the atlas bothers me more than I expected"
    direction: negative
  - option_id: backdoor-probe-study
    reaction: "the probe study's scoped ambition is actually fine"
    direction: positive
"""


def spend_count(state: RunState, role: str, context: str | None) -> int:
    return sum(1 for e in state.spend if e.role == role and e.context == context)


async def test_full_mock_run_end_to_end(tmp_path):
    ws = make_workspace(tmp_path)  # quick profile, senior-project goal, mode=mock
    config = ws.load_config()
    emitted: list[str] = []
    engine = Engine(ws, emit=emitted.append)  # ask_user=None: non-interactive S0

    # ---- S0 + S1 run to the first pause: Gate A -------------------------------
    assert await engine.run() is Node.GATE_A
    state = ws.load_state()
    assert state.status is RunStatus.GATE_PENDING
    assert state.pending_gate is GateName.A

    # ---- Gate A: approve with one modification (a prior adjustment) -----------
    ws.path("gates/gate-a.yaml").write_text(GATE_A_DECISION, encoding="utf-8")
    assert await engine.run() is Node.GATE_B  # S2 → S3 → S4, pause at Gate B

    angle_map = ws.read_artifact("angles/map.yaml", AngleMap)
    priors = {a.id: a.relevance_prior for a in angle_map.angles}
    assert priors[ADJUSTED_ANGLE] == 0.9  # the gate edit landed before S2

    # S2's table is exactly the deterministic formula over the post-gate priors.
    table = ws.read_artifact("allocation.yaml", AllocationTable)
    expected = allocate(
        priors,
        total_budget_units=config.total_budget_units,
        floor=config.floor,
        gamma=config.gamma,
        per_angle_cap_pct=config.per_angle_cap_pct,
    )
    assert table.model_dump() == expected.model_dump()

    # ---- Gate B: approve with one weight adjusted ------------------------------
    ws.path("gates/gate-b.yaml").write_text(GATE_B_DECISION, encoding="utf-8")
    # S5 screens, S6 deep-dives every finalist, S7 runs the tournament, and the
    # machine pauses at the contender review.
    assert await engine.run() is Node.GATE_C

    rubric = ws.read_artifact("rubric.yaml", Rubric)
    weights = {c.id: c.weight for c in rubric.criteria}
    assert weights["letter-strength"] == 0.35  # pinned by the override
    # Untouched criteria rescaled proportionally: factor (1-0.35)/(1-0.25).
    assert weights["publication-potential"] == pytest.approx(0.3 * 0.65 / 0.75)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert rubric.preference_slot.weight == 0.25

    # ---- every artifact in the workspace validates -----------------------------
    ws.read_artifact("brief.md", Brief)
    ws.read_artifact("destination.md", DestinationModel)
    ws.read_artifact("preferences.yaml", Preferences)
    ws.read_artifact("angles/map-report.md", CoverageReport)
    raw_reports = sorted(ws.path("angles/raw").glob("*.yaml"))
    assert len(raw_reports) >= config.initial_cartographers
    for path in raw_reports:
        ws.read_artifact(f"angles/raw/{path.name}", CartographerReport)
    for row in table.rows:
        assert row.units > 0  # quick profile floor=1: every angle got budget
        ws.read_artifact(f"options/{row.angle_id}/cards.yaml", OptionCardSet)
        ws.read_artifact(f"options/{row.angle_id}/critique.md", CardCritique)
    ws.read_artifact("options/reflow.yaml", AllocationTable)  # reflow happened
    ws.read_artifact("screening/scores.yaml", ScreeningResult)
    shortlist = ws.read_artifact("screening/shortlist.md", Shortlist)
    assert shortlist.finalist_ids  # the run produced an auditable shortlist
    for option_id in shortlist.finalist_ids:  # one settled deep dive per finalist
        ws.read_artifact(f"dossiers/{option_id}.md", Dossier)
        ws.read_artifact(f"dossiers/{option_id}-verification.md", VerificationReport)
        log = ws.read_artifact(f"dossiers/{option_id}-rounds.yaml", DeepDiveRoundLog)
        assert log.status is not DeepDiveStatus.IN_PROGRESS
    dive_scores = ws.read_artifact("dossiers/scores.yaml", ScreeningResult)
    assert {o.option_id for o in dive_scores.options} == set(shortlist.finalist_ids)
    # The scenario exercises all three S6 termination paths in one run.
    assert ws.read_artifact("dossiers/sae-feature-atlas-rounds.yaml", DeepDiveRoundLog).status is (
        DeepDiveStatus.CONVERGED
    )
    capped = ws.read_artifact("dossiers/contamination-robust-benchmark.md", Dossier)
    assert capped.budget_capped and capped.open_questions
    revised = ws.read_artifact("dossiers/backdoor-probe-study-rounds.yaml", DeepDiveRoundLog)
    assert revised.verification is not None and revised.verification.revision_completed
    ws.read_artifact("ledger/contradictions.md", ContradictionLedger)

    # ---- S7: the engineered inversion drives the docket ------------------------
    dest_board, adj_board = dual_scoreboards(dive_scores, rubric)
    assert dest_board[0].option_id == "contamination-robust-benchmark"  # 4.6 dest-only
    assert adj_board[0].option_id == "sae-feature-atlas"  # the slot flips the winner
    for option_id in (o.option_id for o in adj_board[:3]):  # top-3 prosecuted
        prosecution = ws.read_artifact(prosecution_path(option_id), Prosecution)
        assert prosecution.option_id == option_id and prosecution.regret_path
    steelman = ws.read_artifact(steelman_path("contamination-robust-benchmark"), Steelman)
    assert steelman.trigger is SteelmanTrigger.RANK_INVERSION  # keyed to the inversion
    frame_check = ws.read_artifact("tournament/frame-check.md", FrameCheck)
    assert frame_check.verdict is FrameCheckVerdict.GAP_FOUND
    assert frame_check.proposal is not None  # surfaced at Gate C, never executed
    assert frame_check.proposal.target_angle_id == "applied-domain-collaboration"
    update_log = ws.read_artifact("tournament/score-updates.yaml", ScoreUpdateLog)
    assert update_log.updates and all(u.cause for u in update_log.updates)
    tournament_scores = ws.read_artifact("tournament/scores.yaml", ScreeningResult)
    updated = {(u.option_id, u.criterion_id): u.new_score for u in update_log.updates}
    for record in tournament_scores.options:  # judge updates applied in code
        for cs in record.criterion_scores:
            expected_score = updated.get((record.option_id, cs.criterion_id))
            if expected_score is not None:
                assert cs.score == expected_score

    # ---- state.json: gate C pending, spend ledger under the cap ----------------
    state = ws.load_state()
    assert state.stage is Stage.S7  # S7 complete; the gate holds the run
    assert state.status is RunStatus.GATE_PENDING
    assert state.pending_gate is GateName.C
    assert state.gates[GateName.A] is GateStatus.APPROVED
    assert state.gates[GateName.B] is GateStatus.APPROVED
    assert state.gates[GateName.C] is GateStatus.PENDING

    assert state.spend, "every agent invocation must land a SpendEntry"
    roles = {e.role for e in state.spend}
    assert {
        "interviewer",
        "merger",
        "scout",
        "card-critic",
        "rubric-builder",
        "screener",
        "analyst",
        "verifier",
        "prosecutor",
        "steelman",
        "frame-checker",
        "judge",
    } <= roles
    assert any(r.startswith("cartographer-") for r in roles)
    assert not any(e.stage is Stage.S2 for e in state.spend)  # S2 is pure code
    assert all(e.usd >= 0 and e.output_tokens > 0 for e in state.spend)
    assert state.total_usd() <= config.max_spend_usd

    # ---- git audit trail: one commit per stage completion + per gate -----------
    subjects = ws.history()
    for expected_subject in (
        "run created (profile=quick)",
        "S0 complete",
        "S1 complete; gate-a pending",
        "S2 complete",
        "S3 complete",
        "S4 complete; gate-b pending",
        "S5 complete",
        "S6 complete",
        "S7 complete; gate-c pending",
    ):
        assert expected_subject in subjects, f"missing commit {expected_subject!r}"
    assert any(s.startswith("gate-a: approved") and "1 prior(s) adjusted" in s for s in subjects)
    assert any(s.startswith("gate-b: approved") and "0.25" in s for s in subjects)

    # ---- surgical rerun: --stage S3 --angle X invalidates exactly that subtree -
    counts_before = {
        (role, context): spend_count(state, role, context)
        for role, context in {(e.role, e.context) for e in state.spend}
    }
    removed = invalidate(ws, Stage.S3, RERUN_ANGLE)
    assert f"options/{RERUN_ANGLE}" in removed

    gone = [
        f"options/{RERUN_ANGLE}",
        "rubric.yaml",
        "rubric-rationale.md",
        "screening/scores.yaml",
        "screening/shortlist.md",
        "dossiers/scores.yaml",
        "dossiers/sae-feature-atlas.md",  # S6 outputs are downstream of S3
        "tournament/frame-check.md",  # so is the whole tournament
        "tournament/scores.yaml",
        "gates/gate-b.yaml",
        "gates/gate-c.yaml",
    ]
    for relpath in gone:
        assert not ws.path(relpath).exists(), f"{relpath} should be invalidated"
    survived = [
        "brief.md",
        "angles/map.yaml",
        "allocation.yaml",
        f"options/{ADJUSTED_ANGLE}/cards.yaml",
        "options/reflow.yaml",  # the settled reflow decision is not S3-angle-scoped
        "gates/gate-a.yaml",  # the kept Gate A decision is upstream
    ]
    for relpath in survived:
        assert ws.path(relpath).exists(), f"{relpath} should survive an S3 angle rerun"
    state = ws.load_state()
    assert state.stage is Stage.S3
    assert state.status is RunStatus.RUNNING
    assert state.gates[GateName.A] is GateStatus.APPROVED  # upstream gate untouched
    assert state.gates[GateName.B] is GateStatus.NOT_REACHED  # downstream gates reset
    assert state.gates[GateName.C] is GateStatus.NOT_REACHED
    assert any(s.startswith(f"rerun S3 (angle: {RERUN_ANGLE})") for s in ws.history())

    # ---- the machine walks forward again: only the invalidated angle re-scouts -
    assert await engine.run() is Node.GATE_B
    ws.path("gates/gate-b.yaml").write_text(
        "approved: true\npreference_slot_weight: 0.25\n", encoding="utf-8"
    )
    assert await engine.run() is Node.GATE_C

    state = ws.load_state()
    assert spend_count(state, "scout", RERUN_ANGLE) == counts_before[("scout", RERUN_ANGLE)] + 1
    assert (
        spend_count(state, "scout", ADJUSTED_ANGLE) == counts_before[("scout", ADJUSTED_ANGLE)]
    ), "an angle-scoped rerun must not re-dispatch other angles' scouts"
    assert spend_count(state, "interviewer", None) == counts_before[("interviewer", None)], (
        "upstream stages must not re-execute on rerun"
    )
    ws.read_artifact(f"options/{RERUN_ANGLE}/cards.yaml", OptionCardSet)
    ws.read_artifact("screening/shortlist.md", Shortlist)
    ws.read_artifact("dossiers/scores.yaml", ScreeningResult)  # S6 rewalked too
    ws.read_artifact("tournament/scores.yaml", ScreeningResult)  # and S7

    # ---- Gate C loop 1: preference feedback moves ONLY the adjusted board ------
    rubric = ws.read_artifact("rubric.yaml", Rubric)
    dest_before, adj_before = dual_scoreboards(
        ws.read_artifact("tournament/scores.yaml", ScreeningResult), rubric
    )
    ws.path("gates/gate-c.yaml").write_text(GATE_C_FEEDBACK, encoding="utf-8")
    assert await engine.run() is Node.GATE_C  # the free re-score reopens the gate
    state = ws.load_state()
    assert state.gate_c_iterations == 1
    assert state.gates[GateName.C] is GateStatus.PENDING
    dest_after, adj_after = dual_scoreboards(
        ws.read_artifact("tournament/scores.yaml", ScreeningResult), rubric
    )
    assert dest_after == dest_before  # tastes cannot move the destination board
    assert adj_after != adj_before  # ...and the boards visibly moved
    assert adj_after[0].option_id == "sae-feature-atlas"  # tempered, not flipped
    assert ws.path("gates/gate-c.1.yaml").is_file()  # the loop is audited
    assert any(s.startswith("gate-c loop 1:") for s in ws.history())

    # ---- approve → S8 synthesis → DONE ------------------------------------------
    ws.path("gates/gate-c.yaml").write_text("approved: true\n", encoding="utf-8")
    assert await engine.run() is Node.DONE
    state = ws.load_state()
    assert state.gates[GateName.C] is GateStatus.APPROVED
    assert state.status is RunStatus.DONE
    assert "synthesist" in {e.role for e in state.spend}
    assert "run complete" in ws.history()

    # The deliverable: all seven design components, claims linked, tables embedded.
    report = ws.read_artifact("report/decision-report.yaml", DecisionReport)
    assert report.winner_option_id == adj_after[0].option_id
    text = ws.path("report/decision-report.md").read_text(encoding="utf-8")
    for heading in (
        "## 1. Recommendation",
        "## 2. Decision matrix",
        "## 3. Sensitivity analysis",
        "## 4. Dissent",
        "## 5. Residual uncertainty",
        "## 6. Next actions",
        "## 7. Appendix",
    ):
        assert heading in text
    assert "[c-sae-precedent](#claim-sae-feature-atlas--c-sae-precedent)" in text
    assert "### Claims index" in text

    # ---- terminal idempotency: re-running changes nothing ----------------------
    history_before = ws.history()
    assert await engine.run() is Node.DONE
    assert ws.history() == history_before
    assert Workspace.open(ws.root).load_state().stage is Stage.S8
