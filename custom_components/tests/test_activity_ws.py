"""Tests for the activity WebSocket command, card registration and wiring."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.tests._lazy import lazy

ha = lazy("custom_components.ha_agenthub")
activity = lazy("custom_components.ha_agenthub.activity")
activity_ws = lazy("custom_components.ha_agenthub.activity_ws")
card = lazy("custom_components.ha_agenthub.card")
const = lazy("custom_components.ha_agenthub.const")
from custom_components.tests.test_activity import (
    FakeResponse,
    FakeSession,
    SleepRecorder,
    _make_hass,
    _wait_until,
)

SUBSCRIBE_MSG = {"id": 7, "type": "ha_agenthub/activity/subscribe"}


class FakeConnection:
    def __init__(self):
        self.subscriptions: dict[int, object] = {}
        self.results: list[int] = []
        self.errors: list[tuple[int, str, str]] = []
        self.sent: list[dict] = []

    def send_result(self, msg_id, result=None):
        self.results.append(msg_id)

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))

    def send_message(self, message):
        self.sent.append(message)

    @property
    def events(self):
        return [m["event"] for m in self.sent]


class FakeRelay:
    """Relay stand-in: records subscribers, lets tests emit events by hand."""

    def __init__(self, agents=None, connected=True, fetch_gate=None):
        self.agents = agents if agents is not None else []
        self.connected = connected
        self.fetch_gate = fetch_gate
        self.subscribers: list = []
        self.unsubscribed = 0

    def async_subscribe(self, subscriber):
        self.subscribers.append(subscriber)

        def _unsub():
            self.unsubscribed += 1
            self.subscribers.remove(subscriber)

        return _unsub

    async def async_fetch_recent(self, limit=7):
        if self.fetch_gate is not None:
            await self.fetch_gate.wait()
        return self.agents

    def emit(self, message):
        for subscriber in list(self.subscribers):
            subscriber(message)


def _entry(entry_id, relay, loaded_runtime=True):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = (
        ha.AgentHubRuntimeData(url="http://x", api_key="k", activity=relay)
        if loaded_runtime
        else None
    )
    return entry


def _hass_with(*entries):
    hass = MagicMock()
    hass.config_entries.async_loaded_entries.return_value = list(entries)
    return hass


# ---------------------------------------------------------------------------
# WebSocket command
# ---------------------------------------------------------------------------


class TestSubscribeCommand:
    def test_command_schema_and_registration_name(self):
        schema = activity_ws.ws_activity_subscribe._ws_schema
        assert "ha_agenthub/activity/subscribe" in schema.values()

    @pytest.mark.asyncio
    async def test_result_then_snapshot_first(self):
        agents = [
            {
                "agent_id": "light-agent",
                "name": "Light",
                "last_used": "2026-01-01T00:00:00Z",
            }
        ]
        relay = FakeRelay(agents=agents, connected=True)
        connection = FakeConnection()

        await activity_ws.ws_activity_subscribe(
            _hass_with(_entry("e1", relay)), connection, dict(SUBSCRIBE_MSG)
        )

        assert connection.results == [7]
        assert connection.errors == []
        assert connection.sent[0] == {
            "id": 7,
            "type": "event",
            "event": {"type": "snapshot", "agents": agents, "connected": True},
        }
        assert 7 in connection.subscriptions

    @pytest.mark.asyncio
    async def test_snapshot_reports_disconnected_state_and_empty_list(self):
        relay = FakeRelay(agents=[], connected=False)
        connection = FakeConnection()
        await activity_ws.ws_activity_subscribe(
            _hass_with(_entry("e1", relay)), connection, dict(SUBSCRIBE_MSG)
        )
        assert connection.events == [
            {"type": "snapshot", "agents": [], "connected": False}
        ]

    @pytest.mark.asyncio
    async def test_events_are_forwarded_after_snapshot(self):
        relay = FakeRelay()
        connection = FakeConnection()
        await activity_ws.ws_activity_subscribe(
            _hass_with(_entry("e1", relay)), connection, dict(SUBSCRIBE_MSG)
        )

        relay.emit(
            {"type": "activity", "event": {"type": "span_start", "span_id": "s1"}}
        )
        relay.emit({"type": "status", "connected": False})

        assert connection.events[1:] == [
            {"type": "activity", "event": {"type": "span_start", "span_id": "s1"}},
            {"type": "status", "connected": False},
        ]

    @pytest.mark.asyncio
    async def test_events_during_snapshot_fetch_are_queued_behind_it(self):
        gate = asyncio.Event()
        relay = FakeRelay(fetch_gate=gate)
        connection = FakeConnection()
        task = asyncio.ensure_future(
            activity_ws.ws_activity_subscribe(
                _hass_with(_entry("e1", relay)), connection, dict(SUBSCRIBE_MSG)
            )
        )
        assert await _wait_until(lambda: connection.results == [7])
        relay.emit({"type": "activity", "event": {"span_id": "early"}})
        assert connection.sent == []

        gate.set()
        await task

        assert [event["type"] for event in connection.events] == [
            "snapshot",
            "activity",
        ]
        assert connection.events[1]["event"] == {"span_id": "early"}

    @pytest.mark.asyncio
    async def test_unsubscribe_cleans_up_and_stops_forwarding(self):
        relay = FakeRelay()
        connection = FakeConnection()
        await activity_ws.ws_activity_subscribe(
            _hass_with(_entry("e1", relay)), connection, dict(SUBSCRIBE_MSG)
        )
        assert len(relay.subscribers) == 1

        connection.subscriptions[7]()

        assert relay.unsubscribed == 1
        assert relay.subscribers == []
        sent_before = len(connection.sent)
        relay.emit({"type": "status", "connected": True})
        assert len(connection.sent) == sent_before

    @pytest.mark.asyncio
    async def test_unsubscribe_during_snapshot_fetch_sends_no_snapshot(self):
        gate = asyncio.Event()
        relay = FakeRelay(fetch_gate=gate)
        connection = FakeConnection()
        task = asyncio.ensure_future(
            activity_ws.ws_activity_subscribe(
                _hass_with(_entry("e1", relay)), connection, dict(SUBSCRIBE_MSG)
            )
        )
        assert await _wait_until(lambda: 7 in connection.subscriptions)
        connection.subscriptions[7]()
        gate.set()
        await task
        assert connection.sent == []
        assert relay.unsubscribed == 1

    @pytest.mark.asyncio
    async def test_no_loaded_entry_returns_not_loaded_error(self):
        connection = FakeConnection()
        await activity_ws.ws_activity_subscribe(
            _hass_with(), connection, dict(SUBSCRIBE_MSG)
        )
        assert connection.errors == [(7, "not_loaded", "HA-AgentHub is not loaded")]
        assert connection.results == []
        assert connection.subscriptions == {}

    @pytest.mark.asyncio
    async def test_entry_without_runtime_data_is_not_loaded(self):
        connection = FakeConnection()
        hass = _hass_with(_entry("e1", None, loaded_runtime=False))
        await activity_ws.ws_activity_subscribe(hass, connection, dict(SUBSCRIBE_MSG))
        assert connection.errors[0][1] == "not_loaded"

    @pytest.mark.asyncio
    async def test_first_loaded_entry_is_used_by_default(self):
        first, second = FakeRelay(), FakeRelay()
        hass = _hass_with(_entry("e1", first), _entry("e2", second))
        await activity_ws.ws_activity_subscribe(
            hass, FakeConnection(), dict(SUBSCRIBE_MSG)
        )
        assert len(first.subscribers) == 1
        assert second.subscribers == []

    @pytest.mark.asyncio
    async def test_explicit_entry_id_selects_that_entry(self):
        first, second = FakeRelay(), FakeRelay()
        hass = _hass_with(_entry("e1", first), _entry("e2", second))
        await activity_ws.ws_activity_subscribe(
            hass, FakeConnection(), {**SUBSCRIBE_MSG, "entry_id": "e2"}
        )
        assert first.subscribers == []
        assert len(second.subscribers) == 1

    @pytest.mark.asyncio
    async def test_unknown_entry_id_does_not_fall_back(self):
        relay = FakeRelay()
        connection = FakeConnection()
        hass = _hass_with(_entry("e1", relay))
        await activity_ws.ws_activity_subscribe(
            hass, connection, {**SUBSCRIBE_MSG, "entry_id": "nope"}
        )
        assert connection.errors[0][1] == "not_loaded"
        assert relay.subscribers == []

    @pytest.mark.asyncio
    async def test_end_to_end_with_real_relay(self):
        chunks = [
            b'event: activity\ndata: {"type": "span_end", "agent_id": "light-agent"}\n\n'
        ]
        session = FakeSession([FakeResponse(chunks=chunks, hold=True)])
        relay = activity.ActivityRelay(
            _make_hass(), "http://container.test", "k", sleep=SleepRecorder()
        )
        recent = {
            "agents": [{"agent_id": "light-agent", "name": "Light", "last_used": None}]
        }
        connection = FakeConnection()
        with (
            patch.object(activity, "async_get_clientsession", return_value=session),
            patch.object(
                activity, "async_get_json", new=AsyncMock(return_value=recent)
            ),
            patch.object(
                activity.aiohttp,
                "ClientTimeout",
                side_effect=lambda **kw: SimpleNamespace(**kw),
            ),
        ):
            await activity_ws.ws_activity_subscribe(
                _hass_with(_entry("e1", relay)), connection, dict(SUBSCRIBE_MSG)
            )
            expected = {
                "type": "activity",
                "event": {"type": "span_end", "agent_id": "light-agent"},
            }
            assert await _wait_until(lambda: expected in connection.events)
            assert connection.events[0]["type"] == "snapshot"
            assert connection.events[0]["agents"] == recent["agents"]

            connection.subscriptions[7]()
            assert relay.connected is False
            assert relay.subscriber_count == 0


class TestRegistration:
    def test_command_registered_once(self):
        hass = MagicMock()
        hass.data = {}
        with patch.object(
            activity_ws.websocket_api, "async_register_command"
        ) as register:
            activity_ws.async_register_activity_websocket(hass)
            activity_ws.async_register_activity_websocket(hass)
        register.assert_called_once_with(hass, activity_ws.ws_activity_subscribe)


# ---------------------------------------------------------------------------
# Integration wiring
# ---------------------------------------------------------------------------


class TestWiring:
    @pytest.mark.asyncio
    async def test_async_setup_registers_command_and_card(self):
        hass = MagicMock()
        with (
            patch(
                "custom_components.ha_agenthub.async_register_activity_websocket"
            ) as register_ws,
            patch(
                "custom_components.ha_agenthub.async_register_card", new=AsyncMock()
            ) as register_card,
        ):
            assert await ha.async_setup(hass, {}) is True
        register_ws.assert_called_once_with(hass)
        register_card.assert_awaited_once_with(hass)

    @pytest.mark.asyncio
    async def test_entry_setup_creates_relay_and_unload_stops_it(self):
        hass = MagicMock()
        hass.data = {}
        hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.title = "HA-AgentHub"
        entry.data = {"url": "http://example.com/", "api_key": "key"}
        entry.options = {}

        with patch(
            "custom_components.ha_agenthub._async_probe_container", new=AsyncMock()
        ):
            assert await ha.async_setup_entry(hass, entry) is True
        relay = entry.runtime_data.activity
        assert isinstance(relay, activity.ActivityRelay)
        # No upstream connection until a card subscribes.
        hass.async_create_background_task.assert_not_called()
        assert relay.connected is False

        with patch.object(relay, "async_stop", new=AsyncMock()) as stop:
            assert await ha.async_unload_entry(hass, entry) is True
        stop.assert_awaited_once()
