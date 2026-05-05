"""Thread-safe in-memory log ring buffer and handler."""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime
from threading import Lock
from typing import Any


class LogBuffer:
    """Thread-safe ring buffer for log entries."""

    def __init__(self, capacity: int = 10000) -> None:
        self._capacity = capacity
        self._buffer: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = Lock()

    def add(self, record: logging.LogRecord) -> None:
        """Store a formatted log record."""
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno,
        }
        with self._lock:
            self._buffer.append(entry)

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
