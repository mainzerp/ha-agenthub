"""Unified cache manager with invalidation and threshold management."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.analytics.collector import track_cache_event, track_rewrite
from app.cache.response_cache import ResponseCache
from app.cache.routing_cache import RoutingCache
from app.cache.vector_store import COLLECTION_RESPONSE_CACHE, COLLECTION_ROUTING_CACHE, VectorStore
from app.models.cache import CachedAction, ResponseCacheEntry, RoutingCacheEntry

logger = logging.getLogger(__name__)


@dataclass
class CacheResult:
    """Result from cache manager process() call."""

    hit_type: str  # "action_hit", "action_partial", "routing_hit", "miss" ("response_hit"/"response_partial" accepted as legacy aliases)
    agent_id: str | None = None
    response_text: str | None = None
    cached_action: CachedAction | None = None
    entry: ResponseCacheEntry | RoutingCacheEntry | None = None
    condensed_task: str | None = None
    similarity: float | None = None
    rewrite_applied: bool = False
    rewrite_latency_ms: float | None = None
    original_response_text: str | None = None


class CacheManager:
    """Coordinates routing and response caches for the orchestrator flow."""

    def __init__(
        self,
        vector_store: VectorStore,
        rewrite_agent=None,
    ) -> None:
        self._vector_store = vector_store
        self._routing_cache = RoutingCache(vector_store)
        self._response_cache = ResponseCache(vector_store)
        self._rewrite_agent = rewrite_agent
        self._rewrite_enabled: bool = False
        self._response_cache_enabled: bool = True

    @property
    def response_cache(self) -> ResponseCache:
        """Public accessor for the response cache tier."""
        return self._response_cache

    @property
    def action_cache(self) -> ResponseCache:
        """Alias for response_cache, added in 0.21.0."""
        return self._response_cache

    async def initialize(self) -> None:
        """Load config for both cache tiers."""
        await self._routing_cache.load_config()
        await self._response_cache.load_config()
        from app.db.repository import SettingsRepository

        personality = await SettingsRepository.get_value("personality.prompt", "")
        self._rewrite_enabled = bool(personality.strip())
        raw = await SettingsRepository.get_value("cache.response.enabled", "true")
        self._response_cache_enabled = raw.lower() == "true"
        # FLOW-HIGH-4: one-shot purge of pre-0.18.0 entries that lack a
        # ``language`` metadata field. The new lookup filters on
        # ``language`` so those entries would be unreachable anyway;
        # removing them keeps the collection tidy and avoids wasted
        # LRU slots.
        try:
            await asyncio.to_thread(
                self._routing_cache.purge_entries_without_language,
            )
            await asyncio.to_thread(
                self._response_cache.purge_entries_without_language,
            )
        except Exception:
            logger.warning(
                "Cache language-migration purge failed (non-fatal)",
                exc_info=True,
            )

    async def reload_config(self) -> None:
        """Hot-reload thresholds and rewrite setting from DB."""
        await self._routing_cache.reload_config()
        await self._response_cache.reload_config()
        from app.db.repository import SettingsRepository

        if self._rewrite_agent:
            personality = await SettingsRepository.get_value("personality.prompt", "")
            self._rewrite_enabled = bool(personality.strip())
        raw = await SettingsRepository.get_value("cache.response.enabled", "true")
        self._response_cache_enabled = raw.lower() == "true"

    async def process(
        self,
        query_text: str,
        *,
        language: str = "en",
    ) -> CacheResult:
        """Check both cache tiers in order: routing first, then response.

        Returns a CacheResult indicating what was found.
        Rewrite is NOT applied here; call apply_rewrite() separately.
        """
        try:
            result = await asyncio.to_thread(
                self._process_inner,
                query_text,
                language,
            )
            # FLOW-TELEM-1 (P2-5): only emit a cache event for real hits
            # (routing_hit / action_hit / action_partial; legacy response_*
            # variants accepted for one minor). Misses
            # would just fill the analytics table with agent_id=None rows
            # that dashboards ignore anyway; aggregate miss counting is
            # handled via the hit-rate derivation in the dashboards.
            if result.hit_type in {
                "action_hit",
                "action_partial",
                "response_hit",
                "response_partial",
                "routing_hit",
            }:
                tier = (
                    "action"
                    if result.hit_type.startswith("action") or result.hit_type.startswith("response")
                    else "routing"
                )
                await track_cache_event(
                    tier=tier,
                    hit_type=result.hit_type,
                    agent_id=result.agent_id,
                    similarity=result.similarity,
                )
            return result
        except Exception:
            logger.warning("Cache lookup failed, bypassing cache", exc_info=True)
            return CacheResult(hit_type="miss")

    async def apply_rewrite(self, result: CacheResult) -> None:
        """Apply rewrite to an action_hit CacheResult in-place."""
        if result.hit_type not in ("action_hit", "response_hit") or not self._rewrite_agent or not result.response_text:
            return
        original_text = result.response_text
        t0 = time.perf_counter()
        try:
            rewritten = await self._rewrite_agent.rewrite(result.response_text)
            rewrite_ms = (time.perf_counter() - t0) * 1000
            if rewritten:
                result.response_text = rewritten
                result.rewrite_applied = True
                result.rewrite_latency_ms = rewrite_ms
                result.original_response_text = original_text
                await track_rewrite(latency_ms=rewrite_ms, success=True)
            else:
                result.rewrite_latency_ms = rewrite_ms
                await track_rewrite(latency_ms=rewrite_ms, success=False)
        except Exception:
            rewrite_ms = (time.perf_counter() - t0) * 1000
            result.rewrite_latency_ms = rewrite_ms
            await track_rewrite(latency_ms=rewrite_ms, success=False)
            logger.warning("Rewrite failed, using original cached text", exc_info=True)

    def _process_inner(self, query_text: str, language: str = "en") -> CacheResult:
        """Internal cache lookup logic.

        FLOW-CACHE-1: Response cache is checked FIRST because a
        response_hit is strictly more valuable than a routing_hit --
        it replays the HA action AND runs the cached text through the
        rewrite-agent for variation, skipping classify + agent dispatch
        + the agent's own LLM turn. The previous routing-first ordering
        silently shadowed the response cache for every repeated action
        query because the routing threshold (0.92) is lower than the
        response threshold (0.95), so routing always matched first.

        A response_hit is only surfaced when the entry carries a
        ``cached_action``. State queries ("what's the temperature in
        the bedroom?") have no replayable action; replaying the cached
        response text would leak stale entity state. Those fall through
        to routing_hit, which re-dispatches the agent so the response
        is recomputed against live HA state.
        """
        # 1. Check response cache first, but only honor hits that can be
        #    replayed deterministically (cached_action present).
        hit_type, resp_entry, resp_similarity = self._response_cache.lookup(
            query_text,
            language=language,
        )
        if hit_type == "hit" and resp_entry is not None and resp_entry.cached_action is not None:
            return CacheResult(
                hit_type="action_hit",
                agent_id=resp_entry.agent_id,
                response_text=resp_entry.response_text,
                cached_action=resp_entry.cached_action,
                entry=resp_entry,
                similarity=resp_similarity,
            )

        # 2. Fall through to routing cache. This covers:
        #    - response_hit entries without cached_action (state queries
        #      that accidentally made it in; replaying would be stale)
        #    - response miss / response_partial (no deterministic replay)
        #    Routing still skips classify; the agent runs against live
        #    state for speech and any downstream reads.
        routing_entry, routing_similarity = self._routing_cache.lookup(
            query_text,
            language=language,
        )
        if routing_entry:
            return CacheResult(
                hit_type="routing_hit",
                agent_id=routing_entry.agent_id,
                entry=routing_entry,
                condensed_task=routing_entry.condensed_task,
                similarity=routing_similarity,
            )

        # 3. No routing hit -- surface a response_partial if we have
        #    one so downstream consumers can factor it into confidence
        #    / tracing. Partial never short-circuits dispatch.
        if hit_type == "partial" and resp_entry is not None:
            return CacheResult(
                hit_type="action_partial",
                agent_id=resp_entry.agent_id,
                response_text=resp_entry.response_text,
                cached_action=resp_entry.cached_action,
                entry=resp_entry,
                similarity=resp_similarity,
            )

        # 4. Complete miss -- do not surface a cross-tier similarity (COR-3).
        # Mixing routing vs response similarities was misleading in the trace UI;
        # downstream consumers already treat ``similarity is None`` as N/A.
        return CacheResult(hit_type="miss", similarity=None)

    def store_routing(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        condensed_task: str = "",
        *,
        language: str = "en",
    ) -> None:
        """Store a routing decision after an agent handles a request."""
        self._routing_cache.store(
            query_text,
            agent_id,
            confidence,
            condensed_task,
            language=language,
        )

    async def store_routing_async(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        condensed_task: str = "",
        *,
        language: str = "en",
    ) -> None:
        """Async wrapper around ``store_routing`` that offloads the ChromaDB
        write to a worker thread so the event loop is not blocked (PERF-4)."""
        await asyncio.to_thread(
            self.store_routing,
            query_text,
            agent_id,
            confidence,
            condensed_task,
            language=language,
        )

    def store_response(self, entry: ResponseCacheEntry) -> None:
        """Store a full response after successful execution."""
        if not self._response_cache_enabled:
            return
        self._response_cache.store(entry)

    async def store_response_async(self, entry: ResponseCacheEntry) -> None:
        """Async wrapper around ``store_response`` that offloads the
        ChromaDB write to a worker thread so the event loop is not
        blocked (PERF-4)."""
        await asyncio.to_thread(self.store_response, entry)

    def invalidate_response(self, entry_id: str) -> None:
        """Reactive invalidation -- remove a response entry on action failure."""
        self._response_cache.invalidate(entry_id)

    def invalidate_routing(self, entry_id: str) -> None:
        """Reactive invalidation -- remove a routing entry after unsafe reuse."""
        self._routing_cache.invalidate(entry_id)

    def flush(self, tier: str | None = None) -> None:
        """Clear one or both cache tiers. Used by admin UI.

        Args:
            tier: ``"routing"``, ``"action"`` (canonical, since 0.21.0),
                ``"response"`` (legacy alias for ``"action"``), or
                ``None`` for both tiers.
        """
        if tier not in (None, "routing", "action", "response"):
            raise ValueError(f"unknown cache tier {tier!r}")
        # Normalise the new canonical "action" value to the internal
        # "response" name so the rest of this method stays unchanged.
        if tier == "action":
            tier = "response"
        if tier is None or tier == "routing":
            self._routing_cache.prepare_for_flush()
            count = self._vector_store.count(COLLECTION_ROUTING_CACHE)
            if count > 0:
                all_data = self._vector_store.get(COLLECTION_ROUTING_CACHE, include=[])
                if all_data["ids"]:
                    self._vector_store.delete(COLLECTION_ROUTING_CACHE, ids=all_data["ids"])
            logger.info("Routing cache flushed")
        if tier is None or tier == "response":
            # P3-4: invalidate in-flight stores BEFORE delete so a
            # concurrent worker thread cannot upsert into the
            # collection we are about to clear.
            self._response_cache.prepare_for_flush()
            count = self._vector_store.count(COLLECTION_RESPONSE_CACHE)
            if count > 0:
                all_data = self._vector_store.get(COLLECTION_RESPONSE_CACHE, include=[])
                if all_data["ids"]:
                    self._vector_store.delete(COLLECTION_RESPONSE_CACHE, ids=all_data["ids"])
            logger.info("Action cache flushed")

    def flush_pending(self) -> None:
        """Flush buffered hit-count updates (call at shutdown)."""
        self._routing_cache.flush_pending()
        self._response_cache.flush_pending()

    def get_stats(self) -> dict:
        """Return combined stats for both tiers."""
        return {
            "routing": self._routing_cache.get_stats(),
            "action": self._response_cache.get_stats(),
        }

    async def purge_readonly_entries(self) -> int:
        """Purge stale read-only response cache entries (no cached action).

        Returns the number of purged entries.
        """
        return await asyncio.to_thread(self._response_cache.purge_readonly_entries)
