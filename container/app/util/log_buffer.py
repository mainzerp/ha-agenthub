"""Thread-safe in-memory log ring buffer and handler."""

from __future__ import annotations

import contextlib
import logging
import traceback
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from threading import Lock
from typing import Any

_MIN_TS = datetime.min.replace(tzinfo=UTC)


def _entry_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of an entry for de-duplicating restored records."""
    return (
        entry.get("timestamp"),
        entry.get("level"),
        entry.get("name"),
        entry.get("message"),
        entry.get("source"),
    )


def _entry_sort_key(entry: dict[str, Any]) -> datetime:
    try:
        ts = datetime.fromisoformat(entry["timestamp"])
    except (KeyError, TypeError, ValueError):
        return _MIN_TS
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


class LogBuffer:
    """Thread-safe ring buffer for log entries.

    An optional persist sink (see :mod:`app.util.log_persist`) receives every
    entry at or above its minimum level. The sink must only enqueue; it is
    called on the logging thread, which may be the event loop.
    """

    def __init__(self, capacity: int = 10000) -> None:
        self._capacity = capacity
        self._buffer: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = Lock()
        self._persist_sink: Callable[[dict[str, Any]], None] | None = None
        self._persist_min_level = logging.WARNING

    def set_persist_sink(
        self,
        sink: Callable[[dict[str, Any]], None] | None,
        min_level: int = logging.WARNING,
    ) -> None:
        """Attach (or detach with ``None``) a non-blocking persistence sink."""
        self._persist_sink = sink
        self._persist_min_level = min_level

    def _maybe_persist(self, levelno: Any, entry: dict[str, Any]) -> None:
        sink = self._persist_sink
        if sink is None or not isinstance(levelno, int) or levelno < self._persist_min_level:
            return
        # Persistence must never break logging.
        with contextlib.suppress(Exception):
            sink(entry)

    def restore_entries(self, entries: Iterable[dict[str, Any]]) -> int:
        """Merge previously persisted entries into the buffer.

        Entries already present (same timestamp, level, logger, message and
        source) are skipped, so repeated restores do not duplicate. The merged
        buffer is ordered by timestamp and trimmed to capacity, keeping the
        newest. Restored entries are not re-sent to the persist sink. Returns
        the number of entries added.
        """
        with self._lock:
            current = list(self._buffer)
            seen = {_entry_key(e) for e in current}
            new: list[dict[str, Any]] = []
            for entry in entries:
                key = _entry_key(entry)
                if key in seen:
                    continue
                seen.add(key)
                new.append({**entry, "restored": True})
            if not new:
                return 0
            merged = new + current
            merged.sort(key=_entry_sort_key)
            self._buffer = deque(merged[-self._capacity :], maxlen=self._capacity)
            return len(new)

    def add(self, record: logging.LogRecord) -> None:
        """Store a formatted log record."""
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno,
        }
        if record.exc_info:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info))
        with self._lock:
            self._buffer.append(entry)
        self._maybe_persist(record.levelno, entry)

    def add_entry(self, entry: dict[str, Any]) -> None:
        """Append a pre-built entry dict (used by the log-ingest API)."""
        with self._lock:
            self._buffer.append(entry)
        self._maybe_persist(logging.getLevelName(str(entry.get("level", ""))), entry)

    def get_entries(
        self,
        level: str | None = None,
        logger_name: str | None = None,
        since: datetime | str | None = None,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Return filtered, paginated entries."""
        limit = min(max(limit, 0), 1000)
        offset = max(offset, 0)

        min_level_no: int | None = None
        if level is not None:
            min_level_no = logging.getLevelName(level.upper())
            if not isinstance(min_level_no, int):
                min_level_no = None

        since_dt: datetime | None = None
        if since is not None:
            if isinstance(since, str):
                since_dt = datetime.fromisoformat(since)
            elif isinstance(since, datetime):
                since_dt = since

        with self._lock:
            entries = list(self._buffer)

        filtered: list[dict[str, Any]] = []
        for entry in entries:
            if min_level_no is not None:
                entry_level_no = logging.getLevelName(entry["level"])
                if isinstance(entry_level_no, int) and entry_level_no < min_level_no:
                    continue

            if logger_name is not None and logger_name not in entry["name"]:
                continue

            if since_dt is not None:
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=UTC)
                entry_ts = datetime.fromisoformat(entry["timestamp"])
                if entry_ts <= since_dt:
                    continue

            if search is not None and search not in entry["message"]:
                continue

            filtered.append(entry)

        total = len(filtered)
        filtered.reverse()
        paginated = filtered[offset : offset + limit]
        return {"entries": paginated, "total": total}


class LogBufferHandler(logging.Handler):
    """Logging handler that stores records in a LogBuffer."""

    def __init__(self, log_buffer: LogBuffer) -> None:
        super().__init__()
        self._log_buffer = log_buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log_buffer.add(record)
        except Exception:
            self.handleError(record)


_log_buffer: LogBuffer | None = None


def set_log_buffer(buf: LogBuffer) -> None:
    """Inject the global log buffer."""
    global _log_buffer
    _log_buffer = buf


def get_log_buffer() -> LogBuffer | None:
    """Return the current global log buffer."""
    return _log_buffer
