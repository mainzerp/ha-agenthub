"""Tests for the routing-cache semantic similarity tier (P4).

The tier runs BETWEEN the exact-hash routing hit and the LLM classification
fallback: on an exact miss, the query embedding is compared (k-NN cosine,
sqlite-vec) against embeddings stored alongside routing entries. A candidate
above ``cache.routing.semantic_threshold`` flows through the identical
fail-closed validation as an exact hit (``routing_hit_is_still_valid``)
before classification may be skipped.

Embeddings are stubbed deterministically via
``app.cache.cache_manager.get_embedding_engine``; the store layer uses a real
SqliteCacheStore on a tmp_path so the vec0 migration and KNN search run for
real (sqlite-vec is a hard dependency of the project).
"""

from __future__ import annotations

import sqlite3
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

from app.agents.cache_orchestrator import CacheOrchestrator  # noqa: E402
from app.analytics.tracer import SpanCollector  # noqa: E402
from app.cache.cache_manager import CacheManager  # noqa: E402
from app.cache.routing_cache import RoutingCache  # noqa: E402
from app.cache.sqlite_cache_store import (  # noqa: E402
    _SCHEMA,
    _SIDECAR_SCHEMA,
    COLLECTION_ROUTING_CACHE,
    SqliteCacheStore,
)
from tests.helpers import make_routing_cache_entry  # noqa: E402

# Deterministic 4-dim unit vectors. cos(V_STORED, V_CLOSE) = 0.96 exactly
# (0.96^2 + 0.28^2 = 1.0); V_FAR is orthogonal to V_STORED.
V_STORED = [1.0, 0.0, 0.0, 0.0]
V_CLOSE = [0.96, 0.28, 0.0, 0.0]
V_FAR = [0.0, 1.0, 0.0, 0.0]


def _make_store(tmp_path) -> SqliteCacheStore:
    return SqliteCacheStore(str(tmp_path / "cache.db"))


def _make_semantic_cache(store: SqliteCacheStore, *, threshold: float = 0.92) -> RoutingCache:
    cache = RoutingCache(store)
    cache._enabled = True
    cache._semantic_enabled = True
    cache._semantic_threshold = threshold
    return cache


def _make_manager(store: SqliteCacheStore, *, threshold: float = 0.92) -> CacheManager:
    manager = CacheManager(store)
    manager._routing_cache._enabled = True
    manager._routing_cache._semantic_enabled = True
    manager._routing_cache._semantic_threshold = threshold
    return manager


def _mock_engine(vector: list[float]) -> MagicMock:
    engine = MagicMock()
    engine.embed = AsyncMock(return_value=vector)
    engine.embed_batch = AsyncMock(return_value=[vector])
    return engine


# ---------------------------------------------------------------------------
# Migration (cache.db user_version ladder, version 2)
# ---------------------------------------------------------------------------


class TestSemanticMigration:
    def test_fresh_store_migrates_to_version_2(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            conn = store._ensure_conn()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 2
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
            assert "routing_cache_embeddings" in tables
            assert "routing_cache_vec_dim" in tables
        finally:
            store.close()

    def test_migration_is_idempotent_across_reopen(self, tmp_path):
        store = _make_store(tmp_path)
        entry = make_routing_cache_entry(query_text="turn on kitchen light")
        RoutingCache(store).store(entry, embedding=V_STORED)
        store.close()

        reopened = _make_store(tmp_path)
        try:
            conn = reopened._ensure_conn()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 2
            assert reopened.count(COLLECTION_ROUTING_CACHE) == 1
            results = reopened.search_routing_embeddings(V_CLOSE, k=4)
            assert len(results) == 1
        finally:
            reopened.close()

    def test_migration_from_version_1_preserves_rows(self, tmp_path):
        db_path = str(tmp_path / "cache.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(_SCHEMA)
        conn.executescript(_SIDECAR_SCHEMA)
        conn.execute(
            "INSERT INTO routing_cache (entry_id, document, metadata_json, last_accessed, created_at) "
            "VALUES ('legacy-1', 'turn on kitchen light', '{}', '2025-01-01', '2025-01-01')"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        store = _make_store(tmp_path)
        try:
            version = store._ensure_conn().execute("PRAGMA user_version").fetchone()[0]
            assert version == 2
            assert store.count(COLLECTION_ROUTING_CACHE) == 1
            # Legacy row has no embedding yet (lazy backfill strategy).
            assert not store.has_routing_embedding("legacy-1")
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Store-level vec sidecar behavior
# ---------------------------------------------------------------------------


class TestSemanticStoreSidecar:
    def test_store_with_embedding_is_searchable(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            cache = _make_semantic_cache(store)
            cache.store(make_routing_cache_entry(), embedding=V_STORED)

            results = store.search_routing_embeddings(V_CLOSE, k=4)

            assert len(results) == 1
            _entry_id, distance = results[0]
            assert (1.0 - distance) == pytest.approx(0.96, abs=1e-3)
        finally:
            store.close()

    def test_invalidate_by_entity_id_purges_embedding(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            cache = _make_semantic_cache(store)
            entry = make_routing_cache_entry(entity_ids=["light.kitchen"])
            cache.store(entry, embedding=V_STORED)
            entry_id = cache.make_entry_id(entry.query_text, language=entry.language)
            assert store.has_routing_embedding(entry_id)

            deleted = cache.invalidate_by_entity_id(["light.kitchen"])

            assert deleted == 1
            assert cache.count() == 0
            assert not store.has_routing_embedding(entry_id)
            assert store.search_routing_embeddings(V_CLOSE, k=4) == []
        finally:
            store.close()

    def test_dimension_change_resets_vec_table(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            cache = _make_semantic_cache(store)
            cache.store(make_routing_cache_entry(), embedding=V_STORED)

            # Same entry re-stored with a different embedding dimension
            # (embedding model change): stale vectors must be dropped.
            cache.store(make_routing_cache_entry(), embedding=[1.0, 0.0, 0.0])

            rows = store._ensure_conn().execute("SELECT entry_id, dim FROM routing_cache_embeddings").fetchall()
            assert len(rows) == 1
            assert rows[0][1] == 3
            assert len(store.search_routing_embeddings([1.0, 0.0, 0.0], k=4)) == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# RoutingCache.lookup_semantic (threshold / language / flag behavior)
# ---------------------------------------------------------------------------


class TestLookupSemantic:
    def test_above_threshold_hits(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            cache = _make_semantic_cache(store)
            cache.store(make_routing_cache_entry(), embedding=V_STORED)

            entry_id, entry, similarity = cache.lookup_semantic(V_CLOSE, language="en")

            assert entry is not None
            assert entry_id is not None
            assert entry.agent_id == "light-agent"
            assert similarity == pytest.approx(0.96, abs=1e-3)
        finally:
            store.close()

    def test_below_threshold_falls_through(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            cache = _make_semantic_cache(store)
            cache.store(make_routing_cache_entry(), embedding=V_STORED)

            entry_id, entry, similarity = cache.lookup_semantic(V_FAR, language="en")

            assert (entry_id, entry, similarity) == (None, None, None)
        finally:
            store.close()

    def test_disabled_flag_returns_none(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            cache = _make_semantic_cache(store)
            cache._semantic_enabled = False
            cache.store(make_routing_cache_entry(), embedding=V_STORED)

            entry_id, entry, similarity = cache.lookup_semantic(V_STORED, language="en")

            assert (entry_id, entry, similarity) == (None, None, None)
        finally:
            store.close()

    def test_language_mismatch_is_skipped(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            cache = _make_semantic_cache(store)
            cache.store(make_routing_cache_entry(query_text="kuche licht an", language="de"), embedding=V_STORED)

            entry_id, entry, similarity = cache.lookup_semantic(V_STORED, language="en")

            assert (entry_id, entry, similarity) == (None, None, None)
        finally:
            store.close()

    def test_threshold_boundary_is_inclusive(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            cache = _make_semantic_cache(store)
            cache.store(make_routing_cache_entry(), embedding=V_STORED)
            # Measure the exact similarity the lookup computes, then pin the
            # threshold to it: similarity == threshold must be a hit.
            _id, _distance = store.search_routing_embeddings(V_CLOSE, k=4)[0]
            measured = 1.0 - float(_distance)

            cache._semantic_threshold = measured
            _eid, entry, _sim = cache.lookup_semantic(V_CLOSE, language="en")
            assert entry is not None

            cache._semantic_threshold = measured + 1e-4
            entry_id, entry, similarity = cache.lookup_semantic(V_CLOSE, language="en")
            assert (entry_id, entry, similarity) == (None, None, None)
        finally:
            store.close()


# ---------------------------------------------------------------------------
# CacheManager.try_routing_skip semantic wiring
# ---------------------------------------------------------------------------


class TestTryRoutingSkipSemantic:
    @pytest.mark.asyncio
    async def test_semantic_hit_returned_on_exact_miss(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=_mock_engine(V_STORED)),
                ),
                patch("app.cache.cache_manager.track_cache_event_background") as track,
            ):
                await manager.store_routing_async("turn on kitchen light", "light-agent", 0.95, language="en")

            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=_mock_engine(V_CLOSE)),
                ),
                patch("app.cache.cache_manager.track_cache_event_background") as track,
            ):
                outcome = await manager.try_routing_skip(query_text="switch on the kitchen lamp", language="en")

            assert outcome is not None
            assert outcome.kind == "semantic_hit"
            assert outcome.agent_id == "light-agent"
            assert outcome.similarity == pytest.approx(0.96, abs=1e-3)
            assert outcome.lookup_ms is not None
            track.assert_called_once_with(
                tier="routing",
                hit_type="semantic_hit",
                agent_id="light-agent",
                similarity=pytest.approx(0.96, abs=1e-3),
            )
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_semantic_disabled_flag_keeps_old_behavior(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            manager._routing_cache._semantic_enabled = False
            manager._routing_cache.store(make_routing_cache_entry(), embedding=V_STORED)
            engine = _mock_engine(V_CLOSE)

            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=engine),
                ),
                patch("app.cache.cache_manager.track_cache_event_background") as track,
            ):
                outcome = await manager.try_routing_skip(query_text="switch on the kitchen lamp", language="en")

            assert outcome is None
            engine.embed.assert_not_awaited()
            track.assert_called_once_with(tier="routing", hit_type="miss")
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_below_threshold_falls_through_to_classification(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            manager._routing_cache.store(make_routing_cache_entry(), embedding=V_STORED)

            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=_mock_engine(V_FAR)),
                ),
                patch("app.cache.cache_manager.track_cache_event_background") as track,
            ):
                outcome = await manager.try_routing_skip(query_text="unrelated utterance", language="en")

            assert outcome is None
            track.assert_called_once_with(tier="routing", hit_type="miss")
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_embedding_failure_falls_through_to_classification(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            manager._routing_cache.store(make_routing_cache_entry(), embedding=V_STORED)
            engine = MagicMock()
            engine.embed = AsyncMock(side_effect=RuntimeError("model exploded"))

            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=engine),
                ),
                patch("app.cache.cache_manager.track_cache_event_background"),
            ):
                outcome = await manager.try_routing_skip(query_text="switch on the kitchen lamp", language="en")

            assert outcome is None
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_exact_hit_schedules_lazy_backfill(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            # Legacy entry without an embedding row.
            manager._routing_cache.store(make_routing_cache_entry())
            entry_id = manager._routing_cache.make_entry_id("turn on kitchen lights", language="en")
            assert not store.has_routing_embedding(entry_id)
            spawned: list = []

            def _capture_spawn(coro, *, name=None):
                spawned.append((coro, name))
                coro.close()
                return MagicMock()

            with (
                patch("app.cache.cache_manager.spawn", side_effect=_capture_spawn),
                patch("app.cache.cache_manager.track_cache_event_background"),
            ):
                outcome = await manager.try_routing_skip(query_text="turn on kitchen lights", language="en")

            assert outcome is not None
            assert outcome.kind == "routing_hit"
            assert len(spawned) == 1
            assert spawned[0][1] == "routing-embedding-backfill"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_backfill_stores_embedding_for_legacy_entry(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            manager._routing_cache.store(make_routing_cache_entry())
            entry_id = manager._routing_cache.make_entry_id("turn on kitchen lights", language="en")

            with patch(
                "app.cache.cache_manager.get_embedding_engine",
                new=AsyncMock(return_value=_mock_engine(V_STORED)),
            ):
                await manager._backfill_routing_embedding(entry_id, "turn on kitchen lights")

            assert store.has_routing_embedding(entry_id)
            assert entry_id not in manager._backfill_inflight
            results = store.search_routing_embeddings(V_CLOSE, k=4)
            assert len(results) == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Empty-cache semantic skip guard (no wasted embed on a fresh DB)
# ---------------------------------------------------------------------------


class TestEmptyCacheSemanticSkip:
    @pytest.mark.asyncio
    async def test_empty_cache_skips_embed_and_semantic_lookup(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            engine = _mock_engine(V_CLOSE)
            lookup_spy = MagicMock(wraps=manager._routing_cache.lookup_semantic)

            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=engine),
                ),
                patch("app.cache.cache_manager.track_cache_event_background") as track,
                patch.object(manager._routing_cache, "lookup_semantic", lookup_spy),
            ):
                outcome = await manager.try_routing_skip(query_text="turn on kitchen light", language="en")

            assert outcome is None
            engine.embed.assert_not_awaited()
            lookup_spy.assert_not_called()
            track.assert_called_once_with(tier="routing", hit_type="miss")
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_non_empty_cache_runs_semantic_lookup(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            manager._routing_cache.store(make_routing_cache_entry(), embedding=V_STORED)
            engine = _mock_engine(V_FAR)

            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=engine),
                ),
                patch("app.cache.cache_manager.track_cache_event_background") as track,
            ):
                outcome = await manager.try_routing_skip(query_text="unrelated utterance", language="en")

            assert outcome is None
            engine.embed.assert_awaited_once()
            track.assert_called_once_with(tier="routing", hit_type="miss")
        finally:
            store.close()


# ---------------------------------------------------------------------------
# CacheOrchestrator validation of semantic hits (fail-closed, Directive 2/7)
# ---------------------------------------------------------------------------


def _make_cache_orchestrator(manager: CacheManager, *, known_agents: set[str]) -> CacheOrchestrator:
    registry = AsyncMock()
    registry.get_known_agents = AsyncMock(return_value=known_agents)
    orchestrator = CacheOrchestrator(cache_manager=manager, entity_index=None, agent_registry=registry)
    orchestrator._get_bool_setting_impl = AsyncMock(return_value=True)
    return orchestrator


class TestSemanticHitValidation:
    @pytest.mark.asyncio
    async def test_validated_semantic_hit_is_returned(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            manager._routing_cache.store(make_routing_cache_entry(), embedding=V_STORED)
            orchestrator = _make_cache_orchestrator(manager, known_agents={"light-agent"})
            spans = SpanCollector("semantic-valid")

            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=_mock_engine(V_CLOSE)),
                ),
                patch("app.cache.cache_manager.track_cache_event_background"),
            ):
                action_hit, routing_hit = await orchestrator.try_cache_replay(
                    user_text="switch on the kitchen lamp",
                    language="en",
                    span_collector=spans,
                )

            assert action_hit is None
            assert routing_hit is not None
            assert routing_hit.kind == "semantic_hit"
            assert routing_hit.agent_id == "light-agent"
            cache_span = next(s for s in spans._spans if s["span_name"] == "cache_lookup")
            assert cache_span["metadata"]["hit_type"] == "semantic_hit"
            assert "routing_lookup_ms" in cache_span["metadata"]
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_unregistered_agent_invalidates_and_falls_through(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            manager._routing_cache.store(make_routing_cache_entry(), embedding=V_STORED)
            orchestrator = _make_cache_orchestrator(manager, known_agents={"general-agent"})
            spans = SpanCollector("semantic-invalid")

            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=_mock_engine(V_CLOSE)),
                ),
                patch("app.cache.cache_manager.track_cache_event_background"),
            ):
                action_hit, routing_hit = await orchestrator.try_cache_replay(
                    user_text="switch on the kitchen lamp",
                    language="en",
                    span_collector=spans,
                )

            assert (action_hit, routing_hit) == (None, None)
            # Stale entry invalidated: the routing row AND its embedding are gone.
            assert manager._routing_cache.count() == 0
            assert store.search_routing_embeddings(V_CLOSE, k=4) == []
            cache_span = next(s for s in spans._spans if s["span_name"] == "cache_lookup")
            assert cache_span["metadata"]["hit_type"] == "semantic_invalid"
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_invisible_referenced_entity_invalidates_and_falls_through(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            manager = _make_manager(store)
            manager._routing_cache.store(
                make_routing_cache_entry(entity_ids=["light.kitchen"]),
                embedding=V_STORED,
            )
            orchestrator = _make_cache_orchestrator(manager, known_agents={"light-agent"})
            orchestrator._entity_index = MagicMock()

            with (
                patch(
                    "app.cache.cache_manager.get_embedding_engine",
                    new=AsyncMock(return_value=_mock_engine(V_CLOSE)),
                ),
                patch("app.cache.cache_manager.track_cache_event_background"),
                patch(
                    "app.agents.cache_orchestrator.entity_is_visible",
                    new=AsyncMock(return_value=False),
                ),
            ):
                action_hit, routing_hit = await orchestrator.try_cache_replay(
                    user_text="switch on the kitchen lamp",
                    language="en",
                )

            assert (action_hit, routing_hit) == (None, None)
            assert manager._routing_cache.count() == 0
        finally:
            store.close()
