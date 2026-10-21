"""Server-Sent Events (SSE) endpoints for live dashboard updates.

Five streams are provided:
- /api/admin/overview/stream
- /api/admin/health/stream
- /api/admin/timers/stream
- /api/admin/traces/stream
- /api/admin/activity/stream (live orchestrator/agent span activity)

The activity stream and the recently-used-agents list are also served to
server-to-server clients at /api/activity/stream and /api/activity/recent
(Bearer API key).

Each uses an in-process asyncio.Queue per subscriber. Multi-worker scaling
is not in scope; the container ships a single worker today.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.analytics.agent_recency import MAX_RECENT_LIMIT, recent_agents, record_agent_use
from app.security.auth import require_admin_session, require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["sse"])
# Server-to-server variant of the activity endpoints (Bearer API key, same
# credential as /api/conversation). The handlers are shared with the admin routes.
api_router = APIRouter(prefix="/api/activity", tags=["activity"], dependencies=[Depends(require_api_key)])

# In-memory broker: topic -> list of queues
_brokers: dict[str, list[asyncio.Queue]] = {}
# Event loop that owns the queues of a topic (recorded on subscribe) so that
# synchronous publishers can hop onto it when called from another thread.
_topic_loops: dict[str, asyncio.AbstractEventLoop] = {}

# Headers shared by every SSE stream. ``X-Accel-Buffering: no`` stops nginx
# style reverse proxies (HA ingress, Nginx Proxy Manager) from buffering frames.
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

ACTIVITY_TOPIC = "activity"

# Span names that are mirrored onto the activity stream. Only these names (plus
# the ``classify.`` prefix below) ever leave the process; everything else is
# dropped before a payload is built.
ACTIVITY_SPAN_NAMES: frozenset[str] = frozenset(
    {
        "dispatch",
        "dispatch_content",
        "dispatch_send",
        "dispatch_fallback",
        "classify",
        "cache_lookup",
        "rewrite",
        "return",
    }
)
ACTIVITY_SPAN_PREFIXES: tuple[str, ...] = ("classify.",)


async def _subscribe(topic: str) -> asyncio.Queue:
    """Return a new queue for the given topic and register it."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    _brokers.setdefault(topic, []).append(queue)
    _topic_loops[topic] = asyncio.get_running_loop()
    return queue


def _unsubscribe(topic: str, queue: asyncio.Queue) -> None:
    """Remove a queue from a topic."""
    subscribers = _brokers.get(topic)
    if subscribers and queue in subscribers:
        subscribers.remove(queue)


def _publish_nowait(topic: str, payload: dict) -> None:
    """Broadcast a payload to all subscribers of a topic without awaiting.

    Must run on the loop that owns the subscriber queues. Drops oldest events
    when a subscriber's queue is full.
    """
    subscribers = _brokers.get(topic)
    if not subscribers:
        return
    dead = []
    message = json.dumps(payload)
    for q in list(subscribers):
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


async def _publish(topic: str, payload: dict) -> None:
    """Broadcast a payload to all subscribers of a topic.

    Drops oldest events when a subscriber's queue is full.
    """
    _publish_nowait(topic, payload)


# ---------------------------------------------------------------------------
# Activity stream publishing (called from the tracing path)
# ---------------------------------------------------------------------------


def has_activity_subscribers() -> bool:
    """Return True when at least one client listens to the activity stream."""
    return bool(_brokers.get(ACTIVITY_TOPIC))


def _is_activity_span(name: Any) -> bool:
    return isinstance(name, str) and (name in ACTIVITY_SPAN_NAMES or name.startswith(ACTIVITY_SPAN_PREFIXES))


def build_activity_event(event_type: str, span: dict[str, Any], source: str | None) -> dict[str, Any] | None:
    """Build the activity payload for a span, or None when it is not allowlisted.

    Privacy: only ids, the span name, timing and status are copied. Span
    metadata and any other span field are never read.
    """
    name = span.get("span_name")
    if event_type not in ("span_start", "span_end") or not _is_activity_span(name):
        return None
    is_end = event_type == "span_end"
    ts = span.get("end_time" if is_end else "start_time") or datetime.now(UTC).isoformat()
    duration = span.get("duration_ms") if is_end else None
    agent_id = span.get("agent_id")
    return {
        "type": event_type,
        "span_id": span.get("span_id"),
        "trace_id": span.get("trace_id"),
        "span_name": name,
        "agent_id": agent_id if isinstance(agent_id, str) else None,
        "ts": ts,
        "status": ("error" if span.get("status") == "error" else "ok") if is_end else None,
        "duration_ms": duration if isinstance(duration, int | float) and not isinstance(duration, bool) else None,
        "source": source if isinstance(source, str) else None,
    }


def publish_activity(event: dict[str, Any]) -> None:
    """Publish an already-built activity event to all activity subscribers.

    Synchronous and non-blocking (``put_nowait`` only). Queues belong to the
    loop that served the subscriber; when called from another thread the
    publish is handed over with ``call_soon_threadsafe`` because asyncio
    queues are not thread-safe. May raise; callers on the request path must
    use :func:`publish_span_activity`.
    """
    if not has_activity_subscribers():
        return
    owner = _topic_loops.get(ACTIVITY_TOPIC)
    if owner is None:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is owner:
        _publish_nowait(ACTIVITY_TOPIC, event)
    else:
        owner.call_soon_threadsafe(_publish_nowait, ACTIVITY_TOPIC, event)


def publish_span_activity(event_type: str, span: dict[str, Any], source: str | None) -> None:
    """Best-effort mirror of a span start/end onto the activity stream.

    Never raises and does no work (no payload building) while nobody is
    subscribed, so the tracing path stays zero-risk and nearly free. The
    agent recency tracker is updated on every dispatch span start regardless
    of subscribers (one dict update, never raises).
    """
    try:
        if event_type == "span_start":
            record_agent_use(span.get("span_name"), span.get("agent_id"), span.get("start_time"))
    except Exception:
        logger.debug("Failed to record agent use", exc_info=True)
    try:
        if not has_activity_subscribers():
            return
        event = build_activity_event(event_type, span, source)
        if event is not None:
            publish_activity(event)
    except Exception:
        logger.debug("Failed to publish activity event", exc_info=True)


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
        headers=_SSE_HEADERS,
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
        headers=_SSE_HEADERS,
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
        headers=_SSE_HEADERS,
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
        headers=_SSE_HEADERS,
    )


async def _activity_stream_response() -> StreamingResponse:
    """Subscribe to the activity topic and wrap it in an SSE response (shared by both auth variants)."""
    queue = await _subscribe(ACTIVITY_TOPIC)
    return StreamingResponse(
        _sse_generator(queue, ACTIVITY_TOPIC),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


async def _recent_agents_response(request: Request, limit: int) -> dict[str, Any]:
    """Build the ``{"agents": [...]}`` payload from the app's A2A registry (shared by both auth variants)."""
    registry = getattr(request.app.state, "registry", None)
    return {"agents": await recent_agents(limit, registry=registry)}


@router.get("/activity/stream")
async def activity_stream(
    request: Request,
    _session: dict = Depends(require_admin_session),
):
    """SSE stream of live orchestrator/agent span activity (ids, names, timing only)."""
    return await _activity_stream_response()


@router.get("/activity/recent")
async def activity_recent(
    request: Request,
    limit: int = Query(7, ge=1, le=MAX_RECENT_LIMIT),
    _session: dict = Depends(require_admin_session),
):
    """Most recently used agents, most recent first (ids, names, timestamps only)."""
    return await _recent_agents_response(request, limit)


@api_router.get("/stream")
async def api_activity_stream(request: Request):
    """Bearer-authenticated activity SSE stream for server-to-server clients."""
    return await _activity_stream_response()


@api_router.get("/recent")
async def api_activity_recent(request: Request, limit: int = Query(7, ge=1, le=MAX_RECENT_LIMIT)):
    """Bearer-authenticated variant of ``/api/admin/activity/recent``."""
    return await _recent_agents_response(request, limit)
