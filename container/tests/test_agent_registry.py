"""Tests for CachedAgentRegistry TTL eviction, stale cards, and known agents."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.agents.agent_registry import CachedAgentRegistry
from app.models.agent import AgentCard


class TestCachedAgentRegistryTTL:
    def _make_registry(self, cards=None):
        underlying = AsyncMock()
        if cards is not None:
            underlying.list_agents = AsyncMock(return_value=cards)
        reg = CachedAgentRegistry(registry=underlying, default_timeout=5, max_dispatch_timeout=60.0)
        return reg, underlying

    # ------------------------------------------------------------------
    # G22: TTL eviction
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ttl_eviction_expires_entries(self):
        """G22: Entries past TTL should be evicted on next access."""
        reg, _ = self._make_registry()
        import time

        # Inject a fake entry with an old timestamp
        reg._agent_card_cache._data["old-agent"] = AgentCard(
            agent_id="old-agent", name="Old", description="", skills=[]
        )
        reg._agent_card_cache._times["old-agent"] = time.monotonic() - 400  # past 300s TTL

        result = reg._agent_card_cache.get("old-agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_ttl_eviction_keeps_fresh_entries(self):
        """G22: Entries within TTL should remain accessible."""
        reg, _ = self._make_registry()
        import time

        card = AgentCard(agent_id="fresh-agent", name="Fresh", description="", skills=[])
        reg._agent_card_cache._data["fresh-agent"] = card
        reg._agent_card_cache._times["fresh-agent"] = time.monotonic() - 10  # within 300s TTL

        result = reg._agent_card_cache.get("fresh-agent")
        assert result is card

    # ------------------------------------------------------------------
    # G23: Stale custom agent cards after reload
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_stale_custom_agent_cards_cleared_on_invalidate(self):
        """G23: invalidate_caches must clear stale agent cards from by-id dict."""
        reg, underlying = self._make_registry()
        stale_card = AgentCard(agent_id="custom-1", name="Stale", description="", skills=[])
        reg._agent_cards_by_id["custom-1"] = stale_card
        reg._agent_card_cache["custom-1"] = stale_card

        # Simulate a reload where the custom agent no longer exists
        underlying.list_agents = AsyncMock(return_value=[])
        reg.invalidate_caches()

        assert "custom-1" not in reg._agent_cards_by_id
        assert "custom-1" not in reg._agent_card_cache

    @pytest.mark.asyncio
    async def test_stale_custom_agent_cards_refreshed_after_invalidate(self):
        """G23: After invalidate, get_agent_card should fetch fresh data."""
        reg, underlying = self._make_registry()
        stale_card = AgentCard(agent_id="custom-1", name="Stale", description="", skills=[])
        reg._agent_cards_by_id["custom-1"] = stale_card
        reg._agent_card_cache["custom-1"] = stale_card

        fresh_card = AgentCard(agent_id="custom-1", name="Fresh", description="", skills=[])
        underlying.list_agents = AsyncMock(return_value=[fresh_card])
        reg.invalidate_caches()

        result = await reg.get_agent_card("custom-1")
        assert result is fresh_card
        assert underlying.list_agents.await_count == 1

    # ------------------------------------------------------------------
    # G24: get_known_agents fallback when registry unavailable
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_known_agents_fallback_when_registry_none(self):
        """G24: When registry is None, get_known_agents should return built-in agent IDs."""
        reg = CachedAgentRegistry(registry=None, default_timeout=5, max_dispatch_timeout=60.0)
        agents = await reg.get_known_agents()
        from app.bootstrap._agents import BUILT_IN_AGENT_IDS

        assert agents == set(BUILT_IN_AGENT_IDS)

    @pytest.mark.asyncio
    async def test_get_known_agents_fallback_on_list_agents_exception(self):
        """G24: When list_agents raises, get_known_agents should return empty set (plus cancel-interaction)."""
        reg, underlying = self._make_registry()
        underlying.list_agents = AsyncMock(side_effect=RuntimeError("registry down"))
        agents = await reg.get_known_agents()
        assert "cancel-interaction" in agents
        assert len(agents) == 1  # only cancel-interaction

    # ------------------------------------------------------------------
    # G26: Per-agent timeout cache invalidation
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_per_agent_timeout_cache_hit(self):
        """G26: Per-agent timeout should be cached and reused."""
        reg, underlying = self._make_registry()
        card = AgentCard(agent_id="light-agent", name="Light", description="", skills=[], timeout_sec=10)
        underlying.list_agents = AsyncMock(return_value=[card])

        first = await reg.resolve_dispatch_timeout("light-agent")
        second = await reg.resolve_dispatch_timeout("light-agent")

        assert first == 10.0
        assert second == 10.0
        underlying.list_agents.assert_awaited_once()  # cached on second call

    @pytest.mark.asyncio
    async def test_per_agent_timeout_cache_invalidated(self):
        """G26: invalidate_caches must clear per-agent timeout cache."""
        reg, underlying = self._make_registry()
        card = AgentCard(agent_id="light-agent", name="Light", description="", skills=[], timeout_sec=10)
        underlying.list_agents = AsyncMock(return_value=[card])

        await reg.resolve_dispatch_timeout("light-agent")
        reg.invalidate_caches()
        await reg.resolve_dispatch_timeout("light-agent")

        assert underlying.list_agents.await_count == 2

    @pytest.mark.asyncio
    async def test_per_agent_timeout_settings_override_priority(self):
        """G26: Settings override should take priority over AgentCard.timeout_sec."""
        reg, underlying = self._make_registry()
        card = AgentCard(agent_id="light-agent", name="Light", description="", skills=[], timeout_sec=10)
        underlying.list_agents = AsyncMock(return_value=[card])

        settings_repo = AsyncMock()
        settings_repo.get_value = AsyncMock(return_value="3")

        result = await reg.resolve_dispatch_timeout("light-agent", settings_repo=settings_repo)
        assert result == 3.0
