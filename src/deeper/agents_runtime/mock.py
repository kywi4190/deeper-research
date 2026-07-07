"""Mock dispatch: the whole pipeline offline, zero network, zero SDK import.

MockDispatcher substitutes only the model call: it renders canned fixture
artifacts into the same `### artifact:` marker + fenced-yaml text a live agent
would emit, then flows through the identical parse/validate/retry/ledger path
in _BaseDispatcher. Integration tests (and `mode: mock` runs) therefore
exercise the real chokepoint, not a parallel code path.
"""

from __future__ import annotations

from pathlib import Path

from deeper.config import RunConfig
from deeper.workspace import Workspace

from .contracts import REPO_ROOT, AgentContract
from .dispatch import _BaseDispatcher, _Invocation

DEFAULT_FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "mock_agents"


class MockFixtureMissing(Exception):
    """A role/schema combination with no fixture file to answer from."""


class MockDispatcher(_BaseDispatcher):
    """Fixture-backed dispatcher selected by config ``mode: mock``.

    ``scripted_responses`` maps a role (or ``"role:context"``) to a list of raw
    response texts consumed one per attempt — how tests script an
    invalid-then-valid schema-retry sequence. Roles without a script answer
    from ``fixtures_dir/<role>/<schema>[.<context>].yaml``.
    """

    def __init__(
        self,
        workspace: Workspace,
        config: RunConfig,
        *,
        fixtures_dir: str | Path = DEFAULT_FIXTURES_DIR,
        scripted_responses: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(workspace, config)
        self.fixtures_dir = Path(fixtures_dir)
        self._scripts = {k: list(v) for k, v in (scripted_responses or {}).items()}

    def _script_for(self, contract: AgentContract) -> str | None:
        for key in (f"{contract.role}:{contract.context}", contract.role):
            script = self._scripts.get(key)
            if script:
                return script.pop(0)
        return None

    def _fixture_text(self, contract: AgentContract, schema: str) -> str:
        candidates = [f"{schema}.{contract.context}.yaml"] if contract.context else []
        candidates.append(f"{schema}.yaml")
        role_dir = self.fixtures_dir / contract.role
        for name in candidates:
            path = role_dir / name
            if path.is_file():
                return path.read_text(encoding="utf-8")
        raise MockFixtureMissing(
            f"no mock fixture for role={contract.role!r} schema={schema!r}: "
            f"looked for {candidates} under {role_dir}"
        )

    async def _invoke(self, prompt: str, contract: AgentContract, attempt: int) -> _Invocation:
        scripted = self._script_for(contract)
        if scripted is not None:
            text = scripted
        else:
            blocks = [
                f"### artifact: {schema}\n```yaml\n"
                f"{self._fixture_text(contract, schema).rstrip()}\n```"
                for schema in contract.output_schemas
            ]
            text = "\n\n".join(blocks) + "\n"
        return _Invocation(
            text=text,
            usd=0.0,  # mock costs nothing — truthful accounting
            input_tokens=len(prompt) // 4,  # rough token estimate keeps
            output_tokens=len(text) // 4,  # accumulation observable in tests
            num_turns=1,
            duration_ms=0,
            session_id=None,
        )
