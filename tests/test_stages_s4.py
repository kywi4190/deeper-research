"""S4 rubric-stage tests: destination + all cards (+ rubric-weight strategic
notes) in, preferences structurally out, slot weight owned by config."""

from __future__ import annotations

import pytest

from deeper.schemas import Rubric
from deeper.stages.s3_scouting import ScoutingStage
from deeper.stages.s4_rubric import RubricStage, render_rationale

from .helpers import (
    RecordingMockDispatcher,
    make_ctx,
    make_workspace,
    write_s0_artifacts,
    write_s1_s2_artifacts,
)


@pytest.fixture()
async def run(tmp_path):
    ws = make_workspace(tmp_path)
    write_s0_artifacts(ws)
    write_s1_s2_artifacts(ws)
    dispatcher = RecordingMockDispatcher(ws, ws.load_config())
    ctx = make_ctx(ws, dispatcher=dispatcher)
    await ScoutingStage().execute(ctx)
    stage = RubricStage()
    stage.validate_inputs(ctx)
    await stage.execute(ctx)
    return ws, ctx, stage, dispatcher


async def test_rubric_written_with_config_slot_weight(run):
    ws, ctx, stage, _ = run
    rubric = ws.read_artifact("rubric.yaml", Rubric)
    assert 5 <= len(rubric.criteria) <= 9
    assert rubric.preference_slot.weight == ctx.config.preference_slot_default_weight == 0.2
    assert stage.is_complete(ctx)


async def test_slot_weight_is_config_owned_not_agent_owned(tmp_path):
    ws = make_workspace(tmp_path, caps=None)
    # Override the config default; the fixture agent still emits 0.2.
    config_text = ws.path("config.yaml").read_text(encoding="utf-8")
    ws.path("config.yaml").write_text(
        config_text + "preference_slot_default_weight: 0.3\n", encoding="utf-8"
    )
    write_s0_artifacts(ws)
    write_s1_s2_artifacts(ws)
    ctx = make_ctx(ws)
    await ScoutingStage().execute(ctx)
    await RubricStage().execute(make_ctx(ws))
    assert ws.read_artifact("rubric.yaml", Rubric).preference_slot.weight == 0.3


async def test_rationale_rendered_beside_the_rubric(run):
    ws, ctx, _, _ = run
    text = ws.path("rubric-rationale.md").read_text(encoding="utf-8")
    rubric = ws.read_artifact("rubric.yaml", Rubric)
    for criterion in rubric.criteria:
        assert criterion.name in text
        assert criterion.justification.split()[0] in text
    assert "Preference slot" in text and "Gate B" in text
    assert text == render_rationale(rubric, ctx.config)


async def test_contract_carries_destination_cards_and_weight_notes_only(run):
    ws, _, _, dispatcher = run
    role, context, prompt = next(
        (r, c, p) for r, c, p in dispatcher.invocations if r == "rubric-builder"
    )
    # Every allocated angle's cards travel in the contract...
    for angle in ("interpretability-research", "negative-results-science"):
        assert f"### input: cards ({angle})" in prompt
    # ...the merged top-up card included (S4 runs on post-reflow sets)...
    assert "feature-steering-study" in prompt
    # ...plus the coverage report's rubric-weight strategic note as candidate
    # judge-reward evidence (README design deviation; build guide Prompt 8)...
    assert "### input: strategic-notes (rubric-weight)" in prompt
    assert "strongest specific letters" in prompt
    # ...but never the execution-kind note, and never preferences.
    assert "workshop paper first" not in prompt
    marker = ws.path("preferences.yaml").read_text(encoding="utf-8").strip().splitlines()[0]
    assert marker not in prompt
