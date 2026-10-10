"""Regression tests for issue #132 theme T3: routing/action cache, validator, embedding.

Covers context-dependent turns, bidirectional origin checks, replay
existence/agent gates, anaphora hints on cache hits, semantic-hit
invalidation, per-key invalidation, stale reply text, compare-and-swap
validator writes, and embedding-model tagging of routing vectors.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.cache_orchestrator import CacheOrchestrator
from app.cache.cache_manager import (
    ActionReplayOutcome,
    ActionReplayRejected,
    CacheManager,
    parse_semantic_entry_token,
)
from app.cache.cache_validator import ActionCacheValidator
from app.cache.sqlite_cache_store import COLLECTION_ACTION_CACHE, COLLECTION_ROUTING_CACHE, SqliteCacheStore
from app.models.agent import IngressTask, LastEntity, TaskContext
from app.models.cache import ActionCacheEntry, CachedAction
from tests.helpers import make_action_cache_entry, make_entity_index_entry

V_STORED = [1.0, 0.0, 0.0, 0.0]
V_CLOSE = [0.96, 0.28, 0.0, 0.0]


def _engine(vector: list[float], model_id: str = "local:model-a") -> SimpleNamespace:
    return SimpleNamespace(embed=AsyncMock(return_value=vector), model_id=model_id)


def _make_store(tmp_path) -> SqliteCacheStore:
    return SqliteCacheStore(str(tmp_path / "cache.db"))


def _make_manager(store: SqliteCacheStore) -> CacheManager:
    manager = CacheManager(store)
    manager._routing_cache._enabled = True
    manager._routing_cache._semantic_enabled = True
    manager._routing_cache._semantic_threshold = 0.92
    manager._action_cache._enabled = True
    return manager


def _cache_orchestrator(cache_manager=None, **kwargs) -> tuple[CacheOrchestrator, MagicMock]:
    cm = cache_manager or MagicMock()
    if cache_manager is None:
        cm.store_routing_async = AsyncMock()
        cm.store_action_async = AsyncMock()
    co = CacheOrchestrator(cache_manager=cm, entity_index=None, ha_client=None, agent_registry=None, **kwargs)
    return co, cm


_WRITE_RESULT = {
    "success": True,
    "action": "turn_off",
    "entity_id": "light.kitchen",
    "service_data": {},
}


async def _store(co: CacheOrchestrator, task: IngressTask, **overrides):
    kwargs = {
        "user_text": task.description,
        "language": "en",
        "target_agent": "light-agent",
        "condensed_task": task.description,
        "confidence": 0.9,
        "speech": "Done.",
        "original_response_text": "Done.",
        "action_executed": dict(_WRITE_RESULT),
        "has_error": False,
        "task": task,
    }
    kwargs.update(overrides)
    with patch.object(co, "_get_bool_setting_impl", new=AsyncMock(return_value=True)):
        return await co.store_after_dispatch(**kwargs)


# ---------------------------------------------------------------------------
# Item 1: context-dependent turns are never stored
# ---------------------------------------------------------------------------


class TestContextDependentTurnsNotStored:
    @pytest.mark.asyncio
    async def test_anaphoric_command_resolved_via_last_entities_is_not_stored(self):
        co, cm = _cache_orchestrator()
        task = IngressTask(
            description="turn it off",
            context=TaskContext(last_entities=[LastEntity(entity_id="light.kitchen", friendly_name="Kitchen")]),
        )

        result = await _store(co, task)

        assert result == (False, False)
        cm.store_action_async.assert_not_called()
        cm.store_routing_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_followup_answer_is_not_stored(self):
        co, cm = _cache_orchestrator()
        task = IngressTask(
            description="the kitchen one",
            context=TaskContext(is_followup=True, pending_question="Which light?"),
        )

        result = await _store(co, task)

        assert result == (False, False)
        cm.store_action_async.assert_not_called()
        cm.store_routing_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_readonly_followup_does_not_store_routing_either(self):
        co, cm = _cache_orchestrator()
        task = IngressTask(description="and that one?", context=TaskContext(is_followup=True))

        result = await _store(
            co,
            task,
            action_executed={"success": True, "action": "query_light_state", "entity_id": "light.kitchen"},
        )

        assert result == (False, False)
        cm.store_routing_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_unrelated_last_entity_still_stores(self):
        co, cm = _cache_orchestrator()
        task = IngressTask(
            description="turn off the kitchen light",
            context=TaskContext(last_entities=[LastEntity(entity_id="media_player.tv")]),
        )

        result = await _store(co, task)

        assert result == (True, False)
        cm.store_action_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_executed_command_entity_is_checked_against_last_entities(self):
        co, cm = _cache_orchestrator()
        task = IngressTask(
            description="turn it off",
            context=TaskContext(last_entities=[LastEntity(entity_id="climate.living")]),
        )
        action = {
            "success": True,
            "action": "turn_off",
            "executed_command": {
                "domain": "climate",
                "service": "turn_off",
                "entity_id": "climate.living",
                "service_data": {},
            },
        }

        result = await _store(co, task, target_agent="climate-agent", action_executed=action)

        assert result == (False, False)
        cm.store_action_async.assert_not_called()


# ---------------------------------------------------------------------------
# Item 9: replay fallback text never carries per-turn additions
# ---------------------------------------------------------------------------


class TestStoredReplyText:
    @pytest.mark.asyncio
    async def test_mediated_speech_with_unknown_additions_stores_base_speech(self):
        co, cm = _cache_orchestrator()
        task = IngressTask(description="turn off the kitchen light", context=TaskContext())

        await _store(
            co,
            task,
            speech="Kuechenlicht ist aus. Uebrigens: Zahnarzt um 15 Uhr. Noch etwas?",
            original_response_text="Done, the kitchen light is now off.",
        )

        entry: ActionCacheEntry = cm.store_action_async.await_args.args[0]
        assert entry.response_text == "Done, the kitchen light is now off."
        assert entry.original_response_text == "Done, the kitchen light is now off."

    @pytest.mark.asyncio
    async def test_mediated_speech_without_additions_is_kept(self):
        co, cm = _cache_orchestrator()
        task = IngressTask(description="turn off the kitchen light", context=TaskContext())

        await _store(
            co,
            task,
            speech="Kuechenlicht ist aus.",
            original_response_text="Done, the kitchen light is now off.",
            speech_has_turn_additions=False,
        )

        entry: ActionCacheEntry = cm.store_action_async.await_args.args[0]
        assert entry.response_text == "Kuechenlicht ist aus."


# ---------------------------------------------------------------------------
# Items 2 and 4: replay gates
# ---------------------------------------------------------------------------


def _replay_manager(entry: ActionCacheEntry) -> CacheManager:
    manager = CacheManager(MagicMock())
    manager._action_cache._enabled = True
    manager._action_cache.lookup_with_id = MagicMock(return_value=("entry-1", entry, 1.0))
    manager._action_cache.invalidate_by_entry_id = MagicMock()
    return manager


class TestReplayGates:
    @pytest.mark.asyncio
    async def test_origin_less_entry_does_not_replay_on_satellite_turn(self):
        entry = make_action_cache_entry(cached_action=CachedAction(service="light/turn_on", entity_id="light.kitchen"))
        manager = _replay_manager(entry)
        execute = AsyncMock(return_value={"success": True})

        result = await manager.try_replay_action(
            query_text=entry.query_text,
            origin_area_id="bedroom",
            check_visibility=AsyncMock(return_value=True),
            execute_cached_action=execute,
        )

        assert isinstance(result, ActionReplayRejected)
        assert result.reason == "origin_mismatch"
        execute.assert_not_called()
        # The row stays valid for origin-less (dashboard/text) turns.
        manager._action_cache.invalidate_by_entry_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_origin_less_entry_replays_on_origin_less_turn(self):
        entry = make_action_cache_entry(cached_action=CachedAction(service="light/turn_on", entity_id="light.kitchen"))
        manager = _replay_manager(entry)

        with patch("app.cache.cache_manager.track_cache_event_background"):
            result = await manager.try_replay_action(
                query_text=entry.query_text,
                check_visibility=AsyncMock(return_value=True),
                execute_cached_action=AsyncMock(return_value={"success": True}),
            )

        assert isinstance(result, ActionReplayOutcome)

    @pytest.mark.asyncio
    async def test_unknown_agent_rejects_and_invalidates(self):
        entry = make_action_cache_entry(cached_action=CachedAction(service="light/turn_on", entity_id="light.kitchen"))
        manager = _replay_manager(entry)
        execute = AsyncMock(return_value={"success": True})

        with patch("app.cache.cache_manager.track_cache_event_background"):
            result = await manager.try_replay_action(
                query_text=entry.query_text,
                check_visibility=AsyncMock(return_value=True),
                execute_cached_action=execute,
                check_agent=AsyncMock(return_value=False),
            )

        assert isinstance(result, ActionReplayRejected)
        assert result.reason == "agent_unavailable"
        execute.assert_not_called()
        manager._action_cache.invalidate_by_entry_id.assert_called_once_with("entry-1")

    @pytest.mark.asyncio
    async def test_try_cache_replay_passes_registry_agent_gate(self):
        registry = MagicMock()
        registry.get_known_agents = AsyncMock(return_value={"climate-agent"})
        cm = MagicMock()
        cm.try_replay_action = AsyncMock(return_value=None)
        cm.try_routing_skip = AsyncMock(return_value=None)
        co = CacheOrchestrator(cache_manager=cm, agent_registry=registry)

        with (
            patch.object(co, "_get_bool_setting_impl", new=AsyncMock(return_value=True)),
            patch("app.agents.cache_orchestrator.track_cache_event_background"),
        ):
            await co.try_cache_replay(user_text="turn on the light")

        check_agent = cm.try_replay_action.await_args.kwargs["check_agent"]
        assert await check_agent("light-agent") is False
        assert await check_agent("climate-agent") is True


# ---------------------------------------------------------------------------
# Item 5: cache hits update anaphora hints
# ---------------------------------------------------------------------------


class TestReplayHitAnaphora:
    @pytest.mark.asyncio
    async def test_finalize_action_replay_hit_stores_resolved_entities(self):
        entity_index = MagicMock()
        entity_index.get_by_id.return_value = make_entity_index_entry("light.kitchen", "Kitchen Light")
        store_turn = AsyncMock()
        cm = MagicMock()
        cm.apply_rewrite = AsyncMock(return_value="Done.")
        co = CacheOrchestrator(
            cache_manager=cm,
            entity_index=entity_index,
            store_turn=store_turn,
            get_turns=AsyncMock(return_value=[]),
        )
        hit = ActionReplayOutcome(
            kind="full_hit",
            entry_id="entry-1",
            agent_id="light-agent",
            response_text="Done.",
            replay_result={"success": True, "entity_id": "light.kitchen", "action": "turn_on"},
        )

        with patch("app.agents.cache_orchestrator.track_request_background"):
            await co.finalize_action_replay_hit(hit, "conv-1", "turn on the kitchen light", None)

        resolved = store_turn.await_args.kwargs["resolved_entities"]
        assert resolved and resolved[0]["entity_id"] == "light.kitchen"


# ---------------------------------------------------------------------------
# Item 6: semantic misroute does not delete the neighbour
# ---------------------------------------------------------------------------


class TestSemanticInvalidation:
    @pytest.mark.asyncio
    async def test_failed_semantic_turn_suppresses_but_keeps_neighbour(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            with patch("app.cache.cache_manager.get_embedding_engine", new=AsyncMock(return_value=_engine(V_STORED))):
                await manager.store_routing_async("turn on kitchen light", "light-agent", 0.95, language="en")
            with (
                patch("app.cache.cache_manager.get_embedding_engine", new=AsyncMock(return_value=_engine(V_CLOSE))),
                patch("app.cache.cache_manager.track_cache_event_background"),
            ):
                hit = await manager.try_routing_skip(query_text="switch on the kitchen lamp", language="en")
            assert hit is not None and hit.kind == "semantic_hit"
            assert parse_semantic_entry_token(hit.entry_id) is not None

            co = CacheOrchestrator(cache_manager=manager)
            await co.invalidate_served_routing(hit.entry_id, reason="cached_agent_turn_failed")

            # The neighbour row survives (its own wording still routes correctly) ...
            assert store.count(COLLECTION_ROUTING_CACHE) == 1
            with (
                patch("app.cache.cache_manager.get_embedding_engine", new=AsyncMock(return_value=_engine(V_STORED))),
                patch("app.cache.cache_manager.track_cache_event_background"),
            ):
                exact = await manager.try_routing_skip(query_text="turn on kitchen light", language="en")
            assert exact is not None and exact.kind == "routing_hit"
            # ... but no longer serves the query that it misrouted.
            with (
                patch("app.cache.cache_manager.get_embedding_engine", new=AsyncMock(return_value=_engine(V_CLOSE))),
                patch("app.cache.cache_manager.track_cache_event_background"),
            ):
                again = await manager.try_routing_skip(query_text="switch on the kitchen lamp", language="en")
            assert again is None
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_exact_hit_invalidation_still_deletes(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            manager._routing_cache._semantic_enabled = False
            await manager.store_routing_async("turn on kitchen light", "light-agent", 0.95, language="en")
            with patch("app.cache.cache_manager.track_cache_event_background"):
                hit = await manager.try_routing_skip(query_text="turn on kitchen light", language="en")
            assert hit is not None and hit.kind == "routing_hit"
            assert hit.source_entry_id == hit.entry_id

            co = CacheOrchestrator(cache_manager=manager)
            await co.invalidate_served_routing(hit.entry_id, reason="cached_agent_turn_failed")

            assert store.count(COLLECTION_ROUTING_CACHE) == 0
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_clarifying_question_signal_is_a_noop(self):
        cm = MagicMock()
        co = CacheOrchestrator(cache_manager=cm)

        await co.invalidate_served_routing("entry-1", reason="cached_agent_turn_failed", clarifying_question=True)

        cm.invalidate_routing.assert_not_called()


# ---------------------------------------------------------------------------
# Item 10: embedding model swap at the same dimension
# ---------------------------------------------------------------------------


class TestEmbeddingModelSwap:
    @pytest.mark.asyncio
    async def test_vectors_from_another_model_are_not_served_and_are_dropped(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            with patch(
                "app.cache.cache_manager.get_embedding_engine",
                new=AsyncMock(return_value=_engine(V_STORED, "local:model-a")),
            ):
                await manager.store_routing_async("turn on kitchen light", "light-agent", 0.95, language="en")
            assert store.has_routing_embedding(manager._routing_cache.make_entry_id("turn on kitchen light"))

            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=_engine(V_CLOSE, "local:model-b")),
                ),
                patch("app.cache.cache_manager.track_cache_event_background"),
            ):
                hit = await manager.try_routing_skip(query_text="switch on the kitchen lamp", language="en")

            assert hit is None
            entry_id = manager._routing_cache.make_entry_id("turn on kitchen light")
            assert not store.has_routing_embedding(entry_id)
            # The routing row itself is kept for exact hits.
            assert store.count(COLLECTION_ROUTING_CACHE) == 1
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_backfill_reembeds_entry_from_another_model(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            with patch(
                "app.cache.cache_manager.get_embedding_engine",
                new=AsyncMock(return_value=_engine(V_STORED, "local:model-a")),
            ):
                await manager.store_routing_async("turn on kitchen light", "light-agent", 0.95, language="en")
            entry_id = manager._routing_cache.make_entry_id("turn on kitchen light")
            engine_b = _engine(V_STORED, "local:model-b")
            with patch("app.cache.cache_manager.get_embedding_engine", new=AsyncMock(return_value=engine_b)):
                await manager._backfill_routing_embedding(entry_id, "turn on kitchen light", "local:model-a")

            engine_b.embed.assert_awaited_once()
            meta = store.get(COLLECTION_ROUTING_CACHE, ids=[entry_id], include=["metadatas"])["metadatas"][0]
            assert meta["embedding_model"] == "local:model-b"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Item 3: validator writes are compare-and-swap and off the loop
# ---------------------------------------------------------------------------


def _action_manager(store: SqliteCacheStore) -> CacheManager:
    manager = CacheManager(store)
    manager._action_cache._enabled = True
    return manager


def _snapshot(manager: CacheManager) -> ActionCacheEntry:
    entries = list(manager.iter_action_entries())
    assert len(entries) == 1
    return entries[0]


class TestValidatorCompareAndSwap:
    @pytest.mark.asyncio
    async def test_update_does_not_resurrect_deleted_row(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _action_manager(store)
            manager.store_action(make_action_cache_entry())
            snapshot = _snapshot(manager)
            manager.invalidate_action(manager._action_cache.make_entry_id(snapshot.query_text))

            snapshot.validated_at = "2026-10-10T00:00:00+00:00"
            updated = await manager.update_action_entry(snapshot)

            assert updated is False
            assert store.count(COLLECTION_ACTION_CACHE) == 0
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_update_does_not_revert_restored_row(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _action_manager(store)
            manager.store_action(make_action_cache_entry(response_text="Old text."))
            snapshot = _snapshot(manager)
            await asyncio.sleep(0.002)
            manager.store_action(make_action_cache_entry(response_text="Fresh text."))

            snapshot.response_text = "Validator text."
            snapshot.validated_at = "2026-10-10T00:00:00+00:00"
            updated = await manager.update_action_entry(snapshot)

            assert updated is False
            assert _snapshot(manager).response_text == "Fresh text."
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_update_patches_unchanged_row_and_keeps_hit_count(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _action_manager(store)
            manager.store_action(make_action_cache_entry(response_text="Old text."))
            snapshot = _snapshot(manager)
            entry_id = manager._action_cache.make_entry_id(snapshot.query_text)
            manager._action_cache.lookup_with_id(snapshot.query_text)
            manager.flush_pending()

            snapshot.response_text = "Validator text."
            snapshot.validated_at = "2026-10-10T00:00:00+00:00"
            assert await manager.update_action_entry(snapshot) is True

            meta = store.get(COLLECTION_ACTION_CACHE, ids=[entry_id], include=["metadatas"])["metadatas"][0]
            assert meta["response_text"] == "Validator text."
            assert meta["validated_at"] == "2026-10-10T00:00:00+00:00"
            assert meta["hit_count"] == "1"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_hit_count_flush_does_not_revert_validator_patch(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _action_manager(store)
            manager.store_action(make_action_cache_entry(response_text="Old text."))
            snapshot = _snapshot(manager)
            entry_id = manager._action_cache.make_entry_id(snapshot.query_text)
            # A hit queues a metadata snapshot carrying the OLD response text.
            manager._action_cache.lookup_with_id(snapshot.query_text)
            store.patch_metadata_if_matches(
                COLLECTION_ACTION_CACHE,
                entry_id,
                {"created_at": snapshot.created_at},
                {"response_text": "Validator text."},
            )
            manager.flush_pending()

            meta = store.get(COLLECTION_ACTION_CACHE, ids=[entry_id], include=["metadatas"])["metadatas"][0]
            assert meta["response_text"] == "Validator text."
            assert meta["hit_count"] == "1"
        finally:
            store.close()

    def test_conditional_delete_keeps_restored_row(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _action_manager(store)
            manager.store_action(make_action_cache_entry(response_text="Old text."))
            snapshot = _snapshot(manager)
            entry_id = manager._action_cache.make_entry_id(snapshot.query_text)
            import time as _time

            _time.sleep(0.002)
            manager.store_action(make_action_cache_entry(response_text="Fresh text."))

            assert manager.invalidate_action(entry_id, expected_created_at=snapshot.created_at) is False
            assert store.count(COLLECTION_ACTION_CACHE) == 1
            fresh = _snapshot(manager)
            assert manager.invalidate_action(entry_id, expected_created_at=fresh.created_at) is True
            assert store.count(COLLECTION_ACTION_CACHE) == 0
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_validator_scan_and_delete_run_off_the_loop(self):
        loop_thread = threading.get_ident()
        threads: dict[str, int] = {}
        entry = make_action_cache_entry()

        def _iter(page_size=1000):
            threads["scan"] = threading.get_ident()
            return iter([entry])

        def _invalidate(entry_id, **kwargs):
            threads["delete"] = threading.get_ident()
            threads["expected"] = kwargs.get("expected_created_at")
            return True

        cache_manager = MagicMock()
        cache_manager.iter_action_entries = MagicMock(side_effect=_iter)
        cache_manager.update_action_entry = AsyncMock(return_value=True)
        cache_manager.invalidate_action = MagicMock(side_effect=_invalidate)
        action_cache = MagicMock()
        action_cache.make_entry_id = MagicMock(return_value="entry-1")
        validator = ActionCacheValidator(action_cache=action_cache, cache_manager=cache_manager)

        async def _settings(key, default=None):
            return {"cache.validator.enabled": "true", "cache.validator.batch_size": "10"}.get(key, default)

        with (
            patch("app.cache.cache_validator.SettingsRepository.get_value", new=AsyncMock(side_effect=_settings)),
            patch("app.cache.cache_validator.CacheValidatorRepository") as repo,
            patch("app.cache.cache_validator.CacheValidatorAuditRepository") as audit,
            patch.object(validator, "_validate_entry", new=AsyncMock(return_value=(False, None, "invalidate"))),
        ):
            repo.insert_started = AsyncMock(return_value=1)
            repo.update_finished = AsyncMock()
            audit.insert_entry = AsyncMock()
            audit.cleanup_old = AsyncMock()
            await validator.run_once()

        assert threads["scan"] != loop_thread
        assert threads["delete"] != loop_thread
        assert threads["expected"] == (entry.created_at or "")


# ---------------------------------------------------------------------------
# Items 7 and 8
# ---------------------------------------------------------------------------


class TestStatsAndPerKeyInvalidation:
    @pytest.mark.asyncio
    async def test_get_stats_async_runs_off_the_loop(self):
        manager = CacheManager(MagicMock())
        loop_thread = threading.get_ident()
        seen: list[int] = []

        def _stats():
            seen.append(threading.get_ident())
            return {"count": 0}

        manager._routing_cache.get_stats = MagicMock(side_effect=_stats)
        manager._action_cache.get_stats = MagicMock(side_effect=_stats)

        stats = await manager.get_stats_async()

        assert stats == {"routing": {"count": 0}, "action": {"count": 0}}
        assert seen and all(ident != loop_thread for ident in seen)

    def test_single_row_invalidation_keeps_unrelated_store(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _action_manager(store)
            cache = manager._action_cache
            original_flush = cache._flush_pending_updates

            def flush_then_invalidate_other():
                original_flush()
                cache.invalidate_by_entry_id("some-other-row")

            cache._flush_pending_updates = flush_then_invalidate_other
            manager.store_action(make_action_cache_entry())

            assert store.count(COLLECTION_ACTION_CACHE) == 1
        finally:
            store.close()
