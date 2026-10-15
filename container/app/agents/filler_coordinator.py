"""Filler coordinator extracted from OrchestratorAgent.

Owns the filler *decision* helpers: ``should_send_filler``,
``should_start_speculative_filler``, ``get_filler_threshold_ms`` and
``invoke_filler_agent``, plus :class:`FillerGeneration`, the timed handle on
one filler-generation task.

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
import time
import uuid
from collections.abc import Awaitable, Callable

from app.a2a._request import build_send_request
from app.models.agent import DispatchTask, TaskContext

logger = logging.getLogger(__name__)


class FillerGeneration:
    """One filler-generation task plus its real timing.

    ``start_ms`` / ``end_ms`` are milliseconds since the request start
    (``t0``, a ``time.perf_counter()`` value) so the ``filler_generate``
    span reflects the actual generation window instead of the threshold
    race. ``speculative`` marks a task started before classification (its
    target agent was unknown at start time).

    The task wraps a coroutine *factory* rather than a coroutine object: a
    task cancelled before its first step then never leaves an un-awaited
    coroutine behind.
    """

    def __init__(
        self,
        factory: Callable[[], Awaitable[str | None]],
        *,
        t0: float,
        speculative: bool,
    ) -> None:
        self._t0 = t0
        self.speculative = speculative
        self.start_ms = self.elapsed_ms()
        self.end_ms: float | None = None
        self.task: asyncio.Task[str | None] = asyncio.create_task(self._run(factory))

    def elapsed_ms(self) -> float:
        """Milliseconds since the request start."""
        return (time.perf_counter() - self._t0) * 1000

    async def _run(self, factory: Callable[[], Awaitable[str | None]]) -> str | None:
        try:
            return await factory()
        finally:
            # Completion, failure and cancellation all close the window.
            self.end_ms = self.elapsed_ms()

    def duration_ms(self) -> float:
        """Generation time; up to now while the task never ran to its end."""
        end_ms = self.end_ms if self.end_ms is not None else self.elapsed_ms()
        return max(0.0, end_ms - self.start_ms)


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

    async def should_start_speculative_filler(self) -> bool:
        """Check whether filler generation may start before classification.

        True when ``filler.enabled`` and ``filler.speculative_start`` are both
        on. Both reads hit the TTL-cached settings store, so the check does
        not delay the classification call.
        """
        try:
            enabled = (await self._settings_repo.get_value("filler.enabled", "false") or "false").lower() == "true"
            if not enabled:
                return False
            speculative = await self._settings_repo.get_value("filler.speculative_start", "true")
            return (speculative or "true").lower() == "true"
        except asyncio.CancelledError:
            raise
        except Exception:
            # Best-effort optimization: a failed read falls back to the
            # dispatch-time start instead of breaking the turn.
            logger.debug("Speculative filler settings read failed", exc_info=True)
            return False

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
            filler_task = DispatchTask(
                description=f"generate_filler:{target_agent}\n{user_text}",
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
