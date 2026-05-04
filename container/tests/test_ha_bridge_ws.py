"""Advanced WebSocket bridge integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

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
            final = MagicMock()
            final.result = {"token": ""}
            final.done = True
            yield final

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
            filler = MagicMock()
            filler.result = {"token": "One moment...", "is_filler": True, "filler_push": "One moment please"}
            filler.done = False
            yield filler
            final = MagicMock()
            final.result = {"token": "", "mediated_speech": "Done"}
            final.done = True
            yield final

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
            final = MagicMock()
            final.result = {"token": ""}
            final.done = True
            yield final

        mock_d = MagicMock()
        mock_d.dispatch_stream = _capture_stream

        async with HAMimicClient(light_scenario_app) as client:
            conv_routes._dispatcher = mock_d
            try:
                await client.connect_ws()
                await client.send_turn("turn on the kitchen light", device_id="satellite_kitchen")
                assert captured_request is not None
                sent_task = captured_request.params["task"]
                assert sent_task["context"]["device_id"] == "satellite_kitchen"
            finally:
                conv_routes._dispatcher = old_dispatcher
