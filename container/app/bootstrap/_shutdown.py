"""Application shutdown teardown.

:func:`teardown` reproduces -- verbatim and in the exact same order -- the
shutdown sequence that previously lived inline in ``app.main.lifespan``. It
must not be reordered: monitors and the WebSocket client are stopped before
background tasks are cancelled, the cache is flushed before its stores are
closed, and the database is closed last. All references are read from
``app.state`` via ``getattr(..., None)`` so the incomplete-setup /
setup-wizard paths do not raise.

The named background tasks cancelled here (purge, flush, ws, sync,
entity_index_init, cache_validator) are the same six that
:func:`app.bootstrap._tasks.spawn_background` registered on ``app.state``
under their conventional attribute names; they are read back by name in the
historical cancellation order rather than by iterating the registry, so the
await/gather order is identical to the previous inline implementation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def teardown(app: FastAPI) -> None:
    """Run the full application shutdown sequence (order preserved)."""
    logger.info("Shutting down agent-assist container")

    # Plugin shutdown (isolated -- errors must not block remaining cleanup)
    plugin_loader = getattr(app.state, "plugin_loader", None)
    if plugin_loader is not None:
        from app.plugins.hooks import LifecyclePhase

        try:
            await plugin_loader.run_lifecycle(LifecyclePhase.SHUTDOWN)
        except Exception:
            logger.warning("Plugin shutdown error (continuing cleanup)", exc_info=True)

    purge_task = getattr(app.state, "purge_task", None)
    flush_task = getattr(app.state, "flush_task", None)
    ws_task = getattr(app.state, "ws_task", None)
    sync_task = getattr(app.state, "sync_task", None)
    alarm_monitor = getattr(app.state, "alarm_monitor", None)
    ws_client = getattr(app.state, "ws_client", None)
    ha_client = getattr(app.state, "ha_client", None)
    cache_manager = getattr(app.state, "cache_manager", None)
    entity_index_init_task = getattr(app.state, "entity_index_init_task", None)
    timer_scheduler = getattr(app.state, "timer_scheduler", None)

    if alarm_monitor:
        await alarm_monitor.stop()

    if timer_scheduler:
        try:
            await timer_scheduler.stop()
        except Exception:
            logger.warning("TimerScheduler.stop failed", exc_info=True)

    if ws_client:
        await ws_client.disconnect()

    # Cancel background tasks and await them to ensure cleanup completes
    tasks_to_cancel = []
    if purge_task and not purge_task.done():
        purge_task.cancel()
        tasks_to_cancel.append(purge_task)
    if flush_task and not flush_task.done():
        flush_task.cancel()
        tasks_to_cancel.append(flush_task)
    if ws_task and not ws_task.done():
        ws_task.cancel()
        tasks_to_cancel.append(ws_task)
    if sync_task and not sync_task.done():
        sync_task.cancel()
        tasks_to_cancel.append(sync_task)
    if entity_index_init_task and not entity_index_init_task.done():
        entity_index_init_task.cancel()
        tasks_to_cancel.append(entity_index_init_task)
    validator_task = getattr(app.state, "cache_validator_task", None)
    if validator_task and not validator_task.done():
        validator_task.cancel()
        tasks_to_cancel.append(validator_task)
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

    # Cancel SSE ticker tasks with timeout
    sse_ticker_tasks = getattr(app.state, "sse_ticker_tasks", [])
    for task in sse_ticker_tasks:
        if not task.done():
            task.cancel()
    if sse_ticker_tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*sse_ticker_tasks, return_exceptions=True), timeout=5.0)
        except TimeoutError:
            logger.warning("SSE ticker tasks did not shut down within 5 seconds")

    # Flush buffered cache hit-count updates before closing stores
    try:
        if cache_manager:
            cache_manager.flush_pending()
    except Exception:
        logger.warning("Cache flush_pending failed at shutdown", exc_info=True)

    from app.cache.vector_store import close_vector_store

    mcp_tool_manager = getattr(app.state, "mcp_tool_manager", None)
    mcp_registry = getattr(app.state, "mcp_registry", None)
    if mcp_tool_manager:
        mcp_tool_manager.invalidate_all()
    if mcp_registry:
        await mcp_registry.disconnect_all()
    if ha_client:
        await ha_client.close()
    close_vector_store()

    cache_store = getattr(app.state, "cache_store", None)
    if cache_store is not None:
        try:
            cache_store.close()
        except Exception:
            logger.warning("Failed to close SQLite cache store", exc_info=True)

    from app.db.schema import close_db

    await close_db()
    logger.info("Shutdown complete")
