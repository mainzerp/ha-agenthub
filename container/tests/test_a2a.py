"""Tests for app.a2a -- protocol models, registry, dispatcher, transport."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.a2a.dispatcher import (
    _INTERNAL_ERROR,
    _INVALID_PARAMS,
    _INVALID_REQUEST,
    _METHOD_NOT_FOUND,
    _PARSE_ERROR,
    _TIMEOUT_ERROR,
    Dispatcher,
    _AgentDiscoverParams,
    _error_response,
    _JsonRpcError,
    _JsonRpcResponse,
    _MessageSendParams,
    _MessageStreamParams,
    _success_response,
)
from app.a2a.protocol import (
    JsonRpcRequest,
)
from app.a2a.registry import AgentRegistry
from app.a2a.transport import InProcessTransport, Transport
from app.models.agent import AgentTask
from tests.helpers import make_agent_card

# ---------------------------------------------------------------------------
# Protocol models
# ---------------------------------------------------------------------------


class TestJsonRpcRequest:
    def test_valid_request(self):
        req = JsonRpcRequest(method="message/send", id="req-1")
        assert req.jsonrpc == "2.0"
        assert req.method == "message/send"
        assert req.id == "req-1"
        assert req.params is None

    def test_request_with_params(self):
        req = JsonRpcRequest(method="message/send", id="req-2", params={"agent_id": "light-agent"})
        assert req.params["agent_id"] == "light-agent"

    def test_json_round_trip(self):
        req = JsonRpcRequest(method="test", id="1", params={"key": "val"})
        data = req.model_dump_json()
        restored = JsonRpcRequest.model_validate_json(data)
        assert restored.method == "test"
        assert restored.params == {"key": "val"}


class TestJsonRpcResponse:
    def test_success_response(self):
        resp = _JsonRpcResponse(id="1", result={"status": "ok"})
        assert resp.error is None
        assert resp.result["status"] == "ok"

    def test_error_response(self):
        resp = _JsonRpcResponse(id="1", error=_JsonRpcError(code=-32601, message="Not found"))
        assert resp.result is None
        assert resp.error.code == -32601


class TestJsonRpcError:
    def test_standard_codes(self):
        assert _PARSE_ERROR == -32700
        assert _INVALID_REQUEST == -32600
        assert _METHOD_NOT_FOUND == -32601
        assert _INVALID_PARAMS == -32602
        assert _INTERNAL_ERROR == -32603
        assert _TIMEOUT_ERROR == -32000

    def test_error_with_data(self):
        err = _JsonRpcError(code=-32600, message="bad", data={"detail": "stuff"})
        assert err.data["detail"] == "stuff"


class TestHelperFactories:
    def test_error_response_factory(self):
        resp = _error_response("req-1", _METHOD_NOT_FOUND, "Not found")
        assert resp.error is not None
        assert resp.error.code == _METHOD_NOT_FOUND
        assert resp.id == "req-1"

    def test_success_response_factory(self):
        resp = _success_response("req-1", {"status": "ok"})
        assert resp.result == {"status": "ok"}
        assert resp.error is None


class TestParamModels:
    def test_message_send_params(self):
        p = _MessageSendParams(agent_id="light-agent", task={"description": "test"})
        assert p.agent_id == "light-agent"

    def test_message_stream_params(self):
        p = _MessageStreamParams(agent_id="music-agent", task={"description": "play"})
        assert p.agent_id == "music-agent"

    def test_agent_discover_params(self):
        p = _AgentDiscoverParams(agent_id="light-agent")
        assert p.agent_id == "light-agent"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _make_mock_agent(agent_id: str = "light-agent") -> MagicMock:
    """Create a mock agent with an agent_card attribute."""
    agent = AsyncMock()
    agent.agent_card = make_agent_card(agent_id=agent_id, name=f"{agent_id} agent")
    return agent


class TestAgentRegistry:
    async def test_register_and_discover(self):
        reg = AgentRegistry()
        agent = _make_mock_agent("light-agent")
        await reg.register(agent)
        card = await reg.discover("light-agent")
        assert card is not None
        assert card.agent_id == "light-agent"

    async def test_discover_unknown_returns_none(self):
        reg = AgentRegistry()
        card = await reg.discover("nonexistent")
        assert card is None

    async def test_list_agents_empty(self):
        reg = AgentRegistry()
        agents = await reg.list_agents()
        assert agents == []

    async def test_list_agents_multiple(self):
        reg = AgentRegistry()
        await reg.register(_make_mock_agent("a"))
        await reg.register(_make_mock_agent("b"))
        agents = await reg.list_agents()
        ids = {a.agent_id for a in agents}
        assert ids == {"a", "b"}

    async def test_unregister(self):
        reg = AgentRegistry()
        await reg.register(_make_mock_agent("x"))
        await reg.unregister("x")
        assert await reg.discover("x") is None

    async def test_get_handler_for_transport(self):
        reg = AgentRegistry()
        agent = _make_mock_agent("h")
        await reg.register(agent)
        handler = await reg._get_handler_for_transport("h")
        assert handler is agent

    async def test_get_handler_for_transport_missing_returns_none(self):
        reg = AgentRegistry()
        handler = await reg._get_handler_for_transport("nope")
        assert handler is None

    async def test_duplicate_registration_rejected(self):
        reg = AgentRegistry()
        agent1 = _make_mock_agent("dup")
        agent2 = _make_mock_agent("dup")
        await reg.register(agent1)
        with pytest.raises(ValueError, match="Agent ID already registered: dup"):
            await reg.register(agent2)
        handler = await reg._get_handler_for_transport("dup")
        assert handler is agent1

    async def test_duplicate_registration_replace_overrides(self):
        reg = AgentRegistry()
        agent1 = _make_mock_agent("dup")
        agent2 = _make_mock_agent("dup")
        await reg.register(agent1)
        await reg.register(agent2, replace=True)
        handler = await reg._get_handler_for_transport("dup")
        assert handler is agent2


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDispatcher:
    def _make_dispatcher(self) -> tuple[Dispatcher, AgentRegistry, InProcessTransport]:
        reg = AgentRegistry()
        transport = InProcessTransport(reg)
        dispatcher = Dispatcher(reg, transport)
        return dispatcher, reg, transport

    async def test_dispatch_message_send(self):
        dispatcher, reg, _ = self._make_dispatcher()
        agent = _make_mock_agent("light-agent")
        agent.handle_task = AsyncMock(return_value={"speech": "Done"})
        await reg.register(agent)

        request = JsonRpcRequest(
            method="message/send",
            id="r1",
            params={"agent_id": "light-agent", "task": {"description": "turn on", "user_text": "turn on"}},
        )
        resp = await dispatcher.dispatch(request)
        assert resp == {"speech": "Done"}

    async def test_dispatch_unknown_method_returns_error(self):
        dispatcher, _, _ = self._make_dispatcher()
        request = JsonRpcRequest(method="unknown/method", id="r2")
        resp = await dispatcher.dispatch(request)
        assert resp.error is not None
        assert resp.error.code == _METHOD_NOT_FOUND

    async def test_dispatch_agent_discover(self):
        dispatcher, reg, _ = self._make_dispatcher()
        await reg.register(_make_mock_agent("light-agent"))
        request = JsonRpcRequest(method="agent/discover", id="r3", params={"agent_id": "light-agent"})
        resp = await dispatcher.dispatch(request)
        assert resp.result is not None
        assert resp.result["agent_id"] == "light-agent"

    async def test_dispatch_agent_discover_missing(self):
        dispatcher, _, _ = self._make_dispatcher()
        request = JsonRpcRequest(method="agent/discover", id="r4", params={"agent_id": "nope"})
        resp = await dispatcher.dispatch(request)
        assert resp.error is not None
        assert resp.error.code == _INVALID_PARAMS

    async def test_dispatch_agent_list(self):
        dispatcher, reg, _ = self._make_dispatcher()
        await reg.register(_make_mock_agent("a1"))
        await reg.register(_make_mock_agent("a2"))
        request = JsonRpcRequest(method="agent/list", id="r5")
        resp = await dispatcher.dispatch(request)
        assert resp.result is not None
        assert len(resp.result["agents"]) == 2

    async def test_dispatch_message_send_missing_params(self):
        dispatcher, _, _ = self._make_dispatcher()
        request = JsonRpcRequest(method="message/send", id="r6", params={})
        resp = await dispatcher.dispatch(request)
        assert resp.error is not None
        assert resp.error.code == _INVALID_PARAMS

    async def test_dispatch_stream_valid(self):
        dispatcher, reg, _ = self._make_dispatcher()
        agent = _make_mock_agent("s-agent")

        async def stream_gen(task):
            yield {"token": "Hi", "done": False}
            yield {"token": "", "done": True}

        agent.handle_task_stream = stream_gen
        await reg.register(agent)

        request = JsonRpcRequest(
            method="message/stream",
            id="s1",
            params={"agent_id": "s-agent", "task": {"description": "test", "user_text": "test"}},
        )
        chunks = []
        async for chunk in dispatcher.dispatch_stream(request):
            chunks.append(chunk)
        assert len(chunks) == 2
        assert chunks[-1].get("done") is True

    async def test_dispatch_stream_unknown_method(self):
        dispatcher, _, _ = self._make_dispatcher()
        request = JsonRpcRequest(method="bad/method", id="s2")
        chunks = []
        async for chunk in dispatcher.dispatch_stream(request):
            chunks.append(chunk)
        assert len(chunks) == 1
        assert chunks[0].get("done") is True


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class TestInProcessTransport:
    async def test_send_calls_handler(self):
        reg = AgentRegistry()
        agent = _make_mock_agent("t-agent")
        agent.handle_task = AsyncMock(return_value={"speech": "ok"})
        await reg.register(agent)

        transport = InProcessTransport(reg)
        task = AgentTask(description="test", user_text="test")
        resp = await transport.send("t-agent", task, "req-1")
        assert resp == {"speech": "ok"}

    async def test_send_unknown_agent_raises_runtime_error(self):
        reg = AgentRegistry()
        transport = InProcessTransport(reg)
        task = AgentTask(description="test", user_text="test")
        with pytest.raises(RuntimeError, match="Agent not found: missing"):
            await transport.send("missing", task, "req-1")

    async def test_send_handler_exception_raises_runtime_error(self):
        reg = AgentRegistry()
        agent = _make_mock_agent("err-agent")
        agent.handle_task = AsyncMock(side_effect=RuntimeError("boom"))
        await reg.register(agent)

        transport = InProcessTransport(reg)
        task = AgentTask(description="test", user_text="test")
        with pytest.raises(RuntimeError, match="Agent error: err-agent"):
            await transport.send("err-agent", task, "req-1")

    async def test_stream_calls_handler(self):
        reg = AgentRegistry()
        agent = _make_mock_agent("st-agent")

        async def stream_gen(task):
            yield {"token": "A", "done": False}
            yield {"token": "", "done": True}

        agent.handle_task_stream = stream_gen
        await reg.register(agent)

        transport = InProcessTransport(reg)
        task = AgentTask(description="test", user_text="test")
        chunks = []
        async for c in transport.stream("st-agent", task, "req-1"):
            chunks.append(c)
        assert len(chunks) == 2

    async def test_stream_unknown_agent_returns_error_chunk(self):
        reg = AgentRegistry()
        transport = InProcessTransport(reg)
        task = AgentTask(description="test", user_text="test")
        chunks = []
        async for c in transport.stream("missing", task, "req-1"):
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].get("done") is True

    async def test_transport_is_abstract(self):
        assert hasattr(Transport, "send")
        assert hasattr(Transport, "stream")
