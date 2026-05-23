"""Unified cache manager with action replay and routing skip tiers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.analytics.collector import track_cache_event, track_rewrite
from app.cache.action_cache import ActionCache
from app.cache.routing_cache import RoutingCache
from app.cache.vector_store import COLLECTION_ACTION_CACHE, COLLECTION_ROUTING_CACHE, VectorStore
from app.models.cache import ActionCacheEntry, CachedAction, RoutingCacheEntry

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


class CacheManager:
    """Coordinates routing skip and action replay cache tiers."""

    def __init__(
        self,
        vector_store: VectorStore,
        rewrite_agent=None,
    ) -> None:
        self._vector_store = vector_store
        self._routing_cache = RoutingCache(vector_store)
        self._action_cache = ActionCache(vector_store)
        self._rewrite_agent = rewrite_agent
        self._rewrite_enabled: bool = False

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
                4,
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
        resolve_entity,
        check_visibility,
        execute_cached_action,
    ) -> ActionReplayOutcome | None:
        """Attempt to replay a cached action after current-turn validation."""
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

        entity_id = entry.cached_action.entity_id
        if entity_id:
            # Re-validation: only check visibility, skip re-resolution.
            # The cached entity_id is already valid from the first run.
            try:
                visible = await check_visibility(
                    entry.agent_id if entry.agent_id is not None else requesting_agent_id, entity_id
                )
            except Exception:
                logger.warning("Action cache visibility recheck failed", exc_info=True)
                visible = False
            if not visible:
                with contextlib.suppress(Exception):
                    if entry_id is not None:
                        await asyncio.to_thread(self._action_cache.invalidate_by_entry_id, entry_id)
                return None

        try:
            replay_result = await execute_cached_action(entry.cached_action)
        except Exception:
            logger.warning("Cached action replay failed", exc_info=True)
            replay_result = None
        if replay_result is None:
            return None

        await track_cache_event(
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
        """Return a routing cache hit that can skip live classification."""
        try:
            entry_id, entry, similarity = await asyncio.to_thread(
                self._routing_cache.lookup_with_id,
                query_text,
                language=language,
            )
        except Exception:
            logger.warning("Routing cache lookup failed", exc_info=True)
            return None
        if entry is None or similarity is None:
            return None
        await track_cache_event(
            tier="routing",
            hit_type="routing_hit",
            agent_id=entry.agent_id,
            similarity=similarity,
        )
        return RoutingSkipOutcome(
            kind="routing_hit",
            entry_id=entry_id or "",
            agent_id=entry.agent_id,
            condensed_task=entry.condensed_task or entry.query_text,
            similarity=similarity,
            language=entry.language,
        )

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
        condensed_task: str,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
    ) -> None:
        """Store a routing decision after dispatch or read-only handling."""
        entry = RoutingCacheEntry(
            query_text=query_text,
            language=language,
            agent_id=agent_id,
            condensed_task=condensed_task,
            confidence=confidence,
            entity_ids=entity_ids or [],
        )
        self._routing_cache.store(entry)

    def store_routing_only(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        condensed_task: str,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
    ) -> None:
        self.store_routing(
            query_text,
            agent_id,
            confidence,
            condensed_task,
            language=language,
            entity_ids=entity_ids,
        )

    async def store_routing_async(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        condensed_task: str,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
    ) -> None:
        """Async wrapper around ``store_routing``."""
        await asyncio.to_thread(
            self.store_routing,
            query_text,
            agent_id,
            confidence,
            condensed_task,
            language=language,
            entity_ids=entity_ids,
        )

    async def store_routing_only_async(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        condensed_task: str,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
    ) -> None:
        await self.store_routing_async(
            query_text,
            agent_id,
            confidence,
            condensed_task,
            language=language,
            entity_ids=entity_ids,
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
            count = self._vector_store.count(COLLECTION_ROUTING_CACHE)
            if count > 0:
                all_data = self._vector_store.get(COLLECTION_ROUTING_CACHE, include=[])
                if all_data["ids"]:
                    self._vector_store.delete(COLLECTION_ROUTING_CACHE, ids=all_data["ids"])
            logger.info("Routing cache flushed")
        if tier is None or tier == "action":
            self._action_cache.prepare_for_flush()
            count = self._vector_store.count(COLLECTION_ACTION_CACHE)
            if count > 0:
                all_data = self._vector_store.get(COLLECTION_ACTION_CACHE, include=[])
                if all_data["ids"]:
                    self._vector_store.delete(COLLECTION_ACTION_CACHE, ids=all_data["ids"])
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
