"""Tests for app.api.routes.sse."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.api.routes.sse import register_sse_tickers


class TestRegisterSseTickers:
    @pytest.mark.asyncio
    async def test_assigns_list_before_creating_tasks(self):
        """CONT-6.1: register_sse_tickers must assign app.state.sse_ticker_tasks = [] before creating tasks."""
        app = MagicMock()
        app.state.sse_ticker_tasks = []

        register_sse_tickers(app)

        # After call, the list must contain exactly 4 tasks
        assert len(app.state.sse_ticker_tasks) == 4
        for task in app.state.sse_ticker_tasks:
            assert isinstance(task, asyncio.Task)

    @pytest.mark.asyncio
    async def test_cancels_existing_tasks(self):
        """Existing ticker tasks must be cancelled before new ones are created."""
        app = MagicMock()
        old_task = MagicMock()
        old_task.done.return_value = False
        app.state.sse_ticker_tasks = [old_task]

        register_sse_tickers(app)

        old_task.cancel.assert_called_once()
        assert len(app.state.sse_ticker_tasks) == 4
