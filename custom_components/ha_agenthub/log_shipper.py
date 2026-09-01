"""Opt-in shipping of the integration's own log records to the container.

A enqueue-only logging handler is attached to the
``custom_components.ha_agenthub`` package logger; a background task
periodically POSTs batches to ``POST /api/logs/ingest`` on the container.
In-memory only (no persistence), drop-and-count on failure with
exponential backoff, and no requeue. Lifecycle is entry-scoped: started
from ``async_setup_entry`` when the ``ship_logs`` option is enabled and
stopped from ``async_unload_entry``.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from .const import (
    LOG_INGEST_PATH,
    SHIP_LOGS_BATCH_SIZE,
    SHIP_LOGS_FLUSH_INTERVAL,
    SHIP_LOGS_MAX_BACKOFF,
    SHIP_LOGS_MAX_MESSAGE,
    SHIP_LOGS_QUEUE_MAX,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_logger = logging.getLogger(__name__)
# Recursion guard (part 1): the shipper logger is a child of the captured
# package logger. Never let its records (e.g. failed-POST warnings) flow
# back into the hierarchy where the shipper handler is attached.
_logger.propagate = False

# Per-turn correlation ids, set by conversation.py around a conversation
# turn; read by LogShipHandler.emit() so shipped records carry them.
current_trace_id: ContextVar[str | None] = ContextVar(
    "ha_agenthub_trace_id", default=None
)
current_conversation_id: ContextVar[str | None] = ContextVar(
    "ha_agenthub_conversation_id", default=None
)

_PACKAGE_LOGGER_NAME = __name__.rpartition(".")[0]  # custom_components.ha_agenthub


class _ShipperNameFilter(logging.Filter):
    """Recursion guard (part 2): a failed-POST warning from this module must
    never re-enter the handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(__name__)


class LogShipHandler(logging.Handler):
    """Enqueue-only logging handler: formats the record into the wire shape
    and drops it onto the shipper queue. Never blocks and never raises into
    the logging call path."""

    def __init__(self, queue: asyncio.Queue) -> None:
        super().__init__()
        self._queue = queue
        self._dropped = 0
        self.addFilter(_ShipperNameFilter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                # levelname is already uppercase for stdlib levels.
                "level": record.levelname,
                "name": record.name,
                "message": self.format(record)[:SHIP_LOGS_MAX_MESSAGE],
                "lineno": record.lineno,
                "module": record.module,
                "funcName": record.funcName,
                "trace_id": current_trace_id.get(None),
                "conversation_id": current_conversation_id.get(None),
            }
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            self._dropped += 1
        except Exception:
            # The logging path must never raise into voice-turn code. Safe to
            # log here: the shipper logger neither propagates nor passes its
            # own records back into this handler (recursion guards above).
            _logger.debug("Failed to enqueue log record for shipping", exc_info=True)


class LogShipper:
    """Batches package log records and POSTs them to the container's
    log-ingest API. Failure policy: drop-and-count the batch, exponential
    backoff (double, capped), no requeue."""

    def __init__(self, url: str, api_key: str, level: str) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._level = level
        self._queue: asyncio.Queue | None = None
        self._handler: LogShipHandler | None = None
        self._task: asyncio.Task | None = None
        self._hass: HomeAssistant | None = None
        self._session: Any = None
        self._backoff = 0.0
        # Observability counters for tests / diagnostics; dropped work is
        # never requeued.
        self._dropped_batches = 0
        self._dropped_records = 0

    def start(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Attach the handler to the package logger and start the flush loop.

        Idempotent against options reload re-running setup: the handler is
        only attached when this instance is not already present. The
        background task is registered on the config entry, so HA cancels it
        automatically on unload as a final safety net.
        """
        # Imported lazily so tests can patch homeassistant.helpers
        # .aiohttp_client.async_get_clientsession before setup runs.
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        self._hass = hass
        self._session = async_get_clientsession(hass)
        self._queue = asyncio.Queue(maxsize=SHIP_LOGS_QUEUE_MAX)
        self._handler = LogShipHandler(self._queue)
        # Gate on the handler only; the package logger's own level stays
        # untouched so local logging behavior does not change.
        self._handler.setLevel(getattr(logging, self._level))
        package_logger = logging.getLogger(_PACKAGE_LOGGER_NAME)
        if self._handler not in package_logger.handlers:
            package_logger.addHandler(self._handler)
        self._task = entry.async_create_background_task(
            hass,
            self._flush_loop(hass),
            name="ha_agenthub_log_ship",
        )

    async def stop(self) -> None:
        """Detach the handler (logging state is global and outside HA task
        tracking, so this is mandatory), cancel the flush loop, and make one
        final best-effort flush."""
        if self._handler is not None:
            logging.getLogger(_PACKAGE_LOGGER_NAME).removeHandler(self._handler)
            self._handler = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._hass is not None:
            try:
                await self._flush_once(self._hass)
            except Exception:
                # Best-effort during unload; nothing useful left to do.
                _logger.debug("Final log flush on unload failed", exc_info=True)

    async def _flush_loop(self, hass: HomeAssistant) -> None:
        while True:
            try:
                await asyncio.sleep(
                    self._backoff if self._backoff > 0 else SHIP_LOGS_FLUSH_INTERVAL
                )
                if await self._flush_once(hass):
                    self._backoff = 0.0
                else:
                    self._backoff = self._next_backoff()
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                # The drained batch was already dropped and counted inside
                # _flush_once; only the backoff matters here.
                self._backoff = self._next_backoff()

    def _next_backoff(self) -> float:
        return min(
            max(self._backoff * 2, SHIP_LOGS_FLUSH_INTERVAL),
            SHIP_LOGS_MAX_BACKOFF,
        )

    async def _flush_once(self, hass: HomeAssistant) -> bool:
        """Drain up to one batch and POST it. Returns True on success (or an
        empty queue), False on a non-2xx response; transport errors are
        counted and re-raised for the loop's backoff handling."""
        batch: list[dict[str, Any]] = []
        while len(batch) < SHIP_LOGS_BATCH_SIZE:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            return True
        try:
            async with self._session.post(
                f"{self._url}{LOG_INGEST_PATH}",
                json=batch,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                _logger.warning(
                    "Log ingest POST to %s failed with status %s; dropping %d record(s)",
                    self._url,
                    resp.status,
                    len(batch),
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            self._dropped_batches += 1
            self._dropped_records += len(batch)
            raise
        self._dropped_batches += 1
        self._dropped_records += len(batch)
        return False
