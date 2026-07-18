"""Unit tests for dashboard_api GET routes.

Lightweight tests that assert 200 OK and verify response schema.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from tests.conftest import build_integration_test_app


def _build_app(**kwargs):
    """Build test app with admin session overridden."""
    return build_integration_test_app(
        setup_complete=True,
        override_api_key=True,
        override_admin_session=True,
        **kwargs,
    )


async def _client_for(app):
    """Return an httpx client with SetupState patched to complete."""
    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=True,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.mark.asyncio
class TestGetAdminOverview:
    async def test_get_admin_overview_returns_200_with_expected_keys(self, db_repository):
        app = _build_app()
        app.state.registry = MagicMock()
        app.state.registry.list_agents = AsyncMock(return_value=[])
        app.state.entity_index = None
        app.state.cache_manager = None
        app.state.mcp_registry = MagicMock()
        app.state.mcp_registry.list_servers.return_value = []

        with patch(
            "app.api.routes.dashboard_api.ensure_setup_runtime_initialized",
            new_callable=AsyncMock,
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert "recent_requests" in data
        assert "cache_hit_rate" in data
        assert "agent_count" in data
        assert "entity_count" in data
        assert "mcp_server_count" in data
        assert "time_range_hours" in data


@pytest.mark.asyncio
class TestGetAdminAgentById:
    async def test_get_admin_agent_by_id_returns_200(self, db_repository):
        app = _build_app()
        with patch(
            "app.api.routes.dashboard_api.AgentConfigRepository.get",
            new_callable=AsyncMock,
            return_value={"agent_id": "light-agent", "enabled": True},
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/agents/light-agent")

        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "light-agent"
        assert data["enabled"] is True

    async def test_get_admin_agent_by_id_returns_404_when_missing(self, db_repository):
        app = _build_app()
        with patch(
            "app.api.routes.dashboard_api.AgentConfigRepository.get",
            new_callable=AsyncMock,
            return_value=None,
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/agents/nonexistent-agent")

        assert resp.status_code == 404


@pytest.mark.asyncio
class TestGetAdminAgentPrompt:
    async def test_get_admin_agent_prompt_returns_200(self, db_repository, tmp_path):
        from app.api.routes import dashboard_api as dash_routes

        app = _build_app()
        # Create a temporary prompts dir with a light.txt file
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "light.txt").write_text("You are a light agent.", encoding="utf-8")

        original_prompts_dir = dash_routes.PROMPTS_DIR
        dash_routes.PROMPTS_DIR = prompts_dir
        try:
            async for client in _client_for(app):
                resp = await client.get("/api/admin/agents/light-agent/prompt")

            assert resp.status_code == 200
            data = resp.json()
            assert data["agent_id"] == "light-agent"
            assert data["filename"] == "light.txt"
            assert "You are a light agent." in data["content"]
        finally:
            dash_routes.PROMPTS_DIR = original_prompts_dir

    async def test_get_admin_agent_prompt_returns_404_when_missing(self, db_repository):
        app = _build_app()
        async for client in _client_for(app):
            resp = await client.get("/api/admin/agents/nonexistent-agent/prompt")

        assert resp.status_code == 404


@pytest.mark.asyncio
class TestGetAdminPersons:
    async def test_get_admin_persons_returns_200_with_person_entities(self, db_repository):
        app = _build_app()
        ha_client = AsyncMock()
        ha_client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "person.john",
                    "state": "home",
                    "attributes": {
                        "friendly_name": "John",
                        "user_id": "u1",
                        "device_trackers": ["device_tracker.phone"],
                        "source": "device_tracker.phone",
                        "id": "john",
                        "latitude": 52.0,
                        "longitude": 13.0,
                        "gps_accuracy": 10,
                    },
                },
                {
                    "entity_id": "light.kitchen",
                    "state": "on",
                    "attributes": {"friendly_name": "Kitchen"},
                },
            ]
        )
        app.state.ha_client = ha_client

        async for client in _client_for(app):
            resp = await client.get("/api/admin/persons")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["entity_id"] == "person.john"
        assert data[0]["friendly_name"] == "John"

    async def test_get_admin_persons_returns_empty_when_no_ha_client(self, db_repository):
        app = _build_app()
        app.state.ha_client = None

        async for client in _client_for(app):
            resp = await client.get("/api/admin/persons")

        assert resp.status_code == 200
        assert resp.json() == []

    async def test_get_admin_persons_returns_empty_on_ha_exception(self, db_repository):
        app = _build_app()
        ha_client = AsyncMock()
        ha_client.get_states = AsyncMock(side_effect=RuntimeError("HA down"))
        app.state.ha_client = ha_client

        async for client in _client_for(app):
            resp = await client.get("/api/admin/persons")

        assert resp.status_code == 200
        assert resp.json() == []


@pytest.mark.asyncio
class TestGetRewriteConfig:
    async def test_get_rewrite_config_returns_defaults(self, db_repository):
        app = _build_app()
        with patch(
            "app.api.routes.dashboard_api.SettingsRepository.get_value",
            new_callable=AsyncMock,
            side_effect=lambda key, default="": {"rewrite.model": "gpt-4o", "rewrite.temperature": "0.5"}.get(
                key, default
            ),
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/rewrite/config")

        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "gpt-4o"
        assert data["temperature"] == 0.5


@pytest.mark.asyncio
class TestGetPersonalityConfig:
    async def test_get_personality_config_returns_defaults(self, db_repository):
        app = _build_app()
        with patch(
            "app.api.routes.dashboard_api.SettingsRepository.get_value",
            new_callable=AsyncMock,
            side_effect=lambda key, default="": {
                "personality.prompt": "Be helpful",
                "mediation.temperature": "0.3",
                "filler.enabled": "true",
                "filler.threshold_ms": "1500",
            }.get(key, default),
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/personality/config")

        assert resp.status_code == 200
        data = resp.json()
        assert data["prompt"] == "Be helpful"
        assert data["mediation_temperature"] == 0.3
        assert data["filler_enabled"] is True
        assert data["filler_threshold_ms"] == 1500


@pytest.mark.asyncio
class TestGetSendDevices:
    async def test_get_send_devices_returns_list(self, db_repository):
        app = _build_app()
        with patch(
            "app.api.routes.dashboard_api.SendDeviceMappingRepository.list_all",
            new_callable=AsyncMock,
            return_value=[{"id": 1, "display_name": "Kitchen Speaker"}],
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/send-devices")

        assert resp.status_code == 200
        assert resp.json() == [{"id": 1, "display_name": "Kitchen Speaker"}]


@pytest.mark.asyncio
class TestAdminChatBridge:
    async def test_chat_stream_mirrors_full_envelope(self, db_repository):
        """M-3/M-4: dashboard stream maps status/agents markers and the full
        done-frame metadata envelope."""
        import json as _json

        from app.api.routes import dashboard_api

        app = _build_app()

        async def _envelope_stream(req):
            yield {
                "token": "",
                "done": False,
                "status": "multi_agent",
                "agents": ["light-agent", "music-agent"],
            }
            yield {
                "token": "",
                "done": True,
                "conversation_id": "conv-1",
                "mediated_speech": "Both done.",
                "voice_followup": True,
                "routed_to": "light-agent, music-agent",
                "action_executed": {"action": "turn_on", "entity_id": "light.kitchen", "success": True},
            }

        mock_d = MagicMock()
        mock_d.dispatch_stream = _envelope_stream
        old_dispatcher = dashboard_api._dispatcher
        dashboard_api._dispatcher = mock_d
        try:
            async for client in _client_for(app):
                resp = await client.post("/api/admin/chat/stream", json={"text": "do both"})
        finally:
            dashboard_api._dispatcher = old_dispatcher

        assert resp.status_code == 200
        lines = [line for line in resp.text.splitlines() if line.startswith("data:")]
        assert len(lines) == 2
        first = _json.loads(lines[0].removeprefix("data:").strip())
        assert first["status"] == "multi_agent"
        assert first["agents"] == ["light-agent", "music-agent"]
        last = _json.loads(lines[1].removeprefix("data:").strip())
        assert last["done"] is True
        assert last["voice_followup"] is True
        assert last["routed_agent"] == "light-agent, music-agent"
        assert last["action_executed"]["entity_id"] == "light.kitchen"
        assert last["action_executed"]["service"] == "light/turn_on"

    async def test_chat_stream_error_frame_surfaces(self, db_repository):
        """M-4: dashboard stream forwards the error field on done frames."""
        import json as _json

        from app.api.routes import dashboard_api

        app = _build_app()

        async def _error_stream(req):
            yield {"token": "", "done": True, "error": "classification failed"}

        mock_d = MagicMock()
        mock_d.dispatch_stream = _error_stream
        old_dispatcher = dashboard_api._dispatcher
        dashboard_api._dispatcher = mock_d
        try:
            async for client in _client_for(app):
                resp = await client.post("/api/admin/chat/stream", json={"text": "hi"})
        finally:
            dashboard_api._dispatcher = old_dispatcher

        assert resp.status_code == 200
        lines = [line for line in resp.text.splitlines() if line.startswith("data:")]
        last = _json.loads(lines[-1].removeprefix("data:").strip())
        assert last["done"] is True
        assert last["error"] == "classification failed"

    async def test_admin_chat_rest_forwards_error(self, db_repository):
        """M-4: REST chat bridge forwards the pipeline error string."""
        from app.api.routes import dashboard_api

        app = _build_app()
        mock_d = MagicMock()
        mock_d.dispatch = AsyncMock(return_value={"speech": "", "error": "classification failed"})
        old_dispatcher = dashboard_api._dispatcher
        dashboard_api._dispatcher = mock_d
        try:
            async for client in _client_for(app):
                resp = await client.post("/api/admin/chat", json={"text": "hi"})
        finally:
            dashboard_api._dispatcher = old_dispatcher

        assert resp.status_code == 200
        assert resp.json()["error"] == "classification failed"
