"""Tests for routing-cache skip behavior."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.cache_orchestrator import CacheOrchestrator

_litellm_mock = MagicMock()


class _AuthenticationError(Exception):
    pass


class _APIError(Exception):
    pass


class _RateLimitError(Exception):
    pass


_litellm_mock.exceptions.AuthenticationError = _AuthenticationError
_litellm_mock.exceptions.APIError = _APIError
_litellm_mock.RateLimitError = _RateLimitError
sys.modules.setdefault("litellm", _litellm_mock)

from app.agents.orchestrator import OrchestratorAgent
from app.cache.cache_manager import CacheManager, RoutingSkipOutcome
from app.cache.vector_store import VectorStore
from app.models.agent import AgentCard, AgentTask, TaskContext


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
    orch._finalize_single_agent_response = AsyncMock(return_value=("Live dispatch speech", False))
    return orch


@pytest.mark.asyncio
async def test_exact_text_routing_hit_skips_classify():
    orch = _make_orchestrator(MagicMock())
    orch._cache_orchestrator.try_cache_replay = AsyncMock(
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
    orch._classification_engine.classify = AsyncMock(
        side_effect=AssertionError("classification should be skipped on routing hit")
    )
    orch._dispatch_manager.dispatch_single = AsyncMock(
        return_value=("light-agent", "Live dispatch speech", {"speech": "Live dispatch speech"})
    )

    result = await orch._handle_task_impl(_make_task("turn on kitchen light"))

    assert result["speech"] == "Live dispatch speech"
    orch._classification_engine.classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_semantic_routing_hit_above_threshold_skips_classify():
    orch = _make_orchestrator(MagicMock())
    orch._cache_orchestrator.try_cache_replay = AsyncMock(
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
    orch._classification_engine.classify = AsyncMock(
        side_effect=AssertionError("classification should be skipped on routing hit")
    )
    orch._dispatch_manager.dispatch_single = AsyncMock(
        return_value=("light-agent", "Live dispatch speech", {"speech": "Live dispatch speech"})
    )

    result = await orch._handle_task_impl(_make_task("switch on the kitchen lamp"))

    assert result["routed_to"] == "light-agent"
    orch._classification_engine.classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_routing_hit_always_runs_live_dispatch():
    orch = _make_orchestrator(MagicMock())
    orch._cache_orchestrator.try_cache_replay = AsyncMock(
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
    orch._classification_engine.classify = AsyncMock(
        side_effect=AssertionError("classification should be skipped on routing hit")
    )
    orch._dispatch_manager.dispatch_single = AsyncMock(
        return_value=("light-agent", "Live dispatch speech", {"speech": "Live dispatch speech"})
    )

    await orch._handle_task_impl(_make_task("turn on kitchen light"))

    orch._dispatch_manager.dispatch_single.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_only_action_stores_routing_only_entry():
    orch = OrchestratorAgent.__new__(OrchestratorAgent)
    orch._cache_manager = MagicMock()
    orch._cache_manager.store_routing_async = AsyncMock()
    orch._cache_manager.store_action_async = AsyncMock()
    orch._legacy_pipeline_enabled = MagicMock(return_value=False)
    orch._get_bool_setting = AsyncMock(return_value=True)
    orch._cache_orchestrator = CacheOrchestrator(cache_manager=orch._cache_manager)
    orch._cache_orchestrator._get_bool_setting_impl = AsyncMock(return_value=True)

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
    orch._cache_orchestrator = CacheOrchestrator(cache_manager=orch._cache_manager)
    orch._cache_orchestrator._get_bool_setting_impl = AsyncMock(return_value=True)

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
    orch._cache_orchestrator = CacheOrchestrator(cache_manager=orch._cache_manager)
    orch._cache_orchestrator._get_bool_setting_impl = AsyncMock(return_value=True)

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


@pytest.mark.asyncio
async def test_routing_hit_for_disabled_agent_is_invalidated_and_falls_through():
    """F2 / T1: a routing-cache hit pointing at a now-disabled agent must be
    invalidated and the turn must fall through to live orchestration instead of
    dispatching to a non-existent agent."""
    cache_manager = MagicMock()
    cache_manager.try_replay_action = AsyncMock(return_value=None)
    stale_outcome = RoutingSkipOutcome(
        kind="routing_hit",
        entry_id="stale-entry-id",
        agent_id="climate-agent",
        condensed_task="What is the bedroom temperature?",
        similarity=0.99,
    )
    cache_manager.try_routing_skip = AsyncMock(return_value=stale_outcome)
    cache_manager.invalidate_routing = MagicMock()

    orch = _make_orchestrator(cache_manager)
    # Live agent registry no longer includes climate-agent.
    orch._get_known_agents = AsyncMock(return_value={"light-agent", "general-agent"})
    orch._get_bool_setting = AsyncMock(side_effect=lambda _key, default: default)
    orch._entity_index = None

    action_replay, routing_skip = await orch._try_cache_replay(
        task=_make_task("what is the bedroom temperature"),
        user_text="what is the bedroom temperature",
        language="en",
    )

    assert action_replay is None
    assert routing_skip is None
    cache_manager.invalidate_routing.assert_called_once_with("stale-entry-id")


@pytest.mark.asyncio
async def test_conditional_action_is_not_stored():
    """Conditional actions (cacheable=False) must not be stored in action cache."""
    orch = OrchestratorAgent.__new__(OrchestratorAgent)
    orch._cache_manager = MagicMock()
    orch._cache_manager.store_routing_async = AsyncMock()
    orch._cache_manager.store_action_async = AsyncMock()
    orch._legacy_pipeline_enabled = MagicMock(return_value=False)
    orch._get_bool_setting = AsyncMock(return_value=True)
    orch._cache_orchestrator = CacheOrchestrator(cache_manager=orch._cache_manager)
    orch._cache_orchestrator._get_bool_setting_impl = AsyncMock(return_value=True)

    stored_action, stored_routing = await orch._store_after_dispatch(
        user_text="turn on kitchen light",
        language="en",
        target_agent="light-agent",
        condensed_task="Turn on kitchen light",
        confidence=0.95,
        speech="Done.",
        action_executed={
            "success": True,
            "action": "turn_on",
            "entity_id": "light.kitchen",
            "cacheable": False,
        },
        has_error=False,
    )

    assert (stored_action, stored_routing) == (False, False)
    orch._cache_manager.store_action_async.assert_not_awaited()
    orch._cache_manager.store_routing_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_routing_hit_rejected_when_referenced_entity_invisible():
    """F2 / T1: a routing-cache hit referencing a now-invisible entity must be
    invalidated and fall through to live orchestration."""
    cache_manager = MagicMock()
    cache_manager.try_replay_action = AsyncMock(return_value=None)
    stale_outcome = RoutingSkipOutcome(
        kind="routing_hit",
        entry_id="stale-entry-id",
        agent_id="light-agent",
        condensed_task="Turn on kitchen and living room lights",
        similarity=0.99,
        entity_ids=["light.kitchen", "light.living_room"],
    )
    cache_manager.try_routing_skip = AsyncMock(return_value=stale_outcome)
    cache_manager.invalidate_routing = MagicMock()

    orch = _make_orchestrator(cache_manager)
    orch._get_known_agents = AsyncMock(return_value={"light-agent", "general-agent"})

    def _entity_visible(agent_id: str, entity_id: str, entity_index, **kwargs) -> bool:
        return entity_id != "light.living_room"

    with patch(
        "app.agents.cache_orchestrator.entity_is_visible",
        new=AsyncMock(side_effect=_entity_visible),
    ):
        action_replay, routing_skip = await orch._try_cache_replay(
            task=_make_task("turn on the lights"),
            user_text="turn on the lights",
            language="en",
        )

    assert action_replay is None
    assert routing_skip is None
    cache_manager.invalidate_routing.assert_called_once_with("stale-entry-id")


@pytest.mark.asyncio
async def test_conditional_action_in_service_data_is_not_stored():
    """Defensive: actions whose service_data contains a condition key must not be stored."""
    orch = OrchestratorAgent.__new__(OrchestratorAgent)
    orch._cache_manager = MagicMock()
    orch._cache_manager.store_routing_async = AsyncMock()
    orch._cache_manager.store_action_async = AsyncMock()
    orch._legacy_pipeline_enabled = MagicMock(return_value=False)
    orch._get_bool_setting = AsyncMock(return_value=True)
    orch._cache_orchestrator = CacheOrchestrator(cache_manager=orch._cache_manager)
    orch._cache_orchestrator._get_bool_setting_impl = AsyncMock(return_value=True)

    stored_action, stored_routing = await orch._store_after_dispatch(
        user_text="turn on kitchen light",
        language="en",
        target_agent="light-agent",
        condensed_task="Turn on kitchen light",
        confidence=0.95,
        speech="Done.",
        action_executed={
            "success": True,
            "action": "turn_on",
            "entity_id": "light.kitchen",
            "service_data": {"condition": {"entity": "light.kitchen", "state": "off"}},
        },
        has_error=False,
    )

    assert (stored_action, stored_routing) == (False, False)
    orch._cache_manager.store_action_async.assert_not_awaited()
    orch._cache_manager.store_routing_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_routing_skip_finalization_writes_trace_summary_row():
    """REGRESSION: a routing-cache hit reaches the normal _create_trace
    finalization site and writes a trace_summary row carrying routing
    provenance and cache_hit_type='routing_hit'.

    Pins Step 2: the routing-skip turn does not short-circuit before
    finalization, so create_trace_summary is awaited.
    """
    from app.analytics.tracer import SpanCollector

    orch = _make_orchestrator(MagicMock())
    orch._store_turn = AsyncMock()

    span_collector = SpanCollector("routing-skip-trace")
    # Span shape produced by the real routing-hit path: a cache_lookup span
    # tagged routing_hit, plus the synthetic classify span (routing_cached=True)
    # emitted by run_classification on a routing skip.
    span_collector._spans.extend(
        [
            {
                "span_name": "cache_lookup",
                "agent_id": "orchestrator",
                "metadata": {"hit_type": "routing_hit", "cached_agent_id": "light-agent"},
                "duration_ms": 2.0,
            },
            {
                "span_name": "classify",
                "agent_id": "orchestrator",
                "metadata": {"routing_cached": True, "target_agent": "light-agent"},
                "duration_ms": 3.0,
            },
        ]
    )

    user_text = "turn on kitchen light"
    with patch("app.analytics.tracer.create_trace_summary", new_callable=AsyncMock) as mock_summary:
        await orch._finalize_post_mediation(
            task=_make_task(user_text),
            user_text=user_text,
            target_agent="light-agent",
            confidence=0.97,
            condensed_task="Turn on kitchen light",
            mediated_speech="Live dispatch speech",
            original_speech="Live dispatch speech",
            action_executed=None,
            has_error=False,
            span_collector=span_collector,
            conversation_id="conv-routing-cache",
            language="en",
            turns=[],
            classifications=[("light-agent", "Turn on kitchen light", 0.97, [])],
            voice_followup_requested=False,
            skip_response_cache=True,
        )

    mock_summary.assert_awaited_once()
    kwargs = mock_summary.await_args.kwargs
    assert kwargs["routing_agent"] == "light-agent"
    assert kwargs["routing_confidence"] == 0.97
    assert kwargs["condensed_task"] == "Turn on kitchen light"
    assert kwargs["cache_hit_type"] == "routing_hit"
    # classify span present -> routing_duration_ms recorded (not None)
    assert kwargs["routing_duration_ms"] is not None
