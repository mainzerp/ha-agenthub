"""Conversation ingress robustness (issue #132, theme T1).

Covers the ``/ws/conversation`` handshake limit, terminal frames for failed
or truncated dispatcher streams, dispatcher-stream closing, and fixed
user-facing speech for REST dispatch failures.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect

from app.a2a.dispatcher import A2ADispatchError
from app.api.routes import conversation as conv_module
from app.models.conversation import ConversationRequest

_ORIGIN = "https://ha.local:8123"


def _make_ws(ip: str, messages: list[str]) -> MagicMock:
    """Mocked WebSocket that delivers ``messages`` and then disconnects."""
    ws = MagicMock()
    ws.headers = {"origin": _ORIGIN}
    ws.client.host = ip
    ws.app.state.allowed_ws_origins = {_ORIGIN}
    ws.scope = {"state": {}}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=[*messages, WebSocketDisconnect(code=1000)])
    ws.send_json = AsyncMock()
    return ws


def _sent_frames(ws: MagicMock) -> list[dict]:
    return [call.args[0] for call in ws.send_json.await_args_list]


def _turn(text: str = "turn on the light") -> str:
    return json.dumps({"text": text, "conversation_id": "c1"})


@pytest.fixture(autouse=True)
def _reset_ws_counters():
    conv_module.reset_active_ws_connections()
    yield
    conv_module.reset_active_ws_connections()


def _install_dispatcher(monkeypatch, stream_fn) -> None:
    dispatcher = MagicMock()
    dispatcher.dispatch_stream = stream_fn
    monkeypatch.setattr(conv_module, "_dispatcher", dispatcher)


class TestWsHandshakeLimit:
    async def test_connection_limit_rejects_before_accept(self):
        ip = "10.9.0.1"
        conv_module._active_ws_connections[ip] = conv_module._MAX_WS_CONNECTIONS_PER_IP
        ws = _make_ws(ip, [])

        await conv_module.ws_conversation(ws)

        # Closing an unaccepted socket rejects the upgrade, so clients see a
        # handshake failure instead of a mid-turn 1008 close.
        ws.accept.assert_not_awaited()
        ws.close.assert_awaited_once_with(code=1008, reason="Connection limit exceeded")
        assert conv_module._active_ws_connections[ip] == conv_module._MAX_WS_CONNECTIONS_PER_IP

    async def test_origin_rejection_releases_connection_slot(self):
        ip = "10.9.0.2"
        ws = _make_ws(ip, [])
        ws.headers = {"origin": "https://evil.example"}

        await conv_module.ws_conversation(ws)

        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once()
        assert ip not in conv_module._active_ws_connections


class TestWsTerminalFrames:
    async def test_dispatcher_exception_sends_terminal_error_and_keeps_socket(self, monkeypatch):
        calls = {"n": 0}

        async def _stream(_request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("secret internal detail")
            yield {"token": "ok", "done": True}

        _install_dispatcher(monkeypatch, _stream)
        ws = _make_ws("10.9.1.1", [_turn(), _turn()])

        await conv_module.ws_conversation(ws)

        frames = _sent_frames(ws)
        assert frames[0]["done"] is True
        assert frames[0]["error"] == "Internal error"
        assert len(frames[0]["trace_id"]) == 16
        assert "secret" not in json.dumps(frames)
        # The socket kept serving: the second turn was answered normally.
        assert frames[1]["done"] is True
        assert frames[1]["token"] == "ok"
        assert len(frames) == 2

    @pytest.mark.parametrize(
        "chunks, expected_error",
        [
            ([{"token": "partial answer", "done": False}], None),
            ([{"token": "", "done": False, "status": "routing"}], "Internal error"),
            ([], "Internal error"),
        ],
    )
    async def test_stream_without_done_gets_terminal_frame(self, monkeypatch, chunks, expected_error):
        async def _stream(_request):
            for chunk in chunks:
                yield chunk

        _install_dispatcher(monkeypatch, _stream)
        ws = _make_ws("10.9.1.2", [_turn()])

        await conv_module.ws_conversation(ws)

        frames = _sent_frames(ws)
        assert len(frames) == len(chunks) + 1
        terminal = frames[-1]
        assert terminal["done"] is True
        assert terminal["error"] == expected_error
        assert terminal["trace_id"]

    async def test_frames_after_done_are_drained_not_sent(self, monkeypatch):
        drained = {"value": False}

        async def _stream(_request):
            yield {"token": "answer", "done": True}
            yield {"token": "late", "done": False}
            drained["value"] = True

        _install_dispatcher(monkeypatch, _stream)
        ws = _make_ws("10.9.1.3", [_turn()])

        await conv_module.ws_conversation(ws)

        frames = _sent_frames(ws)
        assert [f["token"] for f in frames] == ["answer"]
        assert drained["value"] is True

    async def test_disconnect_closes_dispatcher_stream(self, monkeypatch):
        state = {"closed": False}

        async def _gen():
            try:
                yield {"token": "a", "done": False}
                yield {"token": "b", "done": True}
            finally:
                state["closed"] = True

        # Hold a strong reference so only an explicit aclose() (not GC
        # finalization) can run the generator's cleanup.
        generators = []

        def _stream(_request):
            gen = _gen()
            generators.append(gen)
            return gen

        _install_dispatcher(monkeypatch, _stream)
        ws = _make_ws("10.9.1.4", [_turn()])
        ws.send_json = AsyncMock(side_effect=WebSocketDisconnect(code=1001))

        await conv_module.ws_conversation(ws)

        # Closed synchronously by the route, not later by the GC finalizer.
        assert state["closed"] is True
        assert "10.9.1.4" not in conv_module._active_ws_connections


class TestSseTerminalFrame:
    async def test_sse_stream_without_done_gets_terminal_frame(self, monkeypatch):
        state = {"closed": False}

        async def _stream(_request):
            try:
                yield {"token": "", "done": False, "status": "routing"}
            finally:
                state["closed"] = True

        _install_dispatcher(monkeypatch, _stream)
        request = SimpleNamespace(state=SimpleNamespace(span_collector=None, root_span_id=None, trace_id="t" * 16))

        response = await conv_module.conversation_sse(request, ConversationRequest(text="hi"), "key")
        events = [event async for event in response.body_iterator]

        payloads = [json.loads(e.removeprefix("data: ").strip()) for e in events]
        assert payloads[-1]["done"] is True
        assert payloads[-1]["error"] == "Internal error"
        assert payloads[-1]["trace_id"] == "t" * 16
        assert state["closed"] is True


class TestRestDispatchFailure:
    async def test_rest_dispatch_error_speech_is_fixed(self, monkeypatch):
        dispatcher = MagicMock()
        dispatcher.dispatch = AsyncMock(side_effect=RuntimeError("Agent error: secret-agent"))
        monkeypatch.setattr(conv_module, "_dispatcher", dispatcher)
        request = SimpleNamespace(state=SimpleNamespace(span_collector=None, trace_id="abc"))

        response = await conv_module.conversation_rest(
            request, ConversationRequest(text="hi", conversation_id="c1"), "key"
        )

        assert response.speech == conv_module._DISPATCH_FAILED_SPEECH
        assert "secret" not in response.speech
        assert response.conversation_id == "c1"

    async def test_rest_a2a_dispatch_error_speech_is_fixed(self, monkeypatch):
        # message/send raises A2ADispatchError (a RuntimeError subclass) for
        # unroutable requests instead of returning a JSON-RPC error envelope.
        dispatcher = MagicMock()
        dispatcher.dispatch = AsyncMock(side_effect=A2ADispatchError(-32602, "Invalid params"))
        monkeypatch.setattr(conv_module, "_dispatcher", dispatcher)
        request = SimpleNamespace(state=SimpleNamespace(span_collector=None, trace_id="abc"))

        response = await conv_module.conversation_rest(
            request, ConversationRequest(text="hi", conversation_id="c1"), "key"
        )

        assert response.speech == conv_module._DISPATCH_FAILED_SPEECH
        assert "Invalid params" not in response.speech
        assert response.conversation_id == "c1"
