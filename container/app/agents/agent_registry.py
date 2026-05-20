"""Agent registry cache with TTL-backed lookups.

Extracted from OrchestratorAgent to keep registry caching concerns
separate from pipeline orchestration.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.models.agent import AgentCard

logger = logging.getLogger(__name__)


class _TTLCache:
    """Minimal in-memory TTL cache with maxsize eviction."""

    def __init__(self, maxsize: int = 256, ttl: float = 300.0):
        self._maxsize = maxsize
        self._ttl = ttl
        self._data: dict[str, Any] = {}
        self._times: dict[str, float] = {}

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, t in self._times.items() if now - t > self._ttl]
        for k in expired:
            self._data.pop(k, None)
            self._times.pop(k, None)

    def _evict_oldest(self) -> None:
        if len(self._data) >= self._maxsize and self._times:
            oldest = min(self._times, key=self._times.get)
            self._data.pop(oldest, None)
            self._times.pop(oldest, None)

    def get(self, key: str, default: Any = None) -> Any:
        self._evict_expired()
        ts = self._times.get(key)
        if ts is not None and time.monotonic() - ts <= self._ttl:
            return self._data[key]
        self._data.pop(key, None)
        self._times.pop(key, None)
        return default

    def __setitem__(self, key: str, value: Any) -> None:
        self._evict_expired()
        self._evict_oldest()
        self._data[key] = value
        self._times[key] = time.monotonic()

    def __contains__(self, key: str) -> bool:
        self._evict_expired()
        ts = self._times.get(key)
        return ts is not None and time.monotonic() - ts <= self._ttl

    def clear(self) -> None:
        self._data.clear()
        self._times.clear()


class AgentRegistry:
    """Caches agent cards, timeouts, and known-agent lookups.

    Uses plain ``dict`` for the per-agent timeout cache (small, explicitly
    invalidated) and a minimal TTL cache for agent cards so that
    agent registrations / card updates are picked up automatically.
    """

    def __init__(
        self,
        registry: Any,
        *,
        default_timeout: int = 5,
        max_dispatch_timeout: float = 60.0,
    ) -> None:
        self._registry = registry
        self._default_timeout = default_timeout
        self._max_dispatch_timeout = max_dispatch_timeout
        self._per_agent_timeout_cache: dict[str, float] = {}
        self._agent_card_cache = _TTLCache(maxsize=256, ttl=300.0)
        self._known_agents_cache: tuple[float, set[str]] | None = None
        self._known_agents_ttl: float = 5.0

    # ------------------------------------------------------------------
    # Config updates (called when orchestrator reloads settings)
    # ------------------------------------------------------------------

    def set_default_timeout(self, timeout: int) -> None:
        self._default_timeout = timeout

    def set_max_dispatch_timeout(self, timeout: float) -> None:
        self._max_dispatch_timeout = timeout

    def invalidate_caches(self) -> None:
        """Clear all caches so the next lookup hits the underlying registry."""
        self._per_agent_timeout_cache.clear()
        self._agent_card_cache.clear()
        self._known_agents_cache = None

    # ------------------------------------------------------------------
    # Timeout resolution
    # ------------------------------------------------------------------

    async def resolve_dispatch_timeout(
        self,
        agent_id: str,
        *,
        default_timeout: int | None = None,
        settings_repo: Any | None = None,
    ) -> float:
        """Return the dispatch timeout (seconds) for ``agent_id``.

        P2-2 (FLOW-TIMEOUT-1): resolution priority --
            1. ``agent.dispatch_timeout.<agent_id>`` settings key
            2. ``AgentCard.timeout_sec`` from the registry
            3. ``self._default_timeout`` (orchestrator-wide fallback)

        Result is capped at ``self._max_dispatch_timeout``.
        """
        cached = self._per_agent_timeout_cache.get(agent_id)
        if cached is not None:
            return cached

        resolved: float | None = None
        # 1. Settings override.
        if settings_repo is not None:
            try:
                raw = await settings_repo.get_value(
                    f"agent.dispatch_timeout.{agent_id}",
                    "",
                )
                if raw:
                    resolved = float(raw)
            except (ValueError, TypeError):
                resolved = None

        # 2. AgentCard.timeout_sec from the registry.
        if resolved is None and self._registry is not None:
            card = await self.get_agent_card(agent_id)
            if card is not None:
                card_timeout = getattr(card, "timeout_sec", None)
                if card_timeout is not None:
                    resolved = float(card_timeout)

        # 3. Orchestrator default.
        fallback = default_timeout if default_timeout is not None else self._default_timeout
        if resolved is None or resolved <= 0:
            resolved = float(fallback)

        # Cap to defend against misconfiguration.
        cap = self._max_dispatch_timeout
        if resolved > cap:
            resolved = float(cap)

        self._per_agent_timeout_cache[agent_id] = resolved
        return resolved

    # ------------------------------------------------------------------
    # Agent card / known-agent helpers
    # ------------------------------------------------------------------

    async def get_agent_card(self, agent_id: str) -> AgentCard | None:
        """Fetch a single agent card, using the TTL cache."""
        cached = self._agent_card_cache.get(agent_id)
        if cached is not None:
            return cached

        if self._registry is None:
            return None

        try:
            cards = await self._registry.list_agents()
        except Exception:
            logger.debug("Registry list_agents failed for %s", agent_id, exc_info=True)
            return None

        for card in cards:
            if getattr(card, "agent_id", None) == agent_id:
                self._agent_card_cache[agent_id] = card
                return card
        return None

    async def list_agents(self) -> list[AgentCard]:
        """Return all registered agent cards."""
        if self._registry is None:
            return []
        try:
            return list(await self._registry.list_agents())
        except Exception:
            logger.debug("Registry list_agents failed", exc_info=True)
            return []

    async def get_known_agents(self, *, exclude: set[str] | frozenset[str] | None = None) -> set[str]:
        """Return set of currently registered agent IDs.

        Result is memoised for ``_known_agents_ttl`` seconds.
        """
        fallback = {"light-agent", "music-agent", "general-agent", "cancel-interaction"}
        if self._registry is None:
            return fallback

        now = time.monotonic()
        cached = self._known_agents_cache
        if cached is not None and (now - cached[0]) < self._known_agents_ttl:
            return set(cached[1])

        try:
            cards = await self._registry.list_agents()
            agents = {card.agent_id for card in cards if card.agent_id != "orchestrator"}
        except Exception:
            logger.debug("Registry list_agents failed for known-agents", exc_info=True)
            agents = set()

        agents.add("cancel-interaction")
        self._known_agents_cache = (now, set(agents))
        return agents
