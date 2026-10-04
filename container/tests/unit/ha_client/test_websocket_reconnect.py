"""Regression tests for HAWebSocketClient connect/reconnect lifecycle."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ha_client.websocket import HAWebSocketClient

pytestmark = pytest.mark.asyncio


def _make_session(receive_json_side_effect):
    """Build a fake aiohttp session whose ws_connect returns a scripted fake ws."""
    ws = MagicMock()
    ws.closed = False
    ws.receive_json = AsyncMock(side_effect=receive_json_side_effect)
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    session = MagicMock()
    session.closed = False
    session.ws_connect = AsyncMock(return_value=ws)
    session.close = AsyncMock()
    return session, ws


def _auth_ok_messages():
    return [{"type": "auth_required"}, {"type": "auth_ok"}]


def _connect_env(session_factory):
    """Patch settings, token, aiohttp session/connector, and backoff sleeps."""
    return (
        patch(
            "app.ha_client.websocket.SettingsRepository.get_value",
            new_callable=AsyncMock,
            side_effect=lambda key: "http://ha.local" if key == "ha_url" else None,
        ),
        patch("app.ha_client.websocket.get_ha_token", new_callable=AsyncMock, return_value="tok"),
        patch("aiohttp.ClientSession", side_effect=session_factory),
        patch("aiohttp.TCPConnector"),
        patch("app.ha_client.websocket.asyncio.sleep", new_callable=AsyncMock),
    )


class TestRunDoesNotDoubleConnect:
    async def test_failed_connect_then_reconnect_calls_connect_twice(self):
        client = HAWebSocketClient()
        connect_calls = 0

        async def _connect():
            nonlocal connect_calls
            connect_calls += 1
            return connect_calls >= 2

        receive_calls = 0

        async def _receive():
            nonlocal receive_calls
            receive_calls += 1
            client._running = False

        client.connect = AsyncMock(side_effect=_connect)
        client._receive_loop = AsyncMock(side_effect=_receive)

        with patch("app.ha_client.websocket.asyncio.sleep", new_callable=AsyncMock):
            await asyncio.wait_for(client.run(), timeout=2.0)

        assert connect_calls == 2
        assert receive_calls == 1

    async def test_receive_error_reconnect_does_not_leak_sessions(self):
        """After a receive-loop error, exactly one new session is opened and the old one is closed."""
        client = HAWebSocketClient()
        sessions: list[MagicMock] = []

        def _factory(*_args, **_kwargs):
            session, _ws = _make_session(_auth_ok_messages())
            sessions.append(session)
            return session

        receive_calls = 0
        open_sessions_seen: list[int] = []

        async def _receive():
            nonlocal receive_calls
            receive_calls += 1
            open_sessions_seen.append(sum(1 for s in sessions if s.close.await_count == 0))
            if receive_calls == 1:
                raise RuntimeError("connection lost")
            client._running = False

        client._receive_loop = AsyncMock(side_effect=_receive)
        p1, p2, p3, p4, p5 = _connect_env(_factory)
        with p1, p2, p3, p4, p5:
            await asyncio.wait_for(client.run(), timeout=2.0)

        assert len(sessions) == 2
        assert receive_calls == 2
        # Only the live session is open whenever the receive loop runs.
        assert open_sessions_seen == [1, 1]
        sessions[0].close.assert_awaited_once()


class TestConnectHandshakeErrors:
    @pytest.mark.parametrize(
        "exc",
        [
            TypeError("Received message 8:1000 is not WSMsgType.TEXT"),
            json.JSONDecodeError("bad json", "x", 0),
            ValueError("bad payload"),
            AttributeError("'list' object has no attribute 'get'"),
            KeyError("type"),
        ],
    )
    async def test_handshake_error_returns_false_and_closes_session(self, exc):
        client = HAWebSocketClient()
        session, ws = _make_session([exc])

        p1, p2, p3, p4, p5 = _connect_env(lambda *a, **k: session)
        with p1, p2, p3, p4, p5:
            result = await asyncio.wait_for(client.connect(), timeout=2.0)

        assert result is False
        assert client._ws is None
        assert client._session is None
        assert client._ws_lock.locked() is False
        ws.close.assert_awaited_once()
        session.close.assert_awaited_once()

    async def test_run_keeps_retrying_after_handshake_type_error(self):
        client = HAWebSocketClient()
        sessions: list[MagicMock] = []
        scripts = [
            [TypeError("Received message 8:1000 is not WSMsgType.TEXT")],
            [ValueError("bad json")],
            _auth_ok_messages(),
        ]

        def _factory(*_args, **_kwargs):
            session, _ws = _make_session(scripts[len(sessions)])
            sessions.append(session)
            return session

        async def _receive():
            client._running = False

        client._receive_loop = AsyncMock(side_effect=_receive)
        p1, p2, p3, p4, p5 = _connect_env(_factory)
        with p1, p2, p3, p4, p5:
            await asyncio.wait_for(client.run(), timeout=2.0)

        assert len(sessions) == 3
        client._receive_loop.assert_awaited_once()
        sessions[0].close.assert_awaited_once()
        sessions[1].close.assert_awaited_once()

    async def test_run_survives_unexpected_error_outside_handshake(self):
        """An error raised before the try block (e.g. settings lookup) must not kill run()."""
        client = HAWebSocketClient()
        calls = 0

        async def _connect():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("settings unavailable")
            return True

        async def _receive():
            client._running = False

        client.connect = AsyncMock(side_effect=_connect)
        client._receive_loop = AsyncMock(side_effect=_receive)
        with patch("app.ha_client.websocket.asyncio.sleep", new_callable=AsyncMock):
            await asyncio.wait_for(client.run(), timeout=2.0)

        assert calls == 2
        client._receive_loop.assert_awaited_once()


class TestConnectDoesNotOwnRunningFlag:
    async def test_connect_does_not_revive_after_disconnect(self):
        client = HAWebSocketClient()
        session, _ws = _make_session(_auth_ok_messages())

        p1, p2, p3, p4, p5 = _connect_env(lambda *a, **k: session)
        with p1, p2, p3, p4, p5:
            result = await client.connect()

        assert result is True
        assert client._running is False
        assert client.is_connected() is False
