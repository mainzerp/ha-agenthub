"""Lifecycle tests for the MCP client owner-task pattern.

Regression test for CRIT-4 (deep code review): ``__aenter__`` and
``__aexit__`` of the MCP transport / session contexts must run in the
same task. We simulate this by providing a fake stdio context manager
that asserts the entering and exiting tasks are identical, and we
exercise connect/disconnect from different tasks.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.client import MCPClient


class _FakeAsyncIO:
    async def receive(self):
        await asyncio.sleep(3600)


class _FakeSession:
    def __init__(self, read=None, write=None):
        self._read = read
        self._write = write
        self._enter_task: asyncio.Task | None = None
        self.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
        self.call_tool = AsyncMock(return_value={"ok": True})
        self.initialize = AsyncMock()

    async def __aenter__(self):
        self._enter_task = asyncio.current_task()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # CRIT-4 guarantee: exit must happen in the same task as enter.
        assert asyncio.current_task() is self._enter_task, (
            "ClientSession.__aexit__ ran in a different task than __aenter__"
        )
        return None


@asynccontextmanager
async def _fake_stdio_factory():
    enter_task = asyncio.current_task()
    try:
        yield (_FakeAsyncIO(), _FakeAsyncIO())
    finally:
        assert asyncio.current_task() is enter_task, "stdio_client.__aexit__ ran in a different task than __aenter__"


@pytest.mark.asyncio
async def test_owner_task_keeps_contexts_in_one_task():
    """Connect from one task, disconnect from another -- contexts stay anchored."""
    client = MCPClient(name="lifecycle", transport="stdio", command_or_url="echo")

    with patch("app.mcp.client.ClientSession", _FakeSession):
        connect_ok = await client._start_owner(_fake_stdio_factory)
        assert connect_ok is True
        assert client.connected is True

        # Drive disconnect from a freshly created sub-task.
        async def _disconnect():
            await client.disconnect()

        await asyncio.create_task(_disconnect())

        assert client.connected is False
        assert client._owner_task is None


@pytest.mark.asyncio
async def test_list_tools_round_trip_via_owner():
    client = MCPClient(name="lifecycle", transport="stdio", command_or_url="echo")
    with patch("app.mcp.client.ClientSession", _FakeSession):
        ok = await client._start_owner(_fake_stdio_factory)
        assert ok

        # Replace the session's list_tools with a typed MagicMock.
        tool = MagicMock()
        tool.name = "ping"
        tool.description = "p"
        tool.input_schema = {"type": "object"}
        client._session.list_tools = AsyncMock(return_value=MagicMock(tools=[tool]))

        tools = await client.list_tools()
        assert tools == [{"name": "ping", "description": "p", "input_schema": {"type": "object"}}]

        await client.disconnect()


@pytest.mark.asyncio
async def test_call_tool_round_trip_via_owner():
    client = MCPClient(name="lifecycle", transport="stdio", command_or_url="echo")
    with patch("app.mcp.client.ClientSession", _FakeSession):
        ok = await client._start_owner(_fake_stdio_factory)
        assert ok

        client._session.call_tool = AsyncMock(return_value={"value": 42})
        result = await client.call_tool("do_thing", {"arg": 1})
        assert result == {"value": 42}
        client._session.call_tool.assert_awaited_once_with("do_thing", arguments={"arg": 1})

        await client.disconnect()


@pytest.mark.asyncio
async def test_disconnect_idempotent_without_owner():
    """disconnect() on a client that never connected must not raise."""
    client = MCPClient(name="never", transport="stdio", command_or_url="echo")
    await client.disconnect()
    assert client.connected is False


@pytest.mark.asyncio
async def test_owner_loop_failure_unblocks_connect():
    """If the transport context raises during enter, connect() returns False."""
    client = MCPClient(name="bad", transport="stdio", command_or_url="echo")

    @asynccontextmanager
    async def _broken_factory():
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with patch("app.mcp.client.ClientSession", _FakeSession):
        ok = await client._start_owner(_broken_factory)
        assert ok is False
        assert client.connected is False
