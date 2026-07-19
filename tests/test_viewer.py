"""Viewer tests (design §8 v2): route smoke over mock-run workspace fixtures,
and gate-form → YAML round-trip equivalence with hand-edited decision files —
the form and the editor must land on the identical validated decision, because
they share one code path (the GateDecision schemas + gates.read_decision).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from deeper.config import RunConfig, profile_config
from deeper.orchestrator import Engine, Node
from deeper.orchestrator.gates import GATE_SPECS, read_decision
from deeper.schemas import Dossier, GateBDecision, GateCDecision, GateName, Rubric, Shortlist
from deeper.stages.s5_screening import SHORTLIST_PATH
from deeper.stages.s6_deepdive import dossier_path
from deeper.viewer import create_app
from deeper.workspace import Workspace

PAGES = ("map", "options", "rubric", "contenders", "report")


def _new_run(runs_root, name: str) -> Workspace:
    data = profile_config("quick").model_dump(mode="json")
    data["goal"] = "pick a senior research project"
    return Workspace.create(runs_root / name, RunConfig.model_validate(data))


def _walk(ws: Workspace, target: Node) -> None:
    """Drive the real engine over the canned mock scenario up to `target`."""

    async def go() -> None:
        engine = Engine(ws, emit=lambda _line: None)
        assert await engine.run() is Node.GATE_A
        if target is Node.GATE_A:
            return
        ws.path("gates/gate-a.yaml").write_text("approved: true\n", encoding="utf-8")
        assert await engine.run() is Node.GATE_B
        if target is Node.GATE_B:
            return
        ws.path("gates/gate-b.yaml").write_text(
            "approved: true\npreference_slot_weight: 0.2\n", encoding="utf-8"
        )
        assert await engine.run() is Node.GATE_C
        if target is Node.GATE_C:
            return
        ws.path("gates/gate-c.yaml").write_text("approved: true\n", encoding="utf-8")
        assert await engine.run() is Node.DONE

    asyncio.run(go())


@pytest.fixture(scope="module")
def runs_root(tmp_path_factory):
    """One runs directory holding a run paused at each interesting node."""
    root = tmp_path_factory.mktemp("viewer-runs")
    _walk(_new_run(root, "at-gate-b"), Node.GATE_B)
    _walk(_new_run(root, "at-gate-c"), Node.GATE_C)
    _walk(_new_run(root, "done"), Node.DONE)
    return root


@pytest.fixture(scope="module")
def client(runs_root):
    return TestClient(create_app(runs_root))


# -- route smoke ------------------------------------------------------------------


def test_run_list_shows_every_run_with_node_and_spend(client):
    page = client.get("/")
    assert page.status_code == 200
    for name, node in (("at-gate-b", "gate-b"), ("at-gate-c", "gate-c"), ("done", "done")):
        assert name in page.text and node in page.text
    assert "spend vs cap" in page.text and "$" in page.text


@pytest.mark.parametrize("run", ["at-gate-b", "at-gate-c", "done"])
@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_at_every_node(client, run, page):
    assert client.get(f"/run/{run}/{page}").status_code == 200


def test_unknown_and_traversal_run_names_404(client):
    assert client.get("/run/no-such-run/map").status_code == 404
    assert client.get("/run/%2e%2e/map").status_code == 404


def test_angle_map_page_shows_priors_and_allocation(client):
    page = client.get("/run/done/map").text
    assert "interpretability-research" in page
    assert "relevance prior" in page and "allocated budget" in page and "units" in page


def test_options_page_filters_by_angle(client, runs_root):
    everything = client.get("/run/done/options").text
    assert "sae-feature-atlas" in everything
    filtered = client.get("/run/done/options?angle=evaluation-science").text
    assert "contamination-robust-benchmark" in filtered
    assert "sae-feature-atlas" not in filtered
    assert "redundancy" in everything  # critique badges render


def test_contenders_page_shows_boards_dossiers_and_adversarial_material(client, runs_root):
    page = client.get("/run/at-gate-c/contenders").text
    assert "destination-only" in page and "preference-adjusted" in page
    assert "inverted" in page  # the mock tournament engineers a rank inversion
    assert "Prosecution" in page and "Regret path" in page
    assert "Frame check" in page and "slot weight" in page
    shortlist = Workspace.open(runs_root / "at-gate-c").read_artifact(SHORTLIST_PATH, Shortlist)
    for option_id in shortlist.finalist_ids:
        assert option_id in page


def test_report_page_renders_markdown_with_claim_anchors(client):
    page = client.get("/run/done/report").text
    assert 'id="claim-' in page  # dossier-claim anchors survive the render
    assert 'href="#claim-' in page  # inline citations link to them
    assert "<h2" in page  # actual markdown rendering, not a <pre> dump
    no_report = client.get("/run/at-gate-b/report").text
    assert "No report yet" in no_report


# -- gate forms: one code path with the CLI ---------------------------------------


def test_gate_b_form_roundtrips_like_a_hand_edited_file(client, runs_root):
    ws = Workspace.open(runs_root / "at-gate-b")
    criterion = ws.read_artifact("rubric.yaml", Rubric).criteria[0]
    response = client.post(
        "/run/at-gate-b/rubric",
        data={
            "approved": "on",
            "preference_slot_weight": "0.25",
            f"weight:{criterion.id}": "0.3",
            "notes": "set from the viewer",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    hand_edited = GateBDecision.load_yaml(
        "approved: true\n"
        "preference_slot_weight: 0.25\n"
        f"weight_overrides:\n  {criterion.id}: 0.3\n"
        "notes: set from the viewer\n"
    )
    decision, problem = read_decision(ws, GATE_SPECS[GateName.B])
    assert problem is None
    assert decision == hand_edited


def test_gate_c_loop_form_roundtrips_like_a_hand_edited_file(client, runs_root):
    ws = Workspace.open(runs_root / "at-gate-c")
    shortlist = ws.read_artifact(SHORTLIST_PATH, Shortlist)
    first, second = shortlist.finalist_ids[0], shortlist.finalist_ids[1]
    claim = ws.read_artifact(dossier_path(first), Dossier).claims[0]
    response = client.post(
        "/run/at-gate-c/contenders",
        data={
            "pf_option": [first, second],
            "pf_reaction": ["the ops burden bothers me", ""],  # blank row: not submitted
            "pf_direction": ["negative", "neutral"],
            "ec_option": [first, second],
            "ec_claim": [claim.id, ""],
            "ec_challenge": ["I don't believe this", ""],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    hand_edited = GateCDecision.load_yaml(
        "approved: false\n"
        "preference_feedback:\n"
        f"  - option_id: {first}\n"
        "    reaction: the ops burden bothers me\n"
        "    direction: negative\n"
        "evidence_challenges:\n"
        f"  - option_id: {first}\n"
        f"    claim_id: {claim.id}\n"
        "    challenge: I don't believe this\n"
    )
    decision, problem = read_decision(ws, GATE_SPECS[GateName.C])
    assert problem is None
    assert decision == hand_edited


def test_invalid_gate_c_combination_is_rejected_by_the_schema_and_writes_nothing(client, runs_root):
    """Approval + loop actions violates the GateCDecision validator; the form
    surfaces THAT schema's message and leaves the file untouched."""
    ws = Workspace.open(runs_root / "at-gate-c")
    target = ws.path("gates/gate-c.yaml")
    before = target.read_text(encoding="utf-8")
    first = ws.read_artifact(SHORTLIST_PATH, Shortlist).finalist_ids[0]
    response = client.post(
        "/run/at-gate-c/contenders",
        data={
            "approved": "on",
            "pf_option": [first],
            "pf_reaction": ["but also this"],
            "pf_direction": ["negative"],
        },
    )
    assert response.status_code == 200
    assert "approve means proceed to synthesis" in response.text
    assert target.read_text(encoding="utf-8") == before


def test_gate_form_refuses_a_run_where_that_gate_is_not_pending(client, runs_root):
    ws = Workspace.open(runs_root / "done")
    before = ws.path("gates/gate-b.yaml").read_text(encoding="utf-8")
    response = client.post(
        "/run/done/rubric",
        data={"approved": "on", "preference_slot_weight": "0.1"},
    )
    assert response.status_code == 200
    assert "not pending" in response.text
    assert ws.path("gates/gate-b.yaml").read_text(encoding="utf-8") == before


def test_viewer_written_gate_b_decision_advances_the_real_engine(tmp_path):
    """The strongest one-code-path proof: a decision written by the form is
    consumed by `deeper resume`'s own machinery and applied to the rubric."""
    ws = _new_run(tmp_path, "fresh")
    _walk(ws, Node.GATE_B)
    local_client = TestClient(create_app(tmp_path))
    response = local_client.post(
        "/run/fresh/rubric",
        data={"approved": "on", "preference_slot_weight": "0.3"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    async def resume() -> Node:
        return await Engine(ws, emit=lambda _line: None).run()

    assert asyncio.run(resume()) is Node.GATE_C
    assert ws.read_artifact("rubric.yaml", Rubric).preference_slot.weight == 0.3
