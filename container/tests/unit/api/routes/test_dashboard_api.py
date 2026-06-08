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
