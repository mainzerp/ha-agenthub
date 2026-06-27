"""Filler coordinator extracted from OrchestratorAgent.

Owns the filler *decision* helpers: ``should_send_filler``,
``get_filler_threshold_ms`` and ``invoke_filler_agent``.

The actual filler *race* orchestration (the sequential-send
``asyncio.wait`` form and the streaming queue form) intentionally stays on the
orchestrator. The streaming token-relay primitive
(``_stream_with_filler`` / ``_reader``) is a P3-9 invariant that must not be
unified with the non-streaming path; the two race forms operate on
fundamentally different primitives (a coroutine future vs. an async-generator
queue) and cannot share a helper without changing observable timing, so both
remain in place exactly as-is. This module therefore relocates the three
reusable decision helpers only.

The dependencies (``settings_repo``, ``agent_registry``, ``dispatcher``,
``dispatch_manager``) are injected at construction time, matching the existing
:class:`~app.agents.dispatch_manager.DispatchManager` /
:class:`~app.agents.cache_orchestrator.CacheOrchestrator` pattern. Because the
orchestrator is constructed inside ``@patch("app.agents.orchestrator.SettingsRepository")``
decorators during tests, the injected ``settings_repo`` is the same mock object
the tests then mutate, so live ``get_value`` changes (e.g.
``test_should_send_filler_picks_up_db_change``) are observed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.a2a._request import build_send_request
from app.models.agent import AgentTask, TaskContext

logger = logging.getLogger(__name__)


class FillerCoordinator:
    """Decides whether/when to emit a filler phrase and generates it via A2A."""

    def __init__(
        self,
        settings_repo,
        agent_registry,
        dispatcher,
        dispatch_manager,
    ) -> None:
        self._settings_repo = settings_repo
        self._agent_registry = agent_registry
        self._dispatcher = dispatcher
        self._dispatch_manager = dispatch_manager

    async def should_send_filler(self, target_agent: str) -> bool:
        """Check if filler is enabled and the target agent is expected to be slow."""
        try:
            val = await self._settings_repo.get_value("filler.enabled", "false")
            enabled = (val or "false").lower() == "true"
        except (ValueError, TypeError):
            enabled = False
        if not enabled:
            return False
        card = await self._agent_registry.get_agent_card(target_agent)
        if card is None:
            return False
        return card.expected_latency == "high"

    async def get_filler_threshold_ms(self) -> int:
        """Read filler threshold from DB (live, not cached)."""
        try:
            val = await self._settings_repo.get_value("filler.threshold_ms", "1000")
            return int(val or "1000")
        except (ValueError, TypeError):
            return 1000

    async def invoke_filler_agent(self, user_text: str, target_agent: str, language: str) -> str | None:
        """Call the filler-agent via the A2A dispatcher to generate a filler phrase.

        Returns the filler text or None if generation fails.
        """
        try:
            context = TaskContext(language=language)
            filler_task = AgentTask(
                description=f"generate_filler:{target_agent}",
                user_text=user_text,
                context=context,
            )
            request = build_send_request(
                "filler-agent",
                filler_task,
                request_id=f"filler-{uuid.uuid4().hex[:8]}",
            )
            response = await self._dispatcher.dispatch(request)
            result_data = self._dispatch_manager.normalize_agent_result(response, agent_id="filler-agent")
            speech = result_data.get("speech", "")
            if not speech or not speech.strip():
                logger.warning("Filler agent returned empty speech; no filler will be spoken")
                return None
            return speech.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Filler agent invocation failed", exc_info=True)
            return None
