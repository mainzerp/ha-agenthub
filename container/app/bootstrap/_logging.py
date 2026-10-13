"""Logging configuration and the LogBufferHandler re-attach guard.

Owns:

* :func:`_configure_logging` -- root level + Stream/RotatingFile/LogBuffer
  handlers.
* :func:`_ensure_log_buffer_handler` -- re-attaches the LogBufferHandler if a
  library displaced it and restores the configured root level.
* :func:`start_log_buffer_guard` -- re-ensures the handler at end of startup
  and starts the periodic re-attach guard task.
* :func:`start_log_persistence` / :func:`stop_log_persistence` -- persist
  WARNING+ buffer entries to ``$LOG_DIR/warnings.jsonl`` and restore them into
  the buffer at startup (see :mod:`app.util.log_persist`).

``_configure_logging`` and ``_ensure_log_buffer_handler`` are re-exported from
:mod:`app.main` (via ``__all__``) so existing callers and the unit tests that
do ``from app.main import _configure_logging, _ensure_log_buffer_handler``
keep working.

GOAL 2 -- log_config injection (DEFERRED, intentionally):
The optimisation plan's Goal 2 intended to move the LogBufferHandler into
uvicorn's ``log_config`` dictConfig so the 10-second re-attach guard could be
removed. That is not feasible here:

1. The container launches uvicorn from the Dockerfile ``CMD``
   (``python -m uvicorn app.main:app ...``) wrapped by ``/entrypoint.sh``.
   There is no Python entry point that accepts ``log_config=`` and no
   ``--log-config`` file in use, so there is no clean injection point.
2. Even if a ``log_config`` were injected, it would not remove the need for
   the guard. ``_configure_logging`` already runs inside the ASGI lifespan,
   which executes *after* uvicorn's own startup ``dictConfig`` -- so uvicorn's
   startup reconfiguration is already overwritten. The displacement the guard
   defends against is caused by third-party libraries (the embedding/ML stack,
   torch, etc.) that call ``logging.basicConfig(force=True)`` /
   ``logging.config.dictConfig`` during setup-dependent initialisation, i.e.
   *after* the lifespan has already started. A uvicorn ``log_config`` cannot
   prevent that.

The guard is therefore the only robust defence and is retained. See
``docs/SubAgent/OPTIMIZE_PROJECT_PART4_PLAN.md`` (risk/rollback) and the
"prefer correctness over removing the guard" instruction.
"""

from __future__ import annotations

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

from app.bootstrap._tasks import spawn_background
from app.config import settings
from app.util.log_buffer import LogBuffer, LogBufferHandler, get_log_buffer, set_log_buffer
from app.util.log_persist import PERSIST_FILENAME, RESTORE_LIMIT, LogPersister

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_log_persister: LogPersister | None = None


def _log_dir() -> str:
    """Return the log directory (``LOG_DIR`` env, default ``/data/logs``)."""
    return os.environ.get("LOG_DIR", "/data/logs")


def _ensure_log_buffer_handler() -> None:
    """Ensure root logger has the correct level and log buffer handler.

    Uvicorn or other libraries may reconfigure logging after our lifespan
    starts, wiping handlers or changing the root level.  This helper re-
    attaches the buffer handler and restores the configured level whenever
    it is called.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()

    root.setLevel(level)

    # Re-attach buffer handler if missing.
    has_buffer = any(isinstance(h, LogBufferHandler) for h in root.handlers)
    if not has_buffer:
        log_buffer = get_log_buffer()
        if log_buffer is None:
            log_buffer = LogBuffer(capacity=10000)
            set_log_buffer(log_buffer)
        buffer_handler = LogBufferHandler(log_buffer)
        root.addHandler(buffer_handler)


def _configure_logging() -> None:
    """Configure structured logging based on settings."""
    log_format = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Only add StreamHandler if root has no handlers yet.
    # Avoid force=True which wipes handlers that uvicorn or other
    # libraries may have already configured.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter(log_format))
        root.addHandler(stream_handler)

    # Add RotatingFileHandler for persistent logs.
    log_dir = _log_dir()
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(logging.Formatter(log_format))
    root.addHandler(file_handler)

    _ensure_log_buffer_handler()


def start_log_buffer_guard(app: FastAPI) -> None:
    """Re-ensure the buffer handler and start the periodic re-attach guard.

    Called once at the end of application startup. The guard re-attaches the
    LogBufferHandler if a third-party library removes it at runtime. See the
    module docstring for why this guard is retained (Goal 2 deferred).
    """
    _ensure_log_buffer_handler()

    async def _log_buffer_guard() -> None:
        while True:
            await asyncio.sleep(10)
            _ensure_log_buffer_handler()

    spawn_background(app, _log_buffer_guard(), "log_buffer_guard")


async def start_log_persistence(log_dir: str | None = None) -> LogPersister | None:
    """Restore persisted WARNING+ entries and start persisting new ones.

    The sink is attached before the file is read so entries logged during the
    restore are queued (not lost) and only written after the read completes,
    which keeps them from being restored twice. All file I/O runs in a worker
    thread. Failures are logged and never abort startup.
    """
    global _log_persister
    if _log_persister is not None:
        return _log_persister

    log_buffer = get_log_buffer()
    if log_buffer is None:
        _ensure_log_buffer_handler()
        log_buffer = get_log_buffer()
    if log_buffer is None:
        return None

    persister = LogPersister(os.path.join(log_dir or _log_dir(), PERSIST_FILENAME))
    log_buffer.set_persist_sink(persister.submit, persister.min_level)
    try:
        entries, skipped = await asyncio.to_thread(persister.load_recent, RESTORE_LIMIT)
        restored = log_buffer.restore_entries(entries)
        await asyncio.to_thread(persister.start)
    except Exception:
        log_buffer.set_persist_sink(None)
        logger.warning("Persistent warning log disabled: could not open %s", persister.path, exc_info=True)
        return None

    _log_persister = persister
    if skipped:
        logger.warning("Skipped %d unreadable line(s) while restoring persisted logs from %s", skipped, persister.path)
    logger.info("Restored %d persisted WARNING+ log entries from %s", restored, persister.path)
    return persister


async def stop_log_persistence() -> None:
    """Detach the persist sink, flush queued entries and stop the writer thread."""
    global _log_persister
    persister, _log_persister = _log_persister, None
    if persister is None:
        return
    log_buffer = get_log_buffer()
    if log_buffer is not None:
        log_buffer.set_persist_sink(None)
    try:
        await asyncio.to_thread(persister.stop)
    except Exception:
        logger.warning("Failed to stop persistent warning log writer", exc_info=True)
    if persister.dropped:
        logger.warning("Persistent warning log dropped %d entries (queue full)", persister.dropped)
