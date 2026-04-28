"""Tests for routing-cache skip behavior."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_litellm_mock = MagicMock()


class _AuthenticationError(Exception):
    pass


_litellm_mock.exceptions.AuthenticationError = _AuthenticationError
sys.modules.setdefault("litellm", _litellm_mock)

from app.agents.orchestrator import OrchestratorAgent
from app.cache.cache_manager import CacheManager, RoutingSkipOutcome
from app.cache.vector_store import VectorStore
from app.models.agent import AgentCard, AgentTask, TaskContext
from app.models.cache import ActionCacheEntry


def _make_manager() -> CacheManager:
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
    return CacheManager(store)


def _make_task(text: str) -> AgentTask:
    return AgentTask(
        description=text,
        user_text=text,
        conversation_id="conv-routing-cache",
        context=TaskContext(language="en"),
    )


def _make_orchestrator(cache_manager) -> OrchestratorAgent:
    dispatcher = AsyncMock()
    registry = AsyncMock()
    registry.list_agents = AsyncMock(
        return_value=[
            AgentCard(agent_id="light-agent", name="Light Agent", description="", skills=["light"]),
        ]
    )
    orch = OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=cache_manager)
    orch._pipeline_resolve_conversation_and_language = AsyncMock(return_value=("conv-routing-cache", "en", []))
    orch._is_background_turn = MagicMock(return_value=False)
    orch._get_turns = AsyncMock(return_value=[])
    orch._get_bool_setting = AsyncMock(side_effect=lambda _key, default: default)
    orch._schedule_ha_voice_followup_if_requested = MagicMock()
    orch._finalize_single_agent_response = AsyncMock(return_value=("Live dispatch speech", False))
    return orch


@pytest.mark.asyncio
async def test_exact_text_routing_hit_skips_classify():
    orch = _make_orchestrator(MagicMock())
    orch._try_cache_replay = AsyncMock(
        return_value=(
            None,
            RoutingSkipOutcome(
                kind="routing_hit",
                entry_id="routing-1",
                agent_id="light-agent",
                condensed_task="Turn on kitchen light",
                similarity=1.0,
            ),
        )
    )
    orch._classify = AsyncMock(side_effect=AssertionError("classification should be skipped on routing hit"))
    orch._dispatch_single = AsyncMock(
        return_value=("light-agent", "Live dispatch speech", {"speech": "Live dispatch speech"})
    )

    result = await orch._handle_task_impl(_make_task("turn on kitchen light"))

    assert result["speech"] == "Live dispatch speech"
    orch._classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_semantic_routing_hit_above_threshold_skips_classify():
    orch = _make_orchestrator(MagicMock())
    orch._try_cache_replay = AsyncMock(
        return_value=(
            None,
            RoutingSkipOutcome(
                kind="routing_hit",
                entry_id="routing-2",
                agent_id="light-agent",
                condensed_task="Turn on kitchen light",
                similarity=0.96,
            ),
        )
    )
    orch._classify = AsyncMock(side_effect=AssertionError("classification should be skipped on routing hit"))
    orch._dispatch_single = AsyncMock(
        return_value=("light-agent", "Live dispatch speech", {"speech": "Live dispatch speech"})
    )

    result = await orch._handle_task_impl(_make_task("switch on the kitchen lamp"))

    assert result["routed_to"] == "light-agent"
    orch._classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_routing_hit_always_runs_live_dispatch():
    orch = _make_orchestrator(MagicMock())
    orch._try_cache_replay = AsyncMock(
        return_value=(
            None,
            RoutingSkipOutcome(
                kind="routing_hit",
                entry_id="routing-3",
                agent_id="light-agent",
                condensed_task="Turn on kitchen light",
                similarity=0.97,
            ),
        )
    )
    orch._classify = AsyncMock(side_effect=AssertionError("classification should be skipped on routing hit"))
    orch._dispatch_single = AsyncMock(
        return_value=("light-agent", "Live dispatch speech", {"speech": "Live dispatch speech"})
    )

    await orch._handle_task_impl(_make_task("turn on kitchen light"))

    orch._dispatch_single.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_only_action_stores_routing_only_entry():
    orch = OrchestratorAgent.__new__(OrchestratorAgent)
    orch._cache_manager = MagicMock()
    orch._cache_manager.store_routing_async = AsyncMock()
    orch._cache_manager.store_action_async = AsyncMock()
    orch._legacy_pipeline_enabled = MagicMock(return_value=False)
    orch._get_bool_setting = AsyncMock(return_value=True)

    stored_action, stored_routing = await orch._store_after_dispatch(
        user_text="what is the kitchen temperature",
        language="en",
        target_agent="climate-agent",
        condensed_task="Read kitchen temperature",
        confidence=0.94,
        speech="It is 21 degrees.",
        action_executed={
            "success": True,
            "action": "query_temperature",
            "entity_id": "sensor.kitchen_temperature",
        },
        has_error=False,
    )

    assert (stored_action, stored_routing) == (False, True)
    orch._cache_manager.store_routing_async.assert_awaited_once()
    orch._cache_manager.store_action_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversational_answer_stores_routing_only_entry():
    orch = OrchestratorAgent.__new__(OrchestratorAgent)
    orch._cache_manager = MagicMock()
    orch._cache_manager.store_routing_async = AsyncMock()
    orch._cache_manager.store_action_async = AsyncMock()
    orch._legacy_pipeline_enabled = MagicMock(return_value=False)
    orch._get_bool_setting = AsyncMock(return_value=True)

    stored_action, stored_routing = await orch._store_after_dispatch(
        user_text="hello there",
        language="en",
        target_agent="general-agent",
        condensed_task="Say hello",
        confidence=0.88,
        speech="Hello!",
        action_executed=None,
        has_error=False,
    )

    assert (stored_action, stored_routing) == (False, True)
    orch._cache_manager.store_routing_async.assert_awaited_once()
    orch._cache_manager.store_action_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_agent_merge_does_not_store_routing():
    orch = OrchestratorAgent.__new__(OrchestratorAgent)
    orch._cache_manager = MagicMock()
    orch._cache_manager.store_routing_async = AsyncMock()
    orch._cache_manager.store_action_async = AsyncMock()
    orch._legacy_pipeline_enabled = MagicMock(return_value=False)
    orch._get_bool_setting = AsyncMock(return_value=True)

    stored_action, stored_routing = await orch._store_after_dispatch(
        user_text="turn off the lights and play music",
        language="en",
        target_agent="light-agent",
        condensed_task="Turn off lights and play music",
        confidence=0.95,
        speech="Done.",
        action_executed={"success": True, "action": "turn_off", "entity_id": "light.kitchen"},
        has_error=False,
        merged_multi_agent=True,
    )

    assert (stored_action, stored_routing) == (False, False)
    orch._cache_manager.store_routing_async.assert_not_awaited()
    orch._cache_manager.store_action_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_routing_cache_disabled_setting_short_circuits():
    manager = _make_manager()
    manager._routing_cache._enabled = False

    with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock) as track:
        result = await manager.try_routing_skip(query_text="turn on kitchen light", language="en")

    assert result is None
    track.assert_not_awaited()
