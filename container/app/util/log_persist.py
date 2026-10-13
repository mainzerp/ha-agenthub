"""Durable persistence of WARNING-and-above log buffer entries.

The in-memory :class:`app.util.log_buffer.LogBuffer` is lost on every
container restart. :class:`LogPersister` mirrors every buffer entry at or
above ``WARNING`` (local records and records ingested from the HA bridge)
into a rotating JSON-lines file under the log directory, and reads the most
recent entries back at startup so the admin log API shows pre-restart
warnings and errors.

Threading model:

* :meth:`LogPersister.submit` only enqueues (``put_nowait`` on a bounded
  queue). It never touches the file system, so it is safe to call from the
  event loop and from logging handlers.
* A :class:`logging.handlers.QueueListener` thread drains the queue,
  serializes each entry to one JSON line and writes it through a
  :class:`logging.handlers.RotatingFileHandler` (size-bounded retention).
* :meth:`LogPersister.start`, :meth:`LogPersister.stop` and
  :meth:`LogPersister.load_recent` block and must be run off the event loop
  (``asyncio.to_thread``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
from datetime import datetime
from logging.handlers import QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any

PERSIST_FILENAME = "warnings.jsonl"
PERSIST_MAX_BYTES = 5 * 1024 * 1024
PERSIST_BACKUP_COUNT = 2
PERSIST_MIN_LEVEL = logging.WARNING
RESTORE_LIMIT = 2000
QUEUE_MAXSIZE = 10000

_REQUIRED_STR_KEYS = ("timestamp", "level", "name", "message")


class _JsonLineFormatter(logging.Formatter):
    """Render the entry dict carried on a queue record as one JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(getattr(record, "entry", {}), ensure_ascii=True, default=str)


class _BlockingSentinelListener(QueueListener):
    """QueueListener whose stop sentinel waits for room in a bounded queue."""

    def enqueue_sentinel(self) -> None:
        self.queue.put(self._sentinel, timeout=5.0)


def _parse_entry(line: str) -> dict[str, Any] | None:
    """Parse one persisted line; return None when it is not a valid entry."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    if not all(isinstance(entry.get(key), str) for key in _REQUIRED_STR_KEYS):
        return None
    try:
        ts = datetime.fromisoformat(entry["timestamp"])
    except ValueError:
        return None
    # LogBuffer.get_entries compares against tz-aware datetimes.
    if ts.tzinfo is None:
        return None
    return entry


class LogPersister:
    """Rotating JSON-lines store for WARNING-and-above log buffer entries."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = PERSIST_MAX_BYTES,
        backup_count: int = PERSIST_BACKUP_COUNT,
        min_level: int = PERSIST_MIN_LEVEL,
        queue_maxsize: int = QUEUE_MAXSIZE,
    ) -> None:
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self.min_level = min_level
        self._queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=queue_maxsize)
        self._file_handler: RotatingFileHandler | None = None
        self._listener: QueueListener | None = None
        self.dropped = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def queue(self) -> queue.Queue[logging.LogRecord]:
        return self._queue

    def files_oldest_first(self) -> list[Path]:
        """Return the rotation set ordered from oldest backup to the live file."""
        backups = [Path(f"{self._path}.{i}") for i in range(self._backup_count, 0, -1)]
        return [*backups, self._path]

    def submit(self, entry: dict[str, Any]) -> None:
        """Enqueue an entry for the writer thread. Never blocks, never raises."""
        record = logging.makeLogRecord({"msg": "", "levelno": logging.WARNING, "levelname": "WARNING"})
        record.entry = dict(entry)
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def start(self) -> None:
        """Create the directory and start the writer thread (blocking)."""
        if self._listener is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            self._path,
            maxBytes=self._max_bytes,
            backupCount=self._backup_count,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(_JsonLineFormatter())
        listener = _BlockingSentinelListener(self._queue, handler)
        listener.start()
        self._file_handler = handler
        self._listener = listener

    def stop(self) -> None:
        """Flush pending entries, stop the writer thread and close the file (blocking)."""
        listener, self._listener = self._listener, None
        if listener is not None:
            with contextlib.suppress(queue.Full):
                listener.stop()
        handler, self._file_handler = self._file_handler, None
        if handler is not None:
            handler.close()

    def load_recent(self, limit: int = RESTORE_LIMIT) -> tuple[list[dict[str, Any]], int]:
        """Read up to ``limit`` most recent entries, oldest first (blocking).

        Returns ``(entries, skipped)`` where ``skipped`` counts unreadable or
        invalid lines. Missing files are ignored.
        """
        collected: list[dict[str, Any]] = []
        skipped = 0
        for path in reversed(self.files_oldest_first()):
            if len(collected) >= limit:
                break
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            except OSError:
                skipped += 1
                continue
            file_entries: list[dict[str, Any]] = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                entry = _parse_entry(line)
                if entry is None:
                    skipped += 1
                    continue
                file_entries.append(entry)
            collected = file_entries + collected
        if limit <= 0:
            return [], skipped
        return collected[-limit:], skipped
