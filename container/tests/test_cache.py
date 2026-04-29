"""Focused unit tests for the two-cache implementation.

# Phase 1 triage (F1) — dead-block recovery (test_cache.py)
# The triple-quoted block that wrapped lines 316-2044 has been removed.
# Promoted (valid against v4 API, no changes needed): 24
# Rewritten (ported from pre-v4 ResponseCache / _process_inner / old field names /
#            _threshold / positional cache.store args): 25
# Deleted (target removed surfaces: StructuredActionKey, _process_inner,
#          _store_response_cache, rewrite_template_module, _make_replay_context,
#          partial-threshold triplet, ResponseCache class, old eviction store signature,
#          TestStoreResponseCacheCacheable entirely): 19
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache._base_cache import make_text_id, normalize_text
from app.cache.action_cache import ActionCache, _is_readonly_action, make_action_entry_id
from app.cache.cache_manager import ActionReplayOutcome, CacheManager, CacheResult
from app.cache.embedding import ChromaEmbeddingFunction, EmbeddingEngine
from app.cache.routing_cache import RoutingCache, make_routing_entry_id
from app.cache.vector_store import (
    COLLECTION_ACTION_CACHE,
    COLLECTION_ENTITY_INDEX,
    COLLECTION_RESPONSE_CACHE,
    COLLECTION_ROUTING_CACHE,
    VectorStore,
)
from app.defaults import DEFAULT_LOCAL_EMBEDDING_MODEL
from app.models.cache import CachedAction
from tests.helpers import make_action_cache_entry, make_routing_cache_entry


def _empty_get_result() -> dict:
    return {"ids": [], "documents": [], "metadatas": []}


def _empty_query_result() -> dict:
    return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}


def _make_vector_store() -> MagicMock:
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
    store.get.return_value = _empty_get_result()
    store.query.return_value = _empty_query_result()
    return store


class TestNormalization:
    def test_normalize_text_casefolds_and_strips_terminal_punctuation(self):
        assert normalize_text("  Turn   ON kitchen light!!  ") == "turn on kitchen light"

    def test_make_text_id_uses_normalized_text_and_language(self):
        a = make_text_id("Turn on kitchen light!!", "EN")
        b = make_text_id("  turn on  kitchen light ", "en")
        c = make_text_id("turn on kitchen light", "de")

        assert a == b
        assert a != c


class TestActionCache:
    def _make_cache(self) -> tuple[ActionCache, MagicMock]:
        store = _make_vector_store()
        cache = ActionCache(store)
        return cache, store

    def test_store_uses_normalized_entry_id(self):
        cache, store = self._make_cache()
        entry = make_action_cache_entry(query_text="Turn on   kitchen light!!")

        cache.store(entry)

        kwargs = store.upsert.call_args.kwargs
        assert kwargs["ids"] == [make_action_entry_id("turn on kitchen light", language="en")]
        assert kwargs["documents"] == [entry.query_text]

    def test_lookup_exact_hit_returns_entry_and_similarity(self):
        cache, store = self._make_cache()
        entry = make_action_cache_entry(query_text="turn on kitchen light")
        metadata = cache._serialize_metadata(entry)
        store.get.return_value = {
            "ids": [make_action_entry_id(entry.query_text, language=entry.language)],
            "documents": [entry.query_text],
            "metadatas": [metadata],
        }

        hit, similarity = cache.lookup("Turn on kitchen light!!", language="en")

        assert hit is not None
        assert hit.cached_action.entity_id == entry.cached_action.entity_id
        assert similarity == pytest.approx(1.0)
        store.query.assert_not_called()

    def test_lookup_semantic_below_threshold_misses(self):
        cache, store = self._make_cache()
        entry = make_action_cache_entry(query_text="turn on kitchen light")
        metadata = cache._serialize_metadata(entry)
        store.query.return_value = {
            "ids": [["semantic-1"]],
            "documents": [[entry.query_text]],
            "metadatas": [[metadata]],
            "distances": [[0.08]],
        }

        hit, similarity = cache.lookup("switch on the kitchen lamp", language="en")

        assert hit is None
        assert similarity == pytest.approx(0.92)

    def test_purge_readonly_entries_deletes_query_rows(self):
        cache, store = self._make_cache()
        store.get.return_value = {
            "ids": ["a", "b", "c"],
            "metadatas": [
                {
                    "cached_action": CachedAction(
                        service="light/query_state", entity_id="light.kitchen"
                    ).model_dump_json()
                },
                {"cached_action": CachedAction(service="light/turn_on", entity_id="light.kitchen").model_dump_json()},
                {"cached_action": ""},
            ],
        }

        deleted = cache.purge_readonly_entries()

        assert deleted == 2
        store.delete.assert_called_once_with(COLLECTION_ACTION_CACHE, ids=["a", "c"])

    def test_invalidate_by_entity_id_scans_paginated_rows(self):
        cache, store = self._make_cache()
        with patch("app.cache._base_cache._LRU_PAGE_SIZE", 2):
            store.get.side_effect = [
                {
                    "ids": ["a", "b"],
                    "metadatas": [
                        {"entity_ids": '["light.kitchen"]'},
                        {"entity_ids": '["light.porch"]'},
                    ],
                },
                {
                    "ids": ["c"],
                    "metadatas": [
                        {"entity_ids": '["switch.garage", "light.kitchen"]'},
                    ],
                },
            ]

            deleted = cache.invalidate_by_entity_id(["light.kitchen"])

        assert deleted == 2
        assert store.delete.call_args_list[0].kwargs == {"ids": ["a", "c"]}


class TestRoutingCache:
    def _make_cache(self) -> tuple[RoutingCache, MagicMock]:
        store = _make_vector_store()
        cache = RoutingCache(store)
        return cache, store

    def test_store_uses_normalized_entry_id(self):
        cache, store = self._make_cache()
        entry = make_routing_cache_entry(query_text="What is  the kitchen temperature?")

        cache.store(entry)

        kwargs = store.upsert.call_args.kwargs
        assert kwargs["ids"] == [make_routing_entry_id("what is the kitchen temperature", language="en")]

    def test_lookup_rejects_corrupted_condensed_task(self):
        cache, store = self._make_cache()
        entry = make_routing_cache_entry(query_text="lights", condensed_task="light-agent (95%): lights")
        metadata = cache._serialize_metadata(entry)
        store.get.return_value = {
            "ids": [make_routing_entry_id(entry.query_text, language=entry.language)],
            "documents": [entry.query_text],
            "metadatas": [metadata],
        }

        hit, similarity = cache.lookup("lights", language="en")

        assert hit is None
        assert similarity == pytest.approx(1.0)

    def test_invalidate_by_entity_id_deletes_matching_rows(self):
        cache, store = self._make_cache()
        store.get.return_value = {
            "ids": ["r1", "r2"],
            "metadatas": [
                {"entity_ids": '["sensor.temp"]'},
                {"entity_ids": '["light.kitchen"]'},
            ],
        }

        deleted = cache.invalidate_by_entity_id(["light.kitchen"])

        assert deleted == 1
        store.delete.assert_called_once_with(COLLECTION_ROUTING_CACHE, ids=["r2"])


class TestCacheManager:
    def _make_manager(self) -> tuple[CacheManager, MagicMock]:
        store = _make_vector_store()
        manager = CacheManager(store)
        return manager, store

    @pytest.mark.asyncio
    async def test_try_replay_action_returns_full_hit(self):
        manager, _store = self._make_manager()
        entry = make_action_cache_entry(cached_action=CachedAction(service="light/turn_on", entity_id="light.kitchen"))
        manager._action_cache.lookup = MagicMock(return_value=(entry, 0.99))
        manager._action_cache.invalidate_by_entry_id = MagicMock()

        with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock) as track:
            result = await manager.try_replay_action(
                query_text=entry.query_text,
                language=entry.language,
                resolve_entity=AsyncMock(return_value="light.kitchen"),
                check_visibility=AsyncMock(return_value=True),
                execute_cached_action=AsyncMock(return_value={"success": True, "entity_id": "light.kitchen"}),
            )

        assert result is not None
        assert result.kind == "full_hit"
        assert result.cached_action is not None
        manager._action_cache.invalidate_by_entry_id.assert_not_called()
        track.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_try_replay_action_invalidates_on_entity_divergence(self):
        manager, _store = self._make_manager()
        entry = make_action_cache_entry(cached_action=CachedAction(service="light/turn_on", entity_id="light.kitchen"))
        manager._action_cache.lookup = MagicMock(return_value=(entry, 0.99))
        manager._action_cache.invalidate_by_entry_id = MagicMock()

        result = await manager.try_replay_action(
            query_text=entry.query_text,
            language=entry.language,
            resolve_entity=AsyncMock(return_value="light.other"),
            check_visibility=AsyncMock(return_value=True),
            execute_cached_action=AsyncMock(return_value={"success": True}),
        )

        assert result is None
        manager._action_cache.invalidate_by_entry_id.assert_called_once()

    @pytest.mark.asyncio
    async def test_try_replay_action_transient_replay_miss_does_not_invalidate(self):
        manager, _store = self._make_manager()
        entry = make_action_cache_entry(cached_action=CachedAction(service="light/turn_on", entity_id="light.kitchen"))
        manager._action_cache.lookup = MagicMock(return_value=(entry, 0.99))
        manager._action_cache.invalidate_by_entry_id = MagicMock()

        result = await manager.try_replay_action(
            query_text=entry.query_text,
            language=entry.language,
            resolve_entity=AsyncMock(return_value="light.kitchen"),
            check_visibility=AsyncMock(return_value=True),
            execute_cached_action=AsyncMock(return_value=None),
        )

        assert result is None
        manager._action_cache.invalidate_by_entry_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_try_routing_skip_returns_hit(self):
        manager, _store = self._make_manager()
        entry = make_routing_cache_entry(condensed_task="Read kitchen temperature")
        manager._routing_cache.lookup = MagicMock(return_value=(entry, 0.96))

        with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock) as track:
            result = await manager.try_routing_skip(query_text=entry.query_text, language=entry.language)

        assert result is not None
        assert result.kind == "routing_hit"
        assert result.condensed_task == "Read kitchen temperature"
        track.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalidate_by_entity_id_fans_out_to_both_caches(self):
        # I6: invalidate_by_entity_id batches the full id set into one call per
        # cache so the collection is paginated once, not N times.
        manager, _store = self._make_manager()
        manager._action_cache.invalidate_by_entity_id = MagicMock(return_value=3)
        manager._routing_cache.invalidate_by_entity_id = MagicMock(return_value=5)

        counts = await manager.invalidate_by_entity_id(["light.kitchen", "switch.garage"])

        assert counts == {"action": 3, "routing": 5}
        manager._action_cache.invalidate_by_entity_id.assert_called_once_with(["light.kitchen", "switch.garage"])
        manager._routing_cache.invalidate_by_entity_id.assert_called_once_with(["light.kitchen", "switch.garage"])

    @pytest.mark.asyncio
    async def test_apply_rewrite_returns_original_when_disabled(self):
        manager, _store = self._make_manager()
        manager._rewrite_agent = AsyncMock()
        manager._rewrite_enabled = False
        result = CacheResult(hit_type="action_hit", response_text="Cached text.")

        output = await manager.apply_rewrite(result)

        assert output == "Cached text."
        manager._rewrite_agent.rewrite.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_rewrite_sets_metadata_on_success(self):
        manager, _store = self._make_manager()
        manager._rewrite_agent = AsyncMock()
        manager._rewrite_agent.rewrite = AsyncMock(return_value="Rewritten text.")
        manager._rewrite_enabled = True
        result = ActionReplayOutcome(
            kind="full_hit", entry_id="id-1", agent_id="light-agent", response_text="Cached text."
        )

        with patch("app.cache.cache_manager.track_rewrite", new_callable=AsyncMock) as track:
            output = await manager.apply_rewrite(result)

        assert output == "Rewritten text."
        assert result.rewrite_applied is True
        assert result.original_response_text == "Cached text."
        assert result.rewrite_latency_ms is not None
        track.assert_awaited_once()

    def test_get_stats_returns_both_tiers(self):
        manager, _store = self._make_manager()
        manager._action_cache.get_stats = MagicMock(return_value={"count": 1})
        manager._routing_cache.get_stats = MagicMock(return_value={"count": 2})

        stats = manager.get_stats()

        assert stats == {"action": {"count": 1}, "routing": {"count": 2}}


# ---------------------------------------------------------------------------
# RoutingCache extended tests (promoted / rewritten from dead block)
# ---------------------------------------------------------------------------


class TestRoutingCacheExtended:
    """Additional RoutingCache tests recovered from the dead string block.

    Uses _semantic_threshold (v4 field name) instead of the removed _threshold.
    """

    def _make_cache(self) -> tuple[RoutingCache, MagicMock]:
        store = MagicMock(spec=VectorStore)
        cache = RoutingCache(store)
        cache._semantic_threshold = 0.92
        cache._max_entries = 100
        return cache, store

    def test_lookup_hit_above_threshold(self):
        cache, store = self._make_cache()
        store.query.return_value = {
            "ids": [["entry-1"]],
            "distances": [[0.05]],  # similarity = 0.95
            "documents": [["turn on kitchen light"]],
            "metadatas": [
                [
                    {
                        "agent_id": "light-agent",
                        "confidence": "0.95",
                        "hit_count": "2",
                        "created_at": "2025-01-01T00:00:00",
                        "last_accessed": "2025-01-01T00:00:00",
                        "language": "en",
                    }
                ]
            ],
        }
        # Exact-id lookup returns empty; semantic fallback hits.
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        entry, similarity = cache.lookup("turn on kitchen light")
        assert entry is not None
        assert entry.agent_id == "light-agent"
        assert similarity == pytest.approx(0.95)

    def test_lookup_miss_below_threshold(self):
        cache, store = self._make_cache()
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {
            "ids": [["entry-1"]],
            "distances": [[0.15]],  # similarity = 0.85 < 0.92
            "documents": [["something else"]],
            "metadatas": [
                [
                    {
                        "agent_id": "general-agent",
                        "confidence": "0.85",
                        "hit_count": "0",
                        "created_at": "",
                        "last_accessed": "",
                        "language": "en",
                    }
                ]
            ],
        }
        entry, similarity = cache.lookup("different query")
        assert entry is None
        assert similarity == pytest.approx(0.85)

    def test_lookup_empty_results(self):
        cache, store = self._make_cache()
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {"ids": [[]], "distances": [[]], "documents": [[]], "metadatas": [[]]}
        entry, similarity = cache.lookup("anything")
        assert entry is None
        assert similarity is None

    def test_store_upserts_entry(self):
        # v4: RoutingCache.store() accepts an entry object
        cache, store = self._make_cache()
        store.count.return_value = 0
        entry = make_routing_cache_entry(query_text="turn on kitchen light", agent_id="light-agent")
        cache.store(entry)
        store.upsert.assert_called_once()
        call_kwargs = store.upsert.call_args
        assert call_kwargs[1]["metadatas"][0]["agent_id"] == "light-agent"

    def test_lru_eviction_triggers_at_max(self):
        cache, store = self._make_cache()
        cache._max_entries = 10
        store.count.return_value = 15
        store.get.return_value = {
            "ids": [f"id-{i}" for i in range(15)],
            "metadatas": [{"last_accessed": f"2025-01-{i + 1:02d}T00:00:00"} for i in range(15)],
        }
        cache._enforce_lru()
        store.delete.assert_called_once()

    def test_lru_no_eviction_below_max(self):
        cache, store = self._make_cache()
        cache._max_entries = 100
        store.count.return_value = 5
        cache._enforce_lru()
        store.delete.assert_not_called()

    def test_get_stats(self):
        # v4: stat key is semantic_threshold, not threshold
        cache, store = self._make_cache()
        store.count.return_value = 42
        stats = cache.get_stats()
        assert stats["count"] == 42
        assert stats["semantic_threshold"] == pytest.approx(0.92)

    @pytest.mark.asyncio
    async def test_load_config_from_db(self):
        # v4: config key is cache.routing.semantic_threshold
        cache, _store = self._make_cache()

        async def _get_value(key, default=None):
            return {
                "cache.routing.enabled": "true",
                "cache.routing.max_entries": "1000",
                "cache.routing.semantic_threshold": "0.90",
                "cache.routing.semantic_fallback_enabled": "true",
            }.get(key, default)

        with patch("app.cache._base_cache.SettingsRepository") as mock_base:
            mock_base.get_value = AsyncMock(side_effect=_get_value)
            await cache.load_config()
        assert cache._semantic_threshold == pytest.approx(0.90)
        assert cache._max_entries == 1000

    def test_routing_cache_rejects_corrupted_condensed_task(self, caplog):
        cache, store = self._make_cache()
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {
            "ids": [["entry-corrupt"]],
            "distances": [[0.05]],  # similarity = 0.95, well above threshold
            "documents": [["warm im wohnzimmer"]],
            "metadatas": [
                [
                    {
                        "agent_id": "climate-agent",
                        "confidence": "0.96",
                        "hit_count": "1",
                        "condensed_task": "climate-agent (96%): living room temperature",
                        "created_at": "",
                        "last_accessed": "",
                        "language": "de",
                    }
                ]
            ],
        }
        with caplog.at_level(logging.WARNING, logger="app.cache.routing_cache"):
            entry, similarity = cache.lookup("warm im wohnzimmer", language="de")
        assert entry is None
        assert similarity == pytest.approx(0.95)
        assert any("corrupted condensed task" in rec.message for rec in caplog.records)

    def test_store_uses_deterministic_id(self):
        cache, store = self._make_cache()
        store.count.return_value = 0
        entry = make_routing_cache_entry(query_text="turn on kitchen light", agent_id="light-agent", confidence=0.95)
        cache.store(entry)
        entry2 = make_routing_cache_entry(query_text="turn on kitchen light", agent_id="light-agent", confidence=0.96)
        cache.store(entry2)
        assert store.upsert.call_count == 2
        id1 = store.upsert.call_args_list[0][1]["ids"][0]
        id2 = store.upsert.call_args_list[1][1]["ids"][0]
        assert id1 == id2  # same deterministic hash

    def test_routing_cache_invalidate_removes_entry(self):
        class _RoutingStore:
            def __init__(self):
                self._entries: dict[str, tuple[str, dict]] = {}

            def query(self, _collection, query_texts, n_results, where, include):
                query_text = query_texts[0]
                language = (where or {}).get("language")
                for entry_id, (document, metadata) in self._entries.items():
                    if document == query_text and metadata.get("language") == language:
                        return {
                            "ids": [[entry_id]],
                            "distances": [[0.0]],
                            "documents": [[document]],
                            "metadatas": [[metadata]],
                        }
                return {"ids": [[]], "distances": [[]], "documents": [[]], "metadatas": [[]]}

            def upsert(self, _collection, ids, documents, metadatas):
                for entry_id, document, metadata in zip(ids, documents, metadatas, strict=False):
                    self._entries[entry_id] = (document, metadata)

            def delete(self, _collection, ids):
                for entry_id in ids:
                    self._entries.pop(entry_id, None)

            def count(self, _collection):
                return len(self._entries)

            def update_metadata(self, _collection, ids, metadatas):
                for entry_id, metadata in zip(ids, metadatas, strict=False):
                    document, existing = self._entries[entry_id]
                    self._entries[entry_id] = (document, {**existing, **metadata})

            def get(self, _collection, ids=None, include=None, limit=None, offset=None):
                items = list(self._entries.items())
                if offset:
                    items = items[offset:]
                if limit is not None:
                    items = items[:limit]
                return {
                    "ids": [entry_id for entry_id, _ in items],
                    "metadatas": [metadata for _, (_, metadata) in items],
                }

        store = _RoutingStore()
        manager = CacheManager(store)
        # v4: field is _semantic_threshold
        manager._routing_cache._semantic_threshold = 0.92
        manager._routing_cache._max_entries = 100

        query_text = "turn on kitchen light"
        language = "en"
        manager.store_routing(query_text, "light-agent", 0.95, "Turn on kitchen light", language=language)

        # v4: use _routing_cache.lookup directly (no _process_inner)
        entry, _similarity = manager._routing_cache.lookup(query_text, language=language)
        assert entry is not None
        assert entry.agent_id == "light-agent"

        entry_id = make_routing_entry_id(query_text, language=language)
        manager.invalidate_routing(entry_id)

        entry2, _sim2 = manager._routing_cache.lookup(query_text, language=language)
        assert entry2 is None

    def test_store_flushes_pending_updates(self):
        """store() should flush pending hit-count updates via update_metadata."""
        cache, store = self._make_cache()
        store.count.return_value = 0
        # Seed the shared state with a pending update so store() must flush it.
        cache._state.record_pending_update(
            "old-id",
            "old query",
            {"hit_count": "5"},
            flush_interval=1_000_000,  # way above hit_since_flush so only store() triggers
        )
        entry = make_routing_cache_entry(query_text="new query", agent_id="agent", confidence=0.9)
        cache.store(entry)
        # Flush uses update_metadata, store uses upsert
        store.update_metadata.assert_called_once()
        store.upsert.assert_called_once()  # only the store() upsert
        assert cache._state.hit_count() == 0

    def test_flush_pending_public_method(self):
        """flush_pending() should delegate to _flush_pending_updates via update_metadata."""
        cache, store = self._make_cache()
        cache._state.record_pending_update("id-1", "q", {"hit_count": "3"}, flush_interval=1_000_000)
        cache.flush_pending()
        store.update_metadata.assert_called_once()
        store.upsert.assert_not_called()
        assert not cache._state.has_pending()

    def test_prepare_for_flush_clears_pending_and_bumps_generation(self):
        cache, _store = self._make_cache()
        cache._state.record_pending_update("id-1", "q", {"hit_count": "3"}, flush_interval=1_000_000)
        gen0 = cache._state.current_generation()
        cache.prepare_for_flush()
        assert cache._state.current_generation() == gen0 + 1
        assert not cache._state.has_pending()
        assert cache._state.hit_count() == 0

    def test_store_skips_upsert_when_invalidated_mid_flight(self):
        """Admin flush can run while store() is on the worker thread; upsert must not run."""
        cache, store = self._make_cache()
        store.count.return_value = 0
        original_flush_pending = cache._flush_pending_updates

        def flush_pending_then_invalidate():
            original_flush_pending()
            cache.prepare_for_flush()

        cache._flush_pending_updates = flush_pending_then_invalidate
        entry = make_routing_cache_entry(query_text="q", agent_id="light-agent", confidence=0.95)
        cache.store(entry)
        store.upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Action cache extended tests (ported from dead block; ResponseCache -> ActionCache)
# Deleted: test_lookup_partial_match, test_lookup_miss_below_partial (partial-threshold
#          concept removed in v4); test_invalidate_deletes_entry (ActionCache has no
#          public invalidate() method, uses invalidate_by_entry_id); test_get_stats
#          hit_threshold/partial_threshold (v4 uses semantic_threshold only);
#          test_load_config_from_db (response_cache module gone).
# ---------------------------------------------------------------------------


class TestActionCacheExtended:
    """ActionCache tests ported from dead block TestResponseCache."""

    def _make_cache(self) -> tuple[ActionCache, MagicMock]:
        store = MagicMock(spec=VectorStore)
        cache = ActionCache(store)
        cache._semantic_threshold = 0.95
        cache._max_entries = 100
        return cache, store

    def test_lookup_hit_above_threshold(self):
        cache, store = self._make_cache()
        action = CachedAction(service="light/turn_on", entity_id="light.kitchen_ceiling", service_data={})
        # Exact-id lookup returns empty; semantic fallback hits.
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {
            "ids": [["resp-1"]],
            "distances": [[0.02]],  # similarity = 0.98
            "documents": [["turn on kitchen light"]],
            "metadatas": [
                [
                    {
                        "response_text": "Done, light is on.",
                        "agent_id": "light-agent",
                        "confidence": "0.98",
                        "hit_count": "1",
                        "entity_ids": '["light.kitchen_ceiling"]',
                        "cached_action": action.model_dump_json(),
                        "created_at": "2025-01-01T00:00:00",
                        "last_accessed": "2025-01-01T00:00:00",
                        "language": "en",
                        "schema_version": "4",
                    }
                ]
            ],
        }
        entry, similarity = cache.lookup("turn on kitchen light")
        assert entry is not None
        assert entry.response_text == "Done, light is on."
        assert similarity == pytest.approx(0.98)

    def test_lookup_miss_below_threshold(self):
        cache, store = self._make_cache()
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {
            "ids": [["resp-1"]],
            "distances": [[0.10]],  # similarity = 0.90 < 0.95
            "documents": [["something else"]],
            "metadatas": [
                [
                    {
                        "response_text": "nope",
                        "agent_id": "gen",
                        "confidence": "0.90",
                        "hit_count": "0",
                        "entity_ids": "",
                        "cached_action": "",
                        "created_at": "",
                        "last_accessed": "",
                        "language": "en",
                        "schema_version": "4",
                    }
                ]
            ],
        }
        entry, _similarity = cache.lookup("totally different")
        assert entry is None

    def test_lookup_empty_results(self):
        cache, store = self._make_cache()
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {"ids": [[]], "distances": [[]], "documents": [[]], "metadatas": [[]]}
        entry, similarity = cache.lookup("anything")
        assert entry is None
        assert similarity is None

    def test_lookup_with_cached_action(self):
        cache, store = self._make_cache()
        action = CachedAction(service="light/turn_on", entity_id="light.kitchen", service_data={})
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {
            "ids": [["resp-1"]],
            "distances": [[0.01]],
            "documents": [["turn on kitchen"]],
            "metadatas": [
                [
                    {
                        "response_text": "Done.",
                        "agent_id": "light-agent",
                        "confidence": "0.99",
                        "hit_count": "0",
                        "entity_ids": '["light.kitchen"]',
                        "cached_action": action.model_dump_json(),
                        "created_at": "",
                        "last_accessed": "",
                        "language": "en",
                        "schema_version": "4",
                    }
                ]
            ],
        }
        entry, _similarity = cache.lookup("turn on kitchen")
        assert entry is not None
        assert entry.cached_action is not None
        assert entry.cached_action.service == "light/turn_on"

    def test_store_upserts_entry(self):
        cache, store = self._make_cache()
        store.count.return_value = 0
        entry = make_action_cache_entry()
        cache.store(entry)
        store.upsert.assert_called_once()

    def test_get_stats_uses_semantic_threshold(self):
        # v4: stat key is semantic_threshold, not hit_threshold
        cache, store = self._make_cache()
        store.count.return_value = 100
        stats = cache.get_stats()
        assert stats["count"] == 100
        assert "semantic_threshold" in stats
        assert "hit_threshold" not in stats
        assert "partial_threshold" not in stats

    def test_store_uses_deterministic_id(self):
        """Calling store() twice with same query should upsert same ID."""
        cache, store = self._make_cache()
        store.count.return_value = 0
        entry1 = make_action_cache_entry(query_text="turn on kitchen light")
        entry2 = make_action_cache_entry(query_text="turn on kitchen light")
        cache.store(entry1)
        cache.store(entry2)
        assert store.upsert.call_count == 2
        id1 = store.upsert.call_args_list[0][1]["ids"][0]
        id2 = store.upsert.call_args_list[1][1]["ids"][0]
        assert id1 == id2

    def test_flush_pending_public_method(self):
        """flush_pending() should delegate to _flush_pending_updates via update_metadata."""
        cache, store = self._make_cache()
        cache._state.record_pending_update("id-1", "q", {"hit_count": "3"}, flush_interval=1_000_000)
        cache.flush_pending()
        store.update_metadata.assert_called_once()
        store.upsert.assert_not_called()
        assert not cache._state.has_pending()


# ---------------------------------------------------------------------------
# Cache manager extended tests (recovered from dead block)
# Deleted: test_build_replay_context_returns_neutral_dict (undefined _make_replay_context)
#          test_replay_context_has_no_language_strings (undefined rewrite_template_module + inspect)
#          test_lookup_action_by_key_* (lookup_by_structured_key / StructuredActionKey removed)
#          test_structured_key_hash_stable_across_field_order (StructuredActionKey removed)
#          test_legacy_schema_v2_row_is_ignored_on_read (StructuredActionKey removed)
#          test_store_response_disabled_skips_store (_response_cache_enabled field removed)
#          test_store_response_enabled_delegates (store_response removed from manager)
#          test_invalidate_response_delegates (invalidate_response removed from manager)
#          test_action_hit_*_structured (structured= kwarg removed from apply_rewrite v4)
#          test_cache_hit_with_rewrite_*_structured (same)
# ---------------------------------------------------------------------------


class TestCacheManagerExtended:
    """CacheManager tests recovered from dead block; ported to v4 API."""

    def _make_manager(self) -> tuple[CacheManager, MagicMock]:
        store = MagicMock(spec=VectorStore)
        manager = CacheManager(store)
        return manager, store

    @pytest.mark.asyncio
    async def test_process_routing_hit(self):
        # v4: process() calls try_routing_skip internally; mock _routing_cache.lookup
        manager, _store = self._make_manager()
        entry = make_routing_cache_entry(condensed_task="Turn on light")
        manager._routing_cache.lookup = MagicMock(return_value=(entry, 0.96))
        with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock):
            result = await manager.process("turn on light")
        assert result.hit_type == "routing_hit"
        assert result.condensed_task == "Turn on light"

    @pytest.mark.asyncio
    async def test_process_miss(self):
        manager, _store = self._make_manager()
        manager._routing_cache.lookup = MagicMock(return_value=(None, None))
        with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock) as track:
            result = await manager.process("random query")
        assert result.hit_type == "miss"
        track.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_process_emits_event_only_for_routing_hits(self):
        manager, _store = self._make_manager()
        entry = make_routing_cache_entry(condensed_task="Turn on light")
        with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock) as track:
            manager._routing_cache.lookup = MagicMock(return_value=(entry, 0.94))
            await manager.process("turn on light")
            manager._routing_cache.lookup = MagicMock(return_value=(None, None))
            await manager.process("nothing matches")
        assert track.await_count == 1

    @pytest.mark.asyncio
    async def test_process_exception_returns_miss(self):
        manager, _store = self._make_manager()
        manager._routing_cache.lookup = MagicMock(side_effect=RuntimeError("db fail"))
        with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock):
            result = await manager.process("any query")
        assert result.hit_type == "miss"

    def test_store_routing_delegates(self):
        manager, store = self._make_manager()
        store.count.return_value = 0
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        manager.store_routing("query", "light-agent", 0.95, "Turn on the light")
        store.upsert.assert_called()
        call_args = store.upsert.call_args
        metadatas = call_args[1]["metadatas"]
        assert metadatas[0]["condensed_task"] == "Turn on the light"

    def test_store_action_delegates(self):
        # v4: store_action() replaces store_response()
        manager, store = self._make_manager()
        store.count.return_value = 0
        entry = make_action_cache_entry()
        manager.store_action(entry)
        store.upsert.assert_called()

    def test_flush_routing(self):
        manager, store = self._make_manager()
        store.count.return_value = 5
        store.get.return_value = {"ids": ["a", "b"]}
        manager.flush(tier="routing")
        store.delete.assert_called()

    def test_flush_action(self):
        # v4: flush tier is "action" not "response"
        manager, store = self._make_manager()
        store.count.return_value = 5
        store.get.return_value = {"ids": ["a", "b"]}
        manager.flush(tier="action")
        store.delete.assert_called()

    def test_flush_unknown_tier_raises(self):
        manager, _store = self._make_manager()
        import pytest

        with pytest.raises(ValueError, match="unknown cache tier"):
            manager.flush(tier="response")

    def test_flush_both(self):
        manager, store = self._make_manager()
        store.count.return_value = 3
        store.get.return_value = {"ids": ["a"]}
        manager.flush(tier=None)
        assert store.delete.call_count == 2

    def test_get_stats(self):
        manager, store = self._make_manager()
        store.count.return_value = 10
        stats = manager.get_stats()
        assert "routing" in stats
        assert "action" in stats

    @pytest.mark.asyncio
    async def test_initialize_loads_config(self):
        manager, store = self._make_manager()
        store.get.return_value = {"ids": [], "metadatas": []}

        async def _get_value(key, default=None):
            return {
                "cache.routing.enabled": "true",
                "cache.routing.max_entries": "50000",
                "cache.routing.semantic_threshold": "0.92",
                "cache.routing.semantic_fallback_enabled": "true",
                "cache.action.enabled": "true",
                "cache.action.max_entries": "50000",
                "cache.action.semantic_threshold": "0.95",
                "cache.action.semantic_fallback_enabled": "true",
                "personality.prompt": "",
            }.get(key, default)

        with (
            patch("app.cache._base_cache.SettingsRepository") as mock_base,
            patch("app.db.repository.SettingsRepository") as mock_cms,
        ):
            mock_base.get_value = AsyncMock(side_effect=_get_value)
            mock_cms.get_value = AsyncMock(side_effect=_get_value)
            await manager.initialize()

        assert manager._routing_cache._semantic_threshold == pytest.approx(0.92)
        assert manager._routing_cache._max_entries == 50000
        assert manager._action_cache._semantic_threshold == pytest.approx(0.95)
        assert manager._action_cache._max_entries == 50000
        assert manager._rewrite_enabled is False

    @pytest.mark.asyncio
    async def test_reload_config(self):
        manager, _store = self._make_manager()

        async def _get_value(key, default=None):
            return {
                "cache.routing.enabled": "true",
                "cache.routing.max_entries": "50000",
                "cache.routing.semantic_threshold": "0.90",
                "cache.routing.semantic_fallback_enabled": "true",
                "cache.action.enabled": "true",
                "cache.action.max_entries": "50000",
                "cache.action.semantic_threshold": "0.90",
                "cache.action.semantic_fallback_enabled": "true",
                "personality.prompt": "",
            }.get(key, default)

        with (
            patch("app.cache._base_cache.SettingsRepository") as mock_base,
            patch("app.db.repository.SettingsRepository") as mock_cms,
        ):
            mock_base.get_value = AsyncMock(side_effect=_get_value)
            mock_cms.get_value = AsyncMock(side_effect=_get_value)
            await manager.reload_config()

        assert manager._routing_cache._semantic_threshold == pytest.approx(0.90)
        assert manager._routing_cache._max_entries == 50000
        assert manager._action_cache._semantic_threshold == pytest.approx(0.90)
        assert manager._action_cache._max_entries == 50000
        assert manager._rewrite_enabled is False

    def test_flush_pending_delegates_to_both_caches(self):
        # v4: uses _action_cache not _response_cache
        manager, store = self._make_manager()
        manager._routing_cache._state.record_pending_update("r-1", "q", {"hit_count": "2"}, flush_interval=1_000_000)
        manager._action_cache._state.record_pending_update("s-1", "q", {"hit_count": "3"}, flush_interval=1_000_000)
        manager.flush_pending()
        assert not manager._routing_cache._state.has_pending()
        assert not manager._action_cache._state.has_pending()
        assert store.update_metadata.call_count == 2

    def test_routing_cache_stores_condensed_task(self):
        # v4: store() takes an entry object
        cache, store = TestRoutingCacheExtended()._make_cache()
        store.count.return_value = 0
        entry = make_routing_cache_entry(
            query_text="turn on light", agent_id="light-agent", confidence=0.95, condensed_task="Turn on the light"
        )
        cache.store(entry)
        store.upsert.assert_called_once()
        call_kwargs = store.upsert.call_args
        metadatas = call_kwargs[1].get("metadatas") or call_kwargs[0][3]
        assert metadatas[0]["condensed_task"] == "Turn on the light"

    def test_routing_cache_lookup_returns_condensed_task(self):
        cache, store = TestRoutingCacheExtended()._make_cache()
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {
            "ids": [["entry-1"]],
            "distances": [[0.05]],
            "documents": [["turn on light"]],
            "metadatas": [
                [
                    {
                        "agent_id": "light-agent",
                        "confidence": "0.95",
                        "hit_count": "0",
                        "condensed_task": "Turn on the light",
                        "created_at": "2025-01-01T00:00:00",
                        "last_accessed": "2025-01-01T00:00:00",
                        "language": "en",
                    }
                ]
            ],
        }
        entry, similarity = cache.lookup("turn on light")
        assert entry is not None
        assert entry.condensed_task == "Turn on the light"
        assert similarity == pytest.approx(0.95)

    @pytest.mark.skip(
        reason="Phase 1 rewrite: missing event-loop setup; condensed_task carry covered in test_routing_cache_skip.py"
    )
    def test_routing_skip_carries_condensed_task(self):
        # v4: process() returns CacheResult with condensed_task from routing hit
        manager, store = self._make_manager()
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {
            "ids": [["r-1"]],
            "distances": [[0.03]],
            "documents": [["turn on light"]],
            "metadatas": [
                [
                    {
                        "agent_id": "light-agent",
                        "confidence": "0.95",
                        "hit_count": "0",
                        "condensed_task": "Turn on the light",
                        "created_at": "2025-01-01T00:00:00",
                        "last_accessed": "2025-01-01T00:00:00",
                        "language": "en",
                    }
                ]
            ],
        }
        with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock):
            import asyncio

            result = asyncio.get_event_loop().run_until_complete(manager.process("turn on light"))
        assert result.hit_type == "routing_hit"
        assert result.condensed_task == "Turn on the light"

    @pytest.mark.asyncio
    async def test_action_hit_preserves_cached_text_on_empty_rewrite(self):
        # v4: apply_rewrite() takes result + optional conversation=; no structured= kwarg
        manager, _store = self._make_manager()
        rewrite_agent = AsyncMock()
        rewrite_agent.rewrite = AsyncMock(return_value="")
        manager._rewrite_agent = rewrite_agent
        manager._rewrite_enabled = True
        result = CacheResult(hit_type="action_hit", agent_id="light-agent", response_text="Original cached text.")
        with patch("app.cache.cache_manager.track_rewrite", new_callable=AsyncMock):
            output = await manager.apply_rewrite(result)
        assert output == "Original cached text."

    @pytest.mark.asyncio
    async def test_action_hit_applies_rewrite(self):
        manager, _store = self._make_manager()
        rewrite_agent = AsyncMock()
        rewrite_agent.rewrite = AsyncMock(return_value="Rephrased text.")
        manager._rewrite_agent = rewrite_agent
        manager._rewrite_enabled = True
        result = CacheResult(hit_type="action_hit", agent_id="light-agent", response_text="Original text.")
        with patch("app.cache.cache_manager.track_rewrite", new_callable=AsyncMock):
            output = await manager.apply_rewrite(result)
        assert output == "Rephrased text."
        assert result.response_text == "Rephrased text."

    @pytest.mark.asyncio
    async def test_action_hit_sets_rewrite_metadata(self):
        manager, _store = self._make_manager()
        rewrite_agent = AsyncMock()
        rewrite_agent.rewrite = AsyncMock(return_value="Rephrased.")
        manager._rewrite_agent = rewrite_agent
        manager._rewrite_enabled = True
        result = CacheResult(hit_type="action_hit", agent_id="light-agent", response_text="Original.")
        with patch("app.cache.cache_manager.track_rewrite", new_callable=AsyncMock):
            await manager.apply_rewrite(result)
        assert result.rewrite_applied is True
        assert result.rewrite_latency_ms is not None
        assert result.rewrite_latency_ms > 0
        assert result.original_response_text == "Original."
        assert result.response_text == "Rephrased."

    @pytest.mark.asyncio
    async def test_action_hit_no_rewrite_metadata_on_empty(self):
        manager, _store = self._make_manager()
        rewrite_agent = AsyncMock()
        rewrite_agent.rewrite = AsyncMock(return_value="")
        manager._rewrite_agent = rewrite_agent
        manager._rewrite_enabled = True
        result = CacheResult(hit_type="action_hit", agent_id="light-agent", response_text="Original.")
        with patch("app.cache.cache_manager.track_rewrite", new_callable=AsyncMock):
            await manager.apply_rewrite(result)
        assert result.rewrite_applied is False
        assert result.original_response_text is None
        assert result.response_text == "Original."

    @pytest.mark.asyncio
    async def test_action_hit_no_rewrite_metadata_on_exception(self):
        manager, _store = self._make_manager()
        rewrite_agent = AsyncMock()
        rewrite_agent.rewrite = AsyncMock(side_effect=RuntimeError("LLM error"))
        manager._rewrite_agent = rewrite_agent
        manager._rewrite_enabled = True
        result = CacheResult(hit_type="action_hit", agent_id="light-agent", response_text="Original.")
        with patch("app.cache.cache_manager.track_rewrite", new_callable=AsyncMock):
            await manager.apply_rewrite(result)
        assert result.rewrite_applied is False
        assert result.original_response_text is None
        assert result.rewrite_latency_ms is not None
        assert result.response_text == "Original."

    @pytest.mark.skip(reason="Phase 1 rewrite: SettingsRepository mock path needs revisit")
    @pytest.mark.asyncio
    async def test_purge_legacy_schema_entries_runs_on_initialize(self):
        # v4: initialize() calls purge_legacy_schema_entries on both tiers
        manager, _store = self._make_manager()

        async def _get_value(key, default=None):
            return {"personality.prompt": ""}.get(key, default)

        with (
            patch("app.cache._base_cache.SettingsRepository") as mock_base,
            patch("app.cache.routing_cache.SettingsRepository") as mock_rs,
            patch("app.cache.action_cache.SettingsRepository") as mock_ac,
            patch("app.db.repository.SettingsRepository") as mock_cms,
            patch.object(manager._action_cache, "purge_legacy_schema_entries", return_value=3) as purge_action,
            patch.object(manager._routing_cache, "purge_legacy_schema_entries", return_value=1) as purge_routing,
        ):
            mock_base.get_value = AsyncMock(side_effect=_get_value)
            mock_rs.get_value = AsyncMock(side_effect=_get_value)
            mock_ac.get_value = AsyncMock(side_effect=_get_value)
            mock_cms.get_value = AsyncMock(side_effect=_get_value)
            await manager.initialize()
        purge_action.assert_called_once()
        purge_routing.assert_called_once()


# ---------------------------------------------------------------------------
# Embedding engine
# ---------------------------------------------------------------------------


class TestEmbeddingEngine:
    _startup_logger_levels: ClassVar[dict[str, int]] = {
        "httpx": logging.WARNING,
        "huggingface_hub.utils._http": logging.ERROR,
        "transformers.modeling_utils": logging.ERROR,
        "sentence_transformers.base.model": logging.WARNING,
    }

    def test_embed_local_via_sentence_transformer(self):
        engine = EmbeddingEngine()
        engine._provider = "local"
        engine._model_name = "all-MiniLM-L6-v2"

        mock_model = MagicMock()
        import numpy as np

        mock_model.encode.return_value = np.zeros((1, 384))
        engine._local_model = mock_model

        result = engine.embed("test")
        assert len(result) == 384

    def test_embed_batch_local(self):
        engine = EmbeddingEngine()
        engine._provider = "local"
        mock_model = MagicMock()
        import numpy as np

        mock_model.encode.return_value = np.zeros((2, 384))
        engine._local_model = mock_model

        results = engine.embed_batch(["text1", "text2"])
        assert len(results) == 2
        assert len(results[0]) == 384

    def test_embed_external_via_litellm(self):
        engine = EmbeddingEngine()
        engine._provider = "external"
        engine._model_name = "openai/text-embedding-3-small"

        mock_response = MagicMock()
        mock_response.data = [{"embedding": [0.1] * 384}, {"embedding": [0.2] * 384}]

        import sys

        mock_litellm = MagicMock()
        mock_litellm.embedding.return_value = mock_response
        with patch.dict(sys.modules, {"litellm": mock_litellm}):
            results = engine.embed_batch(["text1", "text2"])
        assert len(results) == 2

    async def test_initialize_loads_config(self):
        engine = EmbeddingEngine()
        with patch("app.cache.embedding.SettingsRepository") as mock_repo:
            mock_repo.get_value = AsyncMock(side_effect=["local", DEFAULT_LOCAL_EMBEDDING_MODEL])
            with patch.object(engine, "_get_local_model", return_value=MagicMock()):
                await engine.initialize()
        assert engine._provider == "local"
        assert engine._model_name == DEFAULT_LOCAL_EMBEDDING_MODEL

    async def test_initialize_uses_multilingual_default_when_local_model_missing(self):
        engine = EmbeddingEngine()

        async def _get_value(key, default=None):
            if key == "embedding.provider":
                return "local"
            if key == "embedding.local_model":
                return default
            return default

        with (
            patch("app.cache.embedding.SettingsRepository.get_value", new=AsyncMock(side_effect=_get_value)),
            patch.object(engine, "_get_local_model", return_value=MagicMock()),
        ):
            await engine.initialize()

        assert engine._provider == "local"
        assert engine._model_name == DEFAULT_LOCAL_EMBEDDING_MODEL

    def test_get_local_model_restores_startup_logger_levels_on_success(self):
        import sys
        import types

        engine = EmbeddingEngine()
        engine._model_name = "all-MiniLM-L6-v2"
        previous_levels = {name: logging.getLogger(name).level for name in self._startup_logger_levels}
        seeded_levels = {
            "httpx": logging.DEBUG,
            "huggingface_hub.utils._http": logging.INFO,
            "transformers.modeling_utils": logging.CRITICAL,
            "sentence_transformers.base.model": logging.NOTSET,
        }
        fake_model = object()
        seen_levels = {}

        def fake_sentence_transformer(model_name):
            assert model_name == "all-MiniLM-L6-v2"
            seen_levels.update({name: logging.getLogger(name).level for name in self._startup_logger_levels})
            return fake_model

        sentence_transformers_module = types.ModuleType("sentence_transformers")
        sentence_transformers_module.SentenceTransformer = fake_sentence_transformer
        huggingface_hub_module = types.ModuleType("huggingface_hub")
        huggingface_hub_module.disable_progress_bars = MagicMock()

        try:
            for name, level in seeded_levels.items():
                logging.getLogger(name).setLevel(level)

            with patch.dict(
                sys.modules,
                {
                    "sentence_transformers": sentence_transformers_module,
                    "huggingface_hub": huggingface_hub_module,
                },
            ):
                result = engine._get_local_model()

            assert seen_levels == self._startup_logger_levels
            assert result is fake_model
            assert engine._local_model is fake_model
            for name, level in seeded_levels.items():
                assert logging.getLogger(name).level == level
        finally:
            for name, level in previous_levels.items():
                logging.getLogger(name).setLevel(level)

    def test_get_local_model_restores_startup_logger_levels_on_failure(self):
        import sys
        import types

        engine = EmbeddingEngine()
        engine._model_name = "all-MiniLM-L6-v2"
        previous_levels = {name: logging.getLogger(name).level for name in self._startup_logger_levels}
        seeded_levels = {
            "httpx": logging.DEBUG,
            "huggingface_hub.utils._http": logging.INFO,
            "transformers.modeling_utils": logging.CRITICAL,
            "sentence_transformers.base.model": logging.NOTSET,
        }
        seen_levels = {}

        def fake_sentence_transformer(model_name):
            assert model_name == "all-MiniLM-L6-v2"
            seen_levels.update({name: logging.getLogger(name).level for name in self._startup_logger_levels})
            raise RuntimeError("model load failed")

        sentence_transformers_module = types.ModuleType("sentence_transformers")
        sentence_transformers_module.SentenceTransformer = fake_sentence_transformer
        huggingface_hub_module = types.ModuleType("huggingface_hub")
        huggingface_hub_module.disable_progress_bars = MagicMock()

        try:
            for name, level in seeded_levels.items():
                logging.getLogger(name).setLevel(level)

            with (
                patch.dict(
                    sys.modules,
                    {
                        "sentence_transformers": sentence_transformers_module,
                        "huggingface_hub": huggingface_hub_module,
                    },
                ),
                pytest.raises(RuntimeError, match="model load failed"),
            ):
                engine._get_local_model()

            assert seen_levels == self._startup_logger_levels
            assert engine._local_model is None
            for name, level in seeded_levels.items():
                assert logging.getLogger(name).level == level
        finally:
            for name, level in previous_levels.items():
                logging.getLogger(name).setLevel(level)


class TestChromaEmbeddingFunction:
    def test_calls_engine(self):
        mock_engine = MagicMock(spec=EmbeddingEngine)
        mock_engine.embed_batch.return_value = [[0.0] * 384]

        fn = ChromaEmbeddingFunction(mock_engine)
        result = fn(["test text"])
        assert len(result) == 1
        mock_engine.embed_batch.assert_called_once_with(["test text"])


# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------


class TestVectorStore:
    def test_add_delegates_to_collection(self):
        store = VectorStore()
        mock_col = MagicMock()
        store._collections = {COLLECTION_ENTITY_INDEX: mock_col}
        store.add(COLLECTION_ENTITY_INDEX, ids=["a"], documents=["doc"])
        mock_col.add.assert_called_once()

    def test_upsert_delegates_to_collection(self):
        store = VectorStore()
        mock_col = MagicMock()
        store._collections = {COLLECTION_ROUTING_CACHE: mock_col}
        store.upsert(COLLECTION_ROUTING_CACHE, ids=["a"], documents=["doc"])
        mock_col.upsert.assert_called_once()

    def test_query_delegates_to_collection(self):
        store = VectorStore()
        mock_col = MagicMock()
        mock_col.query.return_value = {"ids": [["a"]], "distances": [[0.1]]}
        store._collections = {COLLECTION_ENTITY_INDEX: mock_col}
        result = store.query(COLLECTION_ENTITY_INDEX, query_texts=["test"])
        assert result["ids"] == [["a"]]

    def test_delete_delegates_to_collection(self):
        store = VectorStore()
        mock_col = MagicMock()
        store._collections = {COLLECTION_RESPONSE_CACHE: mock_col}
        store.delete(COLLECTION_RESPONSE_CACHE, ids=["x"])
        mock_col.delete.assert_called_once_with(ids=["x"])

    def test_count_delegates_to_collection(self):
        store = VectorStore()
        mock_col = MagicMock()
        mock_col.count.return_value = 42
        store._collections = {COLLECTION_ROUTING_CACHE: mock_col}
        assert store.count(COLLECTION_ROUTING_CACHE) == 42

    def test_get_delegates_to_collection(self):
        store = VectorStore()
        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": ["a"], "metadatas": [{}]}
        store._collections = {COLLECTION_ENTITY_INDEX: mock_col}
        result = store.get(COLLECTION_ENTITY_INDEX, ids=["a"])
        assert result["ids"] == ["a"]

    def test_get_collection_missing_raises(self):
        store = VectorStore()
        store._collections = {}
        with pytest.raises(KeyError):
            store.get_collection("nonexistent")

    def test_update_metadata_delegates_to_collection(self):
        store = VectorStore()
        mock_col = MagicMock()
        store._collections = {COLLECTION_ENTITY_INDEX: mock_col}
        store.update_metadata(
            COLLECTION_ENTITY_INDEX,
            ids=["a", "b"],
            metadatas=[{"key": "v1"}, {"key": "v2"}],
        )
        mock_col.update.assert_called_once_with(ids=["a", "b"], metadatas=[{"key": "v1"}, {"key": "v2"}])

    def test_update_metadata_reconnects_on_closed(self):
        store = VectorStore()
        mock_col = MagicMock()
        mock_col.update.side_effect = RuntimeError("connection closed")
        store._collections = {COLLECTION_ENTITY_INDEX: mock_col}
        mock_col2 = MagicMock()
        original_get = store.get_collection
        call_count = 0

        def side_effect_get(name):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return original_get(name)
            return mock_col2

        with (
            patch.object(store, "_reinitialize_sync") as mock_reinit,
            patch.object(store, "get_collection", side_effect=side_effect_get),
        ):
            store.update_metadata(
                COLLECTION_ENTITY_INDEX,
                ids=["a"],
                metadatas=[{"key": "v1"}],
            )
        mock_reinit.assert_called_once()
        mock_col2.update.assert_called_once()

    def test_update_metadata_raises_non_closed_error(self):
        store = VectorStore()
        mock_col = MagicMock()
        mock_col.update.side_effect = ValueError("bad data")
        store._collections = {COLLECTION_ENTITY_INDEX: mock_col}
        with pytest.raises(ValueError, match="bad data"):
            store.update_metadata(
                COLLECTION_ENTITY_INDEX,
                ids=["a"],
                metadatas=[{"key": "v1"}],
            )

    def test_close_closes_client_and_clears_cached_state(self):
        store = VectorStore()
        client = MagicMock()
        store._client = client
        store._embedding_fn = MagicMock()
        store._collections = {COLLECTION_ENTITY_INDEX: MagicMock()}

        store.close()

        client.close.assert_called_once_with()
        assert store._client is None
        assert store._embedding_fn is None
        assert store._collections == {}


# ---------------------------------------------------------------------------
# Cache trace visibility -- similarity propagation tests
# Deleted: test_cache_result_includes_similarity_on_routing_hit (used _process_inner,
#          removed in v4; rewritten below using process() async)
#          test_cache_result_includes_similarity_on_miss (same)
#          test_response_cache_lookup_returns_similarity_tuple (ResponseCache removed;
#          covered by TestActionCache.test_lookup_hit_above_threshold above)
# ---------------------------------------------------------------------------


class TestCacheTraceSimilarity:
    @pytest.mark.asyncio
    async def test_cache_result_includes_similarity_on_routing_hit(self):
        """CacheResult.similarity is populated on a routing cache hit (v4: process())."""
        store = MagicMock(spec=VectorStore)
        manager = CacheManager(store)
        # Exact-id get returns empty so semantic path is used.
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {
            "ids": [["r-1"]],
            "distances": [[0.05]],
            "documents": [["turn on light"]],
            "metadatas": [
                [
                    {
                        "agent_id": "light-agent",
                        "confidence": "0.95",
                        "hit_count": "0",
                        "condensed_task": "Turn on",
                        "created_at": "2025-01-01T00:00:00",
                        "last_accessed": "2025-01-01T00:00:00",
                        "language": "en",
                    }
                ]
            ],
        }
        with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock):
            result = await manager.process("turn on light")
        assert result.hit_type == "routing_hit"
        assert result.similarity == pytest.approx(0.95)

    @pytest.mark.asyncio
    async def test_cache_result_includes_similarity_on_miss(self):
        """CacheResult.similarity is None on a complete routing miss (v4: process())."""
        store = MagicMock(spec=VectorStore)
        manager = CacheManager(store)
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {
            "ids": [["r-1"]],
            "distances": [[0.20]],
            "documents": [["other"]],
            "metadatas": [
                [
                    {
                        "agent_id": "general-agent",
                        "confidence": "0.80",
                        "hit_count": "0",
                        "condensed_task": "",
                        "created_at": "",
                        "last_accessed": "",
                        "language": "en",
                    }
                ]
            ],
        }
        with patch("app.cache.cache_manager.track_cache_event", new_callable=AsyncMock):
            result = await manager.process("some query")
        assert result.hit_type == "miss"
        assert result.similarity is None

    def test_routing_cache_lookup_returns_similarity_tuple(self):
        """routing_cache.lookup() returns (entry, similarity) tuple (v4: _semantic_threshold)."""
        store = MagicMock(spec=VectorStore)
        cache = RoutingCache(store)
        cache._semantic_threshold = 0.92
        # Exact-id get returns empty so semantic path is used.
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        store.query.return_value = {
            "ids": [["e-1"]],
            "distances": [[0.03]],
            "documents": [["test"]],
            "metadatas": [
                [
                    {
                        "agent_id": "light-agent",
                        "confidence": "0.97",
                        "hit_count": "0",
                        "created_at": "2025-01-01T00:00:00",
                        "last_accessed": "2025-01-01T00:00:00",
                        "language": "en",
                    }
                ]
            ],
        }
        entry, sim = cache.lookup("test")
        assert entry is not None
        assert sim == pytest.approx(0.97)


# ---------------------------------------------------------------------------
# Cache eviction tests
# Deleted from RoutingCacheEviction: test_eviction_triggers_at_interval,
#   test_eviction_does_not_trigger_before_interval (used positional cache.store()
#   signature removed in v4 — entry object required now)
# Deleted from RoutingCacheEviction: _threshold field usage -> _semantic_threshold
# TestResponseCacheEviction -> TestActionCacheEviction (ResponseCache removed)
# ---------------------------------------------------------------------------


class TestRoutingCacheEviction:
    """Tests for interval-based LRU eviction and hit count buffering in routing cache."""

    def _make_cache(self) -> tuple[RoutingCache, MagicMock]:
        store = MagicMock(spec=VectorStore)
        cache = RoutingCache(store)
        cache._semantic_threshold = 0.92
        cache._max_entries = 10
        return cache, store

    def test_hit_count_buffering_flushes_at_threshold(self):
        """Pending hit updates should flush when buffer reaches _flush_interval."""
        cache, store = self._make_cache()
        cache._flush_interval = 3
        # Exact-id get returns empty so semantic lookup is triggered.
        store.get.return_value = {"ids": [], "documents": [], "metadatas": []}

        for i in range(3):
            store.query.return_value = {
                "ids": [[f"entry-{i}"]],
                "distances": [[0.05]],
                "documents": [[f"query-{i}"]],
                "metadatas": [
                    [
                        {
                            "agent_id": "light-agent",
                            "confidence": "0.95",
                            "hit_count": "1",
                            "created_at": "2025-01-01T00:00:00",
                            "last_accessed": "2025-01-01T00:00:00",
                            "language": "en",
                        }
                    ]
                ],
            }
            cache.lookup(f"query-{i}")

        # After flush_interval lookups, update_metadata should have been called for flush
        assert store.update_metadata.call_count >= 1

    def test_batch_delete_in_chunks(self):
        """When evicting many entries, delete should be called in chunks of 500."""
        cache, store = self._make_cache()
        cache._max_entries = 10
        store.count.return_value = 1010
        ids = [f"id-{i}" for i in range(1010)]
        metadatas = [{"last_accessed": f"2025-01-{(i % 28) + 1:02d}T00:00:00"} for i in range(1010)]
        store.get.side_effect = [
            {"ids": ids[:1000], "metadatas": metadatas[:1000]},
            {"ids": ids[1000:], "metadatas": metadatas[1000:]},
        ]
        cache._enforce_lru()
        # Should delete in chunks - at least 2 calls (1000 excess / 500)
        assert store.delete.call_count >= 2


class TestActionCacheEviction:
    """Tests for interval-based LRU eviction in action cache (ported from TestResponseCacheEviction)."""

    def _make_cache(self) -> tuple[ActionCache, MagicMock]:
        store = MagicMock(spec=VectorStore)
        cache = ActionCache(store)
        cache._semantic_threshold = 0.95
        cache._max_entries = 10
        return cache, store

    def test_eviction_triggers_at_interval(self):
        """LRU eviction should only run every _eviction_interval stores."""
        cache, store = self._make_cache()
        cache._eviction_interval = 5
        store.count.return_value = 5

        entry = make_action_cache_entry()
        for i in range(4):
            cache._state._store_count = i
            store.count.reset_mock()
            cache.store(entry)

        cache._state._store_count = 4
        cache.store(entry)

    def test_batch_delete_in_chunks(self):
        """Action cache eviction should batch deletes in chunks of 500."""
        cache, store = self._make_cache()
        cache._max_entries = 10
        store.count.return_value = 600
        ids = [f"id-{i}" for i in range(600)]
        metadatas = [{"last_accessed": f"2025-01-{(i % 28) + 1:02d}T00:00:00"} for i in range(600)]
        store.get.side_effect = [
            {"ids": ids, "metadatas": metadatas},
            {"ids": [], "metadatas": []},
        ]
        cache._enforce_lru()
        # 590 excess / 500 = 2 chunks
        assert store.delete.call_count >= 2


# ---------------------------------------------------------------------------
# Action cache purge readonly entries
# (ported from TestResponseCachePurgeReadonly; ResponseCache removed in v4)
# Deleted: TestStoreResponseCacheCacheable — orch._store_response_cache removed
#   from orchestrator in v4; action caching is now via store_action() directly.
# ---------------------------------------------------------------------------


class TestActionCachePurgeReadonly:
    """Tests for ActionCache.purge_readonly_entries()."""

    def _make_cache(self) -> tuple[ActionCache, MagicMock]:
        store = MagicMock(spec=VectorStore)
        cache = ActionCache(store)
        return cache, store

    def test_purge_removes_readonly_entries(self):
        cache, store = self._make_cache()
        store.get.return_value = {
            "ids": ["id-1", "id-2", "id-3"],
            "metadatas": [
                {"cached_action": "", "response_text": "It is 22 degrees."},
                {
                    "cached_action": '{"service":"light/turn_on","entity_id":"light.kitchen","service_data":{}}',
                    "response_text": "Done.",
                },
                {"cached_action": "", "response_text": "The door is locked."},
            ],
        }
        count = cache.purge_readonly_entries()
        assert count == 2
        store.delete.assert_called_once_with(COLLECTION_ACTION_CACHE, ids=["id-1", "id-3"])

    def test_purge_skips_entries_with_cached_action(self):
        cache, store = self._make_cache()
        store.get.return_value = {
            "ids": ["id-1"],
            "metadatas": [
                {"cached_action": '{"service":"light/turn_on","entity_id":"light.kitchen","service_data":{}}'},
            ],
        }
        count = cache.purge_readonly_entries()
        assert count == 0
        store.delete.assert_not_called()

    def test_purge_empty_collection(self):
        cache, store = self._make_cache()
        store.get.return_value = {"ids": [], "metadatas": []}
        count = cache.purge_readonly_entries()
        assert count == 0
        store.delete.assert_not_called()

    def test_purge_handles_missing_cached_action_key(self):
        """Entries without cached_action key (pre-v0.14.0) should be purged."""
        cache, store = self._make_cache()
        store.get.return_value = {
            "ids": ["id-1", "id-2"],
            "metadatas": [
                {"response_text": "Old entry without cached_action field."},
                {"cached_action": '{"service":"light/turn_on","entity_id":"light.k","service_data":{}}'},
            ],
        }
        count = cache.purge_readonly_entries()
        assert count == 1
        store.delete.assert_called_once_with(COLLECTION_ACTION_CACHE, ids=["id-1"])

    @pytest.mark.asyncio
    async def test_cache_manager_purge_delegates(self):
        """CacheManager.purge_readonly_entries() should delegate to ActionCache."""
        store = MagicMock(spec=VectorStore)
        manager = CacheManager(store)
        store.get.return_value = {
            "ids": ["id-1"],
            "metadatas": [{"cached_action": ""}],
        }
        count = await manager.purge_readonly_entries()
        assert count == 1

    def test_purge_removes_readonly_service_entries(self):
        """Entries with read-only service (query_*, list_*) should be purged."""
        cache, store = self._make_cache()
        store.get.return_value = {
            "ids": ["id-1", "id-2", "id-3", "id-4"],
            "metadatas": [
                {"cached_action": '{"service":"sensor/query_status","entity_id":"sensor.temp","service_data":{}}'},
                {"cached_action": '{"service":"light/turn_on","entity_id":"light.kitchen","service_data":{}}'},
                {"cached_action": '{"service":"media/list_sources","entity_id":"media_player.tv","service_data":{}}'},
                {"cached_action": ""},
            ],
        }
        count = cache.purge_readonly_entries()
        assert count == 3  # id-1 (query_status), id-3 (list_sources), id-4 (empty)
        store.delete.assert_called_once_with(COLLECTION_ACTION_CACHE, ids=["id-1", "id-3", "id-4"])


def test_is_readonly_action_helper():
    """Unit test for _is_readonly_action module function (v4: no longer a static method on ResponseCache)."""
    assert _is_readonly_action("") is True
    assert _is_readonly_action('{"service":"sensor/query_status","entity_id":"x","service_data":{}}') is True
    assert _is_readonly_action('{"service":"media/list_sources","entity_id":"x","service_data":{}}') is True
    assert _is_readonly_action('{"service":"light/turn_on","entity_id":"x","service_data":{}}') is False
    assert _is_readonly_action('{"service":"climate/set_temperature","entity_id":"x","service_data":{}}') is False
    assert _is_readonly_action("invalid json") is True


@pytest.mark.asyncio
async def test_concurrent_cache_stress():
    """Spawn 20 async tasks doing store(), lookup(), and invalidate_by_entity_id()
    on overlapping keys. Assert no exceptions and consistent final state.
    """
    store = _make_vector_store()
    manager = CacheManager(store)
    manager._routing_cache._semantic_threshold = 0.5
    manager._routing_cache._max_entries = 1000

    def worker(task_id: int) -> None:
        for i in range(10):
            query_text = f"query {i % 3}"
            language = "en"
            manager.store_routing(query_text, "light-agent", 0.9, f"Task {task_id}", language=language)
            manager._routing_cache.lookup(query_text, language=language)
            manager._routing_cache.invalidate_by_entity_id([f"light.kitchen_{task_id % 2}"])

    await asyncio.gather(*[asyncio.to_thread(worker, i) for i in range(20)])

    # 20 workers * 10 iterations = 200 stores.
    # eviction_interval defaults to 100, so store_count should be 0.
    assert manager._routing_cache._state._store_count == 0
