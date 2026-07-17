"""Server-Sent Events (SSE) endpoints for live dashboard updates.

Four streams are provided:
- /api/admin/overview/stream
- /api/admin/health/stream
- /api/admin/timers/stream
- /api/admin/traces/stream

Each uses an in-process asyncio.Queue per subscriber. Multi-worker scaling
is not in scope; the container ships a single worker today.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.security.auth import require_admin_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["sse"])

# In-memory broker: topic -> list of queues
_brokers: dict[str, list[asyncio.Queue]] = {}


async def _subscribe(topic: str) -> asyncio.Queue:
    """Return a new queue for the given topic and register it."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    _brokers.setdefault(topic, []).append(queue)
    return queue


def _unsubscribe(topic: str, queue: asyncio.Queue) -> None:
    """Remove a queue from a topic."""
    subscribers = _brokers.get(topic)
    if subscribers and queue in subscribers:
        subscribers.remove(queue)


async def _publish(topic: str, payload: dict) -> None:
    """Broadcast a payload to all subscribers of a topic.

    Drops oldest events when a subscriber's queue is full.
    """
    subscribers = _brokers.get(topic)
    if not subscribers:
        return
    dead = []
    message = json.dumps(payload)
    for q in subscribers:
        # Drop oldest events until there is room (or the queue is empty).
        # The ``full()`` snapshot is racy, so ``put_nowait`` below still
        # handles ``QueueFull`` as the failure path.
        while q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            dead.append(q)
    for d in dead:
        if d in subscribers:
            subscribers.remove(d)


async def _sse_generator(
    queue: asyncio.Queue,
    topic: str,
    keepalive_interval: float = 15.0,
) -> AsyncGenerator[str, None]:
    """Yield SSE formatted lines from a queue."""
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=keepalive_interval)
                yield f"event: {topic}\ndata: {message}\n\n"
            except TimeoutError:
                yield ": keep-alive\n\n"
    except asyncio.CancelledError:
        raise
    finally:
        _unsubscribe(topic, queue)


# ---------------------------------------------------------------------------
# Periodic tickers that publish to topics
# ---------------------------------------------------------------------------


async def _overview_ticker(app) -> None:
    """Publish overview metrics every 30 seconds."""
    while True:
        await asyncio.sleep(30)
        try:
            # Minimal metadata-only payload (no entity-level data)
            payload = {"t": "overview", "ts": asyncio.get_event_loop().time()}
            await _publish("overview", payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("overview ticker error")


async def _health_ticker(app) -> None:
    """Publish health heartbeat every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        try:
            payload = {"t": "health", "ts": asyncio.get_event_loop().time()}
            await _publish("health", payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("health ticker error")


async def _timers_ticker(app) -> None:
    """Publish timers heartbeat every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        try:
            payload = {"t": "timers", "ts": asyncio.get_event_loop().time()}
            await _publish("timers", payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("timers ticker error")


async def _traces_ticker(app) -> None:
    """Publish traces heartbeat every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        try:
            payload = {"t": "traces", "ts": asyncio.get_event_loop().time()}
            await _publish("traces", payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("traces ticker error")


def _log_task_exception(task: asyncio.Task) -> None:
    """Done callback that logs unhandled exceptions from SSE ticker tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None and not isinstance(exc, asyncio.CancelledError):
        logger.error("SSE ticker task %r raised an exception", task.get_name(), exc_info=exc)


def register_sse_tickers(app) -> None:
    """Register SSE background tickers at application startup."""
    existing = getattr(app.state, "sse_ticker_tasks", [])
    for task in existing:
        if not task.done():
            task.cancel()
    app.state.sse_ticker_tasks = []
    tasks = [
        asyncio.create_task(_overview_ticker(app)),
        asyncio.create_task(_health_ticker(app)),
        asyncio.create_task(_timers_ticker(app)),
        asyncio.create_task(_traces_ticker(app)),
    ]
    for task in tasks:
        task.add_done_callback(_log_task_exception)
    app.state.sse_ticker_tasks.extend(tasks)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/overview/stream")
async def overview_stream(
    request: Request,
    _session: dict = Depends(require_admin_session),
):
    """SSE stream for overview page updates."""
    queue = await _subscribe("overview")
    return StreamingResponse(
        _sse_generator(queue, "overview"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/health/stream")
async def health_stream(
    request: Request,
    _session: dict = Depends(require_admin_session),
):
    """SSE stream for system health updates."""
    queue = await _subscribe("health")
    return StreamingResponse(
        _sse_generator(queue, "health"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/timers/stream")
async def timers_stream(
    request: Request,
    _session: dict = Depends(require_admin_session),
):
    """SSE stream for timers page updates."""
    queue = await _subscribe("timers")
    return StreamingResponse(
        _sse_generator(queue, "timers"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/traces/stream")
async def traces_stream(
    request: Request,
    _session: dict = Depends(require_admin_session),
):
    """SSE stream for traces list updates."""
    queue = await _subscribe("traces")
    return StreamingResponse(
        _sse_generator(queue, "traces"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
