"""Unified cache manager with action replay and routing skip tiers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.analytics.collector import track_cache_event_background, track_rewrite
from app.analytics.tracer import _optional_span
from app.cache.action_cache import ActionCache
from app.cache.embedding import get_embedding_engine
from app.cache.routing_cache import RoutingCache
from app.cache.sqlite_cache_store import COLLECTION_ACTION_CACHE, COLLECTION_ROUTING_CACHE, SqliteCacheStore
from app.models.cache import ActionCacheEntry, CachedAction, RoutingCacheEntry
from app.util.tasks import spawn

logger = logging.getLogger(__name__)


@dataclass
class CacheResult:
    """Compatibility result type used by auxiliary callers/tests."""

    hit_type: str
    agent_id: str | None = None
    response_text: str | None = None
    cached_action: CachedAction | None = None
    entry: ActionCacheEntry | RoutingCacheEntry | None = None
    condensed_task: str | None = None
    similarity: float | None = None
    rewrite_applied: bool = False
    rewrite_latency_ms: float | None = None
    original_response_text: str | None = None
    entity_ids: list[str] | None = None


@dataclass
class ActionReplayOutcome:
    kind: str
    entry_id: str
    agent_id: str
    response_text: str
    replay_result: dict[str, Any] | None = None
    similarity: float | None = None
    language: str = "en"
    cached_action: CachedAction | None = None
    rewrite_applied: bool = False
    rewrite_latency_ms: float | None = None
    original_response_text: str | None = None


@dataclass
class RoutingSkipOutcome:
    kind: str
    entry_id: str
    agent_id: str
    condensed_task: str
    similarity: float
    language: str = "en"
    entity_ids: list[str] = field(default_factory=list)
    entity_candidates: list[tuple[str, str, float]] = field(default_factory=list)
    lookup_ms: float | None = None


class CacheManager:
    """Coordinates routing skip and action replay cache tiers."""

    def __init__(
        self,
        cache_store: SqliteCacheStore,
        rewrite_agent=None,
    ) -> None:
        self._cache_store = cache_store
        self._routing_cache = RoutingCache(cache_store)
        self._action_cache = ActionCache(cache_store)
        self._rewrite_agent = rewrite_agent
        self._rewrite_enabled: bool = False
        self._backfill_inflight: set[str] = set()

    @property
    def response_cache(self) -> ActionCache:
        """Compatibility alias for older callers/tests."""
        return self._action_cache

    @property
    def action_cache(self) -> ActionCache:
        return self._action_cache

    async def initialize(self) -> None:
        """Load config for both cache tiers."""
        await self._routing_cache.load_config()
        await self._action_cache.load_config()

        # Rewrite is enabled whenever the rewrite agent is present.
        # Personality injection is now handled inside RewriteAgent itself.
        self._rewrite_enabled = self._rewrite_agent is not None
        try:
            await asyncio.to_thread(
                self._routing_cache.purge_entries_without_language,
            )
            await asyncio.to_thread(
                self._action_cache.purge_entries_without_language,
            )
            await asyncio.to_thread(
                self._routing_cache.purge_legacy_schema_entries,
                5,
            )
            await asyncio.to_thread(
                self._action_cache.purge_legacy_schema_entries,
                4,
            )
        except Exception:
            logger.warning(
                "Cache language-migration purge failed (non-fatal)",
                exc_info=True,
            )

    async def reload_config(self) -> None:
        """Hot-reload thresholds and rewrite setting from DB."""
        await self._routing_cache.reload_config()
        await self._action_cache.reload_config()

        self._rewrite_enabled = self._rewrite_agent is not None

    async def process(
        self,
        query_text: str,
        *,
        language: str = "en",
    ) -> CacheResult:
        """Compatibility wrapper that exposes routing hits as CacheResult."""
        try:
            routing = await self.try_routing_skip(query_text=query_text, language=language)
            if routing is None:
                return CacheResult(hit_type="miss")
            return CacheResult(
                hit_type="routing_hit",
                agent_id=routing.agent_id,
                condensed_task=routing.condensed_task,
                similarity=routing.similarity,
                entity_ids=routing.entity_ids,
            )
        except Exception:
            logger.warning("Cache lookup failed, bypassing cache", exc_info=True)
            return CacheResult(hit_type="miss")

    async def try_replay_action(
        self,
        *,
        query_text: str,
        language: str = "en",
        requesting_agent_id: str = "orchestrator",
        check_visibility,
        execute_cached_action,
        span_collector=None,
    ) -> ActionReplayOutcome | None:
        """Attempt to replay a cached action after current-turn validation."""
        if not self._action_cache._enabled:
            return None
        try:
            entry_id, entry, similarity = await asyncio.to_thread(
                self._action_cache.lookup_with_id,
                query_text,
                language=language,
            )
        except Exception:
            logger.warning("Action cache lookup failed", exc_info=True)
            return None
        if entry is None or entry.cached_action is None:
            return None

        # Defensive: never replay context-dependent (conditional) entries.
        if getattr(entry, "context_dependent", False):
            return None

        # Re-validation: check visibility for every entity referenced by the
        # cached entry, not just the primary action target.
        cached_agent_id = entry.agent_id if entry.agent_id is not None else requesting_agent_id
        entity_ids_to_check = list(dict.fromkeys([entry.cached_action.entity_id, *(entry.entity_ids or [])]))
        try:
            visibility_results = await asyncio.gather(
                *[check_visibility(cached_agent_id, eid) for eid in entity_ids_to_check if eid]
            )
        except Exception:
            logger.warning("Action cache visibility recheck failed", exc_info=True)
            visibility_results = [False]
        if not all(visibility_results):
            with contextlib.suppress(Exception):
                if entry_id is not None:
                    await asyncio.to_thread(self._action_cache.invalidate_by_entry_id, entry_id)
            track_cache_event_background(tier="action", hit_type="miss")
            return None

        # The ha_action span wraps the ACTUAL replay REST call here (it used
        # to be emitted downstream in finalize_action_replay_hit, where it
        # wrapped nothing because the replay had already happened).
        async with _optional_span(span_collector, "ha_action", agent_id=cached_agent_id) as ha_span:
            ha_span["metadata"]["action"] = entry.cached_action.service
            ha_span["metadata"]["entity"] = entry.cached_action.entity_id
            ha_span["metadata"]["cached"] = True
            try:
                replay_result = await execute_cached_action(entry.cached_action)
            except Exception:
                logger.warning("Cached action replay failed", exc_info=True)
                replay_result = None
            ha_span["metadata"]["success"] = replay_result is not None
        if replay_result is None:
            track_cache_event_background(tier="action", hit_type="miss")
            return None

        track_cache_event_background(
            tier="action",
            hit_type="action_hit",
            agent_id=entry.agent_id,
            similarity=similarity,
        )
        if entry_id is None:
            return None
        return ActionReplayOutcome(
            kind="full_hit",
            entry_id=entry_id,
            agent_id=entry.agent_id,
            response_text=entry.response_text,
            replay_result=replay_result,
            similarity=similarity,
            language=entry.language,
            cached_action=entry.cached_action,
            rewrite_applied=entry.rewrite_applied,
            original_response_text=entry.original_response_text,
        )

    async def try_routing_skip(
        self,
        *,
        query_text: str,
        language: str = "en",
    ) -> RoutingSkipOutcome | None:
        """Return a routing cache hit that can skip live classification.

        Lookup order: exact SHA-256 hash first, then -- when the semantic
        tier is enabled -- a k-NN cosine search over stored query embeddings
        (embedding similarity only, Directive 11). Both tiers return an
        unvalidated candidate; the caller (CacheOrchestrator) applies the
        identical fail-closed ``routing_hit_is_still_valid`` check to both
        before classification may be skipped (Directives 2, 7, 12).
        """
        if not self._routing_cache._enabled:
            return None
        t0 = time.perf_counter()
        try:
            entry_id, entry, similarity = await asyncio.to_thread(
                self._routing_cache.lookup_with_id,
                query_text,
                language=language,
            )
        except Exception:
            logger.warning("Routing cache lookup failed", exc_info=True)
            return None
        if entry is not None and similarity is not None:
            # Lazy backfill: entries stored before the semantic tier existed
            # get their embedding on the next exact hit (documented choice --
            # no bulk re-embed migration).
            self._schedule_semantic_backfill(entry_id, query_text)
            track_cache_event_background(
                tier="routing",
                hit_type="routing_hit",
                agent_id=entry.agent_id,
                similarity=similarity,
            )
            return RoutingSkipOutcome(
                kind="routing_hit",
                entry_id=entry_id or "",
                agent_id=entry.agent_id,
                condensed_task=query_text,
                similarity=similarity,
                language=entry.language,
                entity_ids=entry.entity_ids or [],
                entity_candidates=entry.entity_candidates or [],
                lookup_ms=(time.perf_counter() - t0) * 1000,
            )

        # Exact miss: semantic tier (P4). The embedding encode offloads the
        # CPU-bound work off the event loop internally (Directive 9). Any
        # failure falls through to live LLM classification (Directive 12).
        # An empty cache cannot produce a semantic hit, so the entry count
        # is checked first to skip the wasted encode (count is blocking
        # sqlite I/O -- offloaded via asyncio.to_thread, Directive 9).
        if self._routing_cache.semantic_available():
            try:
                entry_count = await asyncio.to_thread(self._routing_cache.count)
            except Exception:
                logger.warning("Routing cache count failed; skipping semantic tier", exc_info=True)
                entry_count = 0
            if entry_count > 0:
                try:
                    engine = await get_embedding_engine()
                    query_embedding = await engine.embed(query_text)
                    sem_id, sem_entry, sem_similarity = await asyncio.to_thread(
                        self._routing_cache.lookup_semantic,
                        query_embedding,
                        language=language,
                    )
                except Exception:
                    logger.warning("Routing semantic lookup failed; falling back to classification", exc_info=True)
                    sem_id, sem_entry, sem_similarity = None, None, None
                if sem_entry is not None and sem_similarity is not None:
                    track_cache_event_background(
                        tier="routing",
                        hit_type="semantic_hit",
                        agent_id=sem_entry.agent_id,
                        similarity=sem_similarity,
                    )
                    return RoutingSkipOutcome(
                        kind="semantic_hit",
                        entry_id=sem_id or "",
                        agent_id=sem_entry.agent_id,
                        condensed_task=query_text,
                        similarity=sem_similarity,
                        language=sem_entry.language,
                        entity_ids=sem_entry.entity_ids or [],
                        entity_candidates=sem_entry.entity_candidates or [],
                        lookup_ms=(time.perf_counter() - t0) * 1000,
                    )

        track_cache_event_background(tier="routing", hit_type="miss")
        return None

    def _schedule_semantic_backfill(self, entry_id: str | None, query_text: str) -> None:
        """Schedule a background embedding backfill for an entry lacking one."""
        if not entry_id or not self._routing_cache.semantic_available():
            return
        if entry_id in self._backfill_inflight:
            return
        self._backfill_inflight.add(entry_id)
        spawn(self._backfill_routing_embedding(entry_id, query_text), name="routing-embedding-backfill")

    async def _backfill_routing_embedding(self, entry_id: str, query_text: str) -> None:
        """Embed an existing routing entry's query text and store the vector."""
        try:
            if await asyncio.to_thread(self._routing_cache.has_embedding, entry_id):
                return
            engine = await get_embedding_engine()
            embedding = await engine.embed(query_text)
            await asyncio.to_thread(self._routing_cache.store_embedding, entry_id, embedding)
        except Exception:
            logger.debug("Routing embedding backfill failed for %s", entry_id, exc_info=True)
        finally:
            self._backfill_inflight.discard(entry_id)

    async def apply_rewrite(
        self,
        result: ActionReplayOutcome | CacheResult,
        *,
        conversation=None,
        user_text: str | None = None,
        reminder_text: str | None = None,
    ) -> str:
        """Apply rewrite + personality to an action-cache full hit and return final speech.

        Uses the original agent response (unmediated raw output) as input so
        the rewrite agent applies both personality and phrasing variation in
        a single LLM call. The cached mediated response_text is no longer used
        for replay.
        """
        fallback_text = result.original_response_text or result.response_text or ""
        if not self._rewrite_agent or not self._rewrite_enabled:
            return fallback_text
        source_text = result.original_response_text or result.response_text
        if not source_text:
            return ""
        language = getattr(result, "language", "en")
        t0 = time.perf_counter()
        try:
            rewritten = await self._rewrite_agent.rewrite(
                source_text, language=language, user_text=user_text, reminder_text=reminder_text
            )
            rewrite_ms = (time.perf_counter() - t0) * 1000
            if rewritten:
                result.response_text = rewritten
                result.rewrite_applied = True
                result.rewrite_latency_ms = rewrite_ms
                result.original_response_text = source_text
                await track_rewrite(latency_ms=rewrite_ms, success=True)
                return rewritten
            result.rewrite_latency_ms = rewrite_ms
            await track_rewrite(latency_ms=rewrite_ms, success=False)
            return fallback_text
        except Exception:
            rewrite_ms = (time.perf_counter() - t0) * 1000
            result.rewrite_latency_ms = rewrite_ms
            await track_rewrite(latency_ms=rewrite_ms, success=False)
            logger.warning("Rewrite failed, using original agent text", exc_info=True)
            return fallback_text

    def store_routing(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
        entity_candidates: list[tuple[str, str, float]] | None = None,
        embedding: list[float] | None = None,
    ) -> None:
        """Store a routing decision after dispatch or read-only handling."""
        # entity_ids stays a superset of the candidate ids so the
        # invalidation scan (which matches only entity_ids) covers them.
        merged_ids = list(dict.fromkeys([*(entity_ids or []), *(c[0] for c in entity_candidates or [])]))
        entry = RoutingCacheEntry(
            query_text=query_text,
            language=language,
            agent_id=agent_id,
            confidence=confidence,
            entity_ids=merged_ids,
            entity_candidates=entity_candidates or [],
        )
        self._routing_cache.store(entry, embedding=embedding)

    def store_routing_only(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
        entity_candidates: list[tuple[str, str, float]] | None = None,
    ) -> None:
        self.store_routing(
            query_text,
            agent_id,
            confidence,
            language=language,
            entity_ids=entity_ids,
            entity_candidates=entity_candidates,
        )

    async def store_routing_async(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
        entity_candidates: list[tuple[str, str, float]] | None = None,
    ) -> None:
        """Async wrapper around ``store_routing``.

        Computes the query embedding up front (CPU-bound encode is offloaded
        off the event loop inside the engine, Directive 9) so the vec row is
        written in the same store transaction. An embedding failure degrades
        to an exact-only entry -- the routing cache keeps working.
        """
        embedding: list[float] | None = None
        if self._routing_cache.semantic_available():
            try:
                engine = await get_embedding_engine()
                embedding = await engine.embed(query_text)
            except Exception:
                logger.warning("Routing embedding compute failed; storing without embedding", exc_info=True)
        await asyncio.to_thread(
            self.store_routing,
            query_text,
            agent_id,
            confidence,
            language=language,
            entity_ids=entity_ids,
            entity_candidates=entity_candidates,
            embedding=embedding,
        )

    async def store_routing_only_async(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
        entity_candidates: list[tuple[str, str, float]] | None = None,
    ) -> None:
        await self.store_routing_async(
            query_text,
            agent_id,
            confidence,
            language=language,
            entity_ids=entity_ids,
            entity_candidates=entity_candidates,
        )

    def store_action(self, entry: ActionCacheEntry) -> None:
        self._action_cache.store(entry)

    async def store_action_async(self, entry: ActionCacheEntry) -> None:
        await asyncio.to_thread(self.store_action, entry)

    def invalidate_action(self, entry_id: str) -> None:
        self._action_cache.invalidate_by_entry_id(entry_id)

    def invalidate_routing(self, entry_id: str) -> None:
        self._routing_cache.invalidate_by_entry_id(entry_id)

    async def invalidate_by_entity_id(self, entity_ids) -> dict[str, int]:
        unique_ids = [str(entity_id) for entity_id in dict.fromkeys(entity_ids or []) if entity_id]
        if not unique_ids:
            return {"action": 0, "routing": 0}

        def _invalidate(cache) -> int:
            # Pass the full id set so the underlying scan paginates the
            # collection once instead of N times for N entity ids.
            return cache.invalidate_by_entity_id(unique_ids)

        action_count, routing_count = await asyncio.gather(
            asyncio.to_thread(_invalidate, self._action_cache),
            asyncio.to_thread(_invalidate, self._routing_cache),
        )
        logger.debug(
            "CacheManager invalidated %d action and %d routing entries for entity_ids=%s",
            action_count,
            routing_count,
            unique_ids,
        )
        return {
            "action": action_count,
            "routing": routing_count,
        }

    def flush(self, tier: str | None = None) -> None:
        """Clear one or both cache tiers."""
        if tier not in (None, "routing", "action"):
            raise ValueError(f"unknown cache tier {tier!r}")
        if tier is None or tier == "routing":
            self._routing_cache.prepare_for_flush()
            count = self._cache_store.count(COLLECTION_ROUTING_CACHE)
            if count > 0:
                self._cache_store.delete_all(COLLECTION_ROUTING_CACHE)
            logger.info("Routing cache flushed")
        if tier is None or tier == "action":
            self._action_cache.prepare_for_flush()
            count = self._cache_store.count(COLLECTION_ACTION_CACHE)
            if count > 0:
                self._cache_store.delete_all(COLLECTION_ACTION_CACHE)
            logger.info("Action cache flushed")

    def flush_pending(self) -> None:
        """Flush buffered hit-count updates (call at shutdown)."""
        self._routing_cache.flush_pending()
        self._action_cache.flush_pending()

    def get_stats(self) -> dict[str, Any]:
        """Return combined stats for both tiers."""
        return {
            "routing": self._routing_cache.get_stats(),
            "action": self._action_cache.get_stats(),
        }

    async def purge_readonly_entries(self) -> int:
        """Purge legacy read-only rows that should now live in routing cache."""
        return await asyncio.to_thread(self._action_cache.purge_readonly_entries)

    def iter_action_entries(self, *, page_size: int = 1000):
        """Yield action-cache entries, paginating through the underlying store."""
        return self._action_cache.iterate_entries(page_size=page_size)

    async def update_action_entry(self, entry: ActionCacheEntry) -> None:
        """Store a corrected action-cache entry (upserts by deterministic entry_id)."""
        await self.store_action_async(entry)
