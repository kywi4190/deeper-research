"""The one fan-out primitive stages use (M2 finding 9).

Bare asyncio.gather propagates the first child exception immediately and
leaves the siblings running as orphaned tasks until loop shutdown — for live
dispatches that means SDK subprocess transports torn down by GC luck
("Event loop is closed" spew on Windows Proactor) and CLI processes whose
pending hook control-requests die mid-stream ("Tool permission stream
closed"). gather_strict cancels the siblings on first failure and AWAITS the
drain, so every generator's aclose → transport close runs on a live loop
before the failure reaches the engine.

Not asyncio.TaskGroup: it raises ExceptionGroup, and the engine's pause
routing (orchestrator/engine.py) is an isinstance over SINGLE exceptions —
one deterministic exception must re-raise. When several children fail in the
same window, the run-level causes win the race: UsageLimitReached (carries
the reset time), then SpendCapExceeded, then whatever gather surfaced first —
the actionable pause beats a coincidental stream-closed sibling. Every failed
attempt is still ledgered by the dispatcher regardless of which exception
names the pause.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from deeper.agents_runtime import SpendCapExceeded, UsageLimitReached

T = TypeVar("T")

_PREFERRED = (UsageLimitReached, SpendCapExceeded)


async def gather_strict(*aws: Awaitable[T]) -> list[T]:
    """Run awaitables concurrently; results in input order. On first failure:
    cancel the siblings, await the drain, re-raise one exception."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return await asyncio.gather(*tasks)
    except BaseException as first:
        for task in tasks:
            task.cancel()
        drained = await asyncio.gather(*tasks, return_exceptions=True)
        if isinstance(first, asyncio.CancelledError):
            raise  # outer cancellation must stay a cancellation
        candidates = [first] + [d for d in drained if isinstance(d, BaseException)]
        for cls in _PREFERRED:
            for exc in candidates:
                if isinstance(exc, cls):
                    if exc is first:
                        raise
                    raise exc from first
        raise
