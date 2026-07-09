"""S8 synthesis tests: the report carries all seven design components
(structural assertion), the code-computed tables are embedded verbatim,
annotations render as links into the claims index, the mechanical citation
pass buys exactly one synthesist retry, and incoherent reports (wrong winner)
pause instead of persisting."""

from __future__ import annotations

import pytest

from deeper.agents_runtime import AgentOutputInvalid
from deeper.report import decision_matrix_table
from deeper.schemas import DecisionReport, Rubric, ScreeningResult
from deeper.sensitivity import dual_scoreboards, render_scoreboards
from deeper.stages.s7_tournament import UPDATED_SCORES_PATH, TournamentStage
from deeper.stages.s8_synthesis import REPORT_MD_PATH, REPORT_YAML_PATH, SynthesisStage

from .helpers import FIXTURES
from .test_stages_s7 import walk_to_s7

SEVEN_SECTIONS = (
    "## 1. Recommendation",
    "## 2. Decision matrix",
    "## 3. Sensitivity analysis",
    "## 4. Dissent",
    "## 5. Residual uncertainty",
    "## 6. Next actions",
    "## 7. Appendix",
)


def fixture_response(mutate=None) -> str:
    """The synthesist fixture as one raw scripted response."""
    text = (FIXTURES / "synthesist" / "decision-report.yaml").read_text(encoding="utf-8")
    if mutate is not None:
        text = mutate(text)
    return f"### artifact: decision-report\n```yaml\n{text.rstrip()}\n```\n"


async def walk_to_s8(tmp_path, **mock_kwargs):
    """A settled tournament plus a ready SynthesisStage context."""
    ws, ctx, dispatcher, emitted = await walk_to_s7(tmp_path, **mock_kwargs)
    await TournamentStage().execute(ctx)
    return ws, ctx, dispatcher, emitted


def synthesist_dispatches(dispatcher) -> int:
    return sum(1 for role, _, _ in dispatcher.invocations if role == "synthesist")


@pytest.fixture()
async def run(tmp_path):
    ws, ctx, dispatcher, emitted = await walk_to_s8(tmp_path)
    stage = SynthesisStage()
    stage.validate_inputs(ctx)
    await stage.execute(ctx)
    return ws, ctx, stage, dispatcher, emitted


async def test_report_contains_all_seven_design_components(run):
    ws, _, _, _, _ = run
    text = ws.path(REPORT_MD_PATH).read_text(encoding="utf-8")
    for heading in SEVEN_SECTIONS:
        assert heading in text
    report = ws.read_artifact(REPORT_YAML_PATH, DecisionReport)
    assert report.winner_option_id == "sae-feature-atlas"
    assert report.dissent_unrebutted  # the fixture's dissent stands, explicitly
    assert "UNREBUTTED" in text


async def test_code_computed_tables_are_embedded_verbatim(run):
    ws, _, _, _, _ = run
    text = ws.path(REPORT_MD_PATH).read_text(encoding="utf-8")
    scores = ws.read_artifact(UPDATED_SCORES_PATH, ScreeningResult)
    rubric = ws.read_artifact("rubric.yaml", Rubric)
    assert render_scoreboards(*dual_scoreboards(scores, rubric)) in text
    assert decision_matrix_table(scores, rubric) in text
    assert "Top flag:" in text  # the code-computed sensitivity flag line


async def test_annotations_become_links_into_the_claims_index(run):
    ws, _, _, _, _ = run
    text = ws.path(REPORT_MD_PATH).read_text(encoding="utf-8")
    assert "[[c-sae-precedent]]" not in text  # every annotation was rewritten
    assert "[c-sae-precedent](#claim-sae-feature-atlas--c-sae-precedent)" in text
    assert '<a id="claim-sae-feature-atlas--c-sae-precedent"></a>' in text
    assert "### Claims index" in text
    # The appendix carries the other audited tables too.
    for heading in (
        "### Angle map",
        "### Allocation",
        "### Cut-option audit trail",
        "### Verification pass rates",
        "### Spend by stage",
    ):
        assert heading in text


async def test_stage_is_complete_and_reentry_dispatches_nothing(run):
    ws, ctx, stage, dispatcher, _ = run
    assert stage.is_complete(ctx)
    before = len(dispatcher.invocations)
    await stage.execute(ctx)
    assert len(dispatcher.invocations) == before
    # A hand-deleted markdown re-renders (the yaml alone is not completion).
    ws.path(REPORT_MD_PATH).unlink()
    assert not stage.is_complete(ctx)


async def test_citation_failure_buys_exactly_one_retry_then_succeeds(tmp_path):
    bad = fixture_response(lambda t: t.replace("[[c-sae-reuse]]", "[[c-invented]]"))
    good = fixture_response()
    ws, ctx, dispatcher, emitted = await walk_to_s8(
        tmp_path, scripted_responses={"synthesist": [bad, good]}
    )
    await SynthesisStage().execute(ctx)
    assert synthesist_dispatches(dispatcher) == 2  # one retry, then clean
    assert any("citation pass failed" in m for m in emitted)
    assert ws.path(REPORT_MD_PATH).is_file()
    # The retry prompt named the exact unresolvable annotation.
    retry_prompt = [p for r, _, p in dispatcher.invocations if r == "synthesist"][1]
    assert "c-invented" in retry_prompt and "CITATION-PASS RETRY" in retry_prompt


async def test_citation_failure_twice_pauses_instead_of_persisting(tmp_path):
    bad = fixture_response(lambda t: t.replace("[[c-sae-reuse]]", "[[c-invented]]"))
    ws, ctx, dispatcher, _ = await walk_to_s8(
        tmp_path, scripted_responses={"synthesist": [bad, bad]}
    )
    with pytest.raises(AgentOutputInvalid, match="c-invented"):
        await SynthesisStage().execute(ctx)
    assert synthesist_dispatches(dispatcher) == 2  # ONE retry, no more
    assert not ws.path(REPORT_MD_PATH).exists()
    assert not ws.path(REPORT_YAML_PATH).exists()


async def test_wrong_winner_is_rejected_as_incoherent(tmp_path):
    wrong = fixture_response(
        lambda t: t.replace(
            "winner_option_id: sae-feature-atlas",
            "winner_option_id: contamination-robust-benchmark",
        )
    )
    ws, ctx, _, _ = await walk_to_s8(tmp_path, scripted_responses={"synthesist": [wrong] * 2})
    with pytest.raises(AgentOutputInvalid, match="preference-adjusted scoreboard"):
        await SynthesisStage().execute(ctx)
    assert not ws.path(REPORT_YAML_PATH).exists()
