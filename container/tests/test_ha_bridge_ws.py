"""Advanced WebSocket bridge integration tests."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from app.api.routes.conversation import ws_conversation
from tests.helpers import HAMimicClient
from tests.scenarios.loader import load_scenario

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def light_scenario_app(db_path):
    scenario_path = Path(__file__).resolve().parent / "data" / "scenarios" / "light" / "turn_on_kitchen.yaml"
    scenario = load_scenario(scenario_path)
    from tests.conftest import build_scenario_backed_app

    app = build_scenario_backed_app(scenario, db_path)
    yield app


# ===================================================================
# Advanced WebSocket tests
# ===================================================================


@pytest.mark.integration
class TestWsAdvanced:
    async def test_ws_per_turn_trace_isolation(self, light_scenario_app):
        """Two turns on the same socket produce two distinct trace summaries."""
        async with HAMimicClient(light_scenario_app) as client:
            await client.connect_ws()
            tokens1 = await client.send_turn("turn on the kitchen light")
            assert tokens1[-1].get("done") is True
            tokens2 = await client.send_turn("turn on the kitchen light")
            assert tokens2[-1].get("done") is True

    async def test_ws_rate_limit_returns_error(self, light_scenario_app, _reset_rate_limit_store):
        """Exceed 20 burst messages; expect rate-limit error JSON."""
        from fastapi.testclient import TestClient

        from app.api.routes import conversation as conv_routes

        old_dispatcher = conv_routes._dispatcher

        async def _fast_stream(req):
            yield {"token": "", "done": True}

        mock_d = MagicMock()
        mock_d.dispatch_stream = _fast_stream

        def _flood():
            with TestClient(light_scenario_app) as c:
                conv_routes._dispatcher = mock_d
                try:
                    with c.websocket_connect(
                        "/ws/conversation",
                        headers={"Authorization": "Bearer test-api-key"},
                    ) as ws:
                        for _ in range(100):
                            ws.send_json({"text": "x"})
                            msg = ws.receive_json()
                            if msg.get("error") == "Rate limit exceeded":
                                return msg
                finally:
                    conv_routes._dispatcher = old_dispatcher
            return None

        result = await asyncio.to_thread(_flood)
        assert result is not None
        assert result.get("error") == "Rate limit exceeded"
        assert "retry_after_ms" in result

    async def test_ws_filler_push_flow(self, light_scenario_app):
        """Mock dispatcher yields a filler token with filler_push; assert mid-stream shape."""
        from app.api.routes import conversation as conv_routes

        old_dispatcher = conv_routes._dispatcher

        async def _filler_push_stream(req):
            yield {"token": "One moment...", "is_filler": True, "filler_push": "One moment please", "done": False}
            yield {"token": "", "mediated_speech": "Done", "done": True}

        mock_d = MagicMock()
        mock_d.dispatch_stream = _filler_push_stream

        async with HAMimicClient(light_scenario_app) as client:
            conv_routes._dispatcher = mock_d
            try:
                await client.connect_ws()
                tokens = await client.send_turn("do something")
                assert len(tokens) >= 2
                filler_token = tokens[0]
                assert filler_token.get("is_filler") is True
                assert filler_token.get("filler_push") == "One moment please"
                assert filler_token.get("sanitized") is False
                assert tokens[-1].get("done") is True
            finally:
                conv_routes._dispatcher = old_dispatcher

    async def test_multi_turn_conversation_id_preserved(self, light_scenario_app):
        """Send two turns with the same conversation_id; assert state carries."""
        async with HAMimicClient(light_scenario_app) as client:
            await client.connect_ws()
            tokens1 = await client.send_turn("turn on the kitchen light", conversation_id="conv-multi")
            assert tokens1[-1].get("conversation_id") == "conv-multi"
            tokens2 = await client.send_turn("turn it off", conversation_id="conv-multi")
            assert tokens2[-1].get("conversation_id") == "conv-multi"

    async def test_device_id_forwarded_to_task_context(self, light_scenario_app):
        """Send device_id in payload; assert it reaches the dispatcher task context."""
        from app.api.routes import conversation as conv_routes

        old_dispatcher = conv_routes._dispatcher
        captured_request = None

        async def _capture_stream(req):
            nonlocal captured_request
            captured_request = req
            yield {"token": "", "done": True}

        mock_d = MagicMock()
        mock_d.dispatch_stream = _capture_stream

        async with HAMimicClient(light_scenario_app) as client:
            conv_routes._dispatcher = mock_d
            try:
                await client.connect_ws()
                await client.send_turn("turn on the kitchen light", device_id="satellite_kitchen")
                assert captured_request is not None
                sent_task = captured_request.params["task"]
                assert sent_task.context.device_id == "satellite_kitchen"
            finally:
                conv_routes._dispatcher = old_dispatcher


class TestWsOriginRejection:
    """Task 3.1 — Reject disallowed WebSocket origins with a clear reason and log line."""

    def _make_ws(self, origin: str | None, allowed_origins: set[str] | None = None):
        ws = MagicMock()
        ws.headers = {"origin": origin}
        ws.client.host = "192.168.1.10"
        ws.app.state.allowed_ws_origins = allowed_origins or set()
        ws.scope = {"state": {}}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()
        ws.receive_text = AsyncMock(side_effect=Exception("stop test"))
        return ws

    async def test_empty_allowed_origins_rejects_with_setup_incomplete_reason(self, caplog):
        ws = self._make_ws("https://ha.local:8123")
        with caplog.at_level(logging.WARNING, logger="app.api.routes.conversation"):
            await ws_conversation(ws)

        ws.close.assert_awaited_once()
        call_kwargs = ws.close.await_args.kwargs
        assert call_kwargs["code"] == 1008
        assert "Setup incomplete" in call_kwargs["reason"]
        assert "allowed origins list is empty" in caplog.text

    async def test_mismatched_origin_rejects_with_origin_reason(self, caplog):
        ws = self._make_ws("https://evil.example.com", {"https://ha.local:8123"})
        with caplog.at_level(logging.WARNING, logger="app.api.routes.conversation"):
            await ws_conversation(ws)

        ws.close.assert_awaited_once()
        call_kwargs = ws.close.await_args.kwargs
        assert call_kwargs["code"] == 1008
        assert "https://evil.example.com" in call_kwargs["reason"]
        assert "disallowed origin" in caplog.text

    async def test_missing_origin_is_allowed(self):
        """Non-browser clients may omit the Origin header; do not reject them."""
        ws = self._make_ws(None, {"https://ha.local:8123"})
        ws.headers = {}
        ws.receive_text = AsyncMock(side_effect=Exception("stop test"))

        from contextlib import suppress

        with suppress(Exception):
            await ws_conversation(ws)

        ws.close.assert_not_awaited()


class TestWsConnectionLimits:
    """Task 4.5 — Enforce per-IP WebSocket connection limits."""

    def _make_limited_ws(self, ip: str, stop_event: asyncio.Event | None = None):
        """Return a mocked WebSocket whose receive_text blocks until ``stop_event`` is set."""
        from starlette.websockets import WebSocketDisconnect

        ws = MagicMock()
        ws.headers = {"origin": "https://ha.local:8123"}
        ws.client.host = ip
        ws.app.state.allowed_ws_origins = {"https://ha.local:8123"}
        ws.scope = {"state": {}}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        async def _receive_text():
            if stop_event is None:
                # Never-ending connection for rejection tests.
                await asyncio.Event().wait()
            await stop_event.wait()
            raise WebSocketDisconnect(code=1000, reason="stop test")

        ws.receive_text = AsyncMock(side_effect=_receive_text)
        return ws

    async def test_multiple_concurrent_connections_from_same_ip_are_rejected(self):
        """When _active_ws_connections already holds the per-IP max, the next is rejected."""
        from app.api.routes import conversation as conv_module
        from app.api.routes.conversation import ws_conversation

        conv_module.reset_active_ws_connections()
        max_conn = conv_module._MAX_WS_CONNECTIONS_PER_IP
        ip = "10.0.0.1"

        # Simulate ``max_conn`` concurrent open connections from one IP.
        conv_module._active_ws_connections[ip] = max_conn
        ws = self._make_limited_ws(ip)
        await ws_conversation(ws)

        ws.close.assert_awaited_once_with(code=1008, reason="Connection limit exceeded")
        conv_module.reset_active_ws_connections()

    async def test_disconnect_frees_connection_slot(self):
        """Closing a connection decrements the per-IP counter so a new one succeeds."""
        from app.api.routes import conversation as conv_module
        from app.api.routes.conversation import ws_conversation

        conv_module.reset_active_ws_connections()
        max_conn = conv_module._MAX_WS_CONNECTIONS_PER_IP
        ip = "10.0.0.2"

        stop_events = [asyncio.Event() for _ in range(max_conn)]
        tasks = []
        try:
            for event in stop_events:
                ws = self._make_limited_ws(ip, event)
                tasks.append(asyncio.create_task(ws_conversation(ws)))

            # Wait for all handlers to be inside their receive loops.
            for _ in range(50):
                if conv_module._active_ws_connections.get(ip, 0) == max_conn:
                    break
                await asyncio.sleep(0.01)
            assert conv_module._active_ws_connections.get(ip, 0) == max_conn

            # A new connection from the same IP must be rejected.
            ws_rejected = self._make_limited_ws(ip)
            await ws_conversation(ws_rejected)
            ws_rejected.close.assert_awaited_once_with(code=1008, reason="Connection limit exceeded")

            # Close one existing connection and confirm the slot is freed.
            stop_events[0].set()
            await asyncio.wait_for(tasks[0], timeout=1.0)
            for _ in range(50):
                if conv_module._active_ws_connections.get(ip, 0) == max_conn - 1:
                    break
                await asyncio.sleep(0.01)
            assert conv_module._active_ws_connections.get(ip, 0) == max_conn - 1

            stop_new = asyncio.Event()
            ws_new = self._make_limited_ws(ip, stop_new)
            task_new = asyncio.create_task(ws_conversation(ws_new))
            await asyncio.sleep(0.05)
            assert conv_module._active_ws_connections.get(ip, 0) == max_conn
            stop_new.set()
            await asyncio.wait_for(task_new, timeout=1.0)
        finally:
            for event in stop_events:
                event.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            conv_module.reset_active_ws_connections()
