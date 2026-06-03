"""Tests for A2A Dispatcher error paths."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.a2a.dispatcher import Dispatcher
from app.a2a.protocol import JsonRpcRequest


class TestDispatcherErrorPaths:
    def _make_dispatcher(self):
        registry = AsyncMock()
        transport = AsyncMock()
        dispatcher = Dispatcher(registry=registry, transport=transport)
        return dispatcher, registry, transport

    @pytest.mark.asyncio
    async def test_dispatch_invalid_params(self):
        """G17: message/send with invalid params must return invalid_params error."""
        dispatcher, _registry, _transport = self._make_dispatcher()
        request = JsonRpcRequest(
            method="message/send",
            params={"bad_key": "value"},
            id="req-1",
        )
        response = await dispatcher.dispatch(request)
        assert response.error is not None
        assert response.error.code == -32602  # _INVALID_PARAMS
        assert "Invalid params" in response.error.message

    @pytest.mark.asyncio
    async def test_dispatch_method_not_found(self):
        """G17: Unknown method must return method_not_found error."""
        dispatcher, _registry, _transport = self._make_dispatcher()
        request = JsonRpcRequest(
            method="message/unknown",
            params={},
            id="req-2",
        )
        response = await dispatcher.dispatch(request)
        assert response.error is not None
        assert response.error.code == -32601  # _METHOD_NOT_FOUND
        assert "Method not found" in response.error.message

    @pytest.mark.asyncio
    async def test_dispatch_stream_invalid_params(self):
        """G17: message/stream with invalid params must yield error chunk."""
        dispatcher, _registry, _transport = self._make_dispatcher()
        request = JsonRpcRequest(
            method="message/stream",
            params={"bad_key": "value"},
            id="req-3",
        )
        chunks = [c async for c in dispatcher.dispatch_stream(request)]
        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert "Invalid params" in chunks[0].get("error", "")

    @pytest.mark.asyncio
    async def test_dispatch_stream_method_not_found(self):
        """G17: Non-message/stream method must yield method not found error."""
        dispatcher, _registry, _transport = self._make_dispatcher()
        request = JsonRpcRequest(
            method="message/send",
            params={},
            id="req-4",
        )
        chunks = [c async for c in dispatcher.dispatch_stream(request)]
        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert "Method not found" in chunks[0].get("error", "")
