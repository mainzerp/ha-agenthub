"""Tests for the opt-in HA log shipping (log_shipper.py + wiring).

Covers the enqueue-only handler (wire shape, level gate, drop counting,
recursion guard, truncation, contextvars), the decision-10 contextvar
propagation claim, the flush loop (success, failure/backoff, cancel), the
config-entry lifecycle wiring, and the trace_id reads on the REST and WS
conversation paths.
"""

import asyncio
import json
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_record(name, level, msg, lineno=42, func="test_func"):
    return logging.LogRecord(name, level, __file__, lineno, msg, (), None, func)


class _FakeChatLog:
    """Minimal ChatLog stand-in (mirrors test_integration._FakeChatLog)."""

    def __init__(self):
        self.deltas: list[dict] = []
        self.added_content: list = []

    def async_add_delta_content_stream(self, agent_id, stream):
        async def _consume():
            async for delta in stream:
                self.deltas.append(delta)
                yield delta

        return _consume()

    def async_add_assistant_content_without_tools(self, content):
        # Sync @callback in HA core -- must NOT be a coroutine.
        self.added_content.append(content)


class _FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self._payload = payload or {}
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    """Fake aiohttp session for shipper/REST tests; records post calls."""

    closed = False

    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.posts: list[tuple] = []
        self.posted = asyncio.Event()

    def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        self.posted.set()
        if self._exc is not None:
            raise self._exc
        return self._response


async def _wait_until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


# ---------------------------------------------------------------------------
# Handler unit tests (pure stdlib)
# ---------------------------------------------------------------------------


class TestLogShipHandler:
    def _make_handler(self, maxsize=10):
        from custom_components.ha_agenthub.log_shipper import LogShipHandler

        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        return LogShipHandler(queue), queue

    def test_enqueued_entry_has_wire_fields(self):
        handler, queue = self._make_handler()
        handler.handle(
            _make_record(
                "custom_components.ha_agenthub.conversation",
                logging.INFO,
                "hello world",
            )
        )
        entry = queue.get_nowait()
        assert entry["level"] == "INFO"  # uppercase stdlib levelname
        assert entry["name"] == "custom_components.ha_agenthub.conversation"
        assert entry["message"] == "hello world"
        assert entry["lineno"] == 42
        assert entry["funcName"] == "test_func"
        assert entry["module"]  # derived from pathname
        assert entry["trace_id"] is None
        assert entry["conversation_id"] is None
        timestamp = datetime.fromisoformat(entry["timestamp"])
        assert timestamp.tzinfo is not None  # tz-aware ISO-8601

    def test_handler_level_gate_drops_lower_levels(self):
        handler, queue = self._make_handler()
        handler.setLevel(logging.WARNING)
        gated = logging.getLogger("custom_components.ha_agenthub.tests.levelgate")
        gated.setLevel(logging.DEBUG)
        gated.propagate = False
        gated.addHandler(handler)
        try:
            gated.info("dropped")
            assert queue.empty()
            gated.warning("kept")
            entry = queue.get_nowait()
            assert entry["level"] == "WARNING"
            assert entry["message"] == "kept"
        finally:
            gated.removeHandler(handler)

    def test_full_queue_drops_and_counts_without_raising(self):
        handler, queue = self._make_handler(maxsize=1)
        handler.handle(
            _make_record(
                "custom_components.ha_agenthub.conversation", logging.INFO, "first"
            )
        )
        handler.handle(
            _make_record(
                "custom_components.ha_agenthub.conversation", logging.INFO, "second"
            )
        )
        assert handler._dropped == 1
        assert queue.get_nowait()["message"] == "first"

    def test_shipper_own_records_are_filtered_out(self):
        handler, queue = self._make_handler()
        handler.handle(
            _make_record(
                "custom_components.ha_agenthub.log_shipper", logging.ERROR, "self noise"
            )
        )
        assert queue.empty()

    def test_long_messages_are_truncated(self):
        handler, queue = self._make_handler()
        handler.handle(
            _make_record(
                "custom_components.ha_agenthub.conversation", logging.INFO, "x" * 3000
            )
        )
        entry = queue.get_nowait()
        assert len(entry["message"]) == 2000

    def test_contextvars_land_in_entry(self):
        from custom_components.ha_agenthub.log_shipper import (
            current_conversation_id,
            current_trace_id,
        )

        handler, queue = self._make_handler()
        token_trace = current_trace_id.set("tid-1")
        token_cid = current_conversation_id.set("cid-1")
        try:
            handler.handle(
                _make_record(
                    "custom_components.ha_agenthub.conversation", logging.INFO, "m"
                )
            )
        finally:
            current_trace_id.reset(token_trace)
            current_conversation_id.reset(token_cid)
        entry = queue.get_nowait()
        assert entry["trace_id"] == "tid-1"
        assert entry["conversation_id"] == "cid-1"


# ---------------------------------------------------------------------------
# Decision 10: contextvar propagation into an async generator (WS delta path)
# ---------------------------------------------------------------------------


class TestContextVarPropagation:
    @pytest.mark.asyncio
    async def test_contextvar_visible_inside_consumed_async_generator(self):
        """An async generator consumed via ``async for`` runs in the
        consumer's task context, so a contextvar set before consumption is
        visible to log records emitted inside the generator. The WS delta
        stream (chat_log.async_add_delta_content_stream) relies on this."""
        from custom_components.ha_agenthub.log_shipper import (
            LogShipHandler,
            current_trace_id,
        )

        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        handler = LogShipHandler(queue)
        test_logger = logging.getLogger("custom_components.ha_agenthub.tests.ctxprop")
        test_logger.setLevel(logging.DEBUG)
        test_logger.propagate = False
        test_logger.addHandler(handler)
        try:

            async def _gen():
                test_logger.info("inside generator")
                yield {"role": "assistant"}

            async def _worker():
                token = current_trace_id.set("abc123")
                try:
                    async for _ in _gen():
                        pass
                finally:
                    current_trace_id.reset(token)

            await asyncio.create_task(_worker())
            entry = queue.get_nowait()
            assert entry["trace_id"] == "abc123"
        finally:
            test_logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Flush loop (driven as a real task; SHIP_LOGS_FLUSH_INTERVAL patched down)
# ---------------------------------------------------------------------------


class TestFlushLoop:
    def _make_shipper(self, session):
        import custom_components.ha_agenthub.log_shipper as shipper_mod
        from custom_components.ha_agenthub.log_shipper import LogShipper

        shipper = LogShipper("http://example.com", "key", "DEBUG")
        shipper._queue = asyncio.Queue(maxsize=10)
        shipper._session = session
        return shipper, shipper_mod

    async def _cancel(self, task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_success_posts_batch_and_resets_backoff(self, monkeypatch):
        import aiohttp

        session = _FakeSession(_FakeResponse(200))
        shipper, shipper_mod = self._make_shipper(session)
        monkeypatch.setattr(shipper_mod, "SHIP_LOGS_FLUSH_INTERVAL", 0.1)
        shipper._queue.put_nowait({"message": "one"})
        shipper._queue.put_nowait({"message": "two"})

        # Fail the first POST to raise the backoff, then succeed.
        session._exc = aiohttp.ClientError("boom")
        task = asyncio.create_task(shipper._flush_loop(MagicMock()))
        try:
            assert await _wait_until(lambda: len(session.posts) == 1)
            assert shipper._dropped_batches == 1
            assert shipper._dropped_records == 2  # first batch dropped
            assert not task.done()  # loop survives the failure

            session._exc = None
            shipper._queue.put_nowait({"message": "three"})
            assert await _wait_until(
                lambda: len(session.posts) >= 2 and shipper._backoff == 0.0
            )
        finally:
            await self._cancel(task)

        args, kwargs = session.posts[0]
        assert args[0] == "http://example.com/api/logs/ingest"
        assert kwargs["headers"]["Authorization"] == "Bearer key"
        assert kwargs["json"] == [{"message": "one"}, {"message": "two"}]
        # The retry sent only the newly queued record (no requeue of drops).
        assert session.posts[1][1]["json"] == [{"message": "three"}]
        assert shipper._queue.empty()
        assert shipper._backoff == 0.0  # reset after success

    @pytest.mark.asyncio
    async def test_transport_error_drops_counts_and_doubles_backoff(self, monkeypatch):
        import aiohttp

        session = _FakeSession(exc=aiohttp.ClientError("boom"))
        shipper, shipper_mod = self._make_shipper(session)
        monkeypatch.setattr(shipper_mod, "SHIP_LOGS_FLUSH_INTERVAL", 0.1)
        shipper._queue.put_nowait({"message": "one"})

        task = asyncio.create_task(shipper._flush_loop(MagicMock()))
        try:
            assert await _wait_until(lambda: len(session.posts) == 1)
            assert await _wait_until(lambda: shipper._backoff > 0)
            first_backoff = shipper._backoff
            assert first_backoff == pytest.approx(0.1)

            shipper._queue.put_nowait({"message": "two"})
            session.posted.clear()
            assert await _wait_until(lambda: len(session.posts) == 2)
            assert await _wait_until(
                lambda: shipper._backoff == pytest.approx(2 * first_backoff)
            )
            assert shipper._dropped_batches == 2
            assert shipper._dropped_records == 2
            assert shipper._queue.empty()  # dropped, never requeued
            assert not task.done()
        finally:
            await self._cancel(task)

    @pytest.mark.asyncio
    async def test_non_2xx_status_is_treated_as_failure(self, monkeypatch):
        session = _FakeSession(_FakeResponse(500))
        shipper, shipper_mod = self._make_shipper(session)
        monkeypatch.setattr(shipper_mod, "SHIP_LOGS_FLUSH_INTERVAL", 0.05)
        shipper._queue.put_nowait({"message": "one"})

        task = asyncio.create_task(shipper._flush_loop(MagicMock()))
        try:
            assert await _wait_until(lambda: shipper._dropped_batches >= 1)
            assert shipper._dropped_records == 1
            assert shipper._backoff > 0
            assert shipper._queue.empty()
            assert not task.done()
        finally:
            await self._cancel(task)

    @pytest.mark.asyncio
    async def test_cancel_propagates_and_ends_task(self):
        session = _FakeSession(_FakeResponse(200))
        shipper, _ = self._make_shipper(session)

        task = asyncio.create_task(shipper._flush_loop(MagicMock()))
        await asyncio.sleep(0)  # let the loop start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()


# ---------------------------------------------------------------------------
# Config-entry lifecycle wiring (__init__.py)
# ---------------------------------------------------------------------------


class TestShipperWiring:
    def _make_hass_entry(self, options):
        hass = MagicMock()
        hass.data = {}
        hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.title = "HA-AgentHub"
        entry.data = {"url": "http://example.com", "api_key": "key"}
        entry.options = options
        entry.async_on_unload = MagicMock()
        entry.add_update_listener = MagicMock(return_value=MagicMock())

        def _create_background_task(hass, coro, name):
            # Real task so the flush coroutine is awaited and unload cancels it.
            return asyncio.ensure_future(coro)

        entry.async_create_background_task = MagicMock(
            side_effect=_create_background_task
        )
        return hass, entry

    @pytest.mark.asyncio
    async def test_ship_logs_disabled_by_default(self):
        from custom_components.ha_agenthub import async_setup_entry
        from custom_components.ha_agenthub.const import DOMAIN

        hass, entry = self._make_hass_entry({})
        assert await async_setup_entry(hass, entry) is True
        assert hass.data[DOMAIN]["e1"]["log_shipper"] is None
        entry.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_ship_logs_enabled_attaches_handler_and_unload_detaches(self):
        from custom_components.ha_agenthub import async_setup_entry, async_unload_entry
        from custom_components.ha_agenthub.const import DOMAIN

        hass, entry = self._make_hass_entry(
            {"ship_logs": True, "ship_logs_level": "WARNING"}
        )
        package_logger = logging.getLogger("custom_components.ha_agenthub")
        shipper = None
        fake_session = MagicMock()
        try:
            with patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                new=lambda hass: fake_session,
            ):
                assert await async_setup_entry(hass, entry) is True
                shipper = hass.data[DOMAIN]["e1"]["log_shipper"]
                assert shipper is not None
                entry.async_create_background_task.assert_called_once()
                assert shipper._handler in package_logger.handlers
                assert shipper._handler.level == logging.WARNING

                assert await async_unload_entry(hass, entry) is True
            assert shipper._handler not in package_logger.handlers
            assert DOMAIN not in hass.data
        finally:
            # Never leak the handler into global logging state on failure.
            if shipper is not None and shipper._handler is not None:
                package_logger.removeHandler(shipper._handler)


# ---------------------------------------------------------------------------
# trace_id reads on the conversation paths (conversation.py)
# ---------------------------------------------------------------------------


class TestTraceIdCapture:
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

    def _make_user_input(self, cid="c1"):
        user_input = MagicMock()
        user_input.conversation_id = cid
        user_input.text = "hello"
        user_input.language = "en"
        user_input.device_id = None
        user_input.context = None
        return user_input

    @pytest.mark.asyncio
    async def test_rest_response_trace_id_lands_in_contextvar(self):
        from custom_components.ha_agenthub.log_shipper import current_trace_id

        entity = self._make_entity()
        entity._session = _FakeSession(
            _FakeResponse(
                200,
                {"speech": "ok", "conversation_id": "c1"},
                headers={"X-Trace-Id": "abc123def4567890"},
            )
        )
        await entity._process_via_rest(self._make_user_input(), _FakeChatLog())
        assert current_trace_id.get(None) == "abc123def4567890"

    @pytest.mark.asyncio
    async def test_ws_done_frame_trace_id_lands_in_contextvar(self):
        import aiohttp

        from custom_components.ha_agenthub.log_shipper import current_trace_id

        entity = self._make_entity()
        done = MagicMock(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps(
                {
                    "done": True,
                    "token": "hi",
                    "trace_id": "traceabc123def456",
                    "sanitized": True,
                }
            ),
        )
        ws = MagicMock()
        ws.closed = False
        ws.send_json = AsyncMock()
        ws.close = AsyncMock()
        ws.receive = AsyncMock(return_value=done)

        await entity._process_via_ws_read(self._make_user_input(), _FakeChatLog(), ws)
        assert current_trace_id.get(None) == "traceabc123def456"
