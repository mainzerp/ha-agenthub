"""Transport abstraction for in-process and HTTP agent communication."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import nullcontext
from typing import Any

from app.a2a.registry import AgentRegistry
from app.ha_client.rest import allow_internal_ha_service_calls
from app.models.agent import BackgroundTask, DispatchTask, IngressTask

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
    async def send(self, agent_id: str, task: IngressTask | DispatchTask | BackgroundTask, request_id: str) -> Any: ...

    @abstractmethod
    def stream(
        self, agent_id: str, task: IngressTask | DispatchTask | BackgroundTask, request_id: str
    ) -> AsyncGenerator[dict[str, Any], None]: ...


class InProcessTransport(Transport):
    """Direct async function calls to agent handlers. Near-zero overhead."""

    _DEFAULT_TIMEOUT = 120  # seconds

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    async def send(self, agent_id: str, task: IngressTask | DispatchTask | BackgroundTask, request_id: str) -> Any:
        handler = await self._registry._get_handler_for_transport(agent_id)
        if handler is None:
            raise RuntimeError(f"Agent not found: {agent_id}")
        try:
            with _internal_ha_service_call_scope(handler):
                result = await asyncio.wait_for(
                    handler.handle_task(task),  # type: ignore[arg-type]  # dispatch invariant: orchestrator accepts IngressTask | BackgroundTask (FLOW_REDEF DP-1)
                    timeout=self._DEFAULT_TIMEOUT,
                )
            return result
        except TimeoutError:
            logger.warning("Agent %s timed out after %ds", agent_id, self._DEFAULT_TIMEOUT)
            raise
        except Exception as e:
            logger.exception("Agent %s failed on handle_task", agent_id)
            raise RuntimeError(f"Agent error: {agent_id}") from e

    async def stream(
        self, agent_id: str, task: IngressTask | DispatchTask | BackgroundTask, request_id: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        handler = await self._registry._get_handler_for_transport(agent_id)
        if handler is None:
            yield {
                "token": "",
                "done": True,
                "error": f"Agent not found: {agent_id}",
            }
            return
        try:
            with _internal_ha_service_call_scope(handler):
                async for token_dict in handler.handle_task_stream(task):  # type: ignore[arg-type]  # dispatch invariant (FLOW_REDEF DP-1)
                    yield token_dict
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Agent %s failed on handle_task_stream", agent_id)
            yield {
                "token": "",
                "done": True,
                "error": f"{agent_id}: internal error",
            }
