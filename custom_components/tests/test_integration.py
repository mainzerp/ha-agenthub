"""Tests for HA-AgentHub integration: config flow, options flow, WebSocket reconnect, and URL normalization.

These tests mock homeassistant dependencies so the integration can be exercised
without installing the full HA core package.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# 5.3.1  _normalize_url  --  config_flow.py
# ---------------------------------------------------------------------------


class TestNormalizeUrlConfigFlow:
    """Test _normalize_url from config_flow.py."""

    def _get_fn(self):
        from custom_components.ha_agenthub.config_flow import _normalize_url

        return _normalize_url

    def test_valid_url_http(self):
        fn = self._get_fn()
        assert fn("http://example.com") == "http://example.com"

    def test_valid_url_https(self):
        fn = self._get_fn()
        assert fn("https://example.com") == "https://example.com"

    def test_strips_trailing_slash(self):
        fn = self._get_fn()
        assert fn("http://example.com/") == "http://example.com"

    def test_strips_trailing_slashes(self):
        fn = self._get_fn()
        assert fn("https://example.com///") == "https://example.com"

    def test_trims_whitespace(self):
        fn = self._get_fn()
        assert fn("  http://example.com  ") == "http://example.com"

    def test_empty_url_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn("")

    def test_none_url_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn(None)

    def test_whitespace_only_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn("   ")

    def test_embedded_whitespace_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn("http://exa mple.com")

    def test_missing_scheme_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn("example.com")

    def test_ftp_scheme_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn("ftp://example.com")

    def test_tab_whitespace_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn("http://exa\tmple.com")

    def test_newline_whitespace_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn("http://exa\nmple.com")

    def test_no_host_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn("http://")


# ---------------------------------------------------------------------------
# 5.3.2  _normalize_url  --  __init__.py
# ---------------------------------------------------------------------------


class TestNormalizeUrlInit:
    """Test _normalize_url from __init__.py."""

    def _get_fn(self):
        from custom_components.ha_agenthub.__init__ import _normalize_url

        return _normalize_url

    def test_valid_url_http(self):
        fn = self._get_fn()
        assert fn("http://example.com") == "http://example.com"

    def test_valid_url_https(self):
        fn = self._get_fn()
        assert fn("https://example.com") == "https://example.com"

    def test_strips_trailing_slash(self):
        fn = self._get_fn()
        assert fn("http://example.com/") == "http://example.com"

    def test_empty_url_returns_empty(self):
        fn = self._get_fn()
        assert fn("") == ""

    def test_missing_scheme_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn("example.com")

    def test_nonempty_without_scheme_raises(self):
        fn = self._get_fn()
        with pytest.raises(ValueError):
            fn("host:8080")

    def test_none_url_returns_empty(self):
        fn = self._get_fn()
        assert fn(None) == ""


# ---------------------------------------------------------------------------
# 5.3.3  Cross-validation: both _normalize_url implementations agree
# ---------------------------------------------------------------------------


class TestNormalizeUrlConsistency:
    """Ensure both _normalize_url implementations produce the same result
    for all valid inputs."""

    def _get_fns(self):
        from custom_components.ha_agenthub.config_flow import _normalize_url as cfg_norm
        from custom_components.ha_agenthub.__init__ import _normalize_url as ini_norm

        return cfg_norm, ini_norm

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "https://example.com",
            "http://example.com/",
            "  http://example.com  ",
            "https://server:8080",
            "http://192.168.1.1:8123",
            "https://sub.domain.example.com",
            "http://localhost:8080/path",
            "https://example.com/path/to/resource",
        ],
    )
    def test_both_agree_on_valid(self, url):
        cfg_norm, ini_norm = self._get_fns()
        assert cfg_norm(url) == ini_norm(url), f"Mismatch for URL: {url!r}"


# ---------------------------------------------------------------------------
# 5.3.4  Config flow URL validation  (mocked HTTP)
# ---------------------------------------------------------------------------


class TestValidateConnection:
    """Test _validate_connection with a mocked shared client session."""

    def _get_fn(self):
        from custom_components.ha_agenthub.config_flow import _validate_connection

        return _validate_connection

    def _patch_session(self, mock_resp):
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        return patch(
            "custom_components.ha_agenthub.config_flow.async_get_clientsession",
            return_value=mock_session,
        )

    def _make_response(self, status, json_payload=None, json_error=None):
        mock_resp = MagicMock()
        mock_resp.status = status
        if json_error is not None:
            mock_resp.json = AsyncMock(side_effect=json_error)
        else:
            mock_resp.json = AsyncMock(return_value=json_payload)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        return mock_resp

    @pytest.mark.asyncio
    async def test_invalid_url_returns_error(self):
        fn = self._get_fn()
        result = await fn(MagicMock(), "not-a-valid-url", "key123")
        assert result == "invalid_url"

    @pytest.mark.asyncio
    async def test_empty_api_key_returns_invalid_auth(self):
        fn = self._get_fn()
        result = await fn(MagicMock(), "http://example.com", "")
        assert result == "invalid_auth"

    @pytest.mark.asyncio
    async def test_successful_connection(self):
        mock_resp = self._make_response(200, json_payload={"status": "ok"})

        with self._patch_session(mock_resp):
            fn = self._get_fn()
            result = await fn(MagicMock(), "http://example.com", "key123")
            assert result is None

    @pytest.mark.asyncio
    async def test_401_returns_invalid_auth(self):
        mock_resp = self._make_response(401)

        with self._patch_session(mock_resp):
            fn = self._get_fn()
            result = await fn(MagicMock(), "http://example.com", "key123")
            assert result == "invalid_auth"

    @pytest.mark.asyncio
    async def test_403_returns_invalid_auth(self):
        mock_resp = self._make_response(403)

        with self._patch_session(mock_resp):
            fn = self._get_fn()
            result = await fn(MagicMock(), "http://example.com", "key123")
            assert result == "invalid_auth"

    @pytest.mark.asyncio
    async def test_500_returns_cannot_connect(self):
        mock_resp = self._make_response(500)

        with self._patch_session(mock_resp):
            fn = self._get_fn()
            result = await fn(MagicMock(), "http://example.com", "key123")
            assert result == "cannot_connect"

    @pytest.mark.asyncio
    async def test_malformed_json_returns_cannot_connect(self):
        mock_resp = self._make_response(200, json_error=ValueError("no json"))

        with self._patch_session(mock_resp):
            fn = self._get_fn()
            result = await fn(MagicMock(), "http://example.com", "key123")
            assert result == "cannot_connect"

    @pytest.mark.asyncio
    async def test_non_dict_json_returns_cannot_connect(self):
        mock_resp = self._make_response(200, json_payload=["not", "a", "dict"])

        with self._patch_session(mock_resp):
            fn = self._get_fn()
            result = await fn(MagicMock(), "http://example.com", "key123")
            assert result == "cannot_connect"

    @pytest.mark.asyncio
    async def test_unhealthy_payload_returns_cannot_connect(self):
        mock_resp = self._make_response(200, json_payload={"status": "degraded"})

        with self._patch_session(mock_resp):
            fn = self._get_fn()
            result = await fn(MagicMock(), "http://example.com", "key123")
            assert result == "cannot_connect"


# ---------------------------------------------------------------------------
# 5.3.5  WebSocket reconnect logic
# ---------------------------------------------------------------------------


class TestWebSocketReconnect:
    """Test WebSocket reconnect loop behaviour."""

    def _get_reconnect_constants(self):
        from custom_components.ha_agenthub import const

        return const

    def test_reconnect_base_delay_is_positive(self):
        const = self._get_reconnect_constants()
        assert const.RECONNECT_BASE_DELAY > 0

    def test_reconnect_max_delay_gt_base(self):
        const = self._get_reconnect_constants()
        assert const.RECONNECT_MAX_DELAY > const.RECONNECT_BASE_DELAY

    def test_exponential_backoff_formula(self):
        const = self._get_reconnect_constants()
        delay = const.RECONNECT_BASE_DELAY
        iterations = []
        for _ in range(10):
            iterations.append(delay)
            delay = min(delay * 2, const.RECONNECT_MAX_DELAY)
        assert iterations[0] == const.RECONNECT_BASE_DELAY
        for i in range(1, len(iterations)):
            assert iterations[i] >= iterations[i - 1]
        all_clamped = [d for d in iterations if d == const.RECONNECT_MAX_DELAY]
        assert len(all_clamped) > 0

    def test_ws_path_is_defined(self):
        const = self._get_reconnect_constants()
        assert const.WS_PATH == "/ws/conversation"

    def test_heartbeat_interval_reasonable(self):
        const = self._get_reconnect_constants()
        assert const.WS_HEARTBEAT_INTERVAL > 0
        assert const.WS_HEARTBEAT_INTERVAL < 300

    def test_idle_threshold_gt_heartbeat(self):
        const = self._get_reconnect_constants()
        assert const.WS_IDLE_THRESHOLD > const.WS_HEARTBEAT_INTERVAL


# ---------------------------------------------------------------------------
# 5.3.6 WebSocket receive timeout is configurable via options
# ---------------------------------------------------------------------------


class TestWsReceiveTimeout:
    def _make_entity(self, options: dict | None = None):
        from custom_components.ha_agenthub.conversation import (
            HaAgentHubConversationEntity,
        )

        entry = MagicMock()
        entry.entry_id = "test-entry"
        entry.options = options or {}
        entry.async_create_background_task = MagicMock(return_value=MagicMock())
        entry.async_on_unload = MagicMock()
        return HaAgentHubConversationEntity(entry, "http://example.com", "key")

    @pytest.mark.asyncio
    async def test_default_timeout_used_when_no_option_set(self):
        from custom_components.ha_agenthub.const import DEFAULT_WS_RECEIVE_TIMEOUT

        entity = self._make_entity()
        entity._ws = MagicMock()
        entity._ws.send_json = AsyncMock()
        entity._ws.receive = AsyncMock(
            return_value=MagicMock(type=1, data='{"done": true, "token": "hi"}')
        )

        user_input = MagicMock()
        user_input.conversation_id = "c1"
        user_input.text = "hello"
        user_input.language = "en"
        user_input.device_id = None

        with (
            patch(
                "custom_components.ha_agenthub.conversation.aiohttp.WSMsgType",
                type("WSMsgType", (), {"TEXT": 1}),
            ),
            patch(
                "custom_components.ha_agenthub.conversation.asyncio.wait_for",
                new=AsyncMock(),
            ) as mock_wait,
        ):
            mock_wait.return_value = entity._ws.receive.return_value
            await entity._process_via_ws(user_input)

        timeout = mock_wait.call_args.kwargs["timeout"]
        assert timeout == DEFAULT_WS_RECEIVE_TIMEOUT

    @pytest.mark.asyncio
    async def test_timeout_read_from_entry_options(self):
        from custom_components.ha_agenthub.const import CONF_WS_RECEIVE_TIMEOUT

        entity = self._make_entity({CONF_WS_RECEIVE_TIMEOUT: 200})
        entity._ws = MagicMock()
        entity._ws.send_json = AsyncMock()
        entity._ws.receive = AsyncMock(
            return_value=MagicMock(type=1, data='{"done": true, "token": "hi"}')
        )

        user_input = MagicMock()
        user_input.conversation_id = "c1"
        user_input.text = "hello"
        user_input.language = "en"
        user_input.device_id = None

        with (
            patch(
                "custom_components.ha_agenthub.conversation.aiohttp.WSMsgType",
                type("WSMsgType", (), {"TEXT": 1}),
            ),
            patch(
                "custom_components.ha_agenthub.conversation.asyncio.wait_for",
                new=AsyncMock(),
            ) as mock_wait,
        ):
            mock_wait.return_value = entity._ws.receive.return_value
            await entity._process_via_ws(user_input)

        timeout = mock_wait.call_args.kwargs["timeout"]
        assert timeout == 200.0

    @pytest.mark.asyncio
    async def test_invalid_timeout_option_falls_back_to_default(self):
        from custom_components.ha_agenthub.const import (
            CONF_WS_RECEIVE_TIMEOUT,
            DEFAULT_WS_RECEIVE_TIMEOUT,
        )

        entity = self._make_entity({CONF_WS_RECEIVE_TIMEOUT: "not-a-number"})
        entity._ws = MagicMock()
        entity._ws.send_json = AsyncMock()
        entity._ws.receive = AsyncMock(
            return_value=MagicMock(type=1, data='{"done": true, "token": "hi"}')
        )

        user_input = MagicMock()
        user_input.conversation_id = "c1"
        user_input.text = "hello"
        user_input.language = "en"
        user_input.device_id = None

        with (
            patch(
                "custom_components.ha_agenthub.conversation.aiohttp.WSMsgType",
                type("WSMsgType", (), {"TEXT": 1}),
            ),
            patch(
                "custom_components.ha_agenthub.conversation.asyncio.wait_for",
                new=AsyncMock(),
            ) as mock_wait,
        ):
            mock_wait.return_value = entity._ws.receive.return_value
            await entity._process_via_ws(user_input)

        timeout = mock_wait.call_args.kwargs["timeout"]
        assert timeout == DEFAULT_WS_RECEIVE_TIMEOUT


# ---------------------------------------------------------------------------
# 5.3.7 Reconnect scheduling is debounced
# ---------------------------------------------------------------------------


class TestReconnectDebounce:
    def _make_entity(self):
        from custom_components.ha_agenthub.conversation import (
            HaAgentHubConversationEntity,
        )

        entry = MagicMock()
        entry.entry_id = "test-entry"
        entry.async_create_background_task = MagicMock(return_value=MagicMock())
        entry.async_on_unload = MagicMock()
        return HaAgentHubConversationEntity(entry, "http://example.com", "key")

    @pytest.mark.asyncio
    async def test_schedule_reconnect_sets_event_and_does_not_spawn_task(self):
        entity = self._make_entity()
        entity._reconnect_requested.clear()
        entity._schedule_reconnect()
        assert entity._reconnect_requested.is_set()
        entry = entity._entry
        entry.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconnect_loop_handles_multiple_requests_without_overlapping(self):
        entity = self._make_entity()
        entity._ws = None
        connect_calls = []

        async def fake_connect():
            connect_calls.append(1)
            entity._ws = MagicMock()
            entity._ws.closed = False
            return True

        entity._connect_ws = AsyncMock(side_effect=fake_connect)
        entity._reconnect_requested.clear()

        # async_create_background_task is mocked; run the coroutine manually.
        task = asyncio.create_task(entity._reconnect_loop())
        await asyncio.sleep(0.05)
        entity._schedule_reconnect()
        entity._schedule_reconnect()
        entity._schedule_reconnect()
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(connect_calls) == 1


# ---------------------------------------------------------------------------
# 5.3.8 Config entry source of truth for URL and API key
# ---------------------------------------------------------------------------


class TestConfigEntrySourceOfTruth:
    @pytest.fixture
    def hass(self):
        hass = MagicMock()
        hass.data = {}
        return hass

    @pytest.mark.asyncio
    async def test_setup_entry_prefers_data_over_options(self, hass):
        from custom_components.ha_agenthub import async_setup_entry
        from custom_components.ha_agenthub.const import DOMAIN
        from homeassistant.const import CONF_API_KEY, CONF_URL

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.title = "HA-AgentHub"
        entry.data = {CONF_URL: "http://data.local", CONF_API_KEY: "data-key"}
        entry.options = {CONF_URL: "http://options.local", CONF_API_KEY: "options-key"}
        entry.async_on_unload = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

        result = await async_setup_entry(hass, entry)
        assert result is True
        stored = hass.data[DOMAIN][entry.entry_id]
        assert stored["url"] == "http://data.local"
        assert stored["api_key"] == "data-key"

    @pytest.mark.asyncio
    async def test_migrate_entry_moves_url_and_api_key_from_options_to_data(self, hass):
        from custom_components.ha_agenthub import async_migrate_entry
        from custom_components.ha_agenthub.const import CONF_NAME
        from homeassistant.const import CONF_API_KEY, CONF_URL

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.version = 2
        entry.data = {CONF_NAME: "HA-AgentHub"}
        entry.options = {CONF_URL: "http://options.local", CONF_API_KEY: "options-key"}
        entry.unique_id = "http://options.local"

        def update_entry(entry, **kwargs):
            for key, value in kwargs.items():
                setattr(entry, key, value)

        hass.config_entries.async_update_entry = MagicMock(side_effect=update_entry)

        result = await async_migrate_entry(hass, entry)
        assert result is True
        assert entry.version == 3
        assert entry.data[CONF_URL] == "http://options.local"
        assert entry.data[CONF_API_KEY] == "options-key"
        assert entry.options == {}


# ---------------------------------------------------------------------------
# 5.3.9 Options flow: ws_receive_timeout persistence and validation
# ---------------------------------------------------------------------------


class TestOptionsFlow:
    def _make_flow(self, unique_id="http://old.local"):
        from custom_components.ha_agenthub.config_flow import HaAgentHubOptionsFlow

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.title = "HA-AgentHub"
        entry.data = {
            "name": "HA-AgentHub",
            "url": "http://old.local",
            "api_key": "stored-token",
        }
        entry.options = {}
        entry.unique_id = unique_id

        flow = HaAgentHubOptionsFlow(entry)
        flow.hass = MagicMock()
        flow.hass.config_entries.async_entries = MagicMock(return_value=[])
        return flow, entry

    @pytest.mark.asyncio
    async def test_timeout_persisted_via_create_entry_single_write(self):
        flow, entry = self._make_flow()

        with patch(
            "custom_components.ha_agenthub.config_flow._validate_connection",
            new=AsyncMock(return_value=None),
        ) as mock_validate:
            result = await flow.async_step_init(
                {
                    "url": "http://old.local",
                    "api_key": "",
                    "name": "",
                    "ws_receive_timeout": "45",
                }
            )

        # The flow manager applies result["data"] to entry.options.
        assert result["type"] == "create_entry"
        assert result["data"] == {"ws_receive_timeout": 45.0}
        entry.options = result["data"]
        assert entry.options["ws_receive_timeout"] == 45.0

        mock_validate.assert_awaited_once_with(
            flow.hass, "http://old.local", "stored-token"
        )
        flow.hass.config_entries.async_update_entry.assert_called_once()
        update_kwargs = flow.hass.config_entries.async_update_entry.call_args.kwargs
        assert "options" not in update_kwargs
        assert "unique_id" not in update_kwargs
        assert update_kwargs["data"]["url"] == "http://old.local"
        assert update_kwargs["data"]["api_key"] == "stored-token"

    @pytest.mark.asyncio
    async def test_invalid_timeout_shows_form_error_without_validation(self):
        flow, entry = self._make_flow()

        with patch(
            "custom_components.ha_agenthub.config_flow._validate_connection",
            new=AsyncMock(return_value=None),
        ) as mock_validate:
            result = await flow.async_step_init(
                {
                    "url": "http://old.local",
                    "api_key": "",
                    "name": "",
                    "ws_receive_timeout": "abc",
                }
            )

        assert result["type"] == "form"
        assert result["errors"] == {"ws_receive_timeout": "invalid_timeout"}
        mock_validate.assert_not_called()
        flow.hass.config_entries.async_update_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_url_change_updates_unique_id_in_same_write(self):
        flow, entry = self._make_flow()

        with patch(
            "custom_components.ha_agenthub.config_flow._validate_connection",
            new=AsyncMock(return_value=None),
        ):
            result = await flow.async_step_init(
                {
                    "url": "http://new.local",
                    "api_key": "",
                    "name": "",
                    "ws_receive_timeout": "30",
                }
            )

        assert result["type"] == "create_entry"
        assert result["data"] == {"ws_receive_timeout": 30.0}
        flow.hass.config_entries.async_update_entry.assert_called_once()
        update_kwargs = flow.hass.config_entries.async_update_entry.call_args.kwargs
        assert update_kwargs["unique_id"] == "http://new.local"
        assert update_kwargs["data"]["url"] == "http://new.local"

    @pytest.mark.asyncio
    async def test_url_change_to_existing_unique_id_shows_error(self):
        flow, entry = self._make_flow()
        other = MagicMock()
        other.entry_id = "other-entry"
        other.unique_id = "http://taken.local"
        flow.hass.config_entries.async_entries = MagicMock(return_value=[other])

        with patch(
            "custom_components.ha_agenthub.config_flow._validate_connection",
            new=AsyncMock(return_value=None),
        ):
            result = await flow.async_step_init(
                {
                    "url": "http://taken.local",
                    "api_key": "",
                    "name": "",
                    "ws_receive_timeout": "30",
                }
            )

        assert result["type"] == "form"
        assert result["errors"] == {"base": "already_configured"}
        flow.hass.config_entries.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# 5.3.10 Reauth flow: unique_id maintenance on URL change
# ---------------------------------------------------------------------------


class TestReauthFlow:
    def _make_flow(self, unique_id="http://old.local"):
        from custom_components.ha_agenthub.config_flow import HaAgentHubConfigFlow

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.title = "HA-AgentHub"
        entry.data = {"url": "http://old.local", "api_key": "stored-token"}
        entry.unique_id = unique_id

        flow = HaAgentHubConfigFlow()
        flow.hass = MagicMock()
        flow._get_reauth_entry = lambda: entry
        flow.hass.config_entries.async_entries = MagicMock(return_value=[entry])
        flow.hass.config_entries.async_reload = AsyncMock()
        return flow, entry

    @pytest.mark.asyncio
    async def test_reauth_same_url_leaves_unique_id_untouched(self):
        flow, entry = self._make_flow()

        with patch(
            "custom_components.ha_agenthub.config_flow._validate_connection",
            new=AsyncMock(return_value=None),
        ):
            result = await flow.async_step_reauth(
                {"url": "http://old.local", "api_key": "new-key"}
            )

        assert result == {"type": "abort", "reason": "reauth_successful"}
        flow.hass.config_entries.async_update_entry.assert_called_once()
        update_kwargs = flow.hass.config_entries.async_update_entry.call_args.kwargs
        assert "unique_id" not in update_kwargs
        assert update_kwargs["data"]["api_key"] == "new-key"
        flow.hass.config_entries.async_reload.assert_awaited_once_with("e1")

    @pytest.mark.asyncio
    async def test_reauth_url_change_updates_unique_id_in_same_write(self):
        flow, entry = self._make_flow()

        with patch(
            "custom_components.ha_agenthub.config_flow._validate_connection",
            new=AsyncMock(return_value=None),
        ):
            result = await flow.async_step_reauth(
                {"url": "http://new.local", "api_key": "new-key"}
            )

        assert result == {"type": "abort", "reason": "reauth_successful"}
        flow.hass.config_entries.async_update_entry.assert_called_once()
        update_kwargs = flow.hass.config_entries.async_update_entry.call_args.kwargs
        assert update_kwargs["unique_id"] == "http://new.local"
        assert update_kwargs["data"]["url"] == "http://new.local"

    @pytest.mark.asyncio
    async def test_reauth_url_change_to_other_entry_aborts(self):
        flow, entry = self._make_flow()
        other = MagicMock()
        other.entry_id = "other-entry"
        other.unique_id = "http://taken.local"
        flow.hass.config_entries.async_entries = MagicMock(return_value=[entry, other])

        with patch(
            "custom_components.ha_agenthub.config_flow._validate_connection",
            new=AsyncMock(return_value=None),
        ):
            result = await flow.async_step_reauth(
                {"url": "http://taken.local", "api_key": "new-key"}
            )

        assert result == {"type": "abort", "reason": "already_configured"}
        flow.hass.config_entries.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# 5.3.11 Automatic reauth trigger on 401/403 REST responses
# ---------------------------------------------------------------------------


class TestReauthTriggerOnAuthFailure:
    def _make_entity(self):
        from custom_components.ha_agenthub.conversation import (
            HaAgentHubConversationEntity,
        )

        entry = MagicMock()
        entry.entry_id = "test-entry"
        entry.options = {}
        entry.async_create_background_task = MagicMock(return_value=MagicMock())
        entry.async_on_unload = MagicMock()
        entry.async_start_reauth = MagicMock()
        entity = HaAgentHubConversationEntity(entry, "http://example.com", "key")
        entity.hass = MagicMock()
        return entity

    class _FakeResponse:
        def __init__(self, status, payload=None):
            self.status = status
            self._payload = payload or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return self._payload

    class _FakeSession:
        closed = False

        def __init__(self, response):
            self._response = response

        def post(self, *args, **kwargs):
            return self._response

    def _make_user_input(self):
        user_input = MagicMock()
        user_input.text = "hello"
        user_input.conversation_id = "c1"
        user_input.language = "en"
        user_input.device_id = None
        return user_input

    @pytest.mark.asyncio
    async def test_reauth_started_once_per_failure_episode(self):
        entity = self._make_entity()
        user_input = self._make_user_input()

        entity._session = self._FakeSession(self._FakeResponse(401))
        await entity._process_via_rest(user_input)
        await entity._process_via_rest(user_input)
        entity._entry.async_start_reauth.assert_called_once_with(entity.hass)

        # A successful response resets the episode guard.
        entity._session = self._FakeSession(
            self._FakeResponse(200, {"speech": "ok", "conversation_id": "c1"})
        )
        await entity._process_via_rest(user_input)

        entity._session = self._FakeSession(self._FakeResponse(403))
        await entity._process_via_rest(user_input)
        assert entity._entry.async_start_reauth.call_count == 2

    @pytest.mark.asyncio
    async def test_no_reauth_on_server_error(self):
        entity = self._make_entity()
        user_input = self._make_user_input()

        entity._session = self._FakeSession(self._FakeResponse(503))
        await entity._process_via_rest(user_input)
        entity._entry.async_start_reauth.assert_not_called()


# ---------------------------------------------------------------------------
# 5.3.12 Setup entry raises ConfigEntryError when URL is missing
# ---------------------------------------------------------------------------


class TestSetupEntryErrors:
    @pytest.mark.asyncio
    async def test_missing_url_raises_config_entry_error(self):
        from custom_components.ha_agenthub import async_setup_entry
        from homeassistant.exceptions import ConfigEntryError

        hass = MagicMock()
        hass.data = {}
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.title = "HA-AgentHub"
        entry.data = {}
        entry.options = {}
        entry.async_on_unload = MagicMock()
        entry.add_update_listener = MagicMock()

        with pytest.raises(ConfigEntryError):
            await async_setup_entry(hass, entry)
