"""Tests for CacheOrchestrator edge cases."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.cache_orchestrator import CacheOrchestrator
from app.models.agent import IngressTask


def _make_cache_orchestrator():
    cache_manager = MagicMock()
    cache_manager.try_replay_action = AsyncMock(return_value=None)
    cache_manager.try_routing_skip = AsyncMock(return_value=None)
    cache_manager.store_routing_async = AsyncMock()
    cache_manager.store_action_async = AsyncMock()
    cache_manager.apply_rewrite = AsyncMock(return_value="rewritten")

    co = CacheOrchestrator(
        cache_manager=cache_manager,
        entity_index=None,
        ha_client=None,
        agent_registry=None,
    )
    return co, cache_manager


class TestCacheOrchestratorEdgeCases:
    # ------------------------------------------------------------------
    # G8: readonly action -> routing cache storage
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_readonly_action_result_stores_routing_not_action(self):
        """G8: A readonly action (query_*) should store routing cache, not action cache."""
        co, cm = _make_cache_orchestrator()
        with (
            patch.object(co, "_get_bool_setting_impl", new=AsyncMock(return_value=True)),
            patch.object(co, "legacy_pipeline_enabled", return_value=False),
        ):
            result = await co.store_after_dispatch(
                user_text="what lights are on",
                language="en",
                target_agent="light-agent",
                condensed_task="list lights",
                confidence=0.95,
                speech="Kitchen and living room lights are on.",
                original_response_text="Kitchen and living room lights are on.",
                action_executed={
                    "success": True,
                    "action": "query_light",
                    "entity_id": "light.kitchen",
                    "service_data": {},
                },
                has_error=False,
                task=IngressTask(description="what lights are on"),
            )
        # Returns (action_stored, routing_stored)
        assert result == (False, True)
        cm.store_routing_async.assert_awaited_once()
        cm.store_action_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_readonly_action_via_service_name_stores_routing(self):
        """G8: readonly detected via service name (no action key) should store routing."""
        co, cm = _make_cache_orchestrator()
        with (
            patch.object(co, "_get_bool_setting_impl", new=AsyncMock(return_value=True)),
            patch.object(co, "legacy_pipeline_enabled", return_value=False),
        ):
            result = await co.store_after_dispatch(
                user_text="list climate entities",
                language="en",
                target_agent="climate-agent",
                condensed_task="list climate",
                confidence=0.9,
                speech="Climate entities listed.",
                original_response_text="Climate entities listed.",
                action_executed={
                    "success": True,
                    "service": "climate/list_entities",
                    "entity_id": "climate.living_room",
                },
                has_error=False,
                task=IngressTask(description="list climate"),
            )
        assert result == (False, True)
        cm.store_routing_async.assert_awaited_once()

    # ------------------------------------------------------------------
    # G8: cacheable=False should skip storage
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cacheable_false_skips_storage(self):
        """G8: action_executed with cacheable=False should skip all cache storage."""
        co, cm = _make_cache_orchestrator()
        with (
            patch.object(co, "_get_bool_setting_impl", new=AsyncMock(return_value=True)),
            patch.object(co, "legacy_pipeline_enabled", return_value=False),
        ):
            result = await co.store_after_dispatch(
                user_text="turn on light",
                language="en",
                target_agent="light-agent",
                condensed_task="turn on light",
                confidence=0.95,
                speech="Done.",
                original_response_text="Done.",
                action_executed={
                    "success": True,
                    "action": "turn_on",
                    "entity_id": "light.kitchen",
                    "cacheable": False,
                    "service_data": {},
                },
                has_error=False,
                task=IngressTask(description="turn on light"),
            )
        assert result == (False, False)
        cm.store_routing_async.assert_not_called()
        cm.store_action_async.assert_not_called()

    # ------------------------------------------------------------------
    # G8: condition in service_data should skip storage
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_condition_in_service_data_skips_storage(self):
        """G8: action_executed with condition in service_data should skip all cache storage."""
        co, cm = _make_cache_orchestrator()
        with (
            patch.object(co, "_get_bool_setting_impl", new=AsyncMock(return_value=True)),
            patch.object(co, "legacy_pipeline_enabled", return_value=False),
        ):
            result = await co.store_after_dispatch(
                user_text="turn on light if dark",
                language="en",
                target_agent="light-agent",
                condensed_task="turn on light if dark",
                confidence=0.95,
                speech="Done.",
                original_response_text="Done.",
                action_executed={
                    "success": True,
                    "action": "turn_on",
                    "entity_id": "light.kitchen",
                    "service_data": {"condition": "sun_below_horizon"},
                },
                has_error=False,
                task=IngressTask(description="turn on light if dark"),
            )
        assert result == (False, False)
        cm.store_routing_async.assert_not_called()
        cm.store_action_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_action_without_entity_id_stores_routing(self):
        co, cm = _make_cache_orchestrator()
        with (
            patch.object(co, "_get_bool_setting_impl", new=AsyncMock(return_value=True)),
            patch.object(co, "legacy_pipeline_enabled", return_value=False),
        ):
            result = await co.store_after_dispatch(
                user_text="set a timer for 5 minutes",
                language="en",
                target_agent="timer-agent",
                condensed_task="set timer for 5 minutes",
                confidence=0.95,
                speech="Timer set for 5 minutes.",
                original_response_text="Timer set for 5 minutes.",
                action_executed={
                    "success": True,
                    "action": "timer.start",
                },
                has_error=False,
                task=IngressTask(description="set a timer"),
            )
        assert result == (False, True)
        cm.store_routing_async.assert_awaited_once()
        cm.store_action_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_action_without_action_name_stores_routing(self):
        co, cm = _make_cache_orchestrator()
        with (
            patch.object(co, "_get_bool_setting_impl", new=AsyncMock(return_value=True)),
            patch.object(co, "legacy_pipeline_enabled", return_value=False),
        ):
            result = await co.store_after_dispatch(
                user_text="turn on the light",
                language="en",
                target_agent="light-agent",
                condensed_task="turn on light",
                confidence=0.95,
                speech="Light turned on.",
                original_response_text="Light turned on.",
                action_executed={
                    "success": True,
                    "entity_id": "light.kitchen",
                },
                has_error=False,
                task=IngressTask(description="turn on light"),
            )
        assert result == (False, True)
        cm.store_routing_async.assert_awaited_once()
        cm.store_action_async.assert_not_called()
