"""Tests for HAWebSocketClient state-change waiters (FLOW-VERIFY-1)."""

from __future__ import annotations

import asyncio

import pytest

from app.ha_client.websocket import HAWebSocketClient


def _state_event(entity_id: str, state: str) -> dict:
    """Build a minimal HA ``state_changed`` event payload."""
    return {
        "event_type": "state_changed",
        "data": {
            "entity_id": entity_id,
            "new_state": {"entity_id": entity_id, "state": state, "attributes": {}},
        },
    }


class TestStateWaiters:
    """Unit tests for register_state_waiter / _dispatch_state_waiters."""

    @pytest.mark.asyncio
    async def test_resolves_on_matching_event(self):
        client = HAWebSocketClient()
        fut = client.register_state_waiter("light.keller", expected="off")

        client._dispatch_state_waiters(_state_event("light.keller", "off"))

        state = await asyncio.wait_for(fut, timeout=0.1)
        assert state == "off"
        assert "light.keller" not in client._state_waiters

    @pytest.mark.asyncio
    async def test_ignores_mismatched_expected(self):
        client = HAWebSocketClient()
        fut = client.register_state_waiter("light.keller", expected="off")

        client._dispatch_state_waiters(_state_event("light.keller", "on"))

        assert not fut.done()
        # a later matching event still resolves it
        client._dispatch_state_waiters(_state_event("light.keller", "off"))
        state = await asyncio.wait_for(fut, timeout=0.1)
        assert state == "off"

    @pytest.mark.asyncio
    async def test_any_state_when_expected_is_none(self):
        client = HAWebSocketClient()
        fut = client.register_state_waiter("light.keller", expected=None)

        client._dispatch_state_waiters(_state_event("light.keller", "on"))

        state = await asyncio.wait_for(fut, timeout=0.1)
        assert state == "on"

    @pytest.mark.asyncio
    async def test_ignores_other_entities(self):
        client = HAWebSocketClient()
        fut = client.register_state_waiter("light.keller", expected="off")

        client._dispatch_state_waiters(_state_event("switch.keller", "off"))

        assert not fut.done()
        assert "light.keller" in client._state_waiters

    @pytest.mark.asyncio
    async def test_cancel_state_waiter_removes_pending_future(self):
        client = HAWebSocketClient()
        fut = client.register_state_waiter("light.keller", expected="off")

        client.cancel_state_waiter("light.keller", fut)

        assert "light.keller" not in client._state_waiters
        assert fut.cancelled()

    @pytest.mark.asyncio
    async def test_multiple_waiters_resolve_fifo(self):
        client = HAWebSocketClient()
        first = client.register_state_waiter("light.keller", expected="off")
        second = client.register_state_waiter("light.keller", expected="off")

        client._dispatch_state_waiters(_state_event("light.keller", "off"))

        assert first.done() and second.done()
        assert (await first) == "off"
        assert (await second) == "off"

    @pytest.mark.asyncio
    async def test_malformed_event_does_not_crash(self):
        client = HAWebSocketClient()
        client.register_state_waiter("light.keller", expected="off")

        # Missing new_state.
        client._dispatch_state_waiters(
            {
                "event_type": "state_changed",
                "data": {"entity_id": "light.keller"},
            }
        )
        # Entirely empty.
        client._dispatch_state_waiters({})
        # The waiter is still pending, not crashed.
        assert "light.keller" in client._state_waiters


class TestStateWaiterReconnectCleanup:
    """P3-5: pending state waiters must fail with WebSocketResetError on reconnect."""

    @pytest.mark.asyncio
    async def test_close_session_cancels_all_state_waiters(self):
        from app.ha_client.websocket import WebSocketResetError

        client = HAWebSocketClient()
        fut1 = client.register_state_waiter("light.keller", expected="off")
        fut2 = client.register_state_waiter("switch.kitchen", expected=None)

        await client._close_session()

        # Both pending futures must now resolve so awaiters can fall back
        # to REST polling instead of hanging forever.
        with pytest.raises(WebSocketResetError):
            await asyncio.wait_for(fut1, timeout=0.1)
        with pytest.raises(WebSocketResetError):
            await asyncio.wait_for(fut2, timeout=0.1)
        # Waiter map is cleared so the reconnect starts fresh.
        assert client._state_waiters == {}

    @pytest.mark.asyncio
    async def test_close_session_no_waiters_does_not_raise(self):
        client = HAWebSocketClient()
        # Calling on an empty waiter map must be a no-op (no exceptions).
        await client._close_session()
        assert client._state_waiters == {}

    @pytest.mark.asyncio
    async def test_close_session_skips_already_done_futures(self):
        from app.ha_client.websocket import WebSocketResetError

        client = HAWebSocketClient()
        fut = client.register_state_waiter("light.keller", expected="off")
        # Pre-resolve the future before close_session runs.
        fut.set_result("off")

        await client._close_session()

        # Result is preserved -- close did not clobber it with the exception.
        assert fut.result() == "off"
        # And the second close still works without raising WebSocketResetError
        # against a finished future.
        assert client._state_waiters == {}
        del WebSocketResetError  # silence unused-import lint when assertion above hits


class TestReceiveLoop:
    @pytest.mark.asyncio
    async def test_receive_loop_propagates_cancelled_error(self):
        """CONT-5.2: _receive_loop must propagate asyncio.CancelledError."""
        from unittest.mock import AsyncMock, MagicMock

        client = HAWebSocketClient()
        client._running = True

        mock_ws = MagicMock()
        mock_ws.closed = False
        mock_ws.receive = AsyncMock(side_effect=asyncio.CancelledError("stop"))
        client._ws = mock_ws

        with pytest.raises(asyncio.CancelledError):
            await client._receive_loop()

    @pytest.mark.asyncio
    async def test_websocket_connector_limits(self):
        """CONT-4.4: connect() must create ClientSession with bounded TCPConnector."""
        from unittest.mock import AsyncMock, MagicMock, patch

        client = HAWebSocketClient()

        mock_settings = AsyncMock(return_value="http://homeassistant:8123")
        mock_token = AsyncMock(return_value="fake-token")

        captured_connector = None
        captured_session = None

        def _capture_session(*, connector, **kwargs):
            nonlocal captured_connector, captured_session
            captured_connector = connector
            mock_ws = MagicMock()
            mock_ws.closed = False
            mock_ws.receive_json = AsyncMock(
                side_effect=[
                    {"type": "auth_required"},
                    {"type": "auth_ok"},
                ]
            )
            mock_ws.send_json = AsyncMock()
            mock_ws.close = AsyncMock()
            mock_session = MagicMock()
            mock_session.ws_connect = AsyncMock(return_value=mock_ws)
            captured_session = mock_session
            return mock_session

        with (
            patch("app.ha_client.websocket.SettingsRepository.get_value", mock_settings),
            patch("app.ha_client.websocket.get_ha_token", mock_token),
            patch("aiohttp.ClientSession", side_effect=_capture_session),
            patch("aiohttp.TCPConnector") as mock_connector_cls,
        ):
            mock_connector_cls.return_value = MagicMock()
            result = await client.connect()

        assert result is True
        mock_connector_cls.assert_called_once_with(limit=10, limit_per_host=5, enable_cleanup_closed=True)
