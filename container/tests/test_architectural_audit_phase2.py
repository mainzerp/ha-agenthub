"""Tests for Architectural Audit — Phase 2 (Security & Async Hardening)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, WebSocket

from app.api.routes import conversation as conv_module
from app.api.routes.conversation import _get_ws_client_ip, reset_active_ws_connections, ws_conversation
from app.api.routes.sse import _brokers, _publish
from app.security.auth import require_api_key


class TestWebSocketClientIpHardening:
    """Task 2.1 — Harden WebSocket client-IP extraction against spoofed X-Forwarded-For."""

    def test_ws_client_ip_uses_rightmost_non_trusted_address(self):
        """X-Forwarded-For: <spoofed>, <trusted_proxy> must resolve to the trusted-side client."""
        ws = MagicMock(spec=WebSocket)
        ws.client.host = "10.0.0.50"  # trusted proxy
        ws.headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.50"}

        with patch("app.middleware.rate_limit._TRUSTED_PROXIES", {"10.0.0.50"}):
            ip = _get_ws_client_ip(ws)

        assert ip == "1.2.3.4"

    def test_ws_client_ip_falls_back_to_direct_without_forwarded(self):
        ws = MagicMock(spec=WebSocket)
        ws.client.host = "192.168.1.10"
        ws.headers = {}

        ip = _get_ws_client_ip(ws)

        assert ip == "192.168.1.10"

    def test_ws_client_ip_ignores_forwarded_from_untrusted_peer(self):
        """A direct peer that is not a trusted proxy cannot supply X-Forwarded-For."""
        ws = MagicMock(spec=WebSocket)
        ws.client.host = "192.168.1.10"
        ws.headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.50"}

        with patch("app.middleware.rate_limit._TRUSTED_PROXIES", {"10.0.0.50"}):
            ip = _get_ws_client_ip(ws)

        assert ip == "192.168.1.10"


class TestWebSocketConnectionCounter:
    """Task 2.2 — Make WebSocket per-IP connection counter thread-safe and self-healing."""

    def setup_method(self):
        reset_active_ws_connections()

    def teardown_method(self):
        reset_active_ws_connections()

    async def _connect_then_disconnect(self, ip: str):
        ws = MagicMock()
        ws.headers = {"origin": "https://ha.local:8123"}
        ws.app.state.allowed_ws_origins = {"https://ha.local:8123"}
        ws.client.host = ip
        ws.scope = {"state": {}}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()
        ws.receive_text = AsyncMock(side_effect=Exception("stop test"))

        with suppress(Exception):
            await ws_conversation(ws)

    async def test_concurrent_connect_disconnect_produces_correct_final_count(self):
        """Concurrent connect/disconnect cycles must leave the counter at zero."""
        ip = "10.0.0.1"
        tasks = [asyncio.create_task(self._connect_then_disconnect(ip)) for _ in range(20)]
        await asyncio.gather(*tasks)

        assert conv_module._active_ws_connections.get(ip, 0) == 0
        assert ip not in conv_module._active_ws_connections

    async def test_negative_count_guard_never_goes_below_zero(self, caplog):
        """If the counter is already zero on disconnect it must stay at zero and log a warning."""
        ws = MagicMock()
        ws.headers = {"origin": "https://ha.local:8123"}
        ws.app.state.allowed_ws_origins = {"https://ha.local:8123"}
        ws.client.host = "10.0.0.2"
        ws.scope = {"state": {}}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()
        ws.receive_text = AsyncMock(side_effect=Exception("stop test"))

        # Pre-seed a missing/zero entry so the finally block exercises the guard.
        conv_module._active_ws_connections["10.0.0.2"] = 0

        # Simulate a lost increment so the finally block sees a zero counter.
        class _NoIncrementDict(dict):
            def __setitem__(self, key, value):
                if key == "10.0.0.2" and value == 1:
                    return
                super().__setitem__(key, value)

        original_connections = conv_module._active_ws_connections
        conv_module._active_ws_connections = _NoIncrementDict({"10.0.0.2": 0})

        try:
            with caplog.at_level(logging.WARNING, logger="app.api.routes.conversation"), suppress(Exception):
                await ws_conversation(ws)
        finally:
            conv_module._active_ws_connections = original_connections

        assert conv_module._active_ws_connections.get("10.0.0.2", 0) == 0
        assert "was already zero on disconnect" in caplog.text


class TestApiKeyAuthFailure:
    """Task 2.3 — Return 401 instead of 500 when API-key retrieval fails."""

    async def test_require_api_key_returns_401_on_runtime_error(self):
        request = MagicMock()
        request.headers = {"Authorization": "Bearer test-key"}

        with (
            patch(
                "app.security.auth.retrieve_secret",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Fernet key rotated"),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await require_api_key(request)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Unauthorized"

    async def test_require_api_key_logs_key_rotation_event(self, caplog):
        request = MagicMock()
        request.headers = {"Authorization": "Bearer test-key"}

        with (
            caplog.at_level(logging.WARNING, logger="app.security.auth"),
            patch(
                "app.security.auth.retrieve_secret",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Fernet key rotated"),
            ),
            suppress(HTTPException),
        ):
            await require_api_key(request)

        assert "API key retrieval failed (possible Fernet key rotation)" in caplog.text


class TestCancelledErrorPropagation:
    """Task 2.4 — CancelledError must propagate through production fallback handlers."""

    async def test_orchestrator_get_personality_re_raises_cancelled_error(self):
        from app.agents.orchestrator import OrchestratorAgent

        agent = OrchestratorAgent(dispatcher=MagicMock())

        with (
            patch(
                "app.agents.orchestrator.SettingsRepository.get_value",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await agent._get_personality_cached()

    async def test_actionable_agent_re_raises_cancelled_error(self):
        from app.agents.actionable import LightAgent

        agent = LightAgent()
        agent._current_task = MagicMock()
        agent._current_task.verbatim_terms = []

        with (
            patch(
                "app.agents.actionable.resolve_entity_deterministic_first",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await agent._resolve_relevant_entities(MagicMock())

    async def test_llm_complete_re_raises_cancelled_error(self):
        from app.llm.client import complete

        config_row = {"agent_id": "x", "model": "openai/test", "timeout": 5, "max_tokens": 10}
        with (
            patch("app.llm.client.AgentConfigRepository.get", new_callable=AsyncMock, return_value=config_row),
            patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={}),
            patch("app.llm.client.litellm.acompletion", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            await complete("x", [{"role": "user", "content": "hi"}])


class TestCleanupExceptionLogging:
    """Task 2.5 — Unexpected exceptions during cleanup must be logged, not swallowed."""

    async def test_mcp_disconnect_logs_unexpected_cleanup_exception(self, caplog):
        from app.mcp.client import MCPClient

        client = MCPClient("test", "stdio", "python -c 'pass'")

        async def _owner_task():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                raise ValueError("cleanup boom") from exc

        task = asyncio.create_task(_owner_task())
        client._owner_task = task
        client._req_q = asyncio.Queue()
        client._ready = asyncio.Event()

        with caplog.at_level(logging.WARNING, logger="app.mcp.client"):
            await client.disconnect()

        assert "Error disconnecting from MCP server" in caplog.text
        assert "cleanup boom" in caplog.text
        assert task.done()


class TestSsePublisherQueueDrop:
    """Task 2.6 — SSE publisher must drop oldest events cleanly from a full queue."""

    def _make_small_queue(self, topic: str, maxsize: int):
        queue = asyncio.Queue(maxsize=maxsize)
        _brokers.setdefault(topic, []).append(queue)
        return queue

    def setup_method(self):
        _brokers.clear()

    def teardown_method(self):
        _brokers.clear()

    async def test_publish_drops_oldest_until_queue_has_room(self):
        queue = self._make_small_queue("test-topic", 3)

        # Fill the queue
        for i in range(3):
            queue.put_nowait(f"old-{i}")

        assert queue.full()

        # Publish a new event; it should evict oldest entries and succeed.
        await _publish("test-topic", {"t": "new"})

        assert queue.qsize() == 3
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())

        # Oldest item(s) should have been dropped and the new event present.
        assert '"t": "new"' in items[-1]
        assert len(items) == 3

    async def test_publish_handles_concurrent_full_queue(self):
        """Concurrent publishes to a full queue must not raise unhandled exceptions."""
        queue = self._make_small_queue("race-topic", 5)

        for i in range(5):
            queue.put_nowait(f"old-{i}")

        async def publish_many():
            for i in range(20):
                await _publish("race-topic", {"t": i})

        await asyncio.gather(*(publish_many() for _ in range(5)))

        assert queue.qsize() == 5
