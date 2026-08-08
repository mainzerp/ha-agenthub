"""Bootstrap: MemoryService singleton + startup embedding backfill task."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.bootstrap._tasks import spawn_background
from app.memory import init_memory_service

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def setup_memory(app: FastAPI) -> None:
    """Initialize the session-memory service and spawn the startup backfill.

    The backfill re-embeds turns whose vectors were reset by an embedding
    model/dimension change; it is a no-op when nothing is unembedded and is
    skipped entirely when ``memory.enabled`` is false.
    """
    backfill_task = getattr(app.state, "memory_backfill_task", None)
    if backfill_task is not None and not backfill_task.done():
        return
    service = init_memory_service()
    if service is None:
        return
    try:
        if not await service.is_enabled():
            return
    except Exception:
        logger.warning("Session-memory enabled check failed; skipping backfill", exc_info=True)
        return
    spawn_background(app, service.backfill(), "memory_backfill_task", name="memory_backfill")
