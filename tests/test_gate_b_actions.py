"""Gate B tests: the template puts the preference-slot weight first, and an
approved decision's edits are applied to rubric.yaml on resume — slot weight
always, weight overrides with proportional rescale, criterion replacements,
and referential problems re-pausing with nothing written."""

from __future__ import annotations

import pytest

from deeper.orchestrator.gates import GATE_SPECS, _interpret_b, apply_gate_b_actions
from deeper.schemas import Criterion, GateBDecision, GateName, Rubric, Stage

from .helpers import FIXTURES, make_workspace

RUBRIC_FIXTURE = FIXTURES / "rubric-builder" / "rubric.yaml"


@pytest.fixture()
def ws(tmp_path):
    ws = make_workspace(tmp_path)
    ws.write_artifact("rubric.yaml", Rubric.from_yaml_file(RUBRIC_FIXTURE))
    return ws


def decision(**kwargs) -> GateBDecision:
    kwargs.setdefault("preference_slot_weight", 0.2)
    return GateBDecision(approved=True, **kwargs)


def weights(ws) -> dict[str, float]:
    return {c.id: c.weight for c in ws.read_artifact("rubric.yaml", Rubric).criteria}


# -- template ---------------------------------------------------------------------


def test_template_puts_the_slot_weight_prominently_at_top():
    template = GATE_SPECS[GateName.B].template
    # The one number this gate exists to set comes before the approval line,
    # with the design's explanation of what it controls.
    assert template.index("preference_slot_weight:") < template.index("approved: false")
    assert "how much your tastes may bend" in template
    assert "destination-optimal answer" in template
    # And the template still parses as a valid, undecided decision.
    parsed = GateBDecision.load_yaml(template)
    assert parsed.approved is False and parsed.preference_slot_weight == 0.2


# -- apply ------------------------------------------------------------------------


def test_slot_weight_always_applied(ws):
    messages, problem = apply_gate_b_actions(ws, decision(preference_slot_weight=0.35))
    assert problem is None
    assert ws.read_artifact("rubric.yaml", Rubric).preference_slot.weight == 0.35
    assert any("0.35" in m for m in messages)
    assert weights(ws) == pytest.approx(
        {c.id: c.weight for c in Rubric.from_yaml_file(RUBRIC_FIXTURE).criteria}
    )


def test_weight_override_rescales_untouched_criteria_to_sum_one(ws):
    before = weights(ws)  # publication-potential 0.3, letter-strength 0.25, ...
    messages, problem = apply_gate_b_actions(
        ws, decision(weight_overrides={"letter-strength": 0.4})
    )
    assert problem is None
    after = weights(ws)
    assert after["letter-strength"] == 0.4
    assert sum(after.values()) == pytest.approx(1.0)
    # Untouched criteria kept their relative proportions (rescaled by 0.6/0.75).
    factor = (1 - 0.4) / (1 - before["letter-strength"])
    for cid, w in before.items():
        if cid != "letter-strength":
            assert after[cid] == pytest.approx(w * factor)
    assert any("rescaled" in m for m in messages)
    assert any("0.25 -> 0.4" in m for m in messages)


def test_edited_criterion_is_replaced_and_its_weight_pinned(ws):
    replacement = Criterion(
        id="feasibility",
        name="Feasibility under real constraints",
        definition="Probability of completion under the actual constraints.",
        measurement_method="Compare compute estimates against the node budget.",
        levels={i: f"level {i}" for i in range(1, 6)},
        weight=0.3,
        justification="The deadline is the destination model's hardest constraint.",
    )
    _, problem = apply_gate_b_actions(ws, decision(edited_criteria=[replacement]))
    assert problem is None
    rubric = ws.read_artifact("rubric.yaml", Rubric)
    edited = next(c for c in rubric.criteria if c.id == "feasibility")
    assert edited.name == "Feasibility under real constraints"
    assert edited.weight == 0.3  # the edit's weight is pinned, others rescaled
    assert sum(c.weight for c in rubric.criteria) == pytest.approx(1.0)


def test_referential_problems_repause_with_nothing_written(ws):
    before = ws.path("rubric.yaml").read_text(encoding="utf-8")
    messages, problem = apply_gate_b_actions(
        ws, decision(weight_overrides={"no-such-criterion": 0.3, "letter-strength": 1.5})
    )
    assert messages == []
    assert problem is not None
    assert "no criterion 'no-such-criterion'" in problem
    assert "must be in (0, 1]" in problem
    assert ws.path("rubric.yaml").read_text(encoding="utf-8") == before


def test_pinning_everything_requires_an_exact_sum(ws):
    ids = [c.id for c in Rubric.from_yaml_file(RUBRIC_FIXTURE).criteria]
    _, problem = apply_gate_b_actions(ws, decision(weight_overrides=dict.fromkeys(ids, 0.3)))
    assert problem is not None and "sum to 1.5" in problem
    _, problem = apply_gate_b_actions(ws, decision(weight_overrides=dict.fromkeys(ids, 0.2)))
    assert problem is None
    assert all(w == 0.2 for w in weights(ws).values())


def test_overrides_consuming_the_whole_budget_repause(ws):
    _, problem = apply_gate_b_actions(ws, decision(weight_overrides={"letter-strength": 1.0}))
    assert problem is not None and "leaving nothing" in problem


def test_missing_rubric_is_a_problem_not_a_crash(tmp_path):
    ws = make_workspace(tmp_path)
    _, problem = apply_gate_b_actions(ws, decision())
    assert problem is not None and "rubric.yaml" in problem


# -- interpret ---------------------------------------------------------------------


def test_approved_decision_advances_to_s5_with_apply(ws):
    outcome = _interpret_b(decision(weight_overrides={"letter-strength": 0.3}))
    assert outcome.advanced and outcome.next_stage is Stage.S5
    assert outcome.apply is not None
    assert "preference-slot weight 0.2" in outcome.commit_label
    messages, problem = outcome.apply(ws)
    assert problem is None and messages


def test_undecided_gate_repauses_with_instructions():
    outcome = _interpret_b(GateBDecision(approved=False, preference_slot_weight=0.2))
    assert not outcome.advanced
    assert any("preference_slot_weight" in m for m in outcome.messages)
