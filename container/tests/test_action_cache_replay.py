"""Tests for action-cache replay behavior."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
from app.cache.cache_manager import ActionReplayOutcome, CacheManager
from app.cache.vector_store import VectorStore
from app.models.agent import AgentCard, IngressTask, TaskContext
from tests.helpers import make_action_cache_entry


def _make_manager() -> CacheManager:
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
    return CacheManager(store)


def _make_task(text: str) -> IngressTask:
    return IngressTask(
        description=text,
        conversation_id="conv-action-cache",
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
    orch._pipeline_resolve_conversation_and_language = AsyncMock(return_value=("conv-action-cache", "en", []))
    orch._is_background_turn = MagicMock(return_value=False)
    orch._get_turns = AsyncMock(return_value=[])
    orch._get_bool_setting = AsyncMock(side_effect=lambda _key, default: default)
    return orch


@pytest.mark.asyncio
async def test_exact_text_hit_replays_without_classify():
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="turn on kitchen light")
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()
    execute_cached_action = AsyncMock(return_value={"success": True, "entity_id": entry.cached_action.entity_id})

    with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock) as track:
        result = await manager.try_replay_action(
            query_text=entry.query_text,
            language=entry.language,
            check_visibility=AsyncMock(return_value=True),
            execute_cached_action=execute_cached_action,
        )

    assert result is not None
    assert result.kind == "full_hit"
    assert result.response_text == entry.response_text
    assert result.similarity == pytest.approx(1.0)
    execute_cached_action.assert_awaited_once_with(entry.cached_action)
    manager._action_cache.invalidate_by_entry_id.assert_not_called()
    track.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_exact_match_no_replay():
    manager = _make_manager()
    manager._action_cache.lookup_with_id = MagicMock(return_value=(None, None, None))
    execute_cached_action = AsyncMock()

    result = await manager.try_replay_action(
        query_text="switch on the kitchen lamp",
        language="en",
        check_visibility=AsyncMock(),
        execute_cached_action=execute_cached_action,
    )

    assert result is None
    execute_cached_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_visibility_recheck_failure_invalidates_row():
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="turn on kitchen light")
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        check_visibility=AsyncMock(return_value=False),
        execute_cached_action=AsyncMock(return_value={"success": True}),
    )

    assert result is None
    manager._action_cache.invalidate_by_entry_id.assert_called_once()


@pytest.mark.asyncio
async def test_transient_replay_miss_does_not_invalidate():
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="turn on kitchen light")
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        check_visibility=AsyncMock(return_value=True),
        execute_cached_action=AsyncMock(return_value=None),
    )

    assert result is None
    manager._action_cache.invalidate_by_entry_id.assert_not_called()


@pytest.mark.asyncio
async def test_full_hit_skips_both_classify_and_dispatch():
    cache_manager = MagicMock()
    cache_manager.apply_rewrite = AsyncMock(return_value="Cached speech")
    orch = _make_orchestrator(cache_manager)
    action_hit = ActionReplayOutcome(
        kind="full_hit",
        entry_id="action-1",
        agent_id="light-agent",
        response_text="Cached speech",
        replay_result={"success": True},
        similarity=1.0,
    )
    orch._cache_orchestrator.try_cache_replay = AsyncMock(return_value=(action_hit, None))
    orch._finalize_action_replay_hit = AsyncMock(
        return_value={
            "speech": "Cached speech",
            "routed_to": "light-agent",
            "action_executed": {"success": True},
            "voice_followup": False,
        }
    )
    orch._classification_engine.classify = AsyncMock(
        side_effect=AssertionError("classification should be skipped on full hit")
    )
    orch._dispatch_manager.dispatch_single = AsyncMock(
        side_effect=AssertionError("dispatch should be skipped on full hit")
    )

    result = await orch._handle_task_impl(_make_task("turn on kitchen light"))

    assert result["speech"] == "Cached speech"
    assert result["routed_to"] == "light-agent"
    orch._finalize_action_replay_hit.assert_awaited_once()
    orch._classification_engine.classify.assert_not_awaited()
    orch._dispatch_manager.dispatch_single.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_hit_rewrite_receives_user_text():
    cache_manager = MagicMock()
    cache_manager.apply_rewrite = AsyncMock(return_value="German speech")
    orch = _make_orchestrator(cache_manager)
    action_hit = ActionReplayOutcome(
        kind="full_hit",
        entry_id="action-1",
        agent_id="light-agent",
        response_text="Done, Keller is on.",
        replay_result={"success": True},
        similarity=1.0,
    )
    orch._store_turn = AsyncMock()
    orch._get_turns = AsyncMock(return_value=[])

    result = await orch._finalize_action_replay_hit(
        hit=action_hit,
        conversation_id="conv-1",
        user_text="Keller einschalten",
        span_collector=None,
    )

    cache_manager.apply_rewrite.assert_awaited_once()
    call_args = cache_manager.apply_rewrite.call_args
    assert call_args[0][0] is action_hit
    assert call_args[1]["user_text"] == "Keller einschalten"
    assert result["speech"] == "German speech"


@pytest.mark.asyncio
async def test_multi_target_visibility_recheck_invalidates_when_secondary_entity_revoked():
    manager = _make_manager()
    entry = make_action_cache_entry(
        query_text="turn on kitchen and living room lights",
        entity_ids=["light.kitchen", "light.living_room"],
    )
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()

    def _check_visibility(agent_id: str, entity_id: str) -> bool:
        return entity_id != "light.living_room"

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        check_visibility=AsyncMock(side_effect=_check_visibility),
        execute_cached_action=AsyncMock(return_value={"success": True}),
    )

    assert result is None
    manager._action_cache.invalidate_by_entry_id.assert_called_once()
