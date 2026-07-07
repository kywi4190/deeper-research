"""S0 intake tests: the interactive interview loop, the question-budget cap,
the confirmation step, and the constraint/preference artifact separation the
mock interview fixture encodes."""

from __future__ import annotations

import pytest

from deeper.agents_runtime import MockDispatcher
from deeper.schemas import Brief, DestinationModel, Preferences
from deeper.stages import IntakeStage, StageInterrupted

from .helpers import (
    RecordingMockDispatcher,
    interviewer_artifacts_text,
    make_ctx,
    make_workspace,
)


async def test_preference_stated_as_constraint_lands_in_preferences_not_brief(tmp_path):
    """The design's central anti-overfitting move (§5/S0): the fixture interview
    contains 'must be a machine learning project', challenged and reclassified —
    it must be quarantined in preferences.yaml, never a brief constraint."""
    ws = make_workspace(tmp_path)
    await IntakeStage().execute(make_ctx(ws))
    brief = ws.read_artifact("brief.md", Brief)
    prefs = ws.read_artifact("preferences.yaml", Preferences)
    ws.read_artifact("destination.md", DestinationModel)
    assert any("machine learning" in item.statement.lower() for item in prefs.items)
    assert all("machine learning" not in c.statement.lower() for c in brief.constraints)
    # And the split is preserved as a strong preference, not watered down.
    ml = next(i for i in prefs.items if "machine learning" in i.statement.lower())
    assert ml.strength.value == "strong"


async def test_interview_streams_questions_and_feeds_answers_back(tmp_path):
    ws = make_workspace(tmp_path)
    question = "Is ML required by the program, or is it what you want to work on?"
    answer = "It's what I want to work on — the program does not require it"
    dispatcher = RecordingMockDispatcher(
        ws,
        ws.load_config(),
        scripted_responses={"interviewer": [question, interviewer_artifacts_text()]},
    )
    asked: list[str] = []

    def ask(q: str) -> str:
        asked.append(q)
        return answer if q == question else "y"

    await IntakeStage().execute(make_ctx(ws, dispatcher=dispatcher, ask_user=ask))
    assert asked[0] == question  # the agent's question reached the user
    final_prompt = dispatcher.invocations[-1][2]
    assert question in final_prompt and answer in final_prompt  # transcript fed back
    assert asked[-1].startswith("Confirm this brief?")
    assert ws.path("brief.md").is_file()


async def test_question_budget_cap_enforced(tmp_path):
    """With a 1-question budget, a second question never reaches the user: the
    over-budget turn is disciplined like a schema failure and the agent must
    emit artifacts."""
    ws = make_workspace(tmp_path, caps={"max_interview_questions": 1})
    scripted = {
        "interviewer": [
            "First question?",
            "Second question over budget?",
            interviewer_artifacts_text(),
        ]
    }
    asked: list[str] = []

    def ask(q: str) -> str:
        asked.append(q)
        return "y" if q.startswith("Confirm") else "an answer"

    ctx = make_ctx(
        ws,
        dispatcher=MockDispatcher(ws, ws.load_config(), scripted_responses=scripted),
        ask_user=ask,
    )
    await IntakeStage().execute(ctx)
    interview_questions = [q for q in asked if not q.startswith("Confirm")]
    assert interview_questions == ["First question?"]
    assert ws.load_state().retry_counts.get("S0:interviewer:-") == 1
    assert ws.path("brief.md").is_file()


async def test_summary_printed_and_decline_writes_nothing(tmp_path):
    ws = make_workspace(tmp_path)
    emitted: list[str] = []
    seen_summary_before_confirm: list[bool] = []

    def ask(q: str) -> str:
        seen_summary_before_confirm.append(any("brief summary" in line for line in emitted))
        return "no, that misses the point"

    with pytest.raises(StageInterrupted):
        await IntakeStage().execute(make_ctx(ws, emitted=emitted, ask_user=ask))
    assert seen_summary_before_confirm == [True]  # summary shown before the question
    assert not ws.path("brief.md").exists()
    assert not ws.path("preferences.yaml").exists()


async def test_non_interactive_session_finalizes_without_confirmation(tmp_path):
    ws = make_workspace(tmp_path)
    await IntakeStage().execute(make_ctx(ws))  # ask_user=None
    assert ws.path("brief.md").is_file()


async def test_missing_goal_interrupts_cleanly(tmp_path):
    ws = make_workspace(tmp_path, goal=" ")
    with pytest.raises(StageInterrupted, match="no goal"):
        await IntakeStage().execute(make_ctx(ws))
