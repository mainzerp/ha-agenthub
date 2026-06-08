"""Unit tests for admin.py route handlers.

Lightweight tests that assert correct response shapes and cover branches
not already exercised by the integration suite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from tests.conftest import build_integration_test_app

from app.api.routes.admin import _validate_setting_value


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


# =====================================================================
# 1. GET /api/admin/ha-connection
# =====================================================================


@pytest.mark.asyncio
class TestGetHaConnection:
    async def test_get_ha_connection_returns_url_and_token_status(self, db_repository):
        app = _build_app()
        with (
            patch(
                "app.api.routes.admin.SettingsRepository.get_value",
                new_callable=AsyncMock,
                return_value="http://ha.local:8123",
            ),
            patch(
                "app.api.routes.admin.get_ha_token",
                new_callable=AsyncMock,
                return_value="supersecrettoken",
            ),
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/ha-connection")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ha_url"] == "http://ha.local:8123"
        assert data["token_configured"] is True

    async def test_get_ha_connection_no_token_returns_false(self, db_repository):
        app = _build_app()
        with (
            patch(
                "app.api.routes.admin.SettingsRepository.get_value",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "app.api.routes.admin.get_ha_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/ha-connection")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ha_url"] is None
        assert data["token_configured"] is False


# =====================================================================
# 2. GET /api/admin/container-api-key
# =====================================================================


@pytest.mark.asyncio
class TestGetContainerApiKeyStatus:
    async def test_get_container_api_key_status_configured_and_not(self, db_repository):
        app = _build_app()

        # configured = True
        with patch(
            "app.api.routes.admin.retrieve_secret",
            new_callable=AsyncMock,
            return_value="some-key",
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/container-api-key")
        assert resp.status_code == 200
        assert resp.json()["configured"] is True

        # configured = False
        with patch(
            "app.api.routes.admin.retrieve_secret",
            new_callable=AsyncMock,
            return_value=None,
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/container-api-key")
        assert resp.status_code == 200
        assert resp.json()["configured"] is False


# =====================================================================
# 3. GET /api/admin/notification-profile
# =====================================================================


@pytest.mark.asyncio
class TestGetNotificationProfile:
    async def test_get_notification_profile_with_and_without_data(self, db_repository):
        app = _build_app()

        # with data
        with patch(
            "app.api.routes.admin.SettingsRepository.get_value",
            new_callable=AsyncMock,
            return_value='{"channels": ["tts"]}',
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/notification-profile")
        assert resp.status_code == 200
        assert resp.json()["profile"] == {"channels": ["tts"]}

        # without data
        with patch(
            "app.api.routes.admin.SettingsRepository.get_value",
            new_callable=AsyncMock,
            return_value=None,
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/notification-profile")
        assert resp.status_code == 200
        assert resp.json()["profile"] == {}


# =====================================================================
# 4. PUT /api/admin/notification-profile
# =====================================================================


@pytest.mark.asyncio
class TestUpdateNotificationProfile:
    async def test_update_notification_profile_persists(self, db_repository):
        app = _build_app()
        calls = []

        async def _fake_set(key, value, **kwargs):
            calls.append((key, value))

        with patch(
            "app.api.routes.admin.SettingsRepository.set",
            new_callable=AsyncMock,
            side_effect=_fake_set,
        ):
            async for client in _client_for(app):
                resp = await client.put(
                    "/api/admin/notification-profile",
                    json={"profile": {"channels": ["persistent_notification"]}},
                )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert any(key == "notification.profile" and "persistent_notification" in value for key, value in calls)


# =====================================================================
# 5. DELETE /api/admin/llm-providers/{provider}
# =====================================================================


@pytest.mark.asyncio
class TestDeleteLlmProviderKey:
    async def test_delete_llm_provider_key_unknown_provider_raises_400(self, db_repository):
        app = _build_app()
        async for client in _client_for(app):
            resp = await client.delete("/api/admin/llm-providers/unknown_provider")
        assert resp.status_code == 400
        assert "Unknown provider" in resp.json()["detail"]


# =====================================================================
# 6. POST /api/admin/ha-connection/test
# =====================================================================


@pytest.mark.asyncio
class TestTestHaConnectionAdmin:
    async def test_test_ha_connection_admin_validation_errors(self, db_repository):
        app = _build_app()

        # Empty URL and token
        with (
            patch(
                "app.api.routes.admin.SettingsRepository.get_value",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "app.api.routes.admin.get_ha_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            async for client in _client_for(app):
                resp = await client.post("/api/admin/ha-connection/test", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "Need both URL and token" in data["detail"]

        # Invalid URL scheme
        with (
            patch(
                "app.api.routes.admin.SettingsRepository.get_value",
                new_callable=AsyncMock,
                return_value="ftp://invalid",
            ),
            patch(
                "app.api.routes.admin.get_ha_token",
                new_callable=AsyncMock,
                return_value="token123",
            ),
        ):
            async for client in _client_for(app):
                resp = await client.post(
                    "/api/admin/ha-connection/test",
                    json={},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "URL must start with http" in data["detail"]

        # Failed connection
        with (
            patch(
                "app.api.routes.admin.SettingsRepository.get_value",
                new_callable=AsyncMock,
                return_value="http://ha.local:8123",
            ),
            patch(
                "app.api.routes.admin.get_ha_token",
                new_callable=AsyncMock,
                return_value="token123",
            ),
            patch(
                "app.api.routes.admin.test_ha_connection",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            async for client in _client_for(app):
                resp = await client.post(
                    "/api/admin/ha-connection/test",
                    json={},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "Could not reach Home Assistant" in data["detail"]

        # Successful connection
        with (
            patch(
                "app.api.routes.admin.SettingsRepository.get_value",
                new_callable=AsyncMock,
                return_value="http://ha.local:8123",
            ),
            patch(
                "app.api.routes.admin.get_ha_token",
                new_callable=AsyncMock,
                return_value="token123",
            ),
            patch(
                "app.api.routes.admin.test_ha_connection",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            async for client in _client_for(app):
                resp = await client.post(
                    "/api/admin/ha-connection/test",
                    json={},
                )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# =====================================================================
# 7. _validate_setting_value
# =====================================================================


class TestValidateSettingValue:
    def test_validate_setting_value_type_errors(self):
        # empty string for typed settings
        with pytest.raises(HTTPException) as exc_info:
            _validate_setting_value("my_key", "", "int")
        assert exc_info.value.status_code == 400
        assert "empty string" in exc_info.value.detail.lower()

        with pytest.raises(HTTPException) as exc_info:
            _validate_setting_value("my_key", "", "float")
        assert exc_info.value.status_code == 400

        with pytest.raises(HTTPException) as exc_info:
            _validate_setting_value("my_key", "", "bool")
        assert exc_info.value.status_code == 400

        # invalid int
        with pytest.raises(HTTPException) as exc_info:
            _validate_setting_value("my_key", "notanint", "int")
        assert exc_info.value.status_code == 400
        assert "expected int" in exc_info.value.detail

        # invalid float
        with pytest.raises(HTTPException) as exc_info:
            _validate_setting_value("my_key", "notafloat", "float")
        assert exc_info.value.status_code == 400
        assert "expected float" in exc_info.value.detail

        # invalid bool
        with pytest.raises(HTTPException) as exc_info:
            _validate_setting_value("my_key", "maybe", "bool")
        assert exc_info.value.status_code == 400
        assert "expected bool" in exc_info.value.detail

        # valid values should not raise
        _validate_setting_value("my_key", "42", "int")
        _validate_setting_value("my_key", "3.14", "float")
        _validate_setting_value("my_key", "true", "bool")
        _validate_setting_value("my_key", "1", "bool")
        _validate_setting_value("my_key", "0", "bool")
        _validate_setting_value("my_key", "false", "bool")


# =====================================================================
# 8. GET /api/admin/llm-providers
# =====================================================================


@pytest.mark.asyncio
class TestGetLlmProviderStatus:
    async def test_get_llm_provider_status_custom_openai_and_ollama(self, db_repository):
        app = _build_app()

        with (
            patch(
                "app.api.routes.admin.SecretsRepository.list_keys",
                new_callable=AsyncMock,
                return_value={"custom_openai_api_key"},
            ),
            patch(
                "app.api.routes.admin.SettingsRepository.get_value",
                new_callable=AsyncMock,
                side_effect=lambda key, default=None: {
                    "custom_openai_provider.base_url": "http://localhost:8080/v1",
                    "custom_openai_provider.name": "My Custom",
                    "ollama_base_url": "http://localhost:11434",
                }.get(key, default),
            ),
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/llm-providers")

        assert resp.status_code == 200
        data = resp.json()
        providers = data["providers"]

        # custom_openai should be configured because key + base_url exist
        assert providers["custom_openai"]["configured"] is True
        assert providers["custom_openai"]["name"] == "My Custom"
        assert providers["custom_openai"]["url"] == "http://localhost:8080/v1"

        # ollama should be configured because url exists
        assert providers["ollama"]["configured"] is True
        assert providers["ollama"]["url"] == "http://localhost:11434"

        # other providers should not be configured
        for provider in ("openrouter", "groq", "anthropic", "cerebras"):
            assert providers[provider]["configured"] is False

    async def test_get_llm_provider_status_custom_openai_missing_url(self, db_repository):
        app = _build_app()

        with (
            patch(
                "app.api.routes.admin.SecretsRepository.list_keys",
                new_callable=AsyncMock,
                return_value={"custom_openai_api_key"},
            ),
            patch(
                "app.api.routes.admin.SettingsRepository.get_value",
                new_callable=AsyncMock,
                side_effect=lambda key, default=None: {
                    "custom_openai_provider.base_url": None,
                    "custom_openai_provider.name": None,
                    "ollama_base_url": None,
                }.get(key, default),
            ),
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/llm-providers")

        assert resp.status_code == 200
        data = resp.json()
        providers = data["providers"]

        # custom_openai should NOT be configured because base_url is missing
        assert providers["custom_openai"]["configured"] is False
        assert providers["custom_openai"]["name"] is None
        assert providers["custom_openai"]["url"] is None

        # ollama should NOT be configured because url is missing
        assert providers["ollama"]["configured"] is False
        assert providers["ollama"]["url"] is None


# =====================================================================
# 9. GET /api/admin/settings
# =====================================================================


@pytest.mark.asyncio
class TestGetSettings:
    async def test_get_settings_returns_grouped_settings(self, db_repository):
        app = _build_app()
        with patch(
            "app.api.routes.admin.SettingsRepository.get_all",
            new_callable=AsyncMock,
            return_value=[
                {"key": "a", "value": "1", "category": "general"},
                {"key": "b", "value": "2", "category": "ha"},
            ],
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/settings")

        assert resp.status_code == 200
        data = resp.json()
        assert "settings" in data
        assert len(data["settings"]["general"]) == 1
        assert len(data["settings"]["ha"]) == 1


# =====================================================================
# 10. GET /api/admin/settings/wake-briefing
# =====================================================================


@pytest.mark.asyncio
class TestGetWakeBriefingSettings:
    async def test_get_wake_briefing_settings_returns_structured_data(self, db_repository):
        app = _build_app()
        with patch(
            "app.api.routes.admin.SettingsRepository.get_value",
            new_callable=AsyncMock,
            side_effect=lambda key, default=None: {
                "wake_briefing.sensor_entities": '["sensor.temp"]',
                "wake_briefing.timeout_seconds": "15",
                "wake_briefing.news_count": "5",
                "wake_briefing.enabled": "true",
                "wake_briefing.sources.weather": "true",
                "wake_briefing.sources.date": "false",
                "wake_briefing.sources.news": "true",
                "wake_briefing.sources.calendar": "false",
                "wake_briefing.sources.sensors": "true",
                "wake_briefing.news_query": "sports",
                "wake_briefing.composer_prompt": "Compose a briefing",
            }.get(key, default),
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/settings/wake-briefing")

        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["sources"]["weather"] is True
        assert data["sources"]["date"] is False
        assert data["sources"]["sensors"] is True
        assert data["sensor_entities"] == ["sensor.temp"]
        assert data["news_query"] == "sports"
        assert data["news_count"] == 5
        assert data["timeout_seconds"] == 15
        assert data["composer_prompt"] == "Compose a briefing"


# =====================================================================
# 11. GET /api/admin/entity-matching-weights
# =====================================================================


@pytest.mark.asyncio
class TestGetEntityMatchingWeights:
    async def test_get_entity_matching_weights_returns_weights(self, db_repository):
        app = _build_app()
        with patch(
            "app.api.routes.admin.EntityMatchingConfigRepository.get_all",
            new_callable=AsyncMock,
            return_value=[
                {"key": "weight.levenshtein", "value": "0.5"},
                {"key": "weight.jaro_winkler", "value": "0.3"},
            ],
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/entity-matching-weights")

        assert resp.status_code == 200
        data = resp.json()
        assert data["weights"]["weight.levenshtein"] == "0.5"
        assert data["weights"]["weight.jaro_winkler"] == "0.3"


# =====================================================================
# 12. GET /api/admin/llm-providers/configured
# =====================================================================


@pytest.mark.asyncio
class TestGetConfiguredProviders:
    async def test_get_configured_providers_mixed_status(self, db_repository):
        app = _build_app()
        with (
            patch(
                "app.api.routes.admin.SecretsRepository.list_keys",
                new_callable=AsyncMock,
                return_value={"groq_api_key", "anthropic_api_key", "custom_openai_api_key"},
            ),
            patch(
                "app.api.routes.admin.SettingsRepository.get_value",
                new_callable=AsyncMock,
                side_effect=lambda key, default=None: {
                    "custom_openai_provider.base_url": "http://localhost:8080",
                    "ollama_base_url": "http://localhost:11434",
                }.get(key, default),
            ),
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/llm-providers/configured")

        assert resp.status_code == 200
        data = resp.json()
        assert "openrouter" in data["providers"]
        assert "groq" in data["configured"]
        assert "anthropic" in data["configured"]
        assert "custom_openai" in data["configured"]
        assert "ollama" in data["configured"]


# =====================================================================
# 13. GET /api/admin/agents/visibility-summary
# =====================================================================


@pytest.mark.asyncio
class TestGetAgentsVisibilitySummary:
    async def test_get_visibility_summary_aggregates_rules(self, db_repository):
        app = _build_app()
        with patch(
            "app.api.routes.admin.EntityVisibilityRepository.list_all",
            new_callable=AsyncMock,
            return_value=[
                {"agent_id": "light-agent", "rule_type": "domain_include", "rule_value": "light"},
                {"agent_id": "light-agent", "rule_type": "domain_exclude", "rule_value": "switch"},
                {"agent_id": "light-agent", "rule_type": "area_include", "rule_value": "kitchen"},
                {"agent_id": "light-agent", "rule_type": "entity_include", "rule_value": "light.living_room"},
                {"agent_id": "light-agent", "rule_type": "entity_exclude", "rule_value": "light.bedroom"},
                {"agent_id": "light-agent", "rule_type": "device_class_include", "rule_value": "outlet"},
                {"agent_id": "light-agent", "rule_type": "device_class_exclude", "rule_value": "plug"},
            ],
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/agents/visibility-summary")

        assert resp.status_code == 200
        data = resp.json()
        summary = data["summary"]["light-agent"]
        assert "light" in summary["domains"]
        assert "area:kitchen" in summary["domains"]
        assert "switch" in summary["excluded_domains"]
        assert "outlet" in summary["device_classes"]
        assert "plug" in summary["excluded_device_classes"]
        assert summary["has_rules"] is True
