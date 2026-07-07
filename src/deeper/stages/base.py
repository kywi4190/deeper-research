"""Stage protocol: what the orchestrator engine runs (design §8).

A stage is a class, not a function, because the engine needs to interrogate it
from the outside: are your inputs present and valid? which outputs must exist
when you claim completion? are you already complete (crash-safe re-entry)?
Stages decide *content* by dispatching agents; they never decide sequencing,
budgets, or stop-rule policy — those numbers come from RunConfig (design P8).

Idempotency contract: `execute()` must check for already-completed sub-work
before dispatching (e.g. a per-cartographer raw report that already validates
is not re-run), because `deeper resume` after ANY interruption re-enters the
current stage. The engine adds a whole-stage shortcut on top: when
`is_complete()` — every declared output exists and validates — the stage is
skipped entirely.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import yaml
from pydantic import ValidationError

from deeper.agents_runtime import Dispatcher
from deeper.config import RunConfig
from deeper.schemas import ArtifactModel, Stage
from deeper.workspace import Workspace, WorkspaceError


class NotImplementedYet(Exception):
    """A stage registered for machine shape but not yet built — the engine
    reports it cleanly and leaves state untouched, so the run resumes once the
    stage lands."""


class StageInputsMissing(Exception):
    """A required input artifact is absent or invalid — the pipeline contract
    (P2: a stage cannot start until its inputs validate) was broken upstream."""


class StageInterrupted(Exception):
    """The stage stopped on a human decision it cannot make itself (declined
    confirmation, interaction needed in a non-interactive session). The engine
    reports it cleanly and leaves state untouched — `deeper resume` re-enters."""


@dataclass
class StageContext:
    """Everything a stage may touch. The dispatcher is the only path to an LLM."""

    workspace: Workspace
    config: RunConfig
    dispatcher: Dispatcher
    emit: Callable[[str], None] = field(default=lambda _line: None)
    # Terminal Q&A channel (S0 interview + confirmations). None when the session
    # is non-interactive — stages must degrade gracefully, never block.
    ask_user: Callable[[str], str] | None = None


class StageBase:
    """One pipeline stage. Subclasses set `stage` and `required_inputs`, and
    implement `execute()` (and `outputs()` when they produce artifacts)."""

    stage: Stage
    # (relpath, model) pairs that must exist and validate before execute().
    required_inputs: tuple[tuple[str, type[ArtifactModel]], ...] = ()

    def validate_inputs(self, ctx: StageContext) -> None:
        """Schema-check that every required input artifact exists and validates."""
        problems = []
        for relpath, model in self.required_inputs:
            try:
                ctx.workspace.read_artifact(relpath, model)
            except (WorkspaceError, ValidationError, yaml.YAMLError) as err:
                problems.append(f"- {relpath}: {err}")
        if problems:
            raise StageInputsMissing(
                f"stage {self.stage.value} cannot start; required inputs are missing "
                "or invalid:\n" + "\n".join(problems)
            )

    async def execute(self, ctx: StageContext) -> None:
        raise NotImplementedYet(f"stage {self.stage.value} is not implemented yet")

    def evaluate_stop_rules(self, ctx: StageContext) -> None:
        """Deterministic diminishing-returns checks (§12). Stages with expansion
        loops (S1 saturation, S3 redundancy, S6 stability) override; the default
        is a no-op because most stages run to completion in one pass."""

    def outputs(self, ctx: StageContext) -> list[tuple[str, type[ArtifactModel]]]:
        """(relpath, model) pairs this stage must have produced when complete.
        Takes ctx because later stages' outputs are data-dependent (one card set
        per allocated angle, one dossier per finalist)."""
        return []

    def is_complete(self, ctx: StageContext) -> bool:
        """True when every declared output exists and validates — the engine's
        idempotent-re-entry shortcut. Stages with no declarable outputs yet
        (stubs) are never complete."""
        declared = self.outputs(ctx)
        if not declared:
            return False
        for relpath, model in declared:
            try:
                ctx.workspace.read_artifact(relpath, model)
            except (WorkspaceError, ValidationError, yaml.YAMLError):
                return False
        return True
