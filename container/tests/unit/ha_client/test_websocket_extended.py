"""Extended tests for app.ha_client.websocket."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from app.ha_client.websocket import HAWebSocketClient

pytestmark = pytest.mark.asyncio


class TestReceiveLoop:
    async def test_receive_loop_text_event_and_callbacks(self):
        """WSMsgType.TEXT dispatches state_changed waiters and invokes callbacks."""
        client = HAWebSocketClient()
        client._running = True

        ws_mock = MagicMock()
        ws_mock.closed = False

        callback = MagicMock()
        client.on_event("state_changed", callback)

        # Simulate a state_changed event
        msg = MagicMock()
        msg.type = aiohttp.WSMsgType.TEXT
        msg.data = json.dumps(
            {
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "light.kitchen",
                        "new_state": {"state": "on"},
                    },
                },
            }
        )

        # Stop the loop after one message
        msg_close = MagicMock()
        msg_close.type = aiohttp.WSMsgType.CLOSE

        ws_mock.receive = AsyncMock(side_effect=[msg, msg_close])
        client._ws = ws_mock

        future = client.register_state_waiter("light.kitchen")

        await client._receive_loop()

        assert future.done()
        assert future.result() == "on"
        callback.assert_called_once()

    async def test_receive_loop_errors_and_close(self):
        """TimeoutError, JSONDecodeError, CLOSE/CLOSING/CLOSED/ERROR all break the loop."""
        client = HAWebSocketClient()
        client._running = True

        ws_mock = MagicMock()
        ws_mock.closed = False

        # Scenario 1: TimeoutError breaks loop
        ws_mock.receive = AsyncMock(side_effect=TimeoutError)
        client._ws = ws_mock
        await client._receive_loop()

        # Scenario 2: JSONDecodeError logs and continues, then CLOSE breaks
        client._running = True
        msg_bad = MagicMock()
        msg_bad.type = aiohttp.WSMsgType.TEXT
        msg_bad.data = "not-json{{"
        msg_close = MagicMock()
        msg_close.type = aiohttp.WSMsgType.CLOSE
        ws_mock.receive = AsyncMock(side_effect=[msg_bad, msg_close])
        client._ws = ws_mock
        await client._receive_loop()

        # Scenario 3: CLOSING breaks
        client._running = True
        msg_closing = MagicMock()
        msg_closing.type = aiohttp.WSMsgType.CLOSING
        ws_mock.receive = AsyncMock(return_value=msg_closing)
        client._ws = ws_mock
        await client._receive_loop()

        # Scenario 4: CLOSED breaks
        client._running = True
        msg_closed = MagicMock()
        msg_closed.type = aiohttp.WSMsgType.CLOSED
        ws_mock.receive = AsyncMock(return_value=msg_closed)
        client._ws = ws_mock
        await client._receive_loop()

        # Scenario 5: ERROR breaks
        client._running = True
        msg_error = MagicMock()
        msg_error.type = aiohttp.WSMsgType.ERROR
        ws_mock.receive = AsyncMock(return_value=msg_error)
        client._ws = ws_mock
        await client._receive_loop()


class TestSendCommand:
    async def test_send_command_lifecycle(self):
        """Unconnected returns None; success path; TimeoutError; generic exception; success=False."""
        client = HAWebSocketClient()

        # Unconnected
        assert await client.send_command("test") is None

        ws_mock = MagicMock()
        ws_mock.closed = False
        ws_mock.send_json = AsyncMock()
        client._ws = ws_mock
        client._running = True

        # Success path
        with (
            patch.object(client, "_next_id", return_value=1),
            patch(
                "asyncio.wait_for", new_callable=AsyncMock, return_value={"success": True, "result": {"key": "value"}}
            ),
        ):
            result = await client.send_command("test")
        assert result == {"key": "value"}

        # TimeoutError
        client._pending_responses.clear()
        with patch.object(client, "_next_id", return_value=2), patch("asyncio.wait_for", side_effect=TimeoutError):
            result = await client.send_command("test")
        assert result is None
        assert 2 not in client._pending_responses

        # Generic exception
        client._pending_responses.clear()
        with (
            patch.object(client, "_next_id", return_value=3),
            patch("asyncio.wait_for", side_effect=RuntimeError("boom")),
        ):
            result = await client.send_command("test")
        assert result is None
        assert 3 not in client._pending_responses

        # success=False
        client._pending_responses.clear()
        with (
            patch.object(client, "_next_id", return_value=4),
            patch("asyncio.wait_for", new_callable=AsyncMock, return_value={"success": False, "error": "nope"}),
        ):
            result = await client.send_command("test")
        assert result is None
