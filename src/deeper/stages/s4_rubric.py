"""S4 — Rubric construction (design §5/S4).

The rubric-builder (Opus-class) derives the value function from the destination
model and — for exactly one purpose, learning which dimensions differentiate
the space — the option cards. It never sees preferences: the quarantine hook
enforces that for tool access, and this stage simply never puts the file in the
contract (the prompt string is the only parent→child channel).

The coverage report's rubric-weight strategic notes ride along as candidate
evidence about what the judge rewards (README "Design deviations": the
strategic-notes channel routes rubric-weight → S4); the builder accepts or
rejects them with a stated reason, the destination model staying the anchor.

The preference slot's weight is process, not content (design P8): whatever the
agent emits, code overwrites the slot with the config default before writing
rubric.yaml — the human sets the real number at Gate B. rubric-rationale.md is
a rendered view of the validated rubric (the structured file is the artifact),
written first so a completed rubric.yaml always has its rationale beside it.
"""

from __future__ import annotations

from deeper.agents_runtime import AgentContract
from deeper.config import RunConfig, SizeClass
from deeper.schemas import (
    AllocationTable,
    CoverageReport,
    DestinationModel,
    OptionCardSet,
    PreferenceSlot,
    Rubric,
    StrategicNoteKind,
)
from deeper.schemas import Stage as StageEnum

from .base import StageBase, StageContext, StageInputsMissing
from .s3_scouting import cards_path

RUBRIC_PATH = "rubric.yaml"
RATIONALE_PATH = "rubric-rationale.md"


def require_cards(ctx: StageContext, stage_name: str) -> None:
    """Dynamic input check shared by S4/S5: every allocated angle's card set
    must exist and validate (the static required_inputs tuple cannot express
    data-dependent inputs)."""
    table = ctx.workspace.read_artifact("allocation.yaml", AllocationTable)
    problems = []
    for row in table.rows:
        if row.units <= 0:
            continue
        try:
            ctx.workspace.read_artifact(cards_path(row.angle_id), OptionCardSet)
        except Exception as err:  # noqa: BLE001 — reported, not swallowed
            problems.append(f"- {cards_path(row.angle_id)}: {err}")
    if problems:
        raise StageInputsMissing(
            f"stage {stage_name} cannot start; option cards are missing or invalid:\n"
            + "\n".join(problems)
        )


def render_rationale(rubric: Rubric, config: RunConfig) -> str:
    """rubric-rationale.md: the rubric's reasoning as prose, straight from the
    validated model — one heading per criterion, weight and justification."""
    lines = [
        "# Rubric rationale",
        "",
        "Derived from the destination model (never from preferences); criterion",
        "weights sum to 1.0 and the preference slot is weighted separately (P9).",
        "",
    ]
    for c in rubric.criteria:
        lines += [
            f"## {c.name} — weight {c.weight:g}",
            "",
            f"**Measures:** {c.definition}",
            "",
            f"**What moves this score:** {c.measurement_method}",
            "",
            f"**Why this weight:** {c.justification}",
            "",
        ]
    lines += [
        f"## Preference slot — weight {rubric.preference_slot.weight:g}",
        "",
        "Reserved and content-free: the one bounded place where the user's tastes",
        "may bend the destination-optimal answer. The weight above is the config",
        f"default ({config.preference_slot_default_weight:g}); the human sets the",
        "final value at Gate B, and the report shows the ranking's sensitivity to it.",
        "",
    ]
    if rubric.notes:
        lines += ["## Builder notes", "", rubric.notes, ""]
    return "\n".join(lines)


class RubricStage(StageBase):
    stage = StageEnum.S4
    required_inputs = (
        ("destination.md", DestinationModel),
        ("allocation.yaml", AllocationTable),
        ("angles/map-report.md", CoverageReport),
    )

    def outputs(self, ctx: StageContext):
        return [(RUBRIC_PATH, Rubric)]

    def validate_inputs(self, ctx: StageContext) -> None:
        super().validate_inputs(ctx)
        require_cards(ctx, self.stage.value)

    async def execute(self, ctx: StageContext) -> None:
        table = ctx.workspace.read_artifact("allocation.yaml", AllocationTable)
        inputs: dict[str, str] = {
            "destination": ctx.workspace.path("destination.md").read_text(encoding="utf-8"),
        }
        n_cards = 0
        for row in table.rows:
            if row.units <= 0:
                continue
            text = ctx.workspace.path(cards_path(row.angle_id)).read_text(encoding="utf-8")
            inputs[f"cards ({row.angle_id})"] = text
            n_cards += 1
        coverage = ctx.workspace.read_artifact("angles/map-report.md", CoverageReport)
        weight_notes = [
            n for n in coverage.strategic_notes if n.kind is StrategicNoteKind.RUBRIC_WEIGHT
        ]
        if weight_notes:
            inputs["strategic-notes (rubric-weight)"] = "\n".join(
                n.dump_yaml() for n in weight_notes
            )
        contract = AgentContract(
            role="rubric-builder",
            stage=StageEnum.S4,
            input_artifacts=inputs,
            output_schemas=("rubric",),
            size_class=SizeClass.L,
            budget_line=(
                "No web research: derive 5-9 criteria from the destination model; the "
                "cards only tell you which dimensions discriminate."
            ),
        )
        ctx.emit(f"S4: rubric-builder reading destination + {n_cards} card sets…")
        result = await ctx.dispatcher.run_agent(contract)
        rubric = result.artifacts["rubric"]
        assert isinstance(rubric, Rubric)
        # The slot weight is a process knob (config default now, Gate B's number
        # later) — never the agent's call.
        rubric = rubric.model_copy(
            update={
                "preference_slot": PreferenceSlot(weight=ctx.config.preference_slot_default_weight)
            }
        )
        ctx.workspace.path(RATIONALE_PATH).write_text(
            render_rationale(rubric, ctx.config), encoding="utf-8", newline="\n"
        )
        ctx.workspace.write_artifact(RUBRIC_PATH, rubric)
        ctx.emit(
            f"S4: rubric written — {len(rubric.criteria)} criteria, preference slot "
            f"{rubric.preference_slot.weight:g} (set the final value at Gate B); "
            "rationale at rubric-rationale.md"
        )
