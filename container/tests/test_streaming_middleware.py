"""Test that pure ASGI middleware does not buffer streaming responses.

Regression test for CRIT-6 (deep code review): the SetupRedirectMiddleware
and TracingMiddleware previously subclassed BaseHTTPMiddleware which buffers
the entire response body before sending it. SSE/WS endpoints could not flush
the first byte until the stream completed.

We drive the ASGI protocol directly (httpx.ASGITransport itself buffers,
which would defeat the test) and assert that the first http.response.body
message arrives well before the downstream generator finishes.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from app.middleware.auth import SetupRedirectMiddleware
from app.middleware.tracing import TracingMiddleware

SLEEP_S = 0.4
TIMING_BUDGET_S = 1.0


async def _slow_sse(request: Request) -> StreamingResponse:
    async def event_gen():
        yield b"data: first\n\n"
        await asyncio.sleep(SLEEP_S)
        yield b"data: done\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


def _make_streaming_app():
    return Starlette(routes=[Route("/sse", _slow_sse)])


def _scope():
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/sse",
        "raw_path": b"/sse",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


async def _drive(app):
    """Run the ASGI app, capturing each send message with its arrival time."""
    received = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        if received:
            return received.pop(0)
        return {"type": "http.disconnect"}

    captured: list[tuple[float, dict]] = []
    t0 = time.perf_counter()

    async def send(message):
        captured.append((time.perf_counter() - t0, message))

    await app(_scope(), receive, send)
    return captured


def _first_body_offset(captured):
    for delta, msg in captured:
        if msg["type"] == "http.response.body" and msg.get("body"):
            return delta, msg["body"]
    raise AssertionError("no body message captured")


@pytest.mark.asyncio
@patch("app.middleware.auth.SetupStateRepository")
async def test_setup_redirect_middleware_does_not_buffer(mock_repo):
    mock_repo.is_complete = AsyncMock(return_value=True)

    app = _make_streaming_app()
    app.add_middleware(SetupRedirectMiddleware)

    captured = await _drive(app)
    first_offset, first_body = _first_body_offset(captured)
    assert first_offset < TIMING_BUDGET_S, (
        f"first body chunk arrived after {first_offset:.3f}s -- middleware is buffering"
    )
    assert b"first" in first_body


@pytest.mark.asyncio
@patch("app.middleware.tracing.TraceSummaryRepository", create=True)
async def test_tracing_middleware_does_not_buffer(mock_summary):
    mock_summary.update_duration = AsyncMock()

    app = _make_streaming_app()
    app.add_middleware(TracingMiddleware)

    captured = await _drive(app)

    start_msg = next(m for _, m in captured if m["type"] == "http.response.start")
    header_names = {k.decode("ascii").lower() for k, _ in start_msg["headers"]}
    assert "x-trace-id" in header_names

    first_offset, first_body = _first_body_offset(captured)
    assert first_offset < TIMING_BUDGET_S, (
        f"first body chunk arrived after {first_offset:.3f}s -- middleware is buffering"
    )
    assert b"first" in first_body


@pytest.mark.asyncio
@patch("app.middleware.tracing.TraceSummaryRepository", create=True)
async def test_tracing_middleware_populates_websocket_span(mock_summary):
    """FLOW-WS-TURN-1: for ``/ws/conversation`` the middleware MUST NOT
    create a connection-level SpanCollector / trace_id / root_span_id
    (the route mints them per turn). It still exposes ``source="ha"``
    and the ``ws_per_turn`` marker so the route handler can read them.
    """
    mock_summary.update_duration = AsyncMock()

    captured_state: dict = {}

    async def dummy_asgi(scope, receive, send):
        captured_state["state"] = scope.get("state", {}).copy()

    middleware = TracingMiddleware(dummy_asgi)

    async def _receive():
        return {"type": "websocket.disconnect"}

    async def _send(_):
        return None

    ws_scope = {
        "type": "websocket",
        "path": "/ws/conversation",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    }

    await middleware(ws_scope, _receive, _send)

    state = captured_state["state"]
    assert state["source"] == "ha"
    assert state["ws_per_turn"] is True
    assert "span_collector" not in state
    assert "trace_id" not in state
    assert "root_span_id" not in state
    mock_summary.update_duration.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.middleware.tracing.TraceSummaryRepository", create=True)
async def test_tracing_middleware_ws_source_defaults_to_api(mock_summary):
    """FLOW-WS-SPAN-1 (P1-6): WS routes that are not /ws/conversation fall
    back to ``source="api"`` instead of silently inheriting ``"ha"``."""
    mock_summary.update_duration = AsyncMock()

    captured_state: dict = {}

    async def dummy_asgi(scope, receive, send):
        captured_state["state"] = scope.get("state", {}).copy()

    middleware = TracingMiddleware(dummy_asgi)

    async def _receive():
        return {"type": "websocket.disconnect"}

    async def _send(_):
        return None

    ws_scope = {
        "type": "websocket",
        "path": "/ws/some-future-route",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    }

    await middleware(ws_scope, _receive, _send)

    sc = captured_state["state"]["span_collector"]
    assert sc.source == "api"


@pytest.mark.asyncio
@patch("app.middleware.tracing.TraceSummaryRepository", create=True)
async def test_ws_conversation_mints_per_turn_trace(mock_summary):
    """FLOW-WS-TURN-1: ``ws_conversation`` mints a fresh trace_id +
    SpanCollector per inbound message, flushes it in ``finally``, and
    the middleware never calls ``TraceSummaryRepository.update_duration``
    for ``/ws/conversation`` (no connection-level row).
    """
    from app.api.routes import conversation as conv_routes

    mock_summary.update_duration = AsyncMock()

    flushed_collectors: list = []
    built_collectors: list = []

    real_build = conv_routes._build_a2a_request

    def _capture_build(conv_request, method, span_collector=None):
        built_collectors.append(span_collector)
        return real_build(conv_request, method, span_collector)

    # Patch flush to record what was flushed without writing to DB.
    from app.analytics import tracer as tracer_mod

    async def _fake_flush(self):
        flushed_collectors.append(self)

    # Fake dispatcher that yields one done chunk per call.
    async def _stream(req):
        chunk = MagicMock()
        chunk.result = {"token": "hi", "conversation_id": "c1"}
        chunk.done = True
        yield chunk

    fake_dispatcher = MagicMock()
    fake_dispatcher.dispatch_stream = _stream

    # Fake WebSocket: serves two inbound messages then disconnects.
    class _FakeWS:
        def __init__(self):
            self.scope = {"state": {"source": "ha", "ws_per_turn": True}}
            self.sent: list = []
            self._inbox = [
                '{"text": "turn one"}',
                '{"text": "turn two"}',
            ]
            self.accepted = False
            self.headers = {}
            self.app = MagicMock()
            self.app.state.allowed_ws_origins = set()

        async def accept(self):
            self.accepted = True

        async def receive_text(self):
            if not self._inbox:
                from fastapi import WebSocketDisconnect

                raise WebSocketDisconnect()
            return self._inbox.pop(0)

        async def send_json(self, payload):
            self.sent.append(payload)

    ws = _FakeWS()

    prev_dispatcher = conv_routes._dispatcher
    conv_routes.set_dispatcher(fake_dispatcher)
    try:
        with (
            patch.object(conv_routes, "_build_a2a_request", _capture_build),
            patch.object(tracer_mod.SpanCollector, "flush", _fake_flush),
        ):
            await conv_routes.ws_conversation(ws, _="test-key")
    finally:
        conv_routes.set_dispatcher(prev_dispatcher)

    assert ws.accepted
    # Two turns -> two collectors built and two flushes.
    assert len(built_collectors) == 2
    assert len(flushed_collectors) == 2
    # Each turn must have its own distinct trace_id.
    trace_ids = [c.trace_id for c in built_collectors]
    assert trace_ids[0] != trace_ids[1]
    # Source is propagated from scope state.
    assert all(c.source == "ha" for c in built_collectors)
    # Each flushed collector contains a synthesised ``ws_turn`` root span.
    for c in flushed_collectors:
        names = [s["span_name"] for s in c._spans]
        assert "ws_turn" in names
    # The middleware path is bypassed for /ws/conversation, so no
    # connection-level update_duration should ever be called.
    mock_summary.update_duration.assert_not_awaited()
    # State is cleared after each turn so stale ids cannot leak.
    assert "trace_id" not in ws.scope["state"]
    assert "span_collector" not in ws.scope["state"]
    assert "root_span_id" not in ws.scope["state"]
