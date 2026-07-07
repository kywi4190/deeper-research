"""Agent dispatch layer — the single chokepoint for all subagent invocations.

Contracts and prompt assembly (`contracts`), the retry/concurrency/accounting
spine and live SDK dispatch (`dispatch`), deterministic enforcement hooks
(`hooks`), spend accounting (`ledger`), and the offline fixture-backed
dispatcher (`mock`). See docs/deeper-research-design.md §6, §8.
"""

from .contracts import (
    AgentContract,
    AgentOutputInvalid,
    AgentResult,
    ArtifactParseError,
    ContractError,
    assemble_prompt,
    load_role,
    parse_artifacts,
)
from .dispatch import (
    AgentDispatchFailed,
    Dispatcher,
    LiveDispatcher,
    SpendCapExceeded,
    create_dispatcher,
)
from .hooks import (
    PREFERENCE_READERS,
    build_hooks,
    cache_web_fetch,
    quarantine_gate,
    sanitize_source_text,
    write_scope_gate,
)
from .ledger import SpendLedger
from .mock import DEFAULT_FIXTURES_DIR, MockDispatcher, MockFixtureMissing

__all__ = [
    "AgentContract",
    "AgentDispatchFailed",
    "AgentOutputInvalid",
    "AgentResult",
    "ArtifactParseError",
    "ContractError",
    "DEFAULT_FIXTURES_DIR",
    "Dispatcher",
    "LiveDispatcher",
    "MockDispatcher",
    "MockFixtureMissing",
    "PREFERENCE_READERS",
    "SpendCapExceeded",
    "SpendLedger",
    "assemble_prompt",
    "build_hooks",
    "cache_web_fetch",
    "create_dispatcher",
    "load_role",
    "parse_artifacts",
    "quarantine_gate",
    "sanitize_source_text",
    "write_scope_gate",
]
