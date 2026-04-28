"""Tests for the cache lookup-order contract."""

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
from app.cache.cache_manager import ActionReplayOutcome, CacheManager, RoutingSkipOutcome
from app.cache.vector_store import VectorStore
from app.models.agent import AgentCard, AgentTask, TaskContext
from tests.helpers import make_action_cache_entry, make_routing_cache_entry


def _make_task(text: str) -> AgentTask:
    return AgentTask(
        description=text,
        user_text=text,
        conversation_id="conv-cache-order",
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
    orch._pipeline_resolve_conversation_and_language = AsyncMock(return_value=("conv-cache-order", "en", []))
    orch._is_background_turn = MagicMock(return_value=False)
    orch._get_turns = AsyncMock(return_value=[])
    orch._get_bool_setting = AsyncMock(side_effect=lambda _key, default: default)
    orch._schedule_ha_voice_followup_if_requested = MagicMock()
    orch._finalize_single_agent_response = AsyncMock(return_value=("Live dispatch speech", False))
    orch._entity_index = MagicMock()
    orch._cached_action_is_still_visible = AsyncMock(return_value=True)
    orch._execute_cached_action = AsyncMock(return_value={"success": True})
    return orch


@pytest.mark.asyncio
async def test_action_hit_short_circuits_routing_lookup():
    cache_manager = MagicMock()
    cache_manager.try_replay_action = AsyncMock(
        return_value=ActionReplayOutcome(
            kind="full_hit",
            entry_id="action-1",
            agent_id="light-agent",
            response_text="Cached speech",
            replay_result={"success": True},
            similarity=1.0,
        )
    )
    cache_manager.try_routing_skip = AsyncMock(side_effect=AssertionError("routing lookup should be skipped"))
    orch = _make_orchestrator(cache_manager)

    action_hit, routing_hit = await orch._try_cache_replay(
        task=_make_task("turn on kitchen light"),
        user_text="turn on kitchen light",
        language="en",
    )

    assert action_hit is not None
    assert routing_hit is None
    cache_manager.try_routing_skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_action_miss_falls_through_to_routing_lookup():
    cache_manager = MagicMock()
    cache_manager.try_replay_action = AsyncMock(return_value=None)
    cache_manager.try_routing_skip = AsyncMock(
        return_value=RoutingSkipOutcome(
            kind="routing_hit",
            entry_id="routing-1",
            agent_id="light-agent",
            condensed_task="Turn on kitchen light",
            similarity=0.96,
        )
    )
    orch = _make_orchestrator(cache_manager)

    action_hit, routing_hit = await orch._try_cache_replay(
        task=_make_task("turn on kitchen light"),
        user_text="turn on kitchen light",
        language="en",
    )

    assert action_hit is None
    assert routing_hit is not None
    cache_manager.try_replay_action.assert_awaited_once()
    cache_manager.try_routing_skip.assert_awaited_once()


@pytest.mark.asyncio
async def test_routing_miss_falls_through_to_live_classify():
    orch = _make_orchestrator(MagicMock())
    orch._try_cache_replay = AsyncMock(return_value=(None, None))
    orch._classify = AsyncMock(return_value=([("light-agent", "Turn on kitchen light", 0.95)], False))
    orch._dispatch_single = AsyncMock(
        return_value=("light-agent", "Live dispatch speech", {"speech": "Live dispatch speech"})
    )

    result = await orch._handle_task_impl(_make_task("turn on kitchen light"))

    assert result["speech"] == "Live dispatch speech"
    orch._classify.assert_awaited_once()
    orch._dispatch_single.assert_awaited_once()


@pytest.mark.asyncio
async def test_action_full_hit_skips_classify_and_dispatch():
    orch = _make_orchestrator(MagicMock())
    action_hit = ActionReplayOutcome(
        kind="full_hit",
        entry_id="action-1",
        agent_id="light-agent",
        response_text="Cached speech",
        replay_result={"success": True},
        similarity=1.0,
    )
    orch._try_cache_replay = AsyncMock(return_value=(action_hit, None))
    orch._finalize_action_replay_hit = AsyncMock(
        return_value={"speech": "Cached speech", "routed_to": "light-agent", "action_executed": None}
    )
    orch._classify = AsyncMock(side_effect=AssertionError("classification should be skipped"))
    orch._dispatch_single = AsyncMock(side_effect=AssertionError("dispatch should be skipped"))

    result = await orch._handle_task_impl(_make_task("turn on kitchen light"))

    assert result["speech"] == "Cached speech"
    orch._classify.assert_not_awaited()
    orch._dispatch_single.assert_not_awaited()


@pytest.mark.asyncio
async def test_routing_hit_skips_classify_runs_dispatch():
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
    orch._classify = AsyncMock(side_effect=AssertionError("classification should be skipped"))
    orch._dispatch_single = AsyncMock(
        return_value=("light-agent", "Live dispatch speech", {"speech": "Live dispatch speech"})
    )

    result = await orch._handle_task_impl(_make_task("turn on kitchen light"))

    assert result["routed_to"] == "light-agent"
    orch._classify.assert_not_awaited()
    orch._dispatch_single.assert_awaited_once()


@pytest.mark.asyncio
async def test_action_disabled_still_consults_routing():
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
    manager = CacheManager(store)
    manager._action_cache._enabled = False
    routing_entry = make_routing_cache_entry(condensed_task="Turn on kitchen light")
    manager._routing_cache.lookup = MagicMock(return_value=(routing_entry, 0.96))
    orch = _make_orchestrator(manager)

    with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock):
        action_hit, routing_hit = await orch._try_cache_replay(
            task=_make_task("turn on kitchen light"),
            user_text="turn on kitchen light",
            language="en",
        )

    assert action_hit is None
    assert routing_hit is not None
    manager._routing_cache.lookup.assert_called_once()


@pytest.mark.asyncio
async def test_routing_disabled_still_consults_action():
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
    manager = CacheManager(store)
    manager._routing_cache.lookup = MagicMock(side_effect=AssertionError("routing should not be consulted"))
    action_entry = make_action_cache_entry(query_text="turn on kitchen light")
    manager._action_cache.lookup = MagicMock(return_value=(action_entry, 1.0))
    orch = _make_orchestrator(manager)

    with (
        patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock),
        patch(
            "app.agents.orchestrator.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={"entity_id": action_entry.cached_action.entity_id},
        ),
    ):
        action_hit, routing_hit = await orch._try_cache_replay(
            task=_make_task("turn on kitchen light"),
            user_text="turn on kitchen light",
            language="en",
        )

    assert action_hit is not None
    assert routing_hit is None
    manager._routing_cache.lookup.assert_not_called()
