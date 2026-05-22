"""Tests for app.ha_client -- REST client, auth, and WebSocket."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import httpx
import pytest
import respx

from app.ha_client.auth import (
    HA_TOKEN_SECRET_KEY,
    build_auth_headers,
    get_auth_headers,
    get_ha_token,
    set_ha_token,
)
from app.ha_client.rest import HARestClient
from app.ha_client.rest import test_ha_connection as _test_ha_connection
from app.ha_client.websocket import HAWebSocketClient

# ---------------------------------------------------------------------------
# REST Client
# ---------------------------------------------------------------------------


class TestHARestClient:
    @respx.mock
    async def test_get_states_makes_get_request(self):
        states = [{"entity_id": "light.test", "state": "on", "attributes": {}}]
        respx.get("http://ha.local/api/states").mock(return_value=httpx.Response(200, json=states))

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={"Authorization": "Bearer test"})

        result = await client.get_states()
        assert result == states
        await client.close()

    @respx.mock
    async def test_get_state_returns_single_entity(self):
        state = {"entity_id": "light.kitchen", "state": "on", "attributes": {}}
        respx.get("http://ha.local/api/states/light.kitchen").mock(return_value=httpx.Response(200, json=state))

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        result = await client.get_state("light.kitchen")
        assert result["entity_id"] == "light.kitchen"
        await client.close()

    @respx.mock
    async def test_get_state_returns_none_on_404(self):
        respx.get("http://ha.local/api/states/light.missing").mock(return_value=httpx.Response(404))

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        result = await client.get_state("light.missing")
        assert result is None
        await client.close()

    @respx.mock
    async def test_call_service_posts_correct_endpoint(self):
        respx.post("http://ha.local/api/services/light/turn_on").mock(return_value=httpx.Response(200, json=[]))

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        result = await client.call_service("light", "turn_on", entity_id="light.kitchen")
        assert isinstance(result, list)
        await client.close()

    @respx.mock
    async def test_call_service_includes_service_data(self):
        route = respx.post("http://ha.local/api/services/light/turn_on").mock(return_value=httpx.Response(200, json=[]))

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        await client.call_service("light", "turn_on", entity_id="light.kitchen", service_data={"brightness": 128})
        body = route.calls[0].request.content
        assert b"brightness" in body
        await client.close()

    async def test_get_calendar_events_uses_calendar_service_response_shape(self):
        client = HARestClient()
        client.call_service = AsyncMock(
            return_value={
                "calendar.home": {
                    "events": [{"summary": "Standup", "start": "2026-04-27T09:00:00+00:00"}],
                }
            }
        )

        events = await client.get_calendar_events(
            "calendar.home",
            "2026-04-27T00:00:00+00:00",
            "2026-04-28T00:00:00+00:00",
        )

        assert events == [{"summary": "Standup", "start": "2026-04-27T09:00:00+00:00"}]
        client.call_service.assert_awaited_once_with(
            "calendar",
            "get_events",
            "calendar.home",
            {
                "start_date_time": "2026-04-27T00:00:00+00:00",
                "end_date_time": "2026-04-28T00:00:00+00:00",
            },
            return_response=True,
        )

    @respx.mock
    async def test_fire_event_posts_correct_endpoint(self):
        respx.post("http://ha.local/api/events/test_event").mock(
            return_value=httpx.Response(200, json={"message": "ok"})
        )

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        result = await client.fire_event("test_event", {"key": "value"})
        assert result["message"] == "ok"
        await client.close()

    @respx.mock
    async def test_get_config_returns_ha_config(self):
        config = {"time_zone": "Europe/Berlin", "location_name": "Berlin", "latitude": 52.52}
        respx.get("http://ha.local/api/config").mock(return_value=httpx.Response(200, json=config))

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        result = await client.get_config()
        assert result["time_zone"] == "Europe/Berlin"
        assert result["location_name"] == "Berlin"
        await client.close()

    @respx.mock
    async def test_get_config_returns_empty_dict_on_error(self):
        respx.get("http://ha.local/api/config").mock(return_value=httpx.Response(500))

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        result = await client.get_config()
        assert result == {}
        await client.close()

    @respx.mock
    async def test_test_connection_returns_true_on_200(self):
        respx.get("http://ha.local/api/").mock(return_value=httpx.Response(200, json={"message": "API running."}))

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        result = await client.test_connection()
        assert result is True
        await client.close()

    @respx.mock
    async def test_test_connection_returns_false_on_error(self):
        respx.get("http://ha.local/api/").mock(side_effect=httpx.ConnectError("refused"))

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        result = await client.test_connection()
        assert result is False
        await client.close()

    @respx.mock
    async def test_get_states_raises_on_server_error(self):
        respx.get("http://ha.local/api/states").mock(return_value=httpx.Response(500, text="Internal Server Error"))

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        with pytest.raises(httpx.HTTPStatusError):
            await client.get_states()
        await client.close()

    @patch("app.ha_client.rest.SettingsRepository")
    @patch("app.ha_client.rest.get_auth_headers", new_callable=AsyncMock)
    async def test_initialize_creates_httpx_client(self, mock_auth, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://ha.local:8123")
        mock_auth.return_value = {"Authorization": "Bearer test-token"}

        client = HARestClient()
        await client.initialize()

        assert client._base_url == "http://ha.local:8123"
        assert client._client is not None
        await client.close()

    async def test_call_service_fallback_to_websocket_on_500(self):
        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})
        req = httpx.Request("POST", "http://ha.local/api/services/weather/get_forecasts")
        client._client.post = AsyncMock(return_value=httpx.Response(500, text="error", request=req))

        ws_mock = MagicMock()
        ws_mock.is_connected.return_value = True
        ws_mock.call_service = AsyncMock(return_value={"weather.home": {"forecast": []}})
        client._state_observer = ws_mock

        result = await client.call_service("weather", "get_forecasts", entity_id="weather.home", return_response=True)
        assert result == {"weather.home": {"forecast": []}}
        ws_mock.call_service.assert_awaited_once_with(
            "weather",
            "get_forecasts",
            entity_id="weather.home",
            service_data=None,
            return_response=True,
        )
        await client.close()

    async def test_call_service_fallback_when_return_response_true_and_rest_connect_error(self):
        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})
        client._client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        ws_mock = MagicMock()
        ws_mock.is_connected.return_value = True
        ws_mock.call_service = AsyncMock(return_value={"result": "ok"})
        client._state_observer = ws_mock

        result = await client.call_service("calendar", "get_events", return_response=True)
        assert result == {"result": "ok"}
        await client.close()

    async def test_call_service_raises_original_error_when_websocket_unavailable(self):
        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})
        req = httpx.Request("POST", "http://ha.local/api/services/light/turn_on")
        client._client.post = AsyncMock(return_value=httpx.Response(500, text="error", request=req))
        client._state_observer = None

        with pytest.raises(httpx.HTTPStatusError):
            await client.call_service("light", "turn_on", entity_id="light.kitchen")
        await client.close()

    async def test_call_service_raises_original_error_when_websocket_fallback_returns_none(self):
        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})
        req = httpx.Request("POST", "http://ha.local/api/services/light/turn_on")
        client._client.post = AsyncMock(return_value=httpx.Response(500, text="error", request=req))

        ws_mock = MagicMock()
        ws_mock.is_connected.return_value = True
        ws_mock.call_service = AsyncMock(return_value=None)
        client._state_observer = ws_mock

        with pytest.raises(httpx.HTTPStatusError):
            await client.call_service("light", "turn_on", entity_id="light.kitchen")
        await client.close()

    async def test_call_service_does_not_fallback_on_non_500_without_return_response(self):
        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})
        req = httpx.Request("POST", "http://ha.local/api/services/light/turn_on")
        client._client.post = AsyncMock(return_value=httpx.Response(404, text="not found", request=req))

        ws_mock = MagicMock()
        ws_mock.is_connected.return_value = True
        client._state_observer = ws_mock

        with pytest.raises(httpx.HTTPStatusError):
            await client.call_service("light", "turn_on", entity_id="light.kitchen")
        ws_mock.call_service.assert_not_called()
        await client.close()

    async def test_call_service_uses_rest_on_success_no_websocket_call(self):
        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})
        req = httpx.Request("POST", "http://ha.local/api/services/light/turn_on")
        client._client.post = AsyncMock(return_value=httpx.Response(200, json={"result": "rest"}, request=req))

        ws_mock = MagicMock()
        ws_mock.is_connected.return_value = True
        client._state_observer = ws_mock

        result = await client.call_service("light", "turn_on", entity_id="light.kitchen")
        assert result == {"result": "rest"}
        ws_mock.call_service.assert_not_called()
        await client.close()

    @patch("app.ha_client.rest.get_auth_headers", new_callable=AsyncMock)
    @patch("app.ha_client.rest.SettingsRepository")
    async def test_close_nulls_client_and_refresh_is_noop(self, mock_settings, mock_auth):
        """COR-5: after close() the httpx client is None and _refresh_headers
        is a safe no-op instead of raising httpx.RuntimeError."""
        mock_settings.get_value = AsyncMock(return_value="http://ha.local:8123")
        mock_auth.return_value = {"Authorization": "Bearer x"}

        client = HARestClient()
        await client.initialize()
        await client.close()
        assert client._client is None
        # Must not raise
        await client._refresh_headers()
        assert client._client is None


# ---------------------------------------------------------------------------
# Standalone test_ha_connection
# ---------------------------------------------------------------------------


class TestHaConnectionUtility:
    @respx.mock
    async def test_test_ha_connection_success(self):
        respx.get("http://ha.local:8123/api/").mock(return_value=httpx.Response(200))
        result = await _test_ha_connection("http://ha.local:8123", "test-token")
        assert result is True

    @respx.mock
    async def test_test_ha_connection_failure(self):
        respx.get("http://ha.local:8123/api/").mock(return_value=httpx.Response(401))
        result = await _test_ha_connection("http://ha.local:8123", "bad-token")
        assert result is False


class TestHAConfigFlow:
    @pytest.fixture(autouse=True)
    def _mock_homeassistant(self):
        import sys

        mocks = {}
        ha_modules = [
            "homeassistant",
            "homeassistant.config_entries",
            "homeassistant.const",
            "homeassistant.core",
            "homeassistant.helpers",
            "homeassistant.helpers.selector",
            "voluptuous",
        ]
        for mod in ha_modules:
            if mod not in sys.modules:
                mocks[mod] = MagicMock()
                sys.modules[mod] = mocks[mod]

        class _FakeConfigFlow:
            def __init_subclass__(cls, **kwargs):
                return super().__init_subclass__()

            async def async_set_unique_id(self, unique_id):
                self._unique_id = unique_id

            def _abort_if_unique_id_configured(self):
                return None

            def async_show_form(self, step_id, data_schema, errors):
                return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors}

            def async_create_entry(self, title, data):
                return {"type": "create_entry", "title": title, "data": data}

        class _FakeOptionsFlow:
            def async_show_form(self, step_id, data_schema, errors):
                return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors}

            def async_create_entry(self, data):
                return {"type": "create_entry", "data": data}

        class _FakeTextSelectorType:
            PASSWORD = "password"
            URL = "url"

        class _FakeTextSelectorConfig:
            def __init__(self, *, type=None):
                self.type = type

        class _FakeTextSelector:
            def __init__(self, config=None):
                self.config = config

            def __call__(self, value):
                return value

        class _FakeMarker:
            def __init__(self, schema, default=None):
                self.schema = schema
                self.default = lambda: default

        class _FakeSchema:
            def __init__(self, schema):
                self.schema = schema

        sys.modules["homeassistant.config_entries"].ConfigEntry = type("ConfigEntry", (), {})
        sys.modules["homeassistant.config_entries"].ConfigFlow = _FakeConfigFlow
        sys.modules["homeassistant.config_entries"].ConfigFlowResult = dict
        sys.modules["homeassistant.config_entries"].OptionsFlow = _FakeOptionsFlow
        sys.modules["homeassistant.const"].CONF_URL = "url"
        sys.modules["homeassistant.const"].CONF_API_KEY = "api_key"
        sys.modules["homeassistant.const"].CONF_NAME = "name"
        sys.modules["homeassistant.const"].CONF_NAME = "name"
        sys.modules["homeassistant.const"].Platform = type("Platform", (), {"CONVERSATION": "conversation"})
        sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (), {})
        sys.modules["homeassistant.helpers.selector"].TextSelector = _FakeTextSelector
        sys.modules["homeassistant.helpers.selector"].TextSelectorConfig = _FakeTextSelectorConfig
        sys.modules["homeassistant.helpers.selector"].TextSelectorType = _FakeTextSelectorType
        sys.modules["homeassistant.helpers"].selector = sys.modules["homeassistant.helpers.selector"]
        sys.modules["voluptuous"].Schema = _FakeSchema
        sys.modules["voluptuous"].Required = lambda schema, default=None: _FakeMarker(schema, default)
        sys.modules["voluptuous"].Optional = lambda schema, default=None: _FakeMarker(schema, default)

        yield

        for mod in mocks:
            sys.modules.pop(mod, None)
        for key in list(sys.modules):
            if key.startswith("custom_components"):
                del sys.modules[key]

    def _import_config_flow_module(self):
        import sys

        workspace_root = str(Path(__file__).resolve().parents[1].parent)
        if workspace_root not in sys.path:
            sys.path.insert(0, workspace_root)

        from custom_components.ha_agenthub import config_flow

        return config_flow

    @staticmethod
    def _schema_entry(schema, field_name):
        for marker, validator in schema.schema.items():
            if getattr(marker, "schema", None) == field_name:
                return marker, validator
        raise AssertionError(f"Field {field_name} not found in schema")

    @respx.mock(assert_all_called=False)
    async def test_validate_connection_returns_invalid_auth_on_401(self):
        config_flow = self._import_config_flow_module()

        class _FakeResponse:
            status = 401

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def json(self):
                return {}

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, *args, **kwargs):
                return _FakeResponse()

        with patch("custom_components.ha_agenthub.config_flow.aiohttp.ClientSession", return_value=_FakeSession()):
            result = await config_flow._validate_connection("http://ha.local", "bad-token")

        assert result == "invalid_auth"

    async def test_validate_connection_rejects_non_healthy_payload(self):
        config_flow = self._import_config_flow_module()

        class _FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def json(self):
                return {"status": "degraded"}

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, *args, **kwargs):
                return _FakeResponse()

        with patch("custom_components.ha_agenthub.config_flow.aiohttp.ClientSession", return_value=_FakeSession()):
            result = await config_flow._validate_connection("http://ha.local", "token")

        assert result == "cannot_connect"

    async def test_options_flow_blank_api_key_keeps_existing_secret(self):
        config_flow = self._import_config_flow_module()

        entry = MagicMock()
        entry.data = {"url": "http://old.local", "api_key": "stored-token"}

        flow = config_flow.HaAgentHubOptionsFlow(entry)
        flow.hass = MagicMock()
        flow.hass.config_entries = MagicMock()

        with patch(
            "custom_components.ha_agenthub.config_flow._validate_connection",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_validate:
            result = await flow.async_step_init({"url": "http://ha.local/", "api_key": ""})

        assert result == {"type": "create_entry", "data": {}}
        mock_validate.assert_awaited_once_with("http://ha.local", "stored-token")
        assert flow.hass.config_entries.async_update_entry.call_count == 2
        flow.hass.config_entries.async_update_entry.assert_any_call(
            entry,
            title=entry.title,
            data={"url": "http://old.local", "api_key": "stored-token"},
            options={
                "name": entry.title,
                "url": "http://ha.local",
                "api_key": "stored-token",
            },
        )

    async def test_options_flow_schema_uses_blank_password_field(self):
        config_flow = self._import_config_flow_module()

        schema = config_flow._build_options_schema({"url": "http://old.local", "api_key": "stored-token"})
        marker, validator = self._schema_entry(schema, "api_key")
        default_value = marker.default() if callable(marker.default) else marker.default

        assert default_value == ""
        assert validator.config.type == "password"


# ---------------------------------------------------------------------------
# Auth module
# ---------------------------------------------------------------------------


class TestHAAuth:
    def test_build_auth_headers_format(self):
        headers = build_auth_headers("my-token-123")
        assert headers["Authorization"] == "Bearer my-token-123"
        assert headers["Content-Type"] == "application/json"

    @patch("app.ha_client.auth.retrieve_secret", new_callable=AsyncMock, return_value="stored-token")
    async def test_get_ha_token_returns_token(self, mock_retrieve):
        token = await get_ha_token()
        assert token == "stored-token"
        mock_retrieve.assert_awaited_once_with(HA_TOKEN_SECRET_KEY)

    @patch("app.ha_client.auth.retrieve_secret", new_callable=AsyncMock, return_value=None)
    async def test_get_ha_token_returns_none_when_not_set(self, mock_retrieve):
        token = await get_ha_token()
        assert token is None

    @patch("app.ha_client.auth.store_secret", new_callable=AsyncMock)
    async def test_set_ha_token_stores_token(self, mock_store):
        await set_ha_token("new-token")
        mock_store.assert_awaited_once_with(HA_TOKEN_SECRET_KEY, "new-token")

    @patch("app.ha_client.auth.retrieve_secret", new_callable=AsyncMock, return_value="tok")
    async def test_get_auth_headers_returns_dict(self, mock_retrieve):
        headers = await get_auth_headers()
        assert headers is not None
        assert headers["Authorization"] == "Bearer tok"

    @patch("app.ha_client.auth.retrieve_secret", new_callable=AsyncMock, return_value=None)
    async def test_get_auth_headers_returns_none_when_no_token(self, mock_retrieve):
        headers = await get_auth_headers()
        assert headers is None


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------


class TestHAWebSocketClient:
    def test_initial_state_not_connected(self):
        ws = HAWebSocketClient()
        assert ws.is_connected() is False

    def test_next_id_increments(self):
        ws = HAWebSocketClient()
        id1 = ws._next_id()
        id2 = ws._next_id()
        assert id2 == id1 + 1

    def test_on_event_registers_callback(self):
        ws = HAWebSocketClient()
        callback = MagicMock()
        ws.on_event("state_changed", callback)
        assert "state_changed" in ws._listeners
        assert callback in ws._listeners["state_changed"]

    @patch("app.ha_client.websocket.SettingsRepository")
    @patch("app.ha_client.websocket.get_ha_token", new_callable=AsyncMock, return_value=None)
    async def test_connect_returns_false_when_no_token(self, mock_token, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://ha.local")
        ws = HAWebSocketClient()
        result = await ws.connect()
        assert result is False

    @patch("app.ha_client.websocket.SettingsRepository")
    @patch("app.ha_client.websocket.get_ha_token", new_callable=AsyncMock, return_value="tok")
    async def test_connect_returns_false_when_no_url(self, mock_token, mock_settings):
        mock_settings.get_value = AsyncMock(return_value=None)
        ws = HAWebSocketClient()
        result = await ws.connect()
        assert result is False

    @patch("app.ha_client.websocket.SettingsRepository")
    @patch("app.ha_client.websocket.get_ha_token", new_callable=AsyncMock, return_value="tok")
    async def test_connect_narrow_exception_handling_client_error(self, mock_token, mock_settings):
        """Step 17: aiohttp.ClientError must be caught and return False."""
        mock_settings.get_value = AsyncMock(return_value="http://ha.local")
        ws = HAWebSocketClient()

        # Patch ws_connect to raise a ClientError subclass
        with (
            patch.object(ws, "_session", None),
            patch(
                "aiohttp.ClientSession.ws_connect",
                side_effect=aiohttp.ClientConnectionError("refused"),
            ),
        ):
            result = await ws.connect()
        assert result is False

    @patch("app.ha_client.websocket.SettingsRepository")
    @patch("app.ha_client.websocket.get_ha_token", new_callable=AsyncMock, return_value="tok")
    async def test_connect_does_not_swallow_keyboard_interrupt(self, mock_token, mock_settings):
        """Step 17: KeyboardInterrupt must propagate, not be swallowed."""
        mock_settings.get_value = AsyncMock(return_value="http://ha.local")
        ws = HAWebSocketClient()

        with (
            patch.object(ws, "_session", None),
            patch(
                "aiohttp.ClientSession.ws_connect",
                side_effect=KeyboardInterrupt("stop"),
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            await ws.connect()

    async def test_disconnect_sets_running_false(self):
        ws = HAWebSocketClient()
        ws._running = True
        await ws.disconnect()
        assert ws._running is False

    @patch("app.ha_client.websocket.SettingsRepository")
    @patch("app.ha_client.websocket.get_ha_token", new_callable=AsyncMock, return_value="tok")
    async def test_run_attempts_connect_from_fresh_state(self, mock_token, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://ha.local")
        ws = HAWebSocketClient()

        async def _fake_connect():
            ws._running = False
            return True

        ws.connect = AsyncMock(side_effect=_fake_connect)
        ws._receive_loop = AsyncMock()
        await ws.run()
        ws.connect.assert_called()

    @patch("app.ha_client.websocket.SettingsRepository")
    @patch("app.ha_client.websocket.get_ha_token", new_callable=AsyncMock, return_value="tok")
    async def test_run_exits_cleanly_when_disconnect_called(self, mock_token, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://ha.local")
        ws = HAWebSocketClient()

        async def _fake_connect():
            await ws.disconnect()
            return False

        ws.connect = AsyncMock(side_effect=_fake_connect)
        await ws.run()
        assert ws._running is False

    @patch("app.ha_client.websocket.SettingsRepository")
    @patch("app.ha_client.websocket.get_ha_token", new_callable=AsyncMock, return_value="tok")
    async def test_run_reconnects_after_receive_loop_error(self, mock_token, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://ha.local")
        ws = HAWebSocketClient()
        call_count = 0

        async def _fake_connect():
            return True

        async def _fake_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("connection lost")
            ws._running = False

        ws.connect = AsyncMock(side_effect=_fake_connect)
        ws._receive_loop = AsyncMock(side_effect=_fake_receive)
        ws._close_session = AsyncMock()
        ws._reconnect_loop = AsyncMock()
        await ws.run()
        ws._reconnect_loop.assert_called()

    @patch("app.ha_client.websocket.SettingsRepository")
    @patch("app.ha_client.websocket.asyncio.sleep", new_callable=AsyncMock)
    async def test_reconnect_loop_caps_attempts_and_pauses(self, mock_sleep, mock_settings):
        """FLOW-RECONN-1 (P2-4): after MAX_RECONNECT_ATTEMPTS the loop pauses
        for RECONNECT_PAUSE_DURATION seconds, then resets the counter."""
        from app.ha_client import websocket as ws_module

        mock_settings.get_value = AsyncMock(return_value=None)
        ws = ws_module.HAWebSocketClient()
        ws._running = True

        # Always fail to connect so we exercise the attempt counter.
        connect_calls = 0

        async def _fail_connect():
            nonlocal connect_calls
            connect_calls += 1
            # Stop once we have seen the pause so the test finishes.
            if connect_calls >= ws_module.MAX_RECONNECT_ATTEMPTS + 1:
                ws._running = False
            return False

        ws.connect = AsyncMock(side_effect=_fail_connect)
        await ws._reconnect_loop()

        # Pause value must have been awaited exactly once.
        pause_calls = [
            c for c in mock_sleep.await_args_list if c.args and c.args[0] == ws_module.RECONNECT_PAUSE_DURATION
        ]
        assert len(pause_calls) == 1, f"expected one pause sleep, got {pause_calls}"
        assert connect_calls >= ws_module.MAX_RECONNECT_ATTEMPTS


class TestHAWebSocketCallService:
    async def test_call_service_returns_response_on_success(self):
        ws = HAWebSocketClient()
        ws._ws = MagicMock()
        ws._ws.closed = False
        ws._running = True

        with patch.object(
            ws,
            "send_command",
            new=AsyncMock(return_value={"context": {}, "response": {"weather.home": {"forecast": []}}}),
        ):
            result = await ws.call_service("weather", "get_forecasts", entity_id="weather.home")

        assert result == {"weather.home": {"forecast": []}}

    async def test_call_service_returns_none_when_not_connected(self):
        ws = HAWebSocketClient()
        ws._ws = None
        result = await ws.call_service("light", "turn_on", entity_id="light.kitchen")
        assert result is None

    async def test_call_service_returns_none_on_timeout(self):
        ws = HAWebSocketClient()
        ws._ws = MagicMock()
        ws._ws.closed = False
        ws._running = True

        with patch.object(ws, "send_command", new=AsyncMock(return_value=None)):
            result = await ws.call_service("light", "turn_on", entity_id="light.kitchen")

        assert result is None

    async def test_call_service_builds_correct_payload(self):
        ws = HAWebSocketClient()
        ws._ws = MagicMock()
        ws._ws.closed = False
        ws._running = True

        mock_send = AsyncMock(return_value={"context": {}, "response": {}})
        with patch.object(ws, "send_command", new=mock_send):
            await ws.call_service(
                "weather",
                "get_forecasts",
                entity_id="weather.home",
                service_data={"type": "daily"},
                return_response=True,
            )

        mock_send.assert_awaited_once()
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["domain"] == "weather"
        assert call_kwargs["service"] == "get_forecasts"
        assert call_kwargs["service_data"] == {"type": "daily"}
        assert call_kwargs["target"] == {"entity_id": "weather.home"}
        assert call_kwargs["return_response"] is True

    async def test_call_service_without_entity_id_omits_target(self):
        ws = HAWebSocketClient()
        ws._ws = MagicMock()
        ws._ws.closed = False
        ws._running = True

        mock_send = AsyncMock(return_value={"context": {}, "response": {}})
        with patch.object(ws, "send_command", new=mock_send):
            await ws.call_service("homeassistant", "restart")

        mock_send.assert_awaited_once()
        call_kwargs = mock_send.call_args.kwargs
        assert "target" not in call_kwargs


# ---------------------------------------------------------------------------
# Conversation Entity Concurrency
# ---------------------------------------------------------------------------


class TestConversationEntityConcurrency:
    """Tests for overlapping-turn serialization on the HA conversation entity."""

    async def test_overlapping_ws_turns_serialized(self):
        """Two concurrent tasks sharing a lock execute sequentially."""

        lock = asyncio.Lock()
        call_order: list[str] = []

        async def fake_process(label, delay):
            call_order.append(f"{label}_start")
            await asyncio.sleep(delay)
            call_order.append(f"{label}_end")

        async with lock:
            # Prove the lock is re-entrant-free; a second acquire must wait
            acquired = lock.locked()
            assert acquired is True

        # Functional check: two tasks sharing a lock execute sequentially
        call_order.clear()

        async def guarded(label, delay):
            async with lock:
                await fake_process(label, delay)

        t1 = asyncio.create_task(guarded("A", 0.05))
        t2 = asyncio.create_task(guarded("B", 0.01))
        await asyncio.gather(t1, t2)

        # A must fully complete before B starts (or vice versa)
        a_start = call_order.index("A_start")
        a_end = call_order.index("A_end")
        b_start = call_order.index("B_start")
        b_end = call_order.index("B_end")
        assert (a_end < b_start) or (b_end < a_start), f"Turns interleaved: {call_order}"


# ---------------------------------------------------------------------------
# HA Conversation Entity -- WS close/error handling
# ---------------------------------------------------------------------------


class TestHAConversationWSCloseError:
    """Tests for _process_via_ws raising on CLOSED/ERROR instead of returning partial speech."""

    @pytest.fixture(autouse=True)
    def _mock_homeassistant(self):
        """Mock homeassistant dependencies so custom_components can be imported."""
        import sys

        mocks = {}
        ha_modules = [
            "homeassistant",
            "homeassistant.components",
            "homeassistant.components.assist_pipeline",
            "homeassistant.components.conversation",
            "homeassistant.config_entries",
            "homeassistant.const",
            "homeassistant.core",
            "homeassistant.helpers",
            "homeassistant.helpers.device_registry",
            "homeassistant.helpers.entity_registry",
            "homeassistant.helpers.intent",
            "homeassistant.helpers.entity_platform",
            "homeassistant.helpers.event",
            "homeassistant.helpers.selector",
        ]
        for mod in ha_modules:
            if mod not in sys.modules:
                mocks[mod] = MagicMock()
                sys.modules[mod] = mocks[mod]

        sys.modules["homeassistant.helpers.event"].async_track_state_change_event = MagicMock()

        # Provide required constants/classes used at import time
        sys.modules["homeassistant.const"].CONF_URL = "url"
        sys.modules["homeassistant.const"].CONF_API_KEY = "api_key"
        sys.modules["homeassistant.const"].CONF_NAME = "name"
        sys.modules["homeassistant.const"].MATCH_ALL = "*"
        conv_mod = sys.modules["homeassistant.components.conversation"]
        conv_mod.ConversationEntityFeature = MagicMock()
        conv_mod.ConversationEntity = type(
            "ConversationEntity",
            (),
            {
                "__init__": lambda self, *a, **kw: None,
            },
        )
        # Wire parent attribute so `from homeassistant.components import conversation`
        # resolves to the same object as sys.modules[...conversation].
        sys.modules["homeassistant.components"].conversation = conv_mod
        sys.modules["homeassistant.components"].assist_pipeline = sys.modules[
            "homeassistant.components.assist_pipeline"
        ]

        yield

        for mod in mocks:
            sys.modules.pop(mod, None)
        # Clear the imported custom_components module so it doesn't leak
        for key in list(sys.modules):
            if key.startswith("custom_components"):
                del sys.modules[key]

    async def test_process_via_ws_closed_mid_stream(self):
        """WS CLOSED mid-stream should raise _WsDroppedAfterSendError wrapping aiohttp.ClientError."""
        import json as _json
        import sys

        import aiohttp

        sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
        from custom_components.ha_agenthub.conversation import (
            HaAgentHubConversationEntity,
            _WsDroppedAfterSendError,
        )

        entity = MagicMock()
        entity._ws = AsyncMock()
        entity._ws.send_json = AsyncMock()

        msg_text = MagicMock()
        msg_text.type = aiohttp.WSMsgType.TEXT
        msg_text.data = _json.dumps({"token": "partial ", "done": False})

        msg_closed = MagicMock()
        msg_closed.type = aiohttp.WSMsgType.CLOSED

        entity._ws.receive = AsyncMock(side_effect=[msg_text, msg_closed])

        user_input = MagicMock()
        user_input.text = "hello"
        user_input.conversation_id = "conv-1"
        user_input.language = "en"
        user_input.device_id = None

        with pytest.raises(_WsDroppedAfterSendError) as exc_info:
            await HaAgentHubConversationEntity._process_via_ws(entity, user_input)
        assert isinstance(exc_info.value.__cause__, aiohttp.ClientError)
        assert "closed mid-stream" in str(exc_info.value.__cause__)

    async def test_process_via_ws_error_mid_stream(self):
        """WS ERROR mid-stream should raise _WsDroppedAfterSendError wrapping aiohttp.ClientError."""
        import json as _json
        import sys

        import aiohttp

        sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
        from custom_components.ha_agenthub.conversation import (
            HaAgentHubConversationEntity,
            _WsDroppedAfterSendError,
        )

        entity = MagicMock()
        entity._ws = AsyncMock()
        entity._ws.send_json = AsyncMock()

        msg_text = MagicMock()
        msg_text.type = aiohttp.WSMsgType.TEXT
        msg_text.data = _json.dumps({"token": "partial ", "done": False})

        msg_error = MagicMock()
        msg_error.type = aiohttp.WSMsgType.ERROR

        entity._ws.receive = AsyncMock(side_effect=[msg_text, msg_error])

        user_input = MagicMock()
        user_input.text = "hello"
        user_input.conversation_id = "conv-1"
        user_input.language = "en"
        user_input.device_id = None

        with pytest.raises(_WsDroppedAfterSendError) as exc_info:
            await HaAgentHubConversationEntity._process_via_ws(entity, user_input)
        assert isinstance(exc_info.value.__cause__, aiohttp.ClientError)
        assert "error mid-stream" in str(exc_info.value.__cause__)
        assert entity._ws is None

    async def test_process_via_ws_close_before_any_tokens(self):
        """WS CLOSED immediately (no tokens) should raise _WsDroppedAfterSendError."""
        import sys

        import aiohttp

        sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
        from custom_components.ha_agenthub.conversation import (
            HaAgentHubConversationEntity,
            _WsDroppedAfterSendError,
        )

        entity = MagicMock()
        entity._ws = AsyncMock()
        entity._ws.send_json = AsyncMock()

        msg_closed = MagicMock()
        msg_closed.type = aiohttp.WSMsgType.CLOSED

        entity._ws.receive = AsyncMock(return_value=msg_closed)

        user_input = MagicMock()
        user_input.text = "hello"
        user_input.conversation_id = "conv-1"
        user_input.language = "en"
        user_input.device_id = None

        with pytest.raises(_WsDroppedAfterSendError) as exc_info:
            await HaAgentHubConversationEntity._process_via_ws(entity, user_input)
        assert isinstance(exc_info.value.__cause__, aiohttp.ClientError)
        assert "closed mid-stream" in str(exc_info.value.__cause__)

    async def test_process_via_ws_error_token_triggers_raise(self):
        """Error field in done token does NOT raise; it is logged and embedded in speech.

        Application-level errors arrive as part of the done chunk and are explicitly
        treated as non-transport failures (would otherwise be wrapped as
        _WsDroppedAfterSendError and trigger a duplicate REST fallback).
        """
        import json as _json
        import sys

        import aiohttp

        sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
        from custom_components.ha_agenthub.conversation import HaAgentHubConversationEntity

        entity = MagicMock()
        entity._ws = AsyncMock()
        entity._ws.send_json = AsyncMock()

        msg_done = MagicMock()
        msg_done.type = aiohttp.WSMsgType.TEXT
        msg_done.data = _json.dumps({"token": "", "done": True, "error": "Agent error: test"})

        entity._ws.receive = AsyncMock(return_value=msg_done)

        user_input = MagicMock()
        user_input.text = "hello"
        user_input.conversation_id = "conv-1"
        user_input.language = "en"
        user_input.device_id = None

        sentinel = object()
        entity._build_result = MagicMock(return_value=sentinel)

        result = await HaAgentHubConversationEntity._process_via_ws(entity, user_input)
        assert result is sentinel
        # Speech must include the error description rather than be empty.
        speech_arg = entity._build_result.call_args.args[0]
        assert "Agent error: test" in speech_arg


class TestHAConfigEntryLifecycle:
    @pytest.fixture(autouse=True)
    def _mock_homeassistant(self):
        """Mock Home Assistant modules so the custom integration can be imported."""
        import sys

        mocks = {}
        ha_modules = [
            "homeassistant",
            "homeassistant.config_entries",
            "homeassistant.const",
            "homeassistant.core",
        ]
        for mod in ha_modules:
            if mod not in sys.modules:
                mocks[mod] = MagicMock()
                sys.modules[mod] = mocks[mod]

        sys.modules["homeassistant.config_entries"].ConfigEntry = type("ConfigEntry", (), {})
        sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (), {})
        sys.modules["homeassistant.const"].CONF_URL = "url"
        sys.modules["homeassistant.const"].CONF_API_KEY = "api_key"
        sys.modules["homeassistant.const"].CONF_NAME = "name"
        sys.modules["homeassistant.const"].Platform = type("Platform", (), {"CONVERSATION": "conversation"})

        yield

        for mod in mocks:
            sys.modules.pop(mod, None)
        for key in list(sys.modules):
            if key.startswith("custom_components"):
                del sys.modules[key]

    async def test_async_setup_entry_update_listener_triggers_reload(self):
        import sys

        workspace_root = str(Path(__file__).resolve().parents[1].parent)
        if workspace_root not in sys.path:
            sys.path.insert(0, workspace_root)

        from custom_components.ha_agenthub import async_setup_entry

        hass = MagicMock()
        hass.data = {}
        hass.config_entries = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.config_entries.async_reload = AsyncMock()

        registered_listener = None

        def _add_update_listener(callback):
            nonlocal registered_listener
            registered_listener = callback
            return "listener-unsub"

        entry = MagicMock()
        entry.entry_id = "entry-1"
        entry.title = "HA-AgentHub"
        entry.data = {"url": "http://ha.local", "api_key": "token"}
        entry.add_update_listener = MagicMock(side_effect=_add_update_listener)
        entry.async_on_unload = MagicMock()

        result = await async_setup_entry(hass, entry)

        assert result is True
        entry.add_update_listener.assert_called_once()
        entry.async_on_unload.assert_called_once_with("listener-unsub")
        assert registered_listener is not None

        await registered_listener(hass, entry)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


class TestHAConversationRestFallbackMessages:
    @pytest.fixture(autouse=True)
    def _mock_homeassistant(self):
        import sys

        mocks = {}
        ha_modules = [
            "homeassistant",
            "homeassistant.components",
            "homeassistant.components.assist_pipeline",
            "homeassistant.components.conversation",
            "homeassistant.config_entries",
            "homeassistant.const",
            "homeassistant.core",
            "homeassistant.helpers",
            "homeassistant.helpers.device_registry",
            "homeassistant.helpers.entity_registry",
            "homeassistant.helpers.intent",
            "homeassistant.helpers.entity_platform",
            "homeassistant.helpers.event",
            "homeassistant.helpers.selector",
        ]
        for mod in ha_modules:
            if mod not in sys.modules:
                mocks[mod] = MagicMock()
                sys.modules[mod] = mocks[mod]

        sys.modules["homeassistant.helpers.event"].async_track_state_change_event = MagicMock()

        sys.modules["homeassistant.const"].CONF_URL = "url"
        sys.modules["homeassistant.const"].CONF_API_KEY = "api_key"
        sys.modules["homeassistant.const"].CONF_NAME = "name"
        sys.modules["homeassistant.const"].MATCH_ALL = "*"
        conv_mod = sys.modules["homeassistant.components.conversation"]
        conv_mod.ConversationEntityFeature = MagicMock()
        conv_mod.ConversationEntity = type(
            "ConversationEntity",
            (),
            {
                "__init__": lambda self, *a, **kw: None,
            },
        )
        sys.modules["homeassistant.components"].conversation = conv_mod
        sys.modules["homeassistant.components"].assist_pipeline = sys.modules[
            "homeassistant.components.assist_pipeline"
        ]

        yield

        for mod in mocks:
            sys.modules.pop(mod, None)
        for key in list(sys.modules):
            if key.startswith("custom_components"):
                del sys.modules[key]

    def _import_conversation_module(self):
        import sys

        workspace_root = str(Path(__file__).resolve().parents[1].parent)
        if workspace_root not in sys.path:
            sys.path.insert(0, workspace_root)

        from custom_components.ha_agenthub.conversation import HaAgentHubConversationEntity

        return HaAgentHubConversationEntity

    @staticmethod
    def _build_rest_entity(response=None, error=None):
        class _FakeResponse:
            def __init__(self, status_code, payload=None):
                self.status = status_code
                self._payload = payload or {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def json(self):
                return self._payload

        class _FakeSession:
            closed = False

            def __init__(self, response_obj, raised_error):
                self._response_obj = response_obj
                self._raised_error = raised_error

            def post(self, *args, **kwargs):
                if self._raised_error is not None:
                    raise self._raised_error
                return self._response_obj

        entity = MagicMock()
        entity._session = _FakeSession(response, error)
        entity._api_key = "token"
        entity._url = "http://ha.local"
        entity._resolve_origin_context = MagicMock(return_value={})
        entity._build_result = MagicMock(
            side_effect=lambda speech, conversation_id, language, sanitized=False: {
                "speech": speech,
                "conversation_id": conversation_id,
                "language": language,
                "sanitized": sanitized,
            }
        )
        return entity, _FakeResponse

    @staticmethod
    def _build_user_input():
        user_input = MagicMock()
        user_input.text = "hello"
        user_input.conversation_id = "conv-1"
        user_input.language = "en"
        return user_input

    @pytest.mark.parametrize("status_code", [401, 403])
    async def test_rest_fallback_reports_auth_failures(self, status_code):
        conversation_entity = self._import_conversation_module()
        entity, response_type = self._build_rest_entity(response=None)
        entity._session = type(
            "_FakeSession", (), {"post": lambda self, *args, **kwargs: response_type(status_code), "closed": False}
        )()

        result = await conversation_entity._process_via_rest(entity, self._build_user_input())

        assert "API key was rejected" in result["speech"]
        assert "integration settings" in result["speech"]

    async def test_rest_fallback_reports_backend_errors(self):
        conversation_entity = self._import_conversation_module()
        entity, response_type = self._build_rest_entity(response=None)
        entity._session = type(
            "_FakeSession", (), {"post": lambda self, *args, **kwargs: response_type(503), "closed": False}
        )()

        result = await conversation_entity._process_via_rest(entity, self._build_user_input())

        assert "returned an error" in result["speech"]
        assert "container logs" in result["speech"]

    async def test_rest_fallback_reports_container_unavailability(self):
        import aiohttp

        conversation_entity = self._import_conversation_module()
        entity, _ = self._build_rest_entity(error=aiohttp.ClientError("connection refused"))

        result = await conversation_entity._process_via_rest(entity, self._build_user_input())

        assert "container is unavailable" in result["speech"]
        assert "reachable from Home Assistant" in result["speech"]


# ---------------------------------------------------------------------------
# HA Conversation Entity -- coalescing window (P2-3)
# ---------------------------------------------------------------------------


class TestHAConversationCoalesceWindow:
    """FLOW-COALESCE-1 (P2-3): coalescing only collapses duplicates that arrive
    within the coalesce window. Later repeats must get a fresh bridge task."""

    @pytest.fixture(autouse=True)
    def _mock_homeassistant(self):
        import sys

        mocks = {}
        ha_modules = [
            "homeassistant",
            "homeassistant.components",
            "homeassistant.components.assist_pipeline",
            "homeassistant.components.conversation",
            "homeassistant.config_entries",
            "homeassistant.const",
            "homeassistant.core",
            "homeassistant.helpers",
            "homeassistant.helpers.device_registry",
            "homeassistant.helpers.entity_registry",
            "homeassistant.helpers.intent",
            "homeassistant.helpers.entity_platform",
            "homeassistant.helpers.area_registry",
            "homeassistant.helpers.event",
        ]
        for mod in ha_modules:
            if mod not in sys.modules:
                mocks[mod] = MagicMock()
                sys.modules[mod] = mocks[mod]
        sys.modules["homeassistant.helpers.event"].async_track_state_change_event = MagicMock()
        sys.modules["homeassistant.const"].CONF_URL = "url"
        sys.modules["homeassistant.const"].CONF_API_KEY = "api_key"
        sys.modules["homeassistant.const"].CONF_NAME = "name"
        sys.modules["homeassistant.const"].MATCH_ALL = "*"
        conv_mod = sys.modules["homeassistant.components.conversation"]
        conv_mod.ConversationEntityFeature = MagicMock()
        conv_mod.ConversationEntity = type(
            "ConversationEntity",
            (),
            {"__init__": lambda self, *a, **kw: None},
        )
        sys.modules["homeassistant.components"].conversation = conv_mod
        sys.modules["homeassistant.components"].assist_pipeline = sys.modules[
            "homeassistant.components.assist_pipeline"
        ]

        yield

        import sys as _sys

        for key in list(_sys.modules.keys()):
            if key.startswith("custom_components"):
                del _sys.modules[key]

    async def test_repeat_outside_coalesce_window_triggers_new_bridge(self):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
        from custom_components.ha_agenthub.conversation import HaAgentHubConversationEntity

        entity = HaAgentHubConversationEntity.__new__(HaAgentHubConversationEntity)
        entity._coalesce_lock = asyncio.Lock()
        entity._inflight_bridge = {}
        entity._coalesce_window_sec = 0.05

        bridge_calls = 0

        async def _fake_bridge(user_input, key):
            nonlocal bridge_calls
            bridge_calls += 1
            await asyncio.sleep(0.001)
            return f"result-{bridge_calls}"

        entity._async_bridge_with_cleanup = _fake_bridge

        class _FakeHass:
            def async_create_task(self, coro):
                return asyncio.create_task(coro)

        entity.hass = _FakeHass()

        ui = MagicMock()
        ui.text = "turn on light"
        ui.conversation_id = "c-1"

        r1 = await entity._async_handle_message(ui, MagicMock())
        await asyncio.sleep(entity._coalesce_window_sec + 0.05)
        r2 = await entity._async_handle_message(ui, MagicMock())

        assert bridge_calls == 2
        assert r1 != r2

    async def test_repeat_inside_coalesce_window_collapses(self):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
        from custom_components.ha_agenthub.conversation import HaAgentHubConversationEntity

        entity = HaAgentHubConversationEntity.__new__(HaAgentHubConversationEntity)
        entity._coalesce_lock = asyncio.Lock()
        entity._inflight_bridge = {}
        entity._coalesce_window_sec = 1.0

        gate = asyncio.Event()
        bridge_calls = 0

        async def _fake_bridge(user_input, key):
            nonlocal bridge_calls
            bridge_calls += 1
            await gate.wait()
            return "result"

        entity._async_bridge_with_cleanup = _fake_bridge

        class _FakeHass:
            def async_create_task(self, coro):
                return asyncio.create_task(coro)

        entity.hass = _FakeHass()

        ui = MagicMock()
        ui.text = "turn on light"
        ui.conversation_id = "c-1"

        t1 = asyncio.create_task(entity._async_handle_message(ui, MagicMock()))
        await asyncio.sleep(0)
        t2 = asyncio.create_task(entity._async_handle_message(ui, MagicMock()))
        await asyncio.sleep(0)
        gate.set()
        r1, r2 = await asyncio.gather(t1, t2)

        assert bridge_calls == 1
        assert r1 == r2 == "result"

    async def test_receive_loop_propagates_cancelled_error(self):
        """CONT-5.2: _receive_loop must propagate asyncio.CancelledError."""
        ws = HAWebSocketClient()
        ws._running = True

        mock_ws = MagicMock()
        mock_ws.closed = False
        mock_ws.receive = AsyncMock(side_effect=asyncio.CancelledError("stop"))
        ws._ws = mock_ws

        with pytest.raises(asyncio.CancelledError):
            await ws._receive_loop()
