"""The viewer (design §8, v2): a single-process FastAPI app over the workspace.

The pipeline is the kernel; this is a viewer. It READS the plain-file run
workspace and WRITES exactly one kind of file: gate decision YAML, through the
SAME GateDecision schemas and `read_decision` machinery `deeper resume`
validates — one code path for gate validation, whether the decision came from
an editor or a form. No database, no state of its own: close the tab and
nothing is lost. Server-rendered Jinja templates + HTMX (`hx-boost`), no build
step; every page degrades to plain links and forms without JavaScript.
"""

from __future__ import annotations

from pathlib import Path

import markdown as md
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from deeper.orchestrator.engine import node_of
from deeper.orchestrator.gates import GATE_SPECS, read_decision
from deeper.schemas import (
    AllocationTable,
    AngleMap,
    CardCritique,
    Dossier,
    FrameCheck,
    GateName,
    OptionCardSet,
    Prosecution,
    Rubric,
    ScreeningResult,
    Shortlist,
    Steelman,
    VerificationReport,
    format_validation_error,
)
from deeper.sensitivity import (
    dual_scoreboards,
    preference_sweep,
    rank_inversions,
    weight_sensitivity,
)
from deeper.stages.s3_scouting import cards_path, critique_path
from deeper.stages.s5_screening import SHORTLIST_PATH
from deeper.stages.s6_deepdive import SCORES_PATH, dossier_path, verification_path
from deeper.stages.s7_tournament import (
    FRAME_CHECK_PATH,
    UPDATED_SCORES_PATH,
    prosecution_path,
    steelman_path,
)
from deeper.stages.s8_synthesis import REPORT_MD_PATH
from deeper.workspace import Workspace, WorkspaceError

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _read(ws: Workspace, relpath: str, model):
    """An artifact, or None while its stage has not (validly) produced it yet —
    every page renders whatever of the run exists so far."""
    try:
        return ws.read_artifact(relpath, model)
    except (WorkspaceError, ValidationError, yaml.YAMLError):
        return None


def create_app(runs_dir: str | Path) -> FastAPI:
    runs_dir = Path(runs_dir)
    app = FastAPI(title="deeper viewer")

    def _ws(name: str) -> Workspace:
        if name != Path(name).name or name in (".", ".."):  # a child dir name, never a path
            raise HTTPException(status_code=404, detail=f"no run named {name!r}")
        try:
            return Workspace.open(runs_dir / name)
        except WorkspaceError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    def _page(request: Request, template: str, name: str, ws: Workspace, extra: dict):
        state = ws.load_state()
        context = {"run": name, "node": node_of(state).value, "state": state, **extra}
        return TEMPLATES.TemplateResponse(request=request, name=template, context=context)

    def _write_gate(ws: Workspace, gate: GateName, payload: dict) -> str | None:
        """The one write this app performs. The form payload goes through the
        gate's own decision schema; only a valid decision reaches disk, and the
        file written is byte-for-byte what `deeper resume` will re-validate."""
        if ws.load_state().pending_gate != gate:
            return f"{gate.value} is not pending for this run — nothing was written"
        spec = GATE_SPECS[gate]
        try:
            decision = spec.model.model_validate(payload)
        except ValidationError as err:
            return format_validation_error(err, spec.model)
        ws.path(spec.relpath).write_text(decision.dump_yaml(), encoding="utf-8", newline="\n")
        return None

    def _gate_file_state(ws: Workspace, gate: GateName) -> dict:
        spec = GATE_SPECS[gate]
        decision, problem = (
            read_decision(ws, spec) if ws.path(spec.relpath).is_file() else (None, None)
        )
        return {"decision": decision, "decision_problem": problem}

    # -- pages ---------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def run_list(request: Request):
        runs = []
        for child in sorted(runs_dir.iterdir()) if runs_dir.is_dir() else []:
            if not (child / "state.json").is_file():
                continue
            try:
                ws = Workspace.open(child)
                state, config = ws.load_state(), ws.load_config()
            except (WorkspaceError, ValidationError, yaml.YAMLError):
                continue
            spent = state.total_usd()
            runs.append(
                {
                    "name": child.name,
                    "goal": config.goal,
                    "node": node_of(state).value,
                    "status": state.status.value,
                    "spent": spent,
                    "cap": config.max_spend_usd,
                    "pct": min(100.0, spent / config.max_spend_usd * 100),
                }
            )
        return TEMPLATES.TemplateResponse(request=request, name="runs.html", context={"runs": runs})

    @app.get("/run/{name}")
    def run_home(name: str):
        return RedirectResponse(f"/run/{name}/map")

    @app.get("/run/{name}/map", response_class=HTMLResponse)
    def angle_map_page(request: Request, name: str):
        ws = _ws(name)
        angle_map = _read(ws, "angles/map.yaml", AngleMap)
        allocation = _read(ws, "allocation.yaml", AllocationTable)
        units = {r.angle_id: r.units for r in allocation.rows} if allocation else {}
        angles = sorted(angle_map.angles, key=lambda a: -a.relevance_prior) if angle_map else []
        return _page(
            request,
            "map.html",
            name,
            ws,
            {
                "angles": angles,
                "units": units,
                "max_units": max(units.values(), default=0),
                "total_units": allocation.total_budget_units if allocation else None,
            },
        )

    @app.get("/run/{name}/options", response_class=HTMLResponse)
    def options_page(request: Request, name: str, angle: str | None = None):
        ws = _ws(name)
        angle_map = _read(ws, "angles/map.yaml", AngleMap)
        groups = []
        for a in angle_map.angles if angle_map else []:
            cards = _read(ws, cards_path(a.id), OptionCardSet)
            if cards is None or (angle and a.id != angle):
                continue
            critique = _read(ws, critique_path(a.id), CardCritique)
            issues: dict[str, list[str]] = {}
            for ci in critique.completeness_issues if critique else []:
                issues.setdefault(ci.card_id, []).append(ci.issue)
            groups.append(
                {"angle": a, "cards": cards.cards, "critique": critique, "issues": issues}
            )
        return _page(
            request,
            "options.html",
            name,
            ws,
            {
                "groups": groups,
                "angles": [a for a in (angle_map.angles if angle_map else [])],
                "selected": angle,
            },
        )

    def _rubric_ctx(ws: Workspace) -> dict:
        return {
            "rubric": _read(ws, "rubric.yaml", Rubric),
            "pending": ws.load_state().pending_gate == GateName.B,
            **_gate_file_state(ws, GateName.B),
        }

    @app.get("/run/{name}/rubric", response_class=HTMLResponse)
    def rubric_page(request: Request, name: str, written: bool = False):
        ws = _ws(name)
        return _page(request, "rubric.html", name, ws, {**_rubric_ctx(ws), "written": written})

    @app.post("/run/{name}/rubric", response_class=HTMLResponse)
    async def rubric_submit(request: Request, name: str):
        ws = _ws(name)
        form = await request.form()
        payload: dict = {
            "approved": form.get("approved") == "on",
            "preference_slot_weight": form.get("preference_slot_weight", ""),
            "weight_overrides": {
                key.removeprefix("weight:"): value
                for key, value in form.multi_items()
                if key.startswith("weight:") and str(value).strip()
            },
        }
        if str(form.get("notes", "")).strip():
            payload["notes"] = str(form.get("notes")).strip()
        problem = _write_gate(ws, GateName.B, payload)
        if problem:
            return _page(request, "rubric.html", name, ws, {**_rubric_ctx(ws), "error": problem})
        return RedirectResponse(f"/run/{name}/rubric?written=1", status_code=303)

    def _contenders_ctx(ws: Workspace) -> dict:
        state, config = ws.load_state(), ws.load_config()
        rubric = _read(ws, "rubric.yaml", Rubric)
        scores = _read(ws, UPDATED_SCORES_PATH, ScreeningResult) or _read(
            ws, SCORES_PATH, ScreeningResult
        )
        shortlist = _read(ws, SHORTLIST_PATH, Shortlist)
        ctx: dict = {
            "boards": None,
            "frame_check": _read(ws, FRAME_CHECK_PATH, FrameCheck),
            "pending": state.pending_gate == GateName.C,
            "iterations": state.gate_c_iterations,
            "cap": config.caps.max_gate_c_loops,
            "capped": state.gate_c_iterations >= config.caps.max_gate_c_loops,
            **_gate_file_state(ws, GateName.C),
        }
        if rubric and scores:
            dest, adjusted = dual_scoreboards(scores, rubric)
            inversions = rank_inversions(dest, adjusted)
            weights = {c.id: c.weight for c in rubric.criteria}
            flips = weight_sensitivity(scores.options, weights, rubric.preference_slot.weight)
            top = (
                max((abs(f.flip_delta) for f in flips if f.flip_delta is not None), default=0)
                or 1.0
            )
            ctx.update(
                boards={"dest": {r.option_id: r for r in dest}, "adjusted": adjusted},
                inversions=inversions,
                inverted={option_id for pair in inversions for option_id in pair},
                slot_weight=rubric.preference_slot.weight,
                # Bar length = |weight delta| to a rank-1<->2 flip: SHORTER = more
                # fragile; a criterion with no in-range flip renders full, muted.
                flip_bars=[
                    {
                        "id": f.criterion_id,
                        "weight": f.weight,
                        "delta": f.flip_delta,
                        "pct": 100 if f.flip_delta is None else abs(f.flip_delta) / top * 100,
                    }
                    for f in flips
                ],
                sweep=preference_sweep(scores.options, weights),
            )
        by_id = {o.option_id: o for o in scores.options} if scores else {}
        ctx["finalists"] = [
            {
                "id": option_id,
                "screening": by_id.get(option_id),
                "dossier": _read(ws, dossier_path(option_id), Dossier),
                "verification": _read(ws, verification_path(option_id), VerificationReport),
                "prosecution": _read(ws, prosecution_path(option_id), Prosecution),
                "steelman": _read(ws, steelman_path(option_id), Steelman),
            }
            for option_id in (shortlist.finalist_ids if shortlist else [])
        ]
        return ctx

    @app.get("/run/{name}/contenders", response_class=HTMLResponse)
    def contenders_page(request: Request, name: str, written: bool = False):
        ws = _ws(name)
        return _page(
            request, "contenders.html", name, ws, {**_contenders_ctx(ws), "written": written}
        )

    @app.post("/run/{name}/contenders", response_class=HTMLResponse)
    async def contenders_submit(request: Request, name: str):
        ws = _ws(name)
        form = await request.form()
        # A row someone started is submitted (and schema-checked), never
        # silently dropped; a row left entirely blank was not started.
        feedback = [
            {"option_id": option, "reaction": reaction, "direction": direction}
            for option, reaction, direction in zip(
                form.getlist("pf_option"),
                form.getlist("pf_reaction"),
                form.getlist("pf_direction"),
                strict=False,
            )
            if str(reaction).strip()
        ]
        challenges = [
            {"option_id": option, "claim_id": claim, "challenge": challenge}
            for option, claim, challenge in zip(
                form.getlist("ec_option"),
                form.getlist("ec_claim"),
                form.getlist("ec_challenge"),
                strict=False,
            )
            if str(claim).strip() or str(challenge).strip()
        ]
        payload: dict = {
            "approved": form.get("approved") == "on",
            "accept_redivergence": form.get("accept_redivergence") == "on",
            "preference_feedback": feedback,
            "evidence_challenges": challenges,
        }
        if str(form.get("notes", "")).strip():
            payload["notes"] = str(form.get("notes")).strip()
        problem = _write_gate(ws, GateName.C, payload)
        if problem:
            return _page(
                request, "contenders.html", name, ws, {**_contenders_ctx(ws), "error": problem}
            )
        return RedirectResponse(f"/run/{name}/contenders?written=1", status_code=303)

    @app.get("/run/{name}/report", response_class=HTMLResponse)
    def report_page(request: Request, name: str):
        ws = _ws(name)
        target = ws.path(REPORT_MD_PATH)
        html = (
            md.markdown(target.read_text(encoding="utf-8"), extensions=["tables"])
            if target.is_file()
            else None
        )
        return _page(request, "report.html", name, ws, {"html": html})

    return app
