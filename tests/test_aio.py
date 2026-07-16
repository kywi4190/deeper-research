"""gather_strict: the one fan-out primitive stages use. On first failure the
siblings are cancelled AND drained before ONE exception re-raises (run-level
causes preferred over per-dispatch ones), so SDK subprocess teardown happens
on a live loop instead of GC luck — and the engine's single-exception
isinstance routing keeps working (never an ExceptionGroup)."""

from __future__ import annotations

import asyncio

import pytest

from deeper.agents_runtime import (
    AgentContract,
    AgentDispatchFailed,
    SpendCapExceeded,
    UsageLimitReached,
)
from deeper.aio import gather_strict
from deeper.config import SizeClass
from deeper.schemas import Stage
from deeper.stages.s3_scouting import ScoutingStage
from deeper.stages.s4_rubric import RubricStage
from deeper.stages.s5_screening import ScreeningStage

from .helpers import make_ctx, make_workspace, write_s0_artifacts, write_s1_s2_artifacts


def _contract() -> AgentContract:
    return AgentContract(
        role="scout",
        stage=Stage.S3,
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="b",
    )


async def test_all_success_returns_results_in_input_order():
    async def make(i: int) -> int:
        await asyncio.sleep(0.001 * (5 - i))  # finish out of order on purpose
        return i

    assert await gather_strict(*(make(i) for i in range(5))) == list(range(5))


async def test_first_failure_cancels_and_drains_siblings_before_reraising():
    """The whole point: a sibling's async cleanup (the SDK generator's aclose →
    subprocess close) must have RUN by the time the failure reaches the
    caller — bare gather leaves the sibling orphaned until loop shutdown."""
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def sibling() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.01)  # awaited cleanup, like a transport close
            cleaned.set()

    async def failer() -> None:
        await started.wait()
        raise AgentDispatchFailed(_contract(), RuntimeError("boom"))

    with pytest.raises(AgentDispatchFailed):
        await gather_strict(sibling(), failer())
    assert cleaned.is_set()


async def test_concurrent_failures_reraise_one_exception_never_a_group():
    async def failer(msg: str) -> None:
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError) as excinfo:
        await gather_strict(failer("a"), failer("b"))
    assert not isinstance(excinfo.value, BaseExceptionGroup)


async def test_run_level_causes_win_over_per_dispatch_ones():
    """When the plan limit trips, many children die near-simultaneously and
    which exception surfaces first is a race — the actionable, run-level
    cause (the limit, with its reset time) must name the pause."""

    async def dispatch_death() -> None:
        raise AgentDispatchFailed(_contract(), RuntimeError("stream closed"))

    async def limit() -> None:
        raise UsageLimitReached(_contract(), "usage limit reached", "3pm")

    with pytest.raises(UsageLimitReached):
        await gather_strict(dispatch_death(), limit())

    async def cap() -> None:
        raise SpendCapExceeded(_contract(), 31.0, 30.0)

    with pytest.raises(SpendCapExceeded):
        await gather_strict(dispatch_death(), cap())

    # ...and the limit outranks the cap when both are present.
    with pytest.raises(UsageLimitReached):
        await gather_strict(cap(), limit())


async def test_outer_cancellation_propagates_and_drains():
    """Cancelling gather_strict itself must stay a cancellation (asyncio
    semantics), never be converted into a child's exception — and children
    still drain."""
    cleaned = asyncio.Event()
    running = asyncio.Event()

    async def child() -> None:
        running.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaned.set()

    task = asyncio.ensure_future(gather_strict(child()))
    await running.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_a_limit_notice_mid_fanout_pauses_the_stage_as_the_limit(tmp_path):
    """Integration: S5 screens angles in parallel; when one screener reply is
    a plan-limit notice, the stage raises UsageLimitReached (the engine's
    pause cause) — siblings are cancelled and drained by gather_strict, not
    left orphaned for loop-shutdown."""
    ws = make_workspace(tmp_path)
    write_s0_artifacts(ws)
    write_s1_s2_artifacts(ws)
    ctx = make_ctx(ws)
    await ScoutingStage().execute(ctx)
    await RubricStage().execute(ctx)
    limited = make_ctx(
        ws,
        scripted_responses={"screener": ["5-hour limit reached ∙ resets 3am"]},
    )
    with pytest.raises(UsageLimitReached) as excinfo:
        await ScreeningStage().execute(limited)
    assert excinfo.value.resets_at == "3am"
