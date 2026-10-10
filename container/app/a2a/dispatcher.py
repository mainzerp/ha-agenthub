"""Message dispatcher routing A2A messages to agents."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import Any

from pydantic import BaseModel

from app.a2a.protocol import JsonRpcRequest
from app.a2a.registry import AgentRegistry
from app.a2a.transport import Transport
from app.models.agent import BackgroundTask, DispatchTask, IngressTask

logger = logging.getLogger(__name__)

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_TIMEOUT_ERROR = -32000


class A2ADispatchError(RuntimeError):
    """A ``message/send`` request the dispatcher cannot route (bad method or params).

    Raised -- like transport failures (``RuntimeError``) -- instead of
    returning a JSON-RPC error envelope, so ``dispatch()`` callers handle
    one shape: the raw agent result on success, an exception otherwise.
    The message is a short generic string that is safe to surface; the
    validation detail is only logged.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class _JsonRpcError(BaseModel):
    code: int
    message: str
    data: Any | None = None


class _JsonRpcResponse(BaseModel):
    jsonrpc: str = "2.0"
    result: Any | None = None
    error: _JsonRpcError | None = None
    id: str


class _MessageSendParams(BaseModel):
    agent_id: str
    task: Any


class _MessageStreamParams(BaseModel):
    agent_id: str
    task: Any


class _AgentDiscoverParams(BaseModel):
    agent_id: str


def _validate_task(agent_id: str, task_data: Any) -> IngressTask | DispatchTask | BackgroundTask:
    """Coerce ``params.task`` into the task type matching the dispatch stage.

    Model instances pass through unchanged. Dict payloads are validated by
    target: orchestrator-bound payloads are ``BackgroundTask`` when they
    carry no ``description`` key, else ``IngressTask``; agent-bound payloads
    are ``DispatchTask``.
    """
    if isinstance(task_data, (IngressTask, DispatchTask, BackgroundTask)):
        return task_data
    if agent_id == "orchestrator":
        if isinstance(task_data, dict) and "description" not in task_data:
            return BackgroundTask(**task_data)
        return IngressTask(**task_data)
    return DispatchTask(**task_data)


def _error_response(request_id: str, code: int, message: str, data: Any | None = None) -> _JsonRpcResponse:
    return _JsonRpcResponse(
        id=request_id,
        error=_JsonRpcError(code=code, message=message, data=data),
    )


def _success_response(request_id: str, result: Any) -> _JsonRpcResponse:
    return _JsonRpcResponse(id=request_id, result=result)


class Dispatcher:
    """Routes incoming JSON-RPC 2.0 requests to the correct handler."""

    def __init__(self, registry: AgentRegistry, transport: Transport) -> None:
        self._registry = registry
        self._transport = transport

    async def dispatch(self, request: JsonRpcRequest) -> Any:
        """Dispatch a non-streaming JSON-RPC request.

        ``message/send`` returns the raw agent result (``TaskResult`` /
        dict) and raises ``RuntimeError`` on failure: ``A2ADispatchError``
        for an unknown method or invalid params, a transport ``RuntimeError``
        for agent failures. The ``agent/discover`` and ``agent/list``
        management methods return JSON-RPC envelopes.
        """
        method = request.method

        if method == "message/send":
            return await self._handle_message_send(request)
        elif method == "agent/discover":
            return await self._handle_agent_discover(request)
        elif method == "agent/list":
            return await self._handle_agent_list(request)
        else:
            logger.warning("A2A dispatch: unknown method %r (request %s)", method, request.id)
            raise A2ADispatchError(_METHOD_NOT_FOUND, f"Method not found: {method}")

    async def dispatch_stream(self, request: JsonRpcRequest) -> AsyncGenerator[dict[str, Any], None]:
        """Dispatch a streaming JSON-RPC request (message/stream)."""
        if request.method != "message/stream":
            yield {
                "token": "",
                "done": True,
                "error": f"Method not found: {request.method}",
            }
            return

        try:
            raw_params = request.params or {}
            span_collector = raw_params.get("_span_collector")
            params = _MessageStreamParams(**{k: v for k, v in raw_params.items() if k != "_span_collector"})
            task = _validate_task(params.agent_id, params.task)
        except Exception as exc:
            # The validation detail (a pydantic dump) is logged, never put
            # into the chunk: chunk errors can reach user-facing speech.
            logger.warning("A2A stream dispatch: invalid params (request %s): %s", request.id, exc)
            yield {
                "token": "",
                "done": True,
                "error": "Invalid params",
            }
            return

        task.span_collector = span_collector
        # Close the inner transport stream deterministically when the
        # consumer stops early (client disconnect, cancellation): otherwise
        # the agent generator is only finalized by the GC, outside the
        # request context.
        stream = self._transport.stream(params.agent_id, task, request.id)
        if hasattr(stream, "aclose"):
            async with contextlib.aclosing(stream):
                async for chunk in stream:
                    yield chunk
        else:
            async for chunk in stream:
                yield chunk

    async def _handle_message_send(self, request: JsonRpcRequest) -> Any:
        try:
            raw_params = request.params or {}
            span_collector = raw_params.get("_span_collector")
            params = _MessageSendParams(**{k: v for k, v in raw_params.items() if k != "_span_collector"})
            task = _validate_task(params.agent_id, params.task)
        except Exception as exc:
            logger.warning("A2A dispatch: invalid params (request %s): %s", request.id, exc)
            raise A2ADispatchError(_INVALID_PARAMS, "Invalid params") from exc

        task.span_collector = span_collector
        return await self._transport.send(params.agent_id, task, request.id)

    async def _handle_agent_discover(self, request: JsonRpcRequest) -> _JsonRpcResponse:
        try:
            params = _AgentDiscoverParams(**(request.params or {}))
        except Exception as exc:
            logger.warning("A2A agent/discover: invalid params (request %s): %s", request.id, exc)
            return _error_response(request.id, _INVALID_PARAMS, "Invalid params")

        card = await self._registry.discover(params.agent_id)
        if card is None:
            return _error_response(request.id, _INVALID_PARAMS, f"Agent not found: {params.agent_id}")
        return _success_response(request.id, card.model_dump())

    async def _handle_agent_list(self, request: JsonRpcRequest) -> _JsonRpcResponse:
        agents = await self._registry.list_agents()
        return _success_response(request.id, {"agents": [a.model_dump() for a in agents]})
