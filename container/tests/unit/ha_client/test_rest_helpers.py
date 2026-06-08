"""Unit tests for HARestClient helper functions.

Tests reload, _refresh_headers, render_template, registry cache,
get_area_registry, get_user_language, and expect_state.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.ha_client.rest import HARestClient


class TestReload:
    @patch("app.ha_client.rest.SettingsRepository")
    @patch("app.ha_client.rest.get_auth_headers", new_callable=AsyncMock)
    async def test_reload_initializes_when_client_none(self, mock_auth, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://ha.local:8123")
        mock_auth.return_value = {"Authorization": "Bearer test-token"}

        client = HARestClient()
        assert client._client is None

        await client.reload()

        assert client._base_url == "http://ha.local:8123"
        assert client._client is not None
        await client.close()

    @patch("app.ha_client.rest.SettingsRepository")
    @patch("app.ha_client.rest.get_auth_headers", new_callable=AsyncMock)
    async def test_reload_rebuilds_when_url_changes(self, mock_auth, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://new.local:8123")
        mock_auth.return_value = {"Authorization": "Bearer test-token"}

        client = HARestClient()
        client._base_url = "http://old.local:8123"
        old_client = MagicMock()
        old_client.aclose = AsyncMock()
        client._client = old_client

        await client.reload()

        assert client._base_url == "http://new.local:8123"
        assert client._client is not old_client
        old_client.aclose.assert_awaited_once()
        await client.close()

    @patch("app.ha_client.rest.SettingsRepository")
    @patch("app.ha_client.rest.get_auth_headers", new_callable=AsyncMock)
    async def test_reload_refreshes_headers_when_url_unchanged(self, mock_auth, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://ha.local:8123")
        mock_auth.return_value = {"Authorization": "Bearer new-token"}

        client = HARestClient()
        client._base_url = "http://ha.local:8123"
        mock_client = MagicMock()
        mock_client.headers = {}
        client._client = mock_client

        await client.reload()

        assert client._base_url == "http://ha.local:8123"
        assert client._client is mock_client
        assert mock_client.headers["Authorization"] == "Bearer new-token"


class TestRefreshHeaders:
    @patch("app.ha_client.rest.SettingsRepository")
    @patch("app.ha_client.rest.get_auth_headers", new_callable=AsyncMock)
    async def test_refresh_headers_is_noop_when_client_closed(self, mock_auth, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://ha.local:8123")
        mock_auth.return_value = {"Authorization": "Bearer test-token"}

        client = HARestClient()
        await client.initialize()
        await client.close()

        assert client._client is None
        await client._refresh_headers()
        assert client._client is None


class TestRenderTemplate:
    async def test_render_template_returns_none_on_empty_whitespace_response(self):
        client = HARestClient()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "   \n  "
        mock_client.post = AsyncMock(return_value=mock_response)
        client._client = mock_client

        result = await client.render_template("{{ 1 + 1 }}")

        assert result is None
        mock_client.post.assert_awaited_once()

    async def test_render_template_success(self):
        client = HARestClient()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "  rendered result  "
        mock_client.post = AsyncMock(return_value=mock_response)
        client._client = mock_client

        result = await client.render_template("{{ states('light.kitchen') }}", variables={"x": 1})

        assert result == "rendered result"
        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.await_args
        assert call_args.args[0] == "/api/template"
        assert call_args.kwargs["json"]["template"] == "{{ states('light.kitchen') }}"
        assert call_args.kwargs["json"]["variables"] == {"x": 1}

    async def test_render_template_returns_none_when_client_none(self):
        client = HARestClient()
        client._client = None

        result = await client.render_template("{{ 1 + 1 }}")
        assert result is None

    async def test_render_template_returns_none_on_error(self):
        client = HARestClient()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx.HTTPError("connection failed"))
        client._client = mock_client

        result = await client.render_template("{{ 1 + 1 }}")
        assert result is None


class TestRegistryCache:
    async def test_registry_cache_and_area_registry_comprehensive(self):
        client = HARestClient()

        # --- _registry_cache_get / _registry_cache_put ---
        client._registry_cache_put("test_key", "test_value")
        assert client._registry_cache_get("test_key") == "test_value"
        assert client._registry_cache_get("missing") is None

        # TTL expiry
        client._registry_cache_ttl_sec = 0.01
        client._registry_cache_put("ttl_key", "ttl_value")
        time.sleep(0.02)
        assert client._registry_cache_get("ttl_key") is None
        client._registry_cache_ttl_sec = 300.0

        # --- clear_area_registry_cache ---
        client._registry_cache_put("area_registry", {"kitchen": "Kitchen"})
        client._registry_cache_put("entity_aliases", {})
        client.clear_area_registry_cache()
        assert client._registry_cache_get("area_registry") is None
        assert client._registry_cache_get("entity_aliases") is None

        # --- get_area_registry: cache miss with successful render ---
        client.render_template = AsyncMock(
            return_value='[{"id": "kitchen", "name": "Kitchen"}, {"id": "bedroom", "name": "Bedroom"}]'
        )
        result = await client.get_area_registry()
        assert result == {"kitchen": "Kitchen", "bedroom": "Bedroom"}
        client.render_template.assert_awaited_once()

        # --- get_area_registry: cache hit ---
        client.render_template.reset_mock()
        result2 = await client.get_area_registry()
        assert result2 == {"kitchen": "Kitchen", "bedroom": "Bedroom"}
        client.render_template.assert_not_called()

        # --- get_area_registry: empty response ---
        client.clear_area_registry_cache()
        client.render_template = AsyncMock(return_value="")
        result3 = await client.get_area_registry()
        assert result3 == {}

        # --- get_area_registry: invalid JSON ---
        client.clear_area_registry_cache()
        client.render_template = AsyncMock(return_value="not-json")
        result4 = await client.get_area_registry()
        assert result4 == {}

        # --- get_user_language: cache miss ---
        client.get_config = AsyncMock(return_value={"language": "de"})
        client._registry_cache.clear()
        lang = await client.get_user_language()
        assert lang == "de"
        client.get_config.assert_awaited_once()

        # --- get_user_language: cache hit ---
        lang2 = await client.get_user_language()
        assert lang2 == "de"
        assert client.get_config.await_count == 1

        # --- get_user_language: no language in config ---
        client._registry_cache.clear()
        client.get_config = AsyncMock(return_value={})
        lang3 = await client.get_user_language()
        assert lang3 is None

        # --- get_user_language: cached empty string returns None ---
        client._registry_cache_put("user_language", "")
        lang4 = await client.get_user_language()
        assert lang4 is None


class TestExpectState:
    async def test_expect_state_ws_success_and_polling_fallback(self):
        client = HARestClient()

        # --- WebSocket success path ---
        ws_mock = MagicMock()
        ws_mock.is_connected.return_value = True
        future = asyncio.Future()
        future.set_result("on")
        ws_mock.register_state_waiter.return_value = future
        ws_mock.cancel_state_waiter = MagicMock()
        client._state_observer = ws_mock

        async with client.expect_state("light.kitchen", expected="on", timeout=0.1) as result:
            pass

        assert result["new_state"] == "on"
        ws_mock.register_state_waiter.assert_called_once_with("light.kitchen", expected="on")
        ws_mock.cancel_state_waiter.assert_not_called()

        # --- WebSocket timeout falls back to polling ---
        ws_mock2 = MagicMock()
        ws_mock2.is_connected.return_value = True
        ws_mock2.register_state_waiter.return_value = asyncio.Future()
        ws_mock2.cancel_state_waiter = MagicMock()
        client._state_observer = ws_mock2
        client.get_state = AsyncMock(return_value={"state": "off"})

        with patch("app.ha_client.rest.asyncio.wait_for", side_effect=TimeoutError):
            async with client.expect_state(
                "light.kitchen", expected="on", timeout=0.01, poll_interval=0.01, poll_max=0.02
            ) as result2:
                pass

        assert result2["new_state"] == "off"
        ws_mock2.cancel_state_waiter.assert_called_once()
        client.get_state.assert_awaited()

        # --- No WS observer: pure polling fallback ---
        client._state_observer = None
        client.get_state = AsyncMock(return_value={"state": "on"})

        async with client.expect_state(
            "light.kitchen", expected="on", timeout=0.1, poll_interval=0.01, poll_max=0.02
        ) as result3:
            pass

        assert result3["new_state"] == "on"
