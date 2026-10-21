"""Tests for the activity relay: SSE parser and upstream connection manager."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.tests._lazy import lazy

activity = lazy("custom_components.ha_agenthub.activity")
client = lazy("custom_components.ha_agenthub.client")
const = lazy("custom_components.ha_agenthub.const")

URL = "http://container.test:8080"
STREAM_URL = f"{URL}/api/activity/stream"


def _sse(payload: str, event: str = "activity") -> bytes:
    return f"event: {event}\ndata: {payload}\n\n".encode()


async def _wait_until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


# ---------------------------------------------------------------------------
# SSE parser
# ---------------------------------------------------------------------------


class TestSSEParser:
    def test_single_event(self):
        events = activity.SSEParser().feed(b'event: activity\ndata: {"a": 1}\n\n')
        assert [(e.event, e.data) for e in events] == [("activity", '{"a": 1}')]

    def test_default_event_name_is_message(self):
        events = activity.SSEParser().feed(b"data: hello\n\n")
        assert events[0].event == "message"

    def test_multi_line_data_is_joined(self):
        events = activity.SSEParser().feed(b"data: one\ndata: two\ndata:three\n\n")
        assert events[0].data == "one\ntwo\nthree"

    def test_comments_and_keep_alive_are_ignored(self):
        parser = activity.SSEParser()
        assert parser.feed(b": keep-alive\n\n") == []
        events = parser.feed(b": note\nevent: activity\ndata: x\n\n")
        assert [e.data for e in events] == ["x"]

    def test_event_without_data_is_not_dispatched(self):
        parser = activity.SSEParser()
        assert parser.feed(b"event: activity\n\n") == []
        # The event name does not leak into the next event.
        assert parser.feed(b"data: x\n\n")[0].event == "message"

    def test_partial_chunks_byte_by_byte(self):
        payload = 'event: activity\ndata: {"name": "café"}\n\nevent: activity\ndata: 2\n\n'.encode()
        parser = activity.SSEParser()
        events = []
        for index in range(len(payload)):
            events.extend(parser.feed(payload[index : index + 1]))
        assert [e.data for e in events] == ['{"name": "café"}', "2"]

    def test_crlf_split_across_chunks(self):
        parser = activity.SSEParser()
        assert parser.feed(b"data: a\r") == []
        assert parser.feed(b"\n\r") == []
        events = parser.feed(b"\n")
        assert [e.data for e in events] == ["a"]

    def test_cr_only_line_endings(self):
        events = activity.SSEParser().feed(b"data: a\r\rdata: b\r\rdata: c")
        assert [e.data for e in events] == ["a", "b"]

    def test_retry_hint_is_recorded(self):
        parser = activity.SSEParser()
        parser.feed(b"retry: 3000\n\n")
        assert parser.retry_ms == 3000
        parser.feed(b"retry: soon\n\n")
        assert parser.retry_ms == 3000

    def test_bom_is_stripped(self):
        events = activity.SSEParser().feed(b"\xef\xbb\xbfdata: a\n\n")
        assert events[0].data == "a"

    def test_oversized_line_is_discarded_and_stream_recovers(self):
        parser = activity.SSEParser(max_line_chars=16)
        assert parser.feed(b"data: " + b"x" * 64) == []
        # The rest of the oversized line is dropped, the next event parses.
        events = parser.feed(b"tail\ndata: ok\n\n")
        assert [e.data for e in events] == ["ok"]

    def test_event_id_is_carried(self):
        events = activity.SSEParser().feed(b"id: 7\ndata: a\n\n")
        assert events[0].event_id == "7"


# ---------------------------------------------------------------------------
# Relay fakes
# ---------------------------------------------------------------------------


class FakeContent:
    def __init__(self, response):
        self._response = response

    async def iter_any(self):
        for chunk in self._response.chunks:
            await asyncio.sleep(0)
            yield chunk
        if self._response.error is not None:
            raise self._response.error
        if self._response.hold:
            await asyncio.Event().wait()


class FakeResponse:
    """Scripted stream response; ``hold`` keeps the stream open after the chunks."""

    def __init__(self, status=200, chunks=(), error=None, hold=False):
        self.status = status
        self.chunks = list(chunks)
        self.error = error
        self.hold = hold
        self.closed = False
        self.content = FakeContent(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True
        return False


class FakeSession:
    """Fake aiohttp session handing out scripted responses per ``get`` call."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, BaseException):
            raise item
        return item


class SleepRecorder:
    """Replacement for asyncio.sleep: records delays, blocks after ``max_calls``."""

    def __init__(self, max_calls=1000):
        self.delays: list[float] = []
        self.max_calls = max_calls

    async def __call__(self, delay):
        self.delays.append(delay)
        if len(self.delays) >= self.max_calls:
            await asyncio.Event().wait()
        await asyncio.sleep(0)


def _make_hass():
    hass = MagicMock()
    hass.async_create_background_task = MagicMock(
        side_effect=lambda coro, name=None: asyncio.ensure_future(coro)
    )
    return hass


@pytest.fixture
def make_relay():
    relays = []
    patches = []

    def _make(script, sleep=None):
        session = FakeSession(script)
        patcher = patch.object(
            activity, "async_get_clientsession", return_value=session
        )
        timeout_patcher = patch.object(
            activity.aiohttp,
            "ClientTimeout",
            side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
        )
        patcher.start()
        timeout_patcher.start()
        patches.extend([patcher, timeout_patcher])
        relay = activity.ActivityRelay(
            _make_hass(), URL + "/", "secret", sleep=sleep or SleepRecorder()
        )
        relays.append(relay)
        return relay, session

    yield _make

    for patcher in patches:
        patcher.stop()


def _collector():
    messages: list[dict] = []
    return messages, messages.append


# ---------------------------------------------------------------------------
# Relay lifecycle
# ---------------------------------------------------------------------------


class TestRelayConnection:
    @pytest.mark.asyncio
    async def test_no_connection_without_subscriber(self, make_relay):
        relay, session = make_relay([FakeResponse(hold=True)])
        await asyncio.sleep(0.02)
        assert session.calls == []
        assert relay.connected is False

    @pytest.mark.asyncio
    async def test_connects_on_first_subscriber_with_auth_and_timeouts(
        self, make_relay
    ):
        response = FakeResponse(chunks=[b": keep-alive\n\n"], hold=True)
        relay, session = make_relay([response])
        messages, callback = _collector()
        unsub = relay.async_subscribe(callback)
        assert await _wait_until(lambda: relay.connected)
        url, kwargs = session.calls[0]
        assert url == STREAM_URL
        assert kwargs["headers"]["Authorization"] == "Bearer secret"
        assert kwargs["headers"]["Accept"] == "text/event-stream"
        assert kwargs["timeout"].total is None
        assert kwargs["timeout"].sock_read == const.ACTIVITY_SOCK_READ_TIMEOUT
        assert {"type": "status", "connected": True} in messages
        unsub()

    @pytest.mark.asyncio
    async def test_events_fan_out_to_all_subscribers_over_one_connection(
        self, make_relay
    ):
        response = FakeResponse(
            chunks=[_sse('{"type": "span_start", "agent_id": "light-agent"}')],
            hold=True,
        )
        relay, session = make_relay([response])
        first, first_cb = _collector()
        second, second_cb = _collector()
        unsub_first = relay.async_subscribe(first_cb)
        unsub_second = relay.async_subscribe(second_cb)
        expected = {
            "type": "activity",
            "event": {"type": "span_start", "agent_id": "light-agent"},
        }
        assert await _wait_until(lambda: expected in first and expected in second)
        assert len(session.calls) == 1
        assert relay.subscriber_count == 2
        unsub_first()
        unsub_second()

    @pytest.mark.asyncio
    async def test_upstream_closed_after_last_unsubscribe(self, make_relay):
        response = FakeResponse(hold=True)
        relay, _ = make_relay([response])
        unsub_a = relay.async_subscribe(lambda message: None)
        unsub_b = relay.async_subscribe(lambda message: None)
        assert await _wait_until(lambda: relay.connected)
        task = relay._task

        unsub_a()
        await asyncio.sleep(0.02)
        assert response.closed is False
        assert relay.connected is True

        unsub_b()
        assert relay.connected is False
        assert await _wait_until(lambda: task.done())
        assert response.closed is True
        assert relay._task is None

        # Unsubscribing twice is harmless.
        unsub_b()

    @pytest.mark.asyncio
    async def test_resubscribe_opens_a_new_connection(self, make_relay):
        relay, session = make_relay([FakeResponse(hold=True), FakeResponse(hold=True)])
        first_unsub = relay.async_subscribe(lambda message: None)
        assert await _wait_until(lambda: relay.connected)
        first_unsub()
        assert relay.connected is False
        unsub = relay.async_subscribe(lambda message: None)
        assert await _wait_until(lambda: len(session.calls) == 2 and relay.connected)
        unsub()

    @pytest.mark.asyncio
    async def test_status_events_follow_connection_state(self, make_relay):
        sleep = SleepRecorder(max_calls=1)
        relay, _ = make_relay([FakeResponse(chunks=[b": hi\n\n"])], sleep=sleep)
        messages, callback = _collector()
        unsub = relay.async_subscribe(callback)
        assert await _wait_until(lambda: len(sleep.delays) == 1)
        # Clean end of the stream: connected, then disconnected, no duplicates.
        statuses = [m["connected"] for m in messages if m["type"] == "status"]
        assert statuses == [True, False]
        unsub()

    @pytest.mark.asyncio
    async def test_malformed_and_foreign_events_are_skipped(self, make_relay):
        chunks = [
            _sse("{not json"),
            _sse('"just a string"'),
            _sse('{"x": 1}', event="other"),
            b": keep-alive\n\n",
            _sse('{"ok": true}'),
        ]
        relay, _ = make_relay([FakeResponse(chunks=chunks, hold=True)])
        messages, callback = _collector()
        unsub = relay.async_subscribe(callback)
        assert await _wait_until(
            lambda: {"type": "activity", "event": {"ok": True}} in messages
        )
        assert [m for m in messages if m["type"] == "activity"] == [
            {"type": "activity", "event": {"ok": True}}
        ]
        unsub()

    @pytest.mark.asyncio
    async def test_event_split_across_chunks(self, make_relay):
        chunks = [b"event: activity\nda", b'ta: {"a"', b": 1}\n", b"\n"]
        relay, _ = make_relay([FakeResponse(chunks=chunks, hold=True)])
        messages, callback = _collector()
        unsub = relay.async_subscribe(callback)
        assert await _wait_until(
            lambda: {"type": "activity", "event": {"a": 1}} in messages
        )
        unsub()

    @pytest.mark.asyncio
    async def test_failing_subscriber_does_not_break_others(self, make_relay):
        relay, _ = make_relay([FakeResponse(chunks=[_sse('{"n": 1}')], hold=True)])
        good, good_cb = _collector()

        def bad(message):
            raise RuntimeError("boom")

        unsub_bad = relay.async_subscribe(bad)
        unsub_good = relay.async_subscribe(good_cb)
        assert await _wait_until(
            lambda: {"type": "activity", "event": {"n": 1}} in good
        )
        unsub_bad()
        unsub_good()


class TestRelayBackoff:
    @pytest.mark.asyncio
    async def test_backoff_doubles_to_cap(self, make_relay):
        sleep = SleepRecorder(max_calls=9)
        relay, _ = make_relay([ConnectionRefusedError("down")], sleep=sleep)
        unsub = relay.async_subscribe(lambda message: None)
        assert await _wait_until(lambda: len(sleep.delays) == 9)
        assert sleep.delays == [2, 4, 8, 16, 32, 60, 60, 60, 60]
        assert relay.connected is False
        unsub()

    @pytest.mark.asyncio
    async def test_backoff_resets_after_first_byte(self, make_relay):
        sleep = SleepRecorder(max_calls=4)
        script = [
            ConnectionRefusedError("down"),
            ConnectionRefusedError("down"),
            FakeResponse(chunks=[b": hi\n\n"]),  # data, then closed by the container
            ConnectionRefusedError("down"),
        ]
        relay, _ = make_relay(script, sleep=sleep)
        unsub = relay.async_subscribe(lambda message: None)
        assert await _wait_until(lambda: len(sleep.delays) == 4)
        # failures: 2, 4; connect with data resets; clean end waits the base delay again.
        assert sleep.delays[:3] == [2, 4, const.ACTIVITY_BACKOFF_BASE]
        assert sleep.delays[3] == 4
        unsub()

    @pytest.mark.asyncio
    async def test_stream_error_after_data_reconnects(self, make_relay):
        sleep = SleepRecorder(max_calls=1)
        script = [
            FakeResponse(chunks=[_sse('{"a": 1}')], error=TimeoutError()),
            FakeResponse(hold=True),
        ]
        relay, _ = make_relay(script, sleep=sleep)
        messages, callback = _collector()
        unsub = relay.async_subscribe(callback)
        assert await _wait_until(lambda: len(sleep.delays) == 1)
        assert sleep.delays == [const.ACTIVITY_BACKOFF_BASE]
        assert {"type": "status", "connected": False} in messages
        unsub()

    @pytest.mark.asyncio
    async def test_unauthorized_logs_once_and_backs_off_at_cap(
        self, make_relay, caplog
    ):
        sleep = SleepRecorder(max_calls=3)
        relay, _ = make_relay([FakeResponse(status=401)], sleep=sleep)
        messages, callback = _collector()
        with caplog.at_level(logging.DEBUG, logger=activity.logger.name):
            unsub = relay.async_subscribe(callback)
            assert await _wait_until(lambda: len(sleep.delays) == 3)
        assert sleep.delays == [const.ACTIVITY_BACKOFF_MAX] * 3
        assert relay.connected is False
        assert [m for m in messages if m["type"] == "status"] == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "rejected the API key" in warnings[0].getMessage()
        assert "secret" not in caplog.text
        unsub()

    @pytest.mark.asyncio
    async def test_forbidden_is_treated_like_unauthorized(self, make_relay):
        sleep = SleepRecorder(max_calls=1)
        relay, _ = make_relay([FakeResponse(status=403)], sleep=sleep)
        unsub = relay.async_subscribe(lambda message: None)
        assert await _wait_until(lambda: len(sleep.delays) == 1)
        assert sleep.delays == [const.ACTIVITY_BACKOFF_MAX]
        unsub()

    @pytest.mark.asyncio
    async def test_not_found_warns_once_with_hint(self, make_relay, caplog):
        sleep = SleepRecorder(max_calls=3)
        relay, _ = make_relay([FakeResponse(status=404)], sleep=sleep)
        with caplog.at_level(logging.DEBUG, logger=activity.logger.name):
            unsub = relay.async_subscribe(lambda message: None)
            assert await _wait_until(lambda: len(sleep.delays) == 3)
        assert sleep.delays == [2, 4, 8]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "too old" in warnings[0].getMessage()
        unsub()


class TestRelayStop:
    @pytest.mark.asyncio
    async def test_stop_closes_upstream_and_notifies_subscribers(self, make_relay):
        response = FakeResponse(hold=True)
        relay, _ = make_relay([response])
        messages, callback = _collector()
        relay.async_subscribe(callback)
        assert await _wait_until(lambda: relay.connected)

        await relay.async_stop()

        assert response.closed is True
        assert relay.connected is False
        assert messages[-1] == {"type": "status", "connected": False}
        assert relay.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_subscribe_after_stop_is_a_noop(self, make_relay):
        relay, session = make_relay([FakeResponse(hold=True)])
        await relay.async_stop()
        unsub = relay.async_subscribe(lambda message: None)
        await asyncio.sleep(0.02)
        assert session.calls == []
        unsub()

    @pytest.mark.asyncio
    async def test_stop_without_subscribers(self, make_relay):
        relay, _ = make_relay([FakeResponse(hold=True)])
        await relay.async_stop()


class TestFetchRecent:
    @pytest.mark.asyncio
    async def test_returns_agent_dicts(self, make_relay):
        relay, _ = make_relay([FakeResponse()])
        payload = {
            "agents": [
                {"agent_id": "light-agent", "name": "Light", "last_used": None},
                "junk",
            ]
        }
        with patch.object(
            activity, "async_get_json", new=AsyncMock(return_value=payload)
        ) as fetch:
            agents = await relay.async_fetch_recent()
        assert agents == [
            {"agent_id": "light-agent", "name": "Light", "last_used": None}
        ]
        args, kwargs = fetch.call_args
        assert args[1:4] == (URL, "secret", "/api/activity/recent?limit=7")
        assert kwargs["timeout"] == 5.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["connection", "auth"])
    async def test_failure_returns_empty_list(self, make_relay, kind):
        error = (
            client.AgentHubConnectionError("down")
            if kind == "connection"
            else client.AgentHubAuthError(401)
        )
        relay, _ = make_relay([FakeResponse()])
        with patch.object(activity, "async_get_json", new=AsyncMock(side_effect=error)):
            assert await relay.async_fetch_recent() == []

    @pytest.mark.asyncio
    async def test_unexpected_shape_returns_empty_list(self, make_relay):
        relay, _ = make_relay([FakeResponse()])
        with patch.object(
            activity, "async_get_json", new=AsyncMock(return_value={"agents": "nope"})
        ):
            assert await relay.async_fetch_recent() == []
