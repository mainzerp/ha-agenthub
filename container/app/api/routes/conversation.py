"""WebSocket, SSE, and REST conversation endpoints."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app.a2a.protocol import JsonRpcRequest
from app.analytics.tracer import SpanCollector
from app.middleware.rate_limit import WsMessageRateLimiter, rate_limit_conversation
from app.models.agent import AgentTask, TaskContext
from app.models.conversation import ConversationRequest, ConversationResponse, StreamToken
from app.security.auth import require_api_key, require_api_key_ws
from app.security.user_input import prepare_user_text

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversation"])

# Maximum allowed WebSocket message size in bytes (10 KB)
_MAX_WS_MESSAGE_SIZE = 10_000

# The dispatcher is set by main.py during startup
_dispatcher = None


def set_dispatcher(dispatcher) -> None:
    """Called by main.py to inject the A2A dispatcher."""
    global _dispatcher
    _dispatcher = dispatcher


def _build_a2a_request(
    conv_request: ConversationRequest, method: str, span_collector=None, request: Request | None = None
) -> tuple[JsonRpcRequest, AgentTask]:
    """Convert a ConversationRequest into an A2A JsonRpcRequest + AgentTask."""
    # FLOW-CTX-1 (0.18.6) / FLOW-WS-SPAN-1 (P1-6): source comes from the
    # SpanCollector that the TracingMiddleware derived from the route
    # path (WS or HTTP). Default when no collector was provided (pure
    # unit-tests hand-crafting a request) is ``"api"`` so missing
    # context does not silently masquerade as an HA voice call.
    source = getattr(span_collector, "source", "api") if span_collector else "api"
    prepared_text = prepare_user_text(conv_request.text)
    context = TaskContext(
        device_id=conv_request.device_id,
        area_id=conv_request.area_id,
        device_name=conv_request.device_name,
        area_name=conv_request.area_name,
        user_id=conv_request.user_id,
        language=conv_request.language or "en",
        source=source,
        injection_detected=prepared_text.injection_detected,
    )
    task = AgentTask(
        description=prepared_text.text,
        user_text=prepared_text.text,
        conversation_id=conv_request.conversation_id,
        context=context,
    )
    request_id = str(uuid.uuid4())
    # Route all requests through the orchestrator for intent classification
    a2a_request = JsonRpcRequest(
        method=method,
        params={
            "agent_id": "orchestrator",
            "task": task.model_dump(),
            "_span_collector": span_collector,
        },
        id=request_id,
    )
    return a2a_request, task


@router.post("/api/conversation", response_model=ConversationResponse, dependencies=[Depends(rate_limit_conversation)])
async def conversation_rest(
    request: Request,
    conv_request: ConversationRequest,
    _: str = Depends(require_api_key),
):
    """REST endpoint -- full response."""
    # FLOW-MED-9: source is now set by TracingMiddleware from the
    # route path, no post-hoc assignment needed.
    span_collector = getattr(request.state, "span_collector", None)

    a2a_request, _ = _build_a2a_request(conv_request, "message/send", span_collector, request)
    response = await _dispatcher.dispatch(a2a_request)

    if response.error:
        return ConversationResponse(
            speech=f"Error: {response.error.message}",
            conversation_id=conv_request.conversation_id,
        )

    result = response.result or {}
    return ConversationResponse(
        speech=result.get("speech", ""),
        conversation_id=result.get("conversation_id") or conv_request.conversation_id,
        voice_followup=bool(result.get("voice_followup")),
        sanitized=bool(result.get("sanitized", True)),
        directive=result.get("directive"),
        reason=result.get("reason"),
    )


@router.post("/api/conversation/stream", dependencies=[Depends(rate_limit_conversation)])
async def conversation_sse(
    request: Request,
    conv_request: ConversationRequest,
    _: str = Depends(require_api_key),
):
    """SSE streaming endpoint."""
    # FLOW-MED-9: source is now set by TracingMiddleware from the
    # route path.
    span_collector = getattr(request.state, "span_collector", None)
    a2a_request, _ = _build_a2a_request(conv_request, "message/stream", span_collector, request)

    async def generate():
        root_span_id = getattr(request.state, "root_span_id", None)
        parent_token = None
        if span_collector and root_span_id:
            parent_token = span_collector.push_parent(root_span_id)
        try:
            async for chunk in _dispatcher.dispatch_stream(a2a_request):
                token = StreamToken(
                    token=chunk.result.get("token", ""),
                    done=chunk.done,
                    conversation_id=chunk.result.get("conversation_id") if chunk.done else None,
                    mediated_speech=chunk.result.get("mediated_speech") if chunk.done else None,
                    is_filler=chunk.result.get("is_filler", False),
                    error=chunk.result.get("error") if chunk.done else None,
                    voice_followup=bool(chunk.result.get("voice_followup")) if chunk.done else False,
                    sanitized=bool(chunk.result.get("sanitized", True))
                    if chunk.done
                    else not chunk.result.get("is_filler", False),
                    directive=chunk.result.get("directive") if chunk.done else None,
                    reason=chunk.result.get("reason") if chunk.done else None,
                    filler_push=chunk.result.get("filler_push") if not chunk.done else None,
                )
                yield f"data: {token.model_dump_json()}\n\n"
        finally:
            if span_collector and parent_token is not None:
                span_collector.pop_parent(parent_token)
            if span_collector:
                await span_collector.flush()

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.websocket("/ws/conversation")
async def ws_conversation(
    websocket: WebSocket,
    _: str = Depends(require_api_key_ws),
):
    """WebSocket streaming endpoint."""
    await websocket.accept()
    # FLOW-WS-TURN-1: ``/ws/conversation`` is a persistent socket
    # carrying many independent HA conversation turns. The
    # TracingMiddleware deliberately does NOT create a connection-
    # level SpanCollector for this path (that would overwrite each
    # per-turn ``total_duration_ms`` with the connection lifetime
    # when the socket eventually closes). Instead, this handler
    # mints a fresh ``trace_id`` + ``SpanCollector`` + root span per
    # inbound message and flushes them in ``finally`` so the
    # dashboard waterfall reflects exactly one HA turn per trace.
    state = websocket.scope.setdefault("state", {})
    source = state.get("source") or "ha"
    ws_rate_limiter = WsMessageRateLimiter(rate=10.0, burst=20)
    try:
        while True:
            if not await ws_rate_limiter.acquire():
                await websocket.close(code=1008, reason="Rate limit exceeded")
                break
            raw = await websocket.receive_text()
            if len(raw) > _MAX_WS_MESSAGE_SIZE:
                await websocket.send_json({"error": "Message too large", "max_bytes": _MAX_WS_MESSAGE_SIZE})
                continue
            try:
                data = json.loads(raw)
                conv_request = ConversationRequest(**data)
            except Exception as exc:
                await websocket.send_json({"error": f"Invalid request: {exc}"})
                continue

            # Per-turn trace boundary.
            trace_id = uuid.uuid4().hex[:16]
            span_collector = SpanCollector(trace_id, source=source)
            root_span_id = uuid.uuid4().hex[:12]
            parent_token = span_collector.push_parent(root_span_id)

            # Expose to anything still reading scope state during this turn.
            state["trace_id"] = trace_id
            state["span_collector"] = span_collector
            state["root_span_id"] = root_span_id

            a2a_request, _ = _build_a2a_request(conv_request, "message/stream", span_collector)

            t0 = time.perf_counter()
            start_time = datetime.now(UTC).isoformat()
            status = "ok"
            disconnect_during_turn = False
            try:
                async for chunk in _dispatcher.dispatch_stream(a2a_request):
                    token = StreamToken(
                        token=chunk.result.get("token", ""),
                        done=chunk.done,
                        conversation_id=chunk.result.get("conversation_id") if chunk.done else None,
                        mediated_speech=chunk.result.get("mediated_speech") if chunk.done else None,
                        is_filler=chunk.result.get("is_filler", False),
                        error=chunk.result.get("error") if chunk.done else None,
                        voice_followup=bool(chunk.result.get("voice_followup")) if chunk.done else False,
                        sanitized=bool(chunk.result.get("sanitized", True))
                        if chunk.done
                        else not chunk.result.get("is_filler", False),
                        directive=chunk.result.get("directive") if chunk.done else None,
                        reason=chunk.result.get("reason") if chunk.done else None,
                        filler_push=chunk.result.get("filler_push") if not chunk.done else None,
                    )
                    await websocket.send_json(token.model_dump())
            except WebSocketDisconnect:
                status = "error"
                disconnect_during_turn = True
            except Exception:
                status = "error"
                raise
            finally:
                span_collector.pop_parent(parent_token)
                duration_ms = (time.perf_counter() - t0) * 1000
                span_collector._spans.append(
                    {
                        "span_id": root_span_id,
                        "trace_id": trace_id,
                        "span_name": "ws_turn",
                        "agent_id": None,
                        "parent_span": None,
                        "start_time": start_time,
                        "duration_ms": round(duration_ms, 2),
                        "status": status,
                        "metadata": {"status_code": 101, "ws_path": "/ws/conversation"},
                    }
                )
                try:
                    await span_collector.flush()
                except Exception:
                    logger.warning("Failed to flush per-turn spans for trace %s", trace_id, exc_info=True)
                # Clear scope state so the next iteration cannot
                # accidentally read stale values before the next mint.
                state.pop("trace_id", None)
                state.pop("span_collector", None)
                state.pop("root_span_id", None)

            if disconnect_during_turn:
                raise WebSocketDisconnect()
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected")
