"""Request tracing middleware with trace ID propagation and span collection.

Implemented as pure ASGI middleware so it does not buffer the response body
(SSE/WS first byte must flush immediately).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime

from app.analytics.tracer import SpanCollector

logger = logging.getLogger(__name__)


class TracingMiddleware:
    """Pure ASGI middleware: trace ID per request, SpanCollector, latency log."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            await self._handle_websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = uuid.uuid4().hex[:16]
        # FLOW-MED-9: derive the span source from the route prefix so
        # it is set at construction, not patched post-hoc. HA-facing
        # routes under /api/conversation* and /ws/conversation use
        # ``"ha"``; dashboard chat is ``"chat"``; everything else
        # falls back to ``"api"``. Route handlers that hit this
        # middleware before the final classification can still
        # override by rebuilding the collector.
        path = scope.get("path", "")
        if path.startswith("/api/admin/chat"):
            source: str = "chat"
        elif path.startswith("/api/conversation") or path.startswith("/ws/conversation"):
            source = "ha"
        else:
            source = "api"
        span_collector = SpanCollector(trace_id, source=source)

        # Make trace_id and span_collector available via request.state.
        # Starlette's Request reads state from scope["state"].
        state = scope.setdefault("state", {})
        state["trace_id"] = trace_id
        state["span_collector"] = span_collector

        method = scope.get("method", "")

        logger.info("[%s] %s %s started", trace_id, method, path)
        t0 = time.perf_counter()
        start_time = datetime.now(UTC).isoformat()

        root_span_id = uuid.uuid4().hex[:12]
        state["root_span_id"] = root_span_id
        parent_token = span_collector.push_parent(root_span_id)

        status_code_holder = {"code": 500}
        trace_header = (b"x-trace-id", trace_id.encode("ascii"))

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_code_holder["code"] = message.get("status", 500)
                headers = list(message.get("headers") or [])
                headers.append(trace_header)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status_code_holder["code"] = 500
            raise
        finally:
            span_collector.pop_parent(parent_token)

            duration_ms = (time.perf_counter() - t0) * 1000
            status_code = status_code_holder["code"]

            span_collector.record_root_span(
                {
                    "span_id": root_span_id,
                    "trace_id": trace_id,
                    "span_name": f"{method} {path}",
                    "agent_id": None,
                    "parent_span": None,
                    "start_time": start_time,
                    "duration_ms": round(duration_ms, 2),
                    "status": "ok" if status_code < 400 else "error",
                    "metadata": {"status_code": status_code},
                }
            )

            try:
                await span_collector.flush()
            except Exception:
                logger.warning("Failed to flush spans for trace %s", trace_id, exc_info=True)

            try:
                from app.db.repository import TraceSummaryRepository

                await TraceSummaryRepository.update_duration(trace_id, round(duration_ms, 2))
            except Exception:
                logger.error("Failed to update trace summary duration for %s", trace_id, exc_info=True)

            logger.info(
                "[%s] %s %s -> %d (%.1fms)",
                trace_id,
                method,
                path,
                status_code,
                duration_ms,
            )

    async def _handle_websocket(self, scope, receive, send) -> None:
        """FLOW-WS-TURN-1: ``/ws/conversation`` is a persistent socket
        carrying many independent HA conversation turns. Creating a
        connection-level trace here would overwrite each per-turn
        ``total_duration_ms`` with the full socket lifetime when the
        connection finally closes. Instead, the route handler mints a
        fresh ``SpanCollector`` per inbound message and flushes it
        synchronously, and this middleware only exposes ``source`` and
        a ``ws_per_turn`` marker on scope state. Other WS paths keep
        the legacy per-connection trace behaviour."""
        path = scope.get("path", "")
        per_turn_route = path.startswith("/ws/conversation")
        if per_turn_route:
            source: str = "ha"
        elif path.startswith("/api/admin/chat"):
            source = "chat"
        else:
            source = "api"

        state = scope.setdefault("state", {})
        state["source"] = source
        state["ws_per_turn"] = per_turn_route

        if per_turn_route:
            # The route owns the trace boundary; do not create a
            # connection-level collector or write a connection-level
            # ``trace_summary`` row.
            state.pop("trace_id", None)
            state.pop("span_collector", None)
            state.pop("root_span_id", None)
            logger.info("WS %s connected (per-turn tracing)", path)
            try:
                await self.app(scope, receive, send)
            finally:
                logger.info("WS %s closed", path)
            return

        # Legacy per-connection behaviour for any other WS route.
        trace_id = uuid.uuid4().hex[:16]
        span_collector = SpanCollector(trace_id, source=source)
        state["trace_id"] = trace_id
        state["span_collector"] = span_collector

        root_span_id = uuid.uuid4().hex[:12]
        state["root_span_id"] = root_span_id
        parent_token = span_collector.push_parent(root_span_id)

        t0 = time.perf_counter()
        start_time = datetime.now(UTC).isoformat()
        logger.info("[%s] WS %s connected", trace_id, path)

        try:
            await self.app(scope, receive, send)
        finally:
            span_collector.pop_parent(parent_token)

            duration_ms = (time.perf_counter() - t0) * 1000
            span_collector.add_root_span(
                {
                    "span_id": root_span_id,
                    "trace_id": trace_id,
                    "span_name": f"WS {path}",
                    "agent_id": None,
                    "parent_span": None,
                    "start_time": start_time,
                    "duration_ms": round(duration_ms, 2),
                    "status": "ok",
                    "metadata": {"status_code": 101},
                }
            )

            try:
                await span_collector.flush()
            except Exception:
                logger.warning("Failed to flush WS spans for trace %s", trace_id, exc_info=True)

            try:
                from app.db.repository import TraceSummaryRepository

                await TraceSummaryRepository.update_duration(trace_id, round(duration_ms, 2))
            except Exception:
                pass

            logger.info("[%s] WS %s closed (%.1fms)", trace_id, path, duration_ms)
