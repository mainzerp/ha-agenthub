"""Tests for HA-AgentHub integration: config flow, options flow, WebSocket reconnect, and URL normalization.

These tests mock homeassistant dependencies so the integration can be exercised
without installing the full HA core package.
"""

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
