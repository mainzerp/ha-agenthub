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
        from custom_components.ha_agenthub.__init__ import _normalize_url as ini_norm
        from custom_components.ha_agenthub.config_flow import _normalize_url as cfg_norm

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
# P1: narrowed WS-lock critical section (connect+send locked, read unlocked)
# ---------------------------------------------------------------------------


class TestWsLockNarrowing:
    """P1: the streaming read runs outside ``_ws_lock`` on a turn-owned
    socket; a concurrent turn must not queue behind a slow read."""

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

    def _make_user_input(self, cid: str, text: str):
        user_input = MagicMock()
        user_input.conversation_id = cid
        user_input.text = text
        user_input.language = "en"
        user_input.device_id = None
        return user_input

    def _make_ws(self, receive):
        ws = MagicMock()
        ws.closed = False
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        ws.receive = receive
        return ws

    @pytest.mark.asyncio
    async def test_concurrent_turn_not_blocked_behind_slow_read(self):
        import json
        import time

        import aiohttp

        entity = self._make_entity()
        release_turn1 = asyncio.Event()

        async def _slow_receive():
            await release_turn1.wait()
            return MagicMock(
                type=aiohttp.WSMsgType.TEXT,
                data=json.dumps({"done": True, "token": "turn one"}),
            )

        ws1 = self._make_ws(AsyncMock(side_effect=_slow_receive))
        ws2 = self._make_ws(
            AsyncMock(
                return_value=MagicMock(
                    type=aiohttp.WSMsgType.TEXT,
                    data=json.dumps({"done": True, "token": "turn two"}),
                )
            )
        )

        entity._ws = ws1
        entity._ws_last_active = time.monotonic()

        async def _connect_second():
            # Turn 2 finds no shared socket (turn 1 owns it) and connects anew.
            entity._ws = ws2
            return True

        with patch.object(entity, "_connect_ws_locked", side_effect=_connect_second):
            turn1 = asyncio.create_task(
                entity._async_bridge_to_container(self._make_user_input("c1", "one"))
            )
            await asyncio.sleep(0.05)  # turn 1 is now blocked in its read
            turn2 = asyncio.create_task(
                entity._async_bridge_to_container(self._make_user_input("c2", "two"))
            )
            # Turn 2 completes while turn 1 is still blocked in its read --
            # with the pre-P1 whole-cycle lock this would time out.
            result2 = await asyncio.wait_for(turn2, timeout=1.0)
            assert not turn1.done()
            release_turn1.set()
            result1 = await asyncio.wait_for(turn1, timeout=1.0)

        assert result1 is not None and result2 is not None
        ws1.send_json.assert_awaited_once()
        ws2.send_json.assert_awaited_once()
        # Turn 2 finished first and republished its socket; turn 1's
        # finished socket was closed instead of replacing it.
        assert entity._ws is ws2
        ws1.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_midstream_failure_closes_turn_socket_and_skips_rest(self):
        import time

        import aiohttp

        entity = self._make_entity()
        # Transport error mid-stream (the conftest aiohttp mock only defines
        # WSMsgType.TEXT, so fail via ClientError instead of a CLOSED frame).
        ws = self._make_ws(
            AsyncMock(side_effect=aiohttp.ClientError("closed mid-stream"))
        )
        entity._ws = ws
        entity._ws_last_active = time.monotonic()

        with patch.object(
            entity, "_process_via_rest", new_callable=AsyncMock
        ) as mock_rest:
            result = await entity._async_bridge_to_container(
                self._make_user_input("c1", "hello")
            )

        # _WsDroppedAfterSendError semantics preserved: the request may have
        # run on the container, so no duplicate REST dispatch.
        mock_rest.assert_not_awaited()
        # The failing turn closed its own socket (not a shared one).
        ws.close.assert_awaited_once()
        assert entity._ws is None
        assert result is not None

    @pytest.mark.asyncio
    async def test_send_failure_disconnects_and_falls_back_to_rest(self):
        import time

        import aiohttp

        entity = self._make_entity()
        ws = self._make_ws(
            AsyncMock(
                return_value=MagicMock(
                    type=aiohttp.WSMsgType.TEXT, data='{"done": true}'
                )
            )
        )
        ws.send_json = AsyncMock(side_effect=aiohttp.ClientError("boom"))
        entity._ws = ws
        entity._ws_last_active = time.monotonic()

        with (
            patch.object(
                entity,
                "_process_via_rest",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ) as mock_rest,
            patch.object(
                entity, "_disconnect_ws_locked", new_callable=AsyncMock
            ) as mock_disconnect,
        ):
            await entity._async_bridge_to_container(
                self._make_user_input("c1", "hello")
            )

        mock_disconnect.assert_awaited_once()
        mock_rest.assert_awaited_once()


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
        from homeassistant.const import CONF_API_KEY, CONF_URL

        from custom_components.ha_agenthub import async_setup_entry
        from custom_components.ha_agenthub.const import DOMAIN

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
        from homeassistant.const import CONF_API_KEY, CONF_URL

        from custom_components.ha_agenthub import async_migrate_entry
        from custom_components.ha_agenthub.const import CONF_NAME

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
        flow, _entry = self._make_flow()

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
        flow, _entry = self._make_flow()

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
        flow, _entry = self._make_flow()
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
        flow, _entry = self._make_flow()

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
        flow, _entry = self._make_flow()

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
        from homeassistant.exceptions import ConfigEntryError

        from custom_components.ha_agenthub import async_setup_entry

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


# ---------------------------------------------------------------------------
# 5.3.13 user_id forwarding to the container (M-5)
# ---------------------------------------------------------------------------


class TestUserIdForwarding:
    """_resolve_origin_context forwards user_id; WS and REST payloads carry it."""

    def _make_entity(self):
        from custom_components.ha_agenthub.conversation import (
            HaAgentHubConversationEntity,
        )

        entry = MagicMock()
        entry.entry_id = "test-entry"
        entry.options = {}
        entry.async_create_background_task = MagicMock(return_value=MagicMock())
        entry.async_on_unload = MagicMock()
        return HaAgentHubConversationEntity(entry, "http://example.com", "key")

    def _make_user_input(self, context=None):
        user_input = MagicMock()
        user_input.text = "hello"
        user_input.conversation_id = "c1"
        user_input.language = "en"
        user_input.device_id = None
        user_input.context = context
        return user_input

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
            self.posted_payload = None

        def post(self, *args, **kwargs):
            self.posted_payload = kwargs.get("json")
            return self._response

    async def _run_ws(self, entity, user_input):
        # The send phase detaches the socket from ``entity._ws`` (the turn
        # owns it exclusively during the unlocked read), so keep a local
        # reference to inspect the payload afterwards.
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ws.receive = AsyncMock(
            return_value=MagicMock(type=1, data='{"done": true, "token": "hi"}')
        )
        entity._ws = ws
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
            mock_wait.return_value = ws.receive.return_value
            await entity._process_via_ws(user_input)
        return ws.send_json.call_args.args[0]

    def test_origin_context_includes_user_id(self):
        entity = self._make_entity()
        context = MagicMock()
        context.user_id = "user-123"
        extra = entity._resolve_origin_context(self._make_user_input(context))
        assert extra["user_id"] == "user-123"

    def test_origin_context_omits_user_id_without_context(self):
        entity = self._make_entity()
        extra = entity._resolve_origin_context(self._make_user_input(None))
        assert "user_id" not in extra

    def test_origin_context_omits_falsy_user_id(self):
        entity = self._make_entity()
        context = MagicMock()
        context.user_id = None
        extra = entity._resolve_origin_context(self._make_user_input(context))
        assert "user_id" not in extra

    @pytest.mark.asyncio
    async def test_ws_payload_includes_user_id(self):
        entity = self._make_entity()
        context = MagicMock()
        context.user_id = "user-123"
        payload = await self._run_ws(entity, self._make_user_input(context))
        assert payload["user_id"] == "user-123"

    @pytest.mark.asyncio
    async def test_ws_payload_omits_user_id_without_context(self):
        entity = self._make_entity()
        payload = await self._run_ws(entity, self._make_user_input(None))
        assert "user_id" not in payload

    @pytest.mark.asyncio
    async def test_rest_payload_includes_user_id(self):
        entity = self._make_entity()
        session = self._FakeSession(
            self._FakeResponse(200, {"speech": "ok", "conversation_id": "c1"})
        )
        entity._session = session
        context = MagicMock()
        context.user_id = "user-123"
        await entity._process_via_rest(self._make_user_input(context))
        assert session.posted_payload["user_id"] == "user-123"

    @pytest.mark.asyncio
    async def test_rest_payload_omits_user_id_without_context(self):
        entity = self._make_entity()
        session = self._FakeSession(
            self._FakeResponse(200, {"speech": "ok", "conversation_id": "c1"})
        )
        entity._session = session
        await entity._process_via_rest(self._make_user_input(None))
        assert "user_id" not in session.posted_payload


# ---------------------------------------------------------------------------
# P3: shared aiohttp session reuse across WS reconnects
# ---------------------------------------------------------------------------


class TestWsSessionReuse:
    """P3 step 1: one ``aiohttp.ClientSession`` is reused across reconnects;
    only entity removal closes it."""

    def _make_entity(self):
        from custom_components.ha_agenthub.conversation import (
            HaAgentHubConversationEntity,
        )

        entry = MagicMock()
        entry.entry_id = "test-entry"
        entry.options = {}
        entry.async_create_background_task = MagicMock(return_value=MagicMock())
        entry.async_on_unload = MagicMock()
        return HaAgentHubConversationEntity(entry, "http://example.com", "key")

    @pytest.mark.asyncio
    async def test_disconnect_closes_ws_but_keeps_session(self):
        entity = self._make_entity()
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        entity._session = session
        ws = MagicMock()
        ws.closed = False
        ws.close = AsyncMock()
        entity._ws = ws

        await entity._disconnect_ws()

        ws.close.assert_awaited_once()
        session.close.assert_not_awaited()
        assert entity._ws is None
        assert entity._session is session

    @pytest.mark.asyncio
    async def test_connect_reuses_existing_session(self):
        entity = self._make_entity()
        session = MagicMock()
        session.closed = False
        ws = MagicMock()
        ws.closed = False
        session.ws_connect = AsyncMock(return_value=ws)
        entity._session = session

        with patch(
            "custom_components.ha_agenthub.conversation.aiohttp.ClientSession"
        ) as session_cls:
            connected = await entity._connect_ws()

        assert connected is True
        session_cls.assert_not_called()
        session.ws_connect.assert_awaited_once()
        assert entity._ws is ws

    @pytest.mark.asyncio
    async def test_failed_connect_keeps_session_for_retry(self):
        import aiohttp

        entity = self._make_entity()
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        session.ws_connect = AsyncMock(side_effect=aiohttp.ClientError("boom"))
        entity._session = session

        connected = await entity._connect_ws()

        assert connected is False
        assert entity._ws is None
        session.close.assert_not_awaited()
        assert entity._session is session

    @pytest.mark.asyncio
    async def test_entity_removal_closes_session(self, monkeypatch):
        import custom_components.ha_agenthub.conversation as conv_mod

        entity = self._make_entity()
        monkeypatch.setattr(
            conv_mod.conversation.ConversationEntity,
            "async_will_remove_from_hass",
            AsyncMock(),
            raising=False,
        )
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        entity._session = session

        await entity.async_will_remove_from_hass()

        session.close.assert_awaited_once()
        assert entity._session is None


# ---------------------------------------------------------------------------
# P3: satellite mapping cache + registry-event invalidation
# ---------------------------------------------------------------------------


class TestSatelliteMappingCache:
    """P3 step 5: device->satellite resolution is memoized per device and
    cleared on assist_satellite registry updates."""

    def _make_entity(self):
        from custom_components.ha_agenthub.conversation import (
            HaAgentHubConversationEntity,
        )

        entry = MagicMock()
        entry.entry_id = "test-entry"
        entry.options = {}
        entry.async_create_background_task = MagicMock(return_value=MagicMock())
        entry.async_on_unload = MagicMock()
        entity = HaAgentHubConversationEntity(entry, "http://example.com", "key")
        entity.hass = MagicMock()
        return entity

    def test_resolution_memoized_per_device(self):
        entity = self._make_entity()
        sat_entry = MagicMock()
        sat_entry.domain = "assist_satellite"
        sat_entry.entity_id = "assist_satellite.kitchen"

        with patch("custom_components.ha_agenthub.conversation.er") as mock_er:
            mock_er.async_entries_for_device.return_value = [sat_entry]
            first = entity._resolve_satellite_entity("dev-1")
            second = entity._resolve_satellite_entity("dev-1")

        assert first == "assist_satellite.kitchen"
        assert second == "assist_satellite.kitchen"
        mock_er.async_entries_for_device.assert_called_once()

    def test_negative_result_cached(self):
        entity = self._make_entity()

        with patch("custom_components.ha_agenthub.conversation.er") as mock_er:
            mock_er.async_entries_for_device.return_value = []
            assert entity._resolve_satellite_entity("dev-2") is None
            assert entity._resolve_satellite_entity("dev-2") is None

        mock_er.async_entries_for_device.assert_called_once()

    def test_per_entry_logs_demoted_to_debug(self, caplog):
        import logging

        entity = self._make_entity()
        sat_entry = MagicMock()
        sat_entry.domain = "assist_satellite"
        sat_entry.entity_id = "assist_satellite.kitchen"

        with (
            patch("custom_components.ha_agenthub.conversation.er") as mock_er,
            caplog.at_level(
                logging.DEBUG, logger="custom_components.ha_agenthub.conversation"
            ),
        ):
            mock_er.async_entries_for_device.return_value = [sat_entry]
            entity._resolve_satellite_entity("dev-1")

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert not any("registry entries" in r.getMessage() for r in info_records)
        assert not any("entry domain=" in r.getMessage() for r in info_records)

    @pytest.mark.asyncio
    async def test_registry_update_clears_cache(self, monkeypatch):
        import custom_components.ha_agenthub.conversation as conv_mod

        entity = self._make_entity()
        monkeypatch.setattr(entity, "async_on_remove", lambda cb: None, raising=False)
        monkeypatch.setattr(
            conv_mod.conversation.ConversationEntity,
            "async_added_to_hass",
            AsyncMock(),
            raising=False,
        )

        await entity.async_added_to_hass()

        callback = entity.hass.bus.async_listen.call_args.args[1]

        entity._satellite_cache["dev-1"] = "assist_satellite.kitchen"
        event = MagicMock()
        event.data = {"action": "remove", "entity_id": "assist_satellite.kitchen"}
        callback(event)
        assert entity._satellite_cache == {}

        # Unrelated entity updates keep the cache intact.
        entity._satellite_cache["dev-1"] = "assist_satellite.kitchen"
        other = MagicMock()
        other.data = {"action": "update", "entity_id": "light.kitchen"}
        callback(other)
        assert entity._satellite_cache == {"dev-1": "assist_satellite.kitchen"}
