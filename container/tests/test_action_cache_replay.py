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

from app.agents.cache_orchestrator import CacheOrchestrator
from app.agents.orchestrator import OrchestratorAgent
from app.cache.cache_manager import ActionReplayOutcome, ActionReplayRejected, CacheManager
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

    with patch("app.cache.cache_manager.track_cache_event_background") as track:
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
    track.assert_called_once()


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

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "visibility_rejected"
    assert result.details == {"entity_id": entry.cached_action.entity_id}
    assert result.allow_routing_skip is True
    manager._action_cache.invalidate_by_entry_id.assert_called_once()
    assert manager.get_deletion_stats()["deletions"] == {"visibility_rejected": 1}


@pytest.mark.asyncio
async def test_visibility_rejection_during_index_warmup_keeps_row_and_refuses_replay():
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="turn on kitchen light")
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()
    manager.set_entity_index_readiness_probe(lambda: False)
    execute_cached_action = AsyncMock(return_value={"success": True})

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        check_visibility=AsyncMock(return_value=False),
        execute_cached_action=execute_cached_action,
    )

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "visibility_deferred_index_not_ready"
    assert result.details == {"entity_id": entry.cached_action.entity_id}
    assert result.allow_routing_skip is True
    execute_cached_action.assert_not_awaited()
    manager._action_cache.invalidate_by_entry_id.assert_not_called()
    assert manager.get_deletion_stats() == {"deletions": {}, "deletions_deferred_index_not_ready": 1}


@pytest.mark.asyncio
async def test_lookup_deletions_record_their_specific_reason():
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="turn on kitchen light")
    entry.context_dependent = True
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()
    # Index readiness only defers index-dependent reasons.
    manager.set_entity_index_readiness_probe(lambda: False)

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        check_visibility=AsyncMock(return_value=True),
        execute_cached_action=AsyncMock(),
    )

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "context_dependent"
    manager._action_cache.invalidate_by_entry_id.assert_called_once_with("entry-1")
    assert manager.get_deletion_stats()["deletions"] == {"context_dependent": 1}


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

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "replay_failed"
    assert result.allow_routing_skip is True
    manager._action_cache.invalidate_by_entry_id.assert_not_called()


@pytest.mark.asyncio
async def test_origin_mismatch_forces_live_without_calling_cached_action():
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="turn on the light")
    entry.origin_required = True
    entry.origin_area_id = "kitchen"
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    replay = AsyncMock(return_value={"success": True})

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        origin_area_id="bedroom",
        check_visibility=AsyncMock(return_value=True),
        execute_cached_action=replay,
    )

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "origin_mismatch"
    assert result.details == {"expected_area_id": "kitchen", "actual_area_id": "bedroom"}
    assert result.allow_routing_skip is False
    replay.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_origin_replays_after_visibility_recheck():
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="turn on the light")
    entry.origin_required = True
    entry.origin_area_id = "kitchen"
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    replay = AsyncMock(return_value={"success": True})

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        origin_area_id="kitchen",
        check_visibility=AsyncMock(return_value=True),
        execute_cached_action=replay,
    )

    assert isinstance(result, ActionReplayOutcome)
    replay.assert_awaited_once_with(entry.cached_action)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_area", "stored_device", "current_area", "current_device"),
    [
        (None, "satellite.kitchen", None, None),
        (None, None, "kitchen", None),
    ],
)
async def test_required_origin_with_missing_current_or_stored_provenance_forces_live(
    stored_area, stored_device, current_area, current_device
):
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="turn on the light")
    entry.origin_required = True
    entry.origin_area_id = stored_area
    entry.origin_device_id = stored_device
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    replay = AsyncMock(return_value={"success": True})

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        origin_area_id=current_area,
        origin_device_id=current_device,
        check_visibility=AsyncMock(return_value=True),
        execute_cached_action=replay,
    )

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "origin_mismatch"
    replay.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_action_entry_is_removed_and_forces_live():
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="set fan to 60")
    entry.schema_version = 4
    entry.cached_action.service = "fan/set_percentage"
    entry.cached_action.service_data = {}
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()
    replay = AsyncMock(return_value={"success": True})

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        check_visibility=AsyncMock(return_value=True),
        execute_cached_action=replay,
    )

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "legacy_schema"
    replay.assert_not_awaited()
    manager._action_cache.invalidate_by_entry_id.assert_called_once_with("entry-1")


@pytest.mark.asyncio
async def test_provenance_rejection_does_not_use_routing_skip():
    cache_manager = MagicMock()
    cache_manager.try_replay_action = AsyncMock(
        return_value=ActionReplayRejected(entry_id="entry-1", reason="origin_mismatch")
    )
    orchestrator = CacheOrchestrator(cache_manager=cache_manager)
    orchestrator._get_bool_setting_impl = AsyncMock(return_value=True)

    action_hit, routing_hit = await orchestrator.try_cache_replay(
        task=_make_task("turn on the light"),
        user_text="turn on the light",
    )

    assert action_hit is None
    assert routing_hit is None
    cache_manager.try_routing_skip.assert_not_called()


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
async def test_streamed_full_hit_done_chunk_carries_bridge_metadata():
    """M-7: streamed action-replay hit -> done chunk carries routed_to +
    action_executed (+ voice_followup when true)."""
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
            "voice_followup": True,
        }
    )
    orch._classification_engine.classify = AsyncMock(
        side_effect=AssertionError("classification should be skipped on full hit")
    )

    chunks = [c async for c in orch.handle_task_stream(_make_task("turn on kitchen light"))]

    assert len(chunks) == 1
    done = chunks[0]
    assert done["done"] is True
    assert done["routed_to"] == "light-agent"
    assert done["action_executed"] == {"success": True}
    assert done["voice_followup"] is True
    assert done["mediated_speech"] == "Cached speech"


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

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "visibility_rejected"
    assert result.details == {"entity_id": "light.living_room"}
    manager._action_cache.invalidate_by_entry_id.assert_called_once()


@pytest.mark.asyncio
async def test_pending_question_skips_action_cache_replay():
    """Follow-up signal: while a clarifying question is pending, the action
    cache replay lookup is skipped so the answer reaches classification."""
    from app.agents.task_pipeline import CacheReplayResult

    orch = _make_orchestrator(MagicMock())
    orch._conversation_manager.set_pending_question("conv-action-cache", "Welches Licht meinst du?", "light-agent")
    orch._pipeline_director.run_cache_replay = AsyncMock(return_value=CacheReplayResult())
    orch._pipeline_director.run_classification = AsyncMock(
        return_value=([("light-agent", "kuche", 0.9)], False, "light-agent", "kuche", 0.9)
    )

    prelude = await orch._run_pipeline_prelude(_make_task("kuche"))

    assert prelude.early_exit is None
    assert orch._pipeline_director.run_cache_replay.await_args.kwargs["skip_lookup"] is True

    # Without a pending question the replay lookup runs as before.
    orch2 = _make_orchestrator(MagicMock())
    orch2._pipeline_director.run_cache_replay = AsyncMock(return_value=CacheReplayResult())
    orch2._pipeline_director.run_classification = AsyncMock(
        return_value=([("light-agent", "kuche", 0.9)], False, "light-agent", "kuche", 0.9)
    )

    prelude2 = await orch2._run_pipeline_prelude(_make_task("kuche"))

    assert prelude2.early_exit is None
    assert orch2._pipeline_director.run_cache_replay.await_args.kwargs["skip_lookup"] is False


# ---------------------------------------------------------------------------
# Schema 6: global rows by default, area binding only, never device-bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_area", "current_device"),
    [
        (None, None),
        ("bedroom", None),
        (None, "satellite.hallway"),
        ("kitchen", "satellite.kitchen"),
    ],
)
async def test_global_row_replays_for_any_origin(current_area, current_device):
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="kitchen off")
    assert entry.origin_required is False
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    replay = AsyncMock(return_value={"success": True})

    with patch("app.cache.cache_manager.track_cache_event_background"):
        result = await manager.try_replay_action(
            query_text=entry.query_text,
            language=entry.language,
            origin_area_id=current_area,
            origin_device_id=current_device,
            check_visibility=AsyncMock(return_value=True),
            execute_cached_action=replay,
        )

    assert isinstance(result, ActionReplayOutcome)
    replay.assert_awaited_once_with(entry.cached_action)


@pytest.mark.asyncio
async def test_area_bound_row_ignores_device_id():
    """An area-bound row replays on any satellite of that area: rows are never device-bound."""
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="light on")
    entry.origin_required = True
    entry.origin_area_id = "kitchen"
    entry.origin_device_id = "satellite.kitchen_left"
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    replay = AsyncMock(return_value={"success": True})

    with patch("app.cache.cache_manager.track_cache_event_background"):
        result = await manager.try_replay_action(
            query_text=entry.query_text,
            language=entry.language,
            origin_area_id="kitchen",
            origin_device_id="satellite.kitchen_right",
            check_visibility=AsyncMock(return_value=True),
            execute_cached_action=replay,
        )

    assert isinstance(result, ActionReplayOutcome)


@pytest.mark.asyncio
@pytest.mark.parametrize("current_area", [None, "bedroom"])
async def test_area_bound_row_rejects_other_or_missing_area_without_invalidating(current_area):
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="light on")
    entry.origin_required = True
    entry.origin_area_id = "kitchen"
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()
    replay = AsyncMock(return_value={"success": True})

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        origin_area_id=current_area,
        origin_device_id="satellite.kitchen",
        check_visibility=AsyncMock(return_value=True),
        execute_cached_action=replay,
    )

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "origin_mismatch"
    assert result.details == {"expected_area_id": "kitchen", "actual_area_id": current_area}
    replay.assert_not_awaited()
    manager._action_cache.invalidate_by_entry_id.assert_not_called()


@pytest.mark.asyncio
async def test_schema_5_row_is_rejected_as_legacy():
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="kitchen off")
    entry.schema_version = 5
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()
    replay = AsyncMock(return_value={"success": True})

    result = await manager.try_replay_action(
        query_text=entry.query_text,
        language=entry.language,
        check_visibility=AsyncMock(return_value=True),
        execute_cached_action=replay,
    )

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "legacy_schema"
    replay.assert_not_awaited()
    manager._action_cache.invalidate_by_entry_id.assert_called_once_with("entry-1")


@pytest.mark.asyncio
async def test_visibility_rejection_logs_entry_and_entity(caplog):
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="turn on kitchen light")
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()

    with caplog.at_level("INFO", logger="app.cache.cache_manager"):
        await manager.try_replay_action(
            query_text=entry.query_text,
            language=entry.language,
            check_visibility=AsyncMock(return_value=False),
            execute_cached_action=AsyncMock(return_value={"success": True}),
        )

    records = [r for r in caplog.records if "visibility recheck" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelname == "INFO"
    assert "entry-1" in records[0].getMessage()
    assert entry.cached_action.entity_id in records[0].getMessage()


@pytest.mark.asyncio
async def test_replay_exception_is_replay_failed_and_logged_as_warning(caplog):
    manager = _make_manager()
    entry = make_action_cache_entry(query_text="turn on kitchen light")
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()

    with caplog.at_level("WARNING", logger="app.cache.cache_manager"):
        result = await manager.try_replay_action(
            query_text=entry.query_text,
            language=entry.language,
            check_visibility=AsyncMock(return_value=True),
            execute_cached_action=AsyncMock(side_effect=RuntimeError("ha down")),
        )

    assert isinstance(result, ActionReplayRejected)
    assert result.reason == "replay_failed"
    assert result.details == {"entity_id": entry.cached_action.entity_id}
    records = [r for r in caplog.records if "replay failed" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is not None
    manager._action_cache.invalidate_by_entry_id.assert_not_called()


# ---------------------------------------------------------------------------
# cache_lookup span: explicit action-tier reasons
# ---------------------------------------------------------------------------


async def _cache_lookup_span(action_result, routing_result=None, *, routing_valid=True) -> dict:
    from app.analytics.tracer import SpanCollector

    cache_manager = MagicMock()
    cache_manager.try_replay_action = AsyncMock(return_value=action_result)
    cache_manager.try_routing_skip = AsyncMock(return_value=routing_result)
    orchestrator = CacheOrchestrator(cache_manager=cache_manager)
    orchestrator._get_bool_setting_impl = AsyncMock(return_value=True)
    orchestrator._routing_hit_is_still_valid = AsyncMock(return_value=routing_valid)
    collector = SpanCollector("trace-action-tier")

    with patch("app.agents.cache_orchestrator.track_cache_event_background"):
        await orchestrator.try_cache_replay(
            task=_make_task("turn on the light"),
            user_text="turn on the light",
            span_collector=collector,
        )

    spans = [s for s in collector._spans if s["span_name"] == "cache_lookup"]
    assert len(spans) == 1
    return spans[0]["metadata"]


@pytest.mark.asyncio
async def test_span_visibility_rejected_on_routing_miss():
    rejected = ActionReplayRejected(
        entry_id="entry-1",
        reason="visibility_rejected",
        details={"entity_id": "light.kitchen"},
        allow_routing_skip=True,
    )

    meta = await _cache_lookup_span(rejected)

    assert meta["hit_type"] == "visibility_rejected"
    assert meta["action_tier_reason"] == "visibility_rejected"
    assert meta["entity_id"] == "light.kitchen"
    assert meta["cache_tier"] == 0


@pytest.mark.asyncio
async def test_span_visibility_deferred_during_index_warmup():
    rejected = ActionReplayRejected(
        entry_id="entry-1",
        reason="visibility_deferred_index_not_ready",
        details={"entity_id": "light.kitchen"},
        allow_routing_skip=True,
    )

    meta = await _cache_lookup_span(rejected)

    assert meta["hit_type"] == "visibility_deferred_index_not_ready"
    assert meta["action_tier_reason"] == "visibility_deferred_index_not_ready"
    assert meta["entity_id"] == "light.kitchen"
    assert meta["cache_tier"] == 0


@pytest.mark.asyncio
async def test_span_records_action_tier_reason_on_routing_hit():
    from app.cache.cache_manager import RoutingSkipOutcome

    rejected = ActionReplayRejected(
        entry_id="entry-1",
        reason="replay_failed",
        details={"entity_id": "light.kitchen"},
        allow_routing_skip=True,
    )
    routing = RoutingSkipOutcome(
        kind="routing_hit",
        entry_id="route-1",
        agent_id="light-agent",
        condensed_task="turn on the light",
        similarity=1.0,
    )

    meta = await _cache_lookup_span(rejected, routing)

    assert meta["hit_type"] == "routing_hit"
    assert meta["action_tier_reason"] == "replay_failed"
    assert meta["entity_id"] == "light.kitchen"
    assert meta["cache_tier"] == 1


@pytest.mark.asyncio
async def test_span_origin_mismatch_carries_expected_and_actual_area():
    rejected = ActionReplayRejected(
        entry_id="entry-1",
        reason="origin_mismatch",
        details={"expected_area_id": "kitchen", "actual_area_id": "bedroom"},
    )

    meta = await _cache_lookup_span(rejected)

    assert meta["hit_type"] == "origin_mismatch"
    assert meta["action_tier_reason"] == "origin_mismatch"
    assert meta["expected_area_id"] == "kitchen"
    assert meta["actual_area_id"] == "bedroom"


@pytest.mark.asyncio
async def test_span_plain_miss_keeps_miss_hit_type():
    meta = await _cache_lookup_span(None)

    assert meta["hit_type"] == "miss"
    assert meta["action_tier_reason"] == "miss"
