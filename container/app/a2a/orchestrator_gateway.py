"""Restricted gateway for dispatching work into the orchestrator."""

from __future__ import annotations

import uuid
from typing import Any

from app.a2a.dispatcher import Dispatcher
from app.a2a.protocol import JsonRpcRequest
from app.a2a.registry import AgentRegistry
from app.models.agent import AgentCard, AgentTask, BackgroundEvent, BackgroundEventType, TaskContext

__all__ = ["AgentCatalog", "OrchestratorGateway"]


class AgentCatalog:
    """Read-only agent discovery surface for plugins and internal producers."""

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    async def list_agents(self) -> list[AgentCard]:
        return await self._registry.list_agents()

    async def discover(self, agent_id: str) -> AgentCard | None:
        return await self._registry.discover(agent_id)


class OrchestratorGateway:
    """Dispatches only to the orchestrator through the existing A2A dispatcher."""

    def __init__(self, dispatcher: Dispatcher) -> None:
        self._dispatcher = dispatcher

    async def dispatch_to_orchestrator(
        self,
        task: AgentTask,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request = JsonRpcRequest(
            method="message/send",
            params={
                "agent_id": "orchestrator",
                "task": task.model_dump(exclude_none=True),
            },
            id=request_id or task.conversation_id or f"orchestrator-gateway-{uuid.uuid4().hex}",
        )
        response = await self._dispatcher.dispatch(request)
        if response.error:
            return {
                "speech": "",
                "error": {
                    "code": "internal",
                    "message": response.error.message,
                    "recoverable": True,
                },
            }
        return response.result or {}

    async def dispatch_text(
        self,
        description: str,
        *,
        user_text: str | None = None,
        conversation_id: str | None = None,
        context: TaskContext | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        task = AgentTask(
            description=description,
            user_text=user_text or description,
            conversation_id=conversation_id,
            context=context,
        )
        return await self.dispatch_to_orchestrator(task, request_id=request_id)

    async def dispatch_background_event(
        self,
        event_type: BackgroundEventType,
        payload: dict[str, Any] | None = None,
        *,
        description: str,
        user_text: str | None = None,
        conversation_id: str | None = None,
        context: TaskContext | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        event_context = context.model_copy(deep=True) if context is not None else TaskContext()
        event_context.source = "background"
        event_context.background_event = BackgroundEvent(
            event_type=event_type,
            payload=dict(payload or {}),
        )
        task = AgentTask(
            description=description,
            user_text=user_text or description,
            conversation_id=conversation_id,
            context=event_context,
        )
        return await self.dispatch_to_orchestrator(task, request_id=request_id)
