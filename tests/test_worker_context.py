"""Tests for the worker context's restartable single-task helper."""

from __future__ import annotations

import anyio
import anyio.lowlevel
import pytest

from anycorn.task_group import TaskGroup
from anycorn.worker_context import AnyioSingleTask


@pytest.mark.anyio
async def test_single_task_reschedules_itself_repeatedly() -> None:
    """A task that restarts its own SingleTask must keep running, not stop after one run.

    This is the shape the QUIC connection timer has: _handle_timer waits for the
    deadline, does its work, then send_all calls restart on the very SingleTask
    driving it. If restart cancels the running handle before the replacement is
    armed, that self-cancellation tears the reschedule down at restart's own await
    and the timer fires exactly once - which on a lossy link stops QUIC
    retransmitting after the first attempt. It reproduces here without any sockets:
    the giveaway is a real await (the timer's own sleep) before the reschedule, so
    the replacement has a checkpoint at which the stale cancellation can hit it.
    """
    fires: list[int] = []
    target = 5
    done = anyio.Event()

    async with TaskGroup() as task_group:
        single_task = AnyioSingleTask()

        async def action() -> None:
            await anyio.sleep(0.01)  # the timer waiting out its deadline
            fires.append(len(fires))
            await anyio.lowlevel.checkpoint()  # stands in for send_all's awaited sends
            if len(fires) < target:
                await single_task.restart(task_group, action)
            else:
                done.set()

        await single_task.restart(task_group, action)

        with anyio.fail_after(5):
            await done.wait()

        await single_task.stop()

    assert len(fires) == target
