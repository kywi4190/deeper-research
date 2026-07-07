"""Spend ledger: per-invocation cost accounting persisted into state.json (§8).

Every agent invocation — including failed schema-retry attempts, whose tokens
were still paid for — lands one SpendEntry in RunState.spend. Aggregations are
computed from the entries (RunState.spend_by_stage/by_role), so they cannot
drift from the audit trail.

Single-writer invariant: record() is a synchronous read-modify-write of
state.json with no awaits inside, so it is atomic within one event loop — but
nothing else may write state.json while agent invocations are in flight, and
stages must re-load state after a dispatch batch rather than holding a stale
RunState across one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from deeper.schemas import RunState, SpendEntry, Stage
from deeper.workspace import Workspace


class SpendLedger:
    """Cost accounting over a run workspace's state.json."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _update(self, mutate) -> RunState:
        state = self.workspace.load_state()
        mutate(state)
        state.updated_at = datetime.now(UTC)
        # No commit: per-invocation commits would drown the git audit trail;
        # stages commit once per stage completion.
        self.workspace.save_state(state)
        return state

    def record(self, entry: SpendEntry) -> None:
        """Append one invocation's spend and persist immediately."""
        self._update(lambda s: s.spend.append(entry))

    def bump_retry(self, key: str) -> int:
        """Increment and persist the schema-retry count for one invocation key
        (``stage:role:context``); returns the new count."""
        state = self._update(
            lambda s: s.retry_counts.__setitem__(key, s.retry_counts.get(key, 0) + 1)
        )
        return state.retry_counts[key]

    def spend_so_far(self, stage: Stage) -> float:
        """USD spent by agents in one stage — what gates report and caps check."""
        return self.workspace.load_state().spend_by_stage().get(stage.value, 0.0)

    def total_usd(self) -> float:
        return self.workspace.load_state().total_usd()
