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
    """Test _validate_connection with mocked aiohttp."""

    def _get_fn(self):
        from custom_components.ha_agenthub.config_flow import _validate_connection

        return _validate_connection

    @pytest.mark.asyncio
    async def test_invalid_url_returns_error(self):
        fn = self._get_fn()
        result = await fn("not-a-valid-url", "key123")
        assert result == "invalid_url"

    @pytest.mark.asyncio
    async def test_empty_api_key_returns_invalid_auth(self):
        fn = self._get_fn()
        result = await fn("http://example.com", "")
        assert result == "invalid_auth"

    @pytest.mark.asyncio
    async def test_successful_connection(self):
        import aiohttp

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"status": "ok"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            fn = self._get_fn()
            result = await fn("http://example.com", "key123")
            assert result is None

    @pytest.mark.asyncio
    async def test_401_returns_invalid_auth(self):
        import aiohttp

        mock_resp = MagicMock()
        mock_resp.status = 401
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            fn = self._get_fn()
            result = await fn("http://example.com", "key123")
            assert result == "invalid_auth"

    @pytest.mark.asyncio
    async def test_403_returns_invalid_auth(self):
        import aiohttp

        mock_resp = MagicMock()
        mock_resp.status = 403
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            fn = self._get_fn()
            result = await fn("http://example.com", "key123")
            assert result == "invalid_auth"

    @pytest.mark.asyncio
    async def test_500_returns_cannot_connect(self):
        import aiohttp

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            fn = self._get_fn()
            result = await fn("http://example.com", "key123")
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
