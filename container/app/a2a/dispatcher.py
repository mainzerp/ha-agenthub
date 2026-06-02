"""Message dispatcher routing A2A messages to agents."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from app.a2a.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    AgentDiscoverParams,
    JsonRpcRequest,
    JsonRpcResponse,
    MessageSendParams,
    MessageStreamParams,
    error_response,
    success_response,
)
from app.a2a.registry import AgentRegistry
from app.a2a.transport import Transport
from app.models.agent import AgentTask

logger = logging.getLogger(__name__)


class Dispatcher:
    """Routes incoming JSON-RPC 2.0 requests to the correct handler."""

    def __init__(self, registry: AgentRegistry, transport: Transport) -> None:
        self._registry = registry
        self._transport = transport

    async def dispatch(self, request: JsonRpcRequest) -> Any:
        """Dispatch a non-streaming JSON-RPC request."""
        method = request.method

        if method == "message/send":
            return await self._handle_message_send(request)
        elif method == "agent/discover":
            return await self._handle_agent_discover(request)
        elif method == "agent/list":
            return await self._handle_agent_list(request)
        else:
            return error_response(request.id, METHOD_NOT_FOUND, f"Method not found: {method}")

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
            params = MessageStreamParams(**{k: v for k, v in raw_params.items() if k != "_span_collector"})
        except Exception as exc:
            yield {
                "token": "",
                "done": True,
                "error": f"Invalid params: {exc}",
            }
            return

        task_data = params.task
        task = task_data if isinstance(task_data, AgentTask) else AgentTask(**task_data)
        task.span_collector = span_collector
        async for chunk in self._transport.stream(params.agent_id, task, request.id):
            yield chunk

    async def _handle_message_send(self, request: JsonRpcRequest) -> Any:
        try:
            raw_params = request.params or {}
            span_collector = raw_params.get("_span_collector")
            params = MessageSendParams(**{k: v for k, v in raw_params.items() if k != "_span_collector"})
        except Exception as exc:
            return error_response(request.id, INVALID_PARAMS, f"Invalid params: {exc}")

        task_data = params.task
        task = task_data if isinstance(task_data, AgentTask) else AgentTask(**task_data)
        task.span_collector = span_collector
        return await self._transport.send(params.agent_id, task, request.id)

    async def _handle_agent_discover(self, request: JsonRpcRequest) -> JsonRpcResponse:
        try:
            params = AgentDiscoverParams(**(request.params or {}))
        except Exception as exc:
            return error_response(request.id, INVALID_PARAMS, f"Invalid params: {exc}")

        card = await self._registry.discover(params.agent_id)
        if card is None:
            return error_response(request.id, INVALID_PARAMS, f"Agent not found: {params.agent_id}")
        return success_response(request.id, card.model_dump())

    async def _handle_agent_list(self, request: JsonRpcRequest) -> JsonRpcResponse:
        agents = await self._registry.list_agents()
        return success_response(request.id, {"agents": [a.model_dump() for a in agents]})
