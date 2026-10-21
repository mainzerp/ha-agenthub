"""Live agent activity relay: one upstream SSE connection, many subscribers.

The container publishes agent span events on ``GET /api/activity/stream``
(Server-Sent Events, Bearer API key). The Lovelace activity card cannot reach
that endpoint itself (a Home Assistant page carries no container session and
no API key), so the bridge relays it:

* :class:`ActivityRelay` keeps ONE upstream connection per config entry while
  at least one frontend subscriber exists, fans every event out to all
  subscribers, reconnects with exponential backoff and closes the upstream
  connection when the last subscriber leaves or the entry unloads.
* :class:`SSEParser` is the incremental, line-based SSE decoder.

Nothing here raises into Home Assistant: every failure is logged and turned
into a ``connected = False`` state plus a reconnect attempt.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import itertools
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import (
    AgentHubApiError,
    AgentHubAuthError,
    AgentHubResponseError,
    async_get_json,
)
from .const import (
    ACTIVITY_BACKOFF_BASE,
    ACTIVITY_BACKOFF_MAX,
    ACTIVITY_CONNECT_TIMEOUT,
    ACTIVITY_EVENT_NAME,
    ACTIVITY_MAX_LINE_BYTES,
    ACTIVITY_RECENT_LIMIT,
    ACTIVITY_RECENT_PATH,
    ACTIVITY_RECENT_TIMEOUT,
    ACTIVITY_SOCK_READ_TIMEOUT,
    ACTIVITY_STREAM_PATH,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

logger = logging.getLogger(__name__)

# Message handed to subscribers: ``{"type": "activity", "event": {...}}`` or
# ``{"type": "status", "connected": bool}``.
ActivityMessage = dict[str, Any]
ActivityCallback = Callable[[ActivityMessage], None]

_EOL = re.compile(r"\r\n|\n|\r")


@dataclass(slots=True)
class SSEEvent:
    """One dispatched Server-Sent Event."""

    event: str
    data: str
    event_id: str | None = None


class SSEParser:
    """Incremental SSE decoder (WHATWG event-stream rules, subset).

    ``feed`` takes raw bytes in arbitrary chunking (a chunk may end in the
    middle of a line or a multi-byte character) and returns the events that
    were completed by it. Comment lines (``:``) are ignored, multiple ``data``
    lines are joined with ``\\n``, ``retry`` is recorded in ``retry_ms`` and a
    blank line dispatches the event (events without data are dropped).
    """

    def __init__(self, max_line_chars: int = ACTIVITY_MAX_LINE_BYTES) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._max_line_chars = max_line_chars
        self._buffer = ""
        self._first_chunk = True
        self._discard_line = False
        self._event_name = ""
        self._data: list[str] = []
        self._last_id: str | None = None
        self.retry_ms: int | None = None

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        text = self._decoder.decode(chunk)
        if self._first_chunk and text:
            self._first_chunk = False
            text = text.removeprefix("﻿")
        buf = self._buffer + text
        events: list[SSEEvent] = []
        pos = 0
        while True:
            match = _EOL.search(buf, pos)
            if match is None:
                break
            if match.group() == "\r" and match.end() == len(buf):
                # A lone CR at the end may be the first half of CRLF.
                break
            line = buf[pos : match.start()]
            pos = match.end()
            if self._discard_line:
                self._discard_line = False
                continue
            event = self._process_line(line)
            if event is not None:
                events.append(event)
        self._buffer = buf[pos:]
        if len(self._buffer) > self._max_line_chars:
            logger.debug("Discarding oversized SSE line (%d chars)", len(self._buffer))
            self._buffer = ""
            self._discard_line = True
        return events

    def _process_line(self, line: str) -> SSEEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event_name = value
        elif field == "data":
            self._data.append(value)
        elif field == "id":
            if "\0" not in value:
                self._last_id = value
        elif field == "retry" and value.isascii() and value.isdigit():
            self.retry_ms = int(value)
        return None

    def _dispatch(self) -> SSEEvent | None:
        data, name = self._data, self._event_name
        self._data = []
        self._event_name = ""
        if not data:
            return None
        return SSEEvent(
            event=name or "message", data="\n".join(data), event_id=self._last_id
        )


def _host(url: str) -> str:
    """Return the host for log messages (the URL may embed credentials)."""
    try:
        return urlparse(url).hostname or "container"
    except ValueError:
        return "container"


class ActivityRelay:
    """Per-entry fan-out of the container's activity stream.

    The upstream task runs only while subscribers exist. ``async_subscribe``
    and the returned unsubscribe function are loop-thread callbacks.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        api_key: str,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._hass = hass
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._sleep = sleep
        self._subscribers: dict[int, ActivityCallback] = {}
        self._ids = itertools.count()
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._stopped = False
        self._delay = ACTIVITY_BACKOFF_BASE
        # Kind of the last failure that was logged at warning level; the same
        # kind is only logged at debug until a connection receives data again.
        self._last_failure: str | None = None

    @property
    def connected(self) -> bool:
        """True while the upstream stream is open."""
        return self._connected

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @callback
    def async_subscribe(self, subscriber: ActivityCallback) -> Callable[[], None]:
        """Register ``subscriber``; the first one starts the upstream connection."""
        if self._stopped:
            return lambda: None
        key = next(self._ids)
        self._subscribers[key] = subscriber
        if self._task is None or self._task.done():
            self._delay = ACTIVITY_BACKOFF_BASE
            self._last_failure = None
            self._task = self._hass.async_create_background_task(
                self._run(), name="ha_agenthub_activity_stream"
            )

        @callback
        def _unsubscribe() -> None:
            if self._subscribers.pop(key, None) is None:
                return
            if not self._subscribers:
                self._cancel_upstream()

        return _unsubscribe

    async def async_stop(self) -> None:
        """Close the upstream connection and release all subscribers (unload)."""
        self._stopped = True
        task = self._cancel_upstream()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._subscribers:
            # Subscribers learn that the feed is gone; they stay registered
            # until their own unsubscribe (no further events arrive).
            self._dispatch({"type": "status", "connected": False})
            self._subscribers.clear()

    async def async_fetch_recent(
        self, limit: int = ACTIVITY_RECENT_LIMIT
    ) -> list[dict[str, Any]]:
        """Return the recently used agents, or an empty list on any failure."""
        try:
            data = await async_get_json(
                self._hass,
                self._url,
                self._api_key,
                f"{ACTIVITY_RECENT_PATH}?limit={limit}",
                timeout=ACTIVITY_RECENT_TIMEOUT,
            )
        except AgentHubApiError as err:
            logger.debug(
                "Fetching recent agents from %s failed: %s", _host(self._url), err
            )
            return []
        agents = data.get("agents")
        if not isinstance(agents, list):
            return []
        return [agent for agent in agents if isinstance(agent, dict)]

    def _cancel_upstream(self) -> asyncio.Task[None] | None:
        task, self._task = self._task, None
        self._connected = False
        if task is not None and not task.done():
            task.cancel()
        return task

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        self._dispatch({"type": "status", "connected": connected})

    def _dispatch(self, message: ActivityMessage) -> None:
        for subscriber in list(self._subscribers.values()):
            try:
                subscriber(message)
            except Exception:
                logger.exception("Activity subscriber callback failed")

    def _report_failure(self, kind: str, text: str) -> None:
        """Log ``text`` at warning level once per failure kind, else at debug."""
        if kind != self._last_failure:
            self._last_failure = kind
            logger.warning("%s", text)
        else:
            logger.debug("%s", text)

    async def _run(self) -> None:
        """Connect, relay, and reconnect with exponential backoff until cancelled."""
        host = _host(self._url)
        while True:
            try:
                await self._stream_once()
                logger.debug("Activity stream from %s closed by the container", host)
            except asyncio.CancelledError:
                raise
            except AgentHubAuthError as err:
                self._report_failure(
                    f"auth:{err.status}",
                    f"HA-AgentHub container at {host} rejected the API key for the activity stream "
                    f"(HTTP {err.status}); retrying every {ACTIVITY_BACKOFF_MAX:g}s",
                )
                self._delay = ACTIVITY_BACKOFF_MAX
            except AgentHubResponseError as err:
                hint = (
                    " (container too old for the activity stream?)"
                    if err.status == 404
                    else ""
                )
                self._report_failure(
                    f"http:{err.status}",
                    f"Activity stream from {host} answered HTTP {err.status}{hint}",
                )
            except (aiohttp.ClientError, TimeoutError, OSError) as err:
                self._report_failure(
                    "transport",
                    f"Activity stream from {host} is unavailable: {type(err).__name__}",
                )
            except Exception:
                logger.warning(
                    "Unexpected activity stream error from %s", host, exc_info=True
                )
            self._set_connected(False)
            await self._sleep(self._delay)
            self._delay = min(self._delay * 2, ACTIVITY_BACKOFF_MAX)

    async def _stream_once(self) -> None:
        """Run one upstream connection until it ends or fails."""
        session = async_get_clientsession(self._hass)
        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=ACTIVITY_CONNECT_TIMEOUT,
            sock_read=ACTIVITY_SOCK_READ_TIMEOUT,
        )
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        parser = SSEParser()
        got_data = False
        async with session.get(
            f"{self._url}{ACTIVITY_STREAM_PATH}", headers=headers, timeout=timeout
        ) as resp:
            if resp.status in (401, 403):
                raise AgentHubAuthError(resp.status)
            if resp.status != 200:
                raise AgentHubResponseError(resp.status, None)
            self._set_connected(True)
            async for chunk in resp.content.iter_any():
                if not chunk:
                    continue
                if not got_data:
                    got_data = True
                    self._delay = ACTIVITY_BACKOFF_BASE
                    self._last_failure = None
                for sse_event in parser.feed(chunk):
                    self._handle_event(sse_event)

    def _handle_event(self, sse_event: SSEEvent) -> None:
        if sse_event.event != ACTIVITY_EVENT_NAME:
            logger.debug("Ignoring SSE event %r", sse_event.event)
            return
        try:
            payload = json.loads(sse_event.data)
        except ValueError:
            logger.debug("Skipping activity event with malformed JSON")
            return
        if not isinstance(payload, dict):
            logger.debug("Skipping activity event that is not a JSON object")
            return
        self._dispatch({"type": "activity", "event": payload})
