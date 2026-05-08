"""Transport abstraction for in-process and HTTP agent communication."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import nullcontext

from app.a2a.protocol import (
    INTERNAL_ERROR,
    TIMEOUT_ERROR,
    JsonRpcResponse,
    JsonRpcStreamChunk,
    error_response,
    success_response,
)
from app.a2a.registry import AgentRegistry
from app.ha_client.rest import allow_internal_ha_service_calls
from app.models.agent import AgentTask

logger = logging.getLogger(__name__)


# Explicit allow-list of handler class names permitted for internal HA service calls.
# More robust than module string matching which is trivially spoofed.
_ALLOWED_INTERNAL_HA_SCOPE: frozenset[str] = frozenset(
    {
        "AutomationAgent",
        "ClimateAgent",
        "CoverAgent",
        "FillerAgent",
        "GeneralAgent",
        "LightAgent",
        "MediaAgent",
        "MusicAgent",
        "OrchestratorAgent",
        "RewriteAgent",
        "SceneAgent",
        "SecurityAgent",
        "SendAgent",
        "TimerAgent",
        "VacuumAgent",
    }
)


def _internal_ha_service_call_scope(handler):
    class_name = type(handler).__name__
    if class_name in _ALLOWED_INTERNAL_HA_SCOPE:
        return allow_internal_ha_service_calls(class_name)
    return nullcontext()


class Transport(ABC):
    """Abstract transport interface for agent communication."""

    @abstractmethod
    async def send(self, agent_id: str, task: AgentTask, request_id: str) -> JsonRpcResponse: ...

    @abstractmethod
    async def stream(
        self, agent_id: str, task: AgentTask, request_id: str
    ) -> AsyncGenerator[JsonRpcStreamChunk, None]: ...


class InProcessTransport(Transport):
    """Direct async function calls to agent handlers. Near-zero overhead."""

    _DEFAULT_TIMEOUT = 120  # seconds

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    async def send(self, agent_id: str, task: AgentTask, request_id: str) -> JsonRpcResponse:
        handler = await self._registry._get_handler_for_transport(agent_id)
        if handler is None:
            return error_response(request_id, INTERNAL_ERROR, f"Agent not found: {agent_id}")
        try:
            with _internal_ha_service_call_scope(handler):
                result = await asyncio.wait_for(
                    handler.handle_task(task),
                    timeout=self._DEFAULT_TIMEOUT,
                )
            # Normalize TaskResult or raw dict to dict for JSON-RPC
            if hasattr(result, "model_dump"):
                result = result.model_dump(exclude_none=True)
            return success_response(request_id, result)
        except TimeoutError:
            logger.warning("Agent %s timed out after %ds", agent_id, self._DEFAULT_TIMEOUT)
            return error_response(request_id, TIMEOUT_ERROR, f"Agent timed out: {agent_id}")
        except Exception:
            logger.exception("Agent %s failed on handle_task", agent_id)
            return error_response(request_id, INTERNAL_ERROR, f"Agent error: {agent_id}")

    async def stream(self, agent_id: str, task: AgentTask, request_id: str) -> AsyncGenerator[JsonRpcStreamChunk, None]:
        handler = await self._registry._get_handler_for_transport(agent_id)
        if handler is None:
            yield JsonRpcStreamChunk(
                id=request_id,
                result={"token": "", "done": True, "error": f"Agent not found: {agent_id}"},
                done=True,
            )
            return
        try:
            with _internal_ha_service_call_scope(handler):
                async for token_dict in handler.handle_task_stream(task):
                    yield JsonRpcStreamChunk(
                        id=request_id,
                        result=token_dict,
                        done=token_dict.get("done", False),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Agent %s failed on handle_task_stream", agent_id)
            yield JsonRpcStreamChunk(
                id=request_id,
                result={
                    "token": "",
                    "done": True,
                    "error": f"{agent_id}: {type(exc).__name__}: {exc}",
                },
                done=True,
            )
