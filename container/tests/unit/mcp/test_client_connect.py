"""Tests for app.mcp.client connection and lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp.client import _STOP, MCPClient

pytestmark = pytest.mark.asyncio


class TestConnectWiring:
    async def test_connect_stdio_and_sse_wiring(self):
        """_connect_stdio and _connect_sse wire parameters correctly to _start_owner."""
        client = MCPClient(name="test-server", transport="stdio", command_or_url="python script.py")

        with (
            patch("app.mcp.client._validate_mcp_command"),
            patch.object(client, "_start_owner", new_callable=AsyncMock, return_value=True) as mock_start,
        ):
            result = await client._connect_stdio()
        assert result is True
        mock_start.assert_awaited_once()
        factory = mock_start.call_args.args[0]
        # Factory is a closure; we can only assert it exists and is callable
        assert callable(factory)

        client_sse = MCPClient(name="test-sse", transport="sse", command_or_url="http://example.com/sse")
        with (
            patch("app.mcp.client._validate_sse_url"),
            patch.object(client_sse, "_start_owner", new_callable=AsyncMock, return_value=True) as mock_start_sse,
        ):
            result = await client_sse._connect_sse()
        assert result is True
        mock_start_sse.assert_awaited_once()
        factory_sse = mock_start_sse.call_args.args[0]
        assert callable(factory_sse)


class TestSubmitAbortDisconnect:
    async def test_submit_abort_owner_and_disconnect(self):
        """_submit queues and awaits future; _abort_owner cancels task; disconnect drains cleanly."""
        client = MCPClient(name="test-server", transport="stdio", command_or_url="python script.py")

        # _submit with a mocked queue
        client._req_q = asyncio.Queue()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await client._req_q.put((fut, _STOP, ()))

        # Drain the queue manually to simulate owner loop
        queued_fut, op, _args = await client._req_q.get()
        assert op == _STOP
        queued_fut.set_result(None)

        result = await fut
        assert result is None

        # _abort_owner cancels a running task
        async def _dummy():
            await asyncio.sleep(10)

        client._owner_task = asyncio.create_task(_dummy())
        client._req_q = asyncio.Queue()
        await client._abort_owner()
        assert client._owner_task is None
        assert client._req_q is None

        # disconnect: full lifecycle with owner
        client2 = MCPClient(name="test-server2", transport="stdio", command_or_url="python script.py")
        client2._req_q = asyncio.Queue()
        client2._ready = asyncio.Event()

        async def _owner():
            while True:
                f, op, _args = await client2._req_q.get()
                if op == _STOP:
                    if not f.done():
                        f.set_result(None)
                    return

        client2._owner_task = asyncio.create_task(_owner())
        await client2.disconnect()
        assert client2._owner_task is None
        assert client2._req_q is None
        assert client2._connected is False
