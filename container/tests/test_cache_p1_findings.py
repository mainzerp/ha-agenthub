"""Regression tests for the two-cache P1 findings."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.cache_orchestrator import CacheOrchestrator
from app.cache._state import _CacheState
from app.cache.action_cache import ActionCache
from app.cache.routing_cache import RoutingCache
from app.cache.vector_store import VectorStore
from app.models.cache import ActionCacheEntry, CachedAction


class TestCacheStateConcurrency:
    def test_record_pending_update_is_threadsafe(self):
        state = _CacheState()
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                state.record_pending_update(f"id-{i}", f"q-{i}", {"hit_count": str(i)}, flush_interval=10_000)
            except BaseException as exc:  # pragma: no cover - defensive
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        pending = state.snapshot_pending()
        assert len(pending) == 100

    def test_swap_pending_returns_consistent_snapshot(self):
        state = _CacheState()
        for i in range(5):
            state.record_pending_update(f"id-{i}", "q", {}, flush_interval=10_000)

        snap = state.swap_pending()
        assert len(snap) == 5
        # After swap the internal buffer must be empty so the next
        # flush does not double-write the same rows.
        assert not state.has_pending()

    def test_requeue_failed_restores_pending(self):
        state = _CacheState()
        state.record_pending_update("id-1", "q", {"hit_count": "1"}, flush_interval=10_000)
        snap = state.swap_pending()
        assert not state.has_pending()
        state.requeue_failed(snap)
        assert state.has_pending()
        restored = state.snapshot_pending()
        assert restored["id-1"][1]["hit_count"] == "1"


class TestRoutingCacheStoreConcurrency:
    def _make_cache(self) -> tuple[RoutingCache, MagicMock]:
        store = MagicMock(spec=VectorStore)
        store.count.return_value = 0
        cache = RoutingCache(store)
        cache._threshold = 0.92
        cache._max_entries = 10_000
        cache._eviction_interval = 10_000
        return cache, store

    def test_parallel_stores_do_not_lose_updates(self):
        """50 concurrent store() calls -- no crash, upserts on every call."""
        cache, store = self._make_cache()

        async def spawn_all() -> None:
            await asyncio.gather(
                *(
                    asyncio.to_thread(
                        cache.store,
                        query_text=f"query-{i}",
                        agent_id="light-agent",
                        confidence=0.95,
                    )
                    for i in range(50)
                )
            )

        asyncio.run(spawn_all())
        # Each store() produces exactly one upsert regardless of
        # scheduling. If the shared counter or the pending map were
        # corrupted, the MagicMock would either raise or the test
        # would deadlock before reaching the assertion.
        assert store.upsert.call_count == 50

    def test_invalidate_by_entry_id_bumps_generation(self):
        """F6 / T3: invalidate_by_entry_id() must bump the invalidation
        generation so a concurrent store() that captured the pre-invalidate
        generation is rejected and cannot resurrect the deleted row."""
        cache, store = self._make_cache()
        gen_before = cache._state.current_generation()
        cache.invalidate_by_entry_id("some-entry-id")
        gen_after = cache._state.current_generation()
        assert gen_after != gen_before, "invalidation must bump the state generation"
        assert store.delete.call_count == 1


class TestRoutingCacheFlushRequeue:
    def test_flush_failure_requeues_pending(self):
        """P1-3: when update_metadata raises, pending rows stay pending."""
        store = MagicMock(spec=VectorStore)
        store.update_metadata.side_effect = RuntimeError("chroma down")
        cache = RoutingCache(store)
        cache._state.record_pending_update("id-1", "q", {"hit_count": "2"}, flush_interval=10_000)
        assert cache._state.has_pending()

        cache._flush_pending_updates()

        # Buffer must be re-populated so the next flush can retry. The
        # previous implementation dropped the pending rows on the
        # floor whenever Chroma raised, silently losing hit counts.
        assert cache._state.has_pending()
        restored = cache._state.snapshot_pending()
        assert "id-1" in restored


# ---------------------------------------------------------------------------
# P1-3: LRU pagination
# ---------------------------------------------------------------------------


class TestRoutingCacheLRUPagination:
    def test_enforce_lru_paginates_get_calls(self):
        """``_enforce_lru`` must issue paginated ``get`` calls, not one
        fat ``get`` that loads the whole collection into memory."""
        store = MagicMock(spec=VectorStore)
        cache = RoutingCache(store)
        cache._max_entries = 10
        store.count.return_value = 6000

        # Two pages: 5000 entries, then a 1000-entry tail that also
        # signals the end of pagination (len < PAGE_SIZE).
        ids_page1 = [f"id-{i}" for i in range(5000)]
        metas_page1 = [{"last_accessed": f"2025-01-{(i % 28) + 1:02d}T00:00:00"} for i in range(5000)]
        ids_page2 = [f"id-{i}" for i in range(5000, 6000)]
        metas_page2 = [{"last_accessed": f"2025-02-{(i % 28) + 1:02d}T00:00:00"} for i in range(1000)]
        store.get.side_effect = [
            {"ids": ids_page1, "metadatas": metas_page1},
            {"ids": ids_page2, "metadatas": metas_page2},
        ]

        cache._enforce_lru()

        # Assert that get() was called with a ``limit`` kwarg at least
        # twice -- this is the distinguishing signal between the old
        # "load everything" behaviour and the new paginated sweep.
        assert store.get.call_count == 2
        for call in store.get.call_args_list:
            assert "limit" in call.kwargs
            assert "offset" in call.kwargs
        # And deletions actually happened for the overage.
        assert store.delete.call_count >= 1


# ---------------------------------------------------------------------------
# P1-4: classify confidence gating
# ---------------------------------------------------------------------------


class TestClassifyNoConfidence:
    @pytest.mark.asyncio
    async def test_old_format_line_yields_none_confidence(self):
        from app.agents.orchestrator import OrchestratorAgent
        from app.models.agent import AgentCard

        orch = OrchestratorAgent(dispatcher=AsyncMock())
        orch._registry = AsyncMock()
        orch._registry.list_agents = AsyncMock(
            return_value=[
                AgentCard(agent_id="light-agent", name="", description="", skills=[]),
            ]
        )
        results = await orch._parse_classification("light-agent: turn on bedroom", "turn on bedroom")
        assert len(results) == 1
        assert results[0][0] == "light-agent"
        assert results[0][2] is None


def _make_action_cache() -> tuple[ActionCache, MagicMock]:
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
    store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    cache = ActionCache(store)
    cache._max_entries = 1000
    cache._eviction_interval = 1000
    return cache, store


class TestActionCacheReplayServiceData:
    def test_store_and_lookup_round_trips_service_data(self):
        cache, store = _make_action_cache()

        cached_action = CachedAction(
            service="light/turn_on",
            entity_id="light.bedroom",
            service_data={"brightness_pct": 30, "transition": 2},
        )
        entry = ActionCacheEntry(
            query_text="dim the bedroom lights",
            language="en",
            response_text="Done.",
            agent_id="light-agent",
            condensed_task="Dim the bedroom lights",
            confidence=0.97,
            cached_action=cached_action,
            entity_ids=["light.bedroom"],
        )
        cache.store(entry)

        store.upsert.assert_called_once()
        kwargs = store.upsert.call_args.kwargs
        meta = kwargs["metadatas"][0]
        restored = CachedAction.model_validate_json(meta["cached_action"])
        assert restored.service_data == {"brightness_pct": 30, "transition": 2}

        store.get.return_value = {
            "ids": [kwargs["ids"][0]],
            "documents": [entry.query_text],
            "metadatas": [meta],
        }
        looked_up, similarity = cache.lookup(entry.query_text, language="en")

        assert looked_up is not None
        assert similarity == pytest.approx(1.0)
        assert looked_up.cached_action.service_data == {
            "brightness_pct": 30,
            "transition": 2,
        }

    def test_invalidate_by_entity_id_accepts_multiple_targets(self):
        cache, store = _make_action_cache()

        with patch("app.cache._base_cache._LRU_PAGE_SIZE", 2):
            store.get.side_effect = [
                {
                    "ids": ["id-1", "id-2"],
                    "metadatas": [
                        {"entity_ids": '["light.kitchen"]'},
                        {"entity_ids": '["light.porch"]'},
                    ],
                },
                {
                    "ids": ["id-3", "id-4"],
                    "metadatas": [
                        {"entity_ids": '["switch.garage"]'},
                        {"entity_ids": '["light.kitchen", "switch.garage"]'},
                    ],
                },
                {"ids": [], "metadatas": []},
            ]

            deleted = cache.invalidate_by_entity_id(["light.kitchen", "switch.garage"])

        assert deleted == 3
        store.delete.assert_called_once_with(cache._collection_name, ids=["id-1", "id-3", "id-4"])


class TestStoreAfterDispatchWhitelist:
    @pytest.mark.asyncio
    async def test_non_whitelisted_keys_dropped(self):
        from app.agents.orchestrator import OrchestratorAgent

        orch = OrchestratorAgent.__new__(OrchestratorAgent)
        stored: list[ActionCacheEntry] = []

        async def fake_store(entry: ActionCacheEntry) -> None:
            stored.append(entry)

        orch._cache_manager = MagicMock()
        orch._cache_manager.store_action_async = AsyncMock(side_effect=fake_store)
        orch._legacy_pipeline_enabled = MagicMock(return_value=False)
        orch._get_bool_setting = AsyncMock(return_value=True)
        orch._cache_orchestrator = CacheOrchestrator(cache_manager=orch._cache_manager)
        orch._cache_orchestrator._get_bool_setting_impl = AsyncMock(return_value=True)

        action_executed = {
            "success": True,
            "action": "turn_on",
            "entity_id": "light.kitchen",
            "cacheable": True,
            "service_data": {
                "brightness_pct": 50,
                "transition": 2,
                "evil_key": "drop me",
                "__proto__": "drop me too",
            },
        }
        stored_action, stored_routing = await orch._store_after_dispatch(
            user_text="turn on kitchen",
            language="en",
            speech="Done.",
            target_agent="light-agent",
            condensed_task="Turn on kitchen light",
            confidence=0.97,
            action_executed=action_executed,
            has_error=False,
        )

        assert (stored_action, stored_routing) == (True, False)
        assert len(stored) == 1
        entry = stored[0]
        assert entry.cached_action is not None
        assert entry.cached_action.service_data == {
            "brightness_pct": 50,
            "transition": 2,
        }
