"""Tests for background task tracking under burst load."""

from __future__ import annotations

import asyncio

import pytest

from app.util import tasks as task_utils


class TestTaskLeakUnderBurst:
    @pytest.mark.asyncio
    async def test_burst_spawn_does_not_leak(self):
        """G21: Many concurrent spawns must all be tracked and cleaned up."""
        initial_count = len(task_utils._pending)

        async def _dummy_coro(idx: int) -> int:
            await asyncio.sleep(0.01)
            return idx

        spawned = [task_utils.spawn(_dummy_coro(i), name=f"burst-{i}") for i in range(50)]
        assert len(task_utils._pending) == initial_count + 50

        await asyncio.gather(*spawned)
        # After completion, done callback should have removed all tasks
        assert len(task_utils._pending) == initial_count

    @pytest.mark.asyncio
    async def test_spawn_logs_exception(self, caplog):
        """G21: Failed background tasks should be logged without propagating."""
        initial_count = len(task_utils._pending)

        async def _failing_coro():
            raise RuntimeError("background failure")

        task = task_utils.spawn(_failing_coro(), name="fail-test")
        with pytest.raises(RuntimeError):
            await task

        # done callback removes it from pending
        assert len(task_utils._pending) == initial_count
        assert "Background task fail-test failed" in caplog.text
