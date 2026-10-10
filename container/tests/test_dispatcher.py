"""Tests for A2A Dispatcher error paths."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.a2a.dispatcher import _INVALID_PARAMS, _METHOD_NOT_FOUND, A2ADispatchError, Dispatcher
from app.a2a.protocol import JsonRpcRequest


class TestDispatcherErrorPaths:
    def _make_dispatcher(self):
        registry = AsyncMock()
        transport = AsyncMock()
        dispatcher = Dispatcher(registry=registry, transport=transport)
        return dispatcher, registry, transport

    @pytest.mark.asyncio
    async def test_dispatch_invalid_params(self):
        """G17: message/send with invalid params raises an invalid_params dispatch error."""
        dispatcher, _registry, transport = self._make_dispatcher()
        request = JsonRpcRequest(
            method="message/send",
            params={"bad_key": "value"},
            id="req-1",
        )
        with pytest.raises(A2ADispatchError) as exc_info:
            await dispatcher.dispatch(request)
        assert exc_info.value.code == _INVALID_PARAMS
        assert str(exc_info.value) == "Invalid params"
        transport.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_invalid_task_payload_raises_invalid_params(self):
        """message/send with a task dict failing DispatchTask validation raises
        a RuntimeError subclass (same contract as transport failures)."""
        dispatcher, _registry, _transport = self._make_dispatcher()
        request = JsonRpcRequest(
            method="message/send",
            params={"agent_id": "light-agent", "task": {}},
            id="req-invalid-task",
        )
        with pytest.raises(RuntimeError) as exc_info:
            await dispatcher.dispatch(request)
        assert isinstance(exc_info.value, A2ADispatchError)
        assert exc_info.value.code == _INVALID_PARAMS

    @pytest.mark.asyncio
    async def test_dispatch_invalid_params_message_has_no_validation_dump(self):
        """The raised message is safe to surface: no pydantic validation detail."""
        dispatcher, _registry, _transport = self._make_dispatcher()
        request = JsonRpcRequest(
            method="message/send",
            params={"agent_id": "light-agent", "task": {}},
            id="req-dump",
        )
        with pytest.raises(A2ADispatchError) as exc_info:
            await dispatcher.dispatch(request)
        message = str(exc_info.value)
        assert "validation error" not in message
        assert "Field required" not in message
        assert "pydantic" not in message

    @pytest.mark.asyncio
    async def test_dispatch_stream_invalid_task_payload_yields_error_chunk(self):
        """message/stream with a task dict failing DispatchTask validation must
        yield an error chunk instead of raising, without the validation dump."""
        dispatcher, _registry, _transport = self._make_dispatcher()
        request = JsonRpcRequest(
            method="message/stream",
            params={"agent_id": "light-agent", "task": {}},
            id="req-invalid-task-stream",
        )
        chunks = [c async for c in dispatcher.dispatch_stream(request)]
        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert chunks[0].get("error") == "Invalid params"

    @pytest.mark.asyncio
    async def test_dispatch_method_not_found(self):
        """G17: Unknown method raises a method_not_found dispatch error."""
        dispatcher, _registry, _transport = self._make_dispatcher()
        request = JsonRpcRequest(
            method="message/unknown",
            params={},
            id="req-2",
        )
        with pytest.raises(A2ADispatchError) as exc_info:
            await dispatcher.dispatch(request)
        assert exc_info.value.code == _METHOD_NOT_FOUND
        assert "Method not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_dispatch_success_returns_raw_result(self):
        """Success keeps returning the raw agent result (no envelope)."""
        dispatcher, _registry, transport = self._make_dispatcher()
        transport.send = AsyncMock(return_value={"speech": "Done"})
        request = JsonRpcRequest(
            method="message/send",
            params={"agent_id": "light-agent", "task": {"description": "turn on the light"}},
            id="req-ok",
        )
        assert await dispatcher.dispatch(request) == {"speech": "Done"}

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
