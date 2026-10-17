"""Unified cache manager with action replay and routing skip tiers."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.analytics.collector import track_cache_event_background, track_rewrite
from app.analytics.tracer import _optional_span
from app.cache._base_cache import _parse_entity_ids, _store_method
from app.cache.action_cache import _ACTION_CACHE_SCHEMA_VERSION, ActionCache
from app.cache.embedding import get_embedding_engine
from app.cache.routing_cache import RoutingCache, make_routing_entry_id
from app.cache.sqlite_cache_store import COLLECTION_ACTION_CACHE, COLLECTION_ROUTING_CACHE, SqliteCacheStore
from app.models.cache import ActionCacheEntry, CachedAction, RoutingCacheEntry
from app.security.sensitive_actions import SensitiveActionBlocked
from app.util.tasks import spawn

logger = logging.getLogger(__name__)

# Served-entry token prefix for semantic routing hits. A semantic hit serves a
# NEIGHBOUR entry stored for different wording; when the turn it routed fails,
# the neighbour itself is not proven wrong (it may route its own wording
# correctly), so invalidating the token suppresses that neighbour for this
# exact query instead of deleting it.
SEMANTIC_ENTRY_TOKEN_PREFIX = "semantic:"
_MAX_SEMANTIC_SUPPRESSIONS = 4096

# Action-cache deletions are logged per row at INFO. Deleting more rows than
# this in one operation also logs one WARNING summary, which survives restarts
# in the persisted warning log.
_BULK_ACTION_DELETION_WARN_THRESHOLD = 3
# Deletion reasons that depend on entity-index content. While the index has
# not loaded its first HA snapshot such a rejection is a metadata gap, not
# proof the row is stale: the row is kept (and still never replayed).
_INDEX_DEPENDENT_DELETION_REASONS = frozenset({"visibility_rejected"})
_MAX_LOGGED_BULK_KEYS = 20


def make_semantic_entry_token(query_entry_id: str, neighbour_entry_id: str) -> str:
    return f"{SEMANTIC_ENTRY_TOKEN_PREFIX}{query_entry_id}:{neighbour_entry_id}"


def parse_semantic_entry_token(token: str) -> tuple[str, str] | None:
    """Return ``(query_entry_id, neighbour_entry_id)`` for a semantic token, else None."""
    if not token or not token.startswith(SEMANTIC_ENTRY_TOKEN_PREFIX):
        return None
    query_entry_id, sep, neighbour_entry_id = token[len(SEMANTIC_ENTRY_TOKEN_PREFIX) :].partition(":")
    if not sep or not query_entry_id or not neighbour_entry_id:
        return None
    return query_entry_id, neighbour_entry_id


def _engine_model_id(engine) -> str | None:
    """The engine's ``model_id`` when it is a real string (test doubles may lack it)."""
    model_id = getattr(engine, "model_id", None)
    return model_id if isinstance(model_id, str) and model_id else None


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
    rewrite_failed: bool = False
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
    rewrite_failed: bool = False
    original_response_text: str | None = None


@dataclass
class ActionReplayRejected:
    """An action-cache row that was found but not replayed.

    ``reason`` is the explicit action-tier miss reason recorded on the
    ``cache_lookup`` span (``origin_mismatch``, ``legacy_schema``,
    ``invalid_command``, ``context_dependent``, ``agent_unavailable``,
    ``visibility_rejected``, ``visibility_deferred_index_not_ready``,
    ``replay_failed``). ``details`` carries
    trace-safe span metadata for the reason (e.g. expected vs actual
    area id, the rejected entity id). ``allow_routing_skip`` is True only
    for reasons that keep the historical fall-through to the routing tier
    (visibility rejection or deferral, replay failure); every other reason forces live
    classification.
    """

    entry_id: str | None
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    allow_routing_skip: bool = False


@dataclass
class RoutingSkipOutcome:
    kind: str
    # Served-entry handle passed back to ``invalidate_routing`` when the
    # routed turn fails. Equals the row id for exact hits; a semantic token
    # (see ``make_semantic_entry_token``) for semantic hits.
    entry_id: str
    agent_id: str
    condensed_task: str
    similarity: float
    language: str = "en"
    entity_ids: list[str] = field(default_factory=list)
    lookup_ms: float | None = None
    # The routing row that produced the hit (the neighbour for semantic hits).
    source_entry_id: str = ""


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
        # query entry id -> neighbour routing entry ids that misrouted it.
        self._semantic_suppressions: OrderedDict[str, set[str]] = OrderedDict()
        self._semantic_suppressions_lock = threading.Lock()
        # Action-cache deletions by reason since process start (cache stats).
        self._action_deletions: Counter[str] = Counter()
        self._action_deletions_deferred: int = 0
        self._deletion_stats_lock = threading.Lock()
        # Returns False while the entity index has not loaded its first HA
        # snapshot (registered by the entity bootstrap). None = always ready.
        self._entity_index_ready_probe: Callable[[], bool] | None = None

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
            purged = await asyncio.to_thread(
                self._action_cache.purge_entries_without_language,
            )
            self._record_action_deletions("startup_missing_language", [], count=purged)
            await asyncio.to_thread(
                self._routing_cache.purge_legacy_schema_entries,
                5,
            )
            # Action rows below the current schema are purged at startup;
            # the lookup path also rejects any that slip through (import).
            purged = await asyncio.to_thread(
                self._action_cache.purge_legacy_schema_entries,
                _ACTION_CACHE_SCHEMA_VERSION,
            )
            self._record_action_deletions(
                "startup_legacy_schema",
                [],
                count=purged,
                detail=f"schema_version<{_ACTION_CACHE_SCHEMA_VERSION}",
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
        origin_area_id: str | None = None,
        origin_device_id: str | None = None,
        check_visibility,
        execute_cached_action,
        span_collector=None,
        check_agent: Callable[[str], Awaitable[bool]] | None = None,
    ) -> ActionReplayOutcome | ActionReplayRejected | None:
        """Attempt to replay a cached action after current-turn validation.

        ``check_agent`` (optional) confirms the cached owning agent is still
        registered/enabled -- the same gate the routing tier applies.
        ``origin_area_id`` is compared against area-bound rows only;
        ``origin_device_id`` is accepted for caller compatibility and is
        never a replay binding (schema 6). A found-but-rejected row returns
        an :class:`ActionReplayRejected` carrying the explicit reason.
        """
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
            # A row that cannot deserialize is distinct from an absent key.
            # It may be a legacy command payload and must force a live turn,
            # never hand the utterance to routing-cache skip.
            if entry_id is not None:
                await self._invalidate_action_entry(entry_id, reason="invalid_command")
                return ActionReplayRejected(entry_id=entry_id, reason="invalid_command")
            return None

        # Context-dependent turns (follow-up answers, anaphora resolved via
        # last_entities) are never stored; a row that still carries the flag
        # (e.g. imported) is removed and forces a live turn.
        if entry.context_dependent:
            await self._invalidate_action_entry(entry_id, reason="context_dependent", entity_ids=entry.entity_ids)
            return ActionReplayRejected(entry_id=entry_id, reason="context_dependent")

        # The current version introduced executor-audited commands and
        # explicit origin provenance.  Old action rows cannot prove either,
        # so delete only that action-cache row and force a live turn.  This
        # must not degrade to a routing-cache skip for the same utterance.
        if entry.schema_version != _ACTION_CACHE_SCHEMA_VERSION or not entry.origin_provenance:
            await self._invalidate_action_entry(entry_id, reason="legacy_schema", entity_ids=entry.entity_ids)
            return ActionReplayRejected(entry_id=entry_id, reason="legacy_schema")

        # Origin binding (schema 6): a global row (``origin_required`` False)
        # replays for every origin -- any satellite, any area, or none. A row
        # whose target entity was selected through the learning turn's origin
        # area is bound to that area and replays only on turns from the same
        # area. Rows are never bound to a device: ``origin_device_id`` is
        # neither stored as a binding nor compared here.
        if entry.origin_required:
            if not entry.origin_area_id:
                # An area-bound row without an area can never replay.
                await self._invalidate_action_entry(entry_id, reason="origin_mismatch", entity_ids=entry.entity_ids)
                return ActionReplayRejected(
                    entry_id=entry_id,
                    reason="origin_mismatch",
                    details={"expected_area_id": None, "actual_area_id": origin_area_id},
                )
            if entry.origin_area_id != origin_area_id:
                # Not invalidated: the row stays valid for its own area.
                return ActionReplayRejected(
                    entry_id=entry_id,
                    reason="origin_mismatch",
                    details={"expected_area_id": entry.origin_area_id, "actual_area_id": origin_area_id},
                )

        if not self._cached_command_is_structurally_valid(entry.cached_action):
            await self._invalidate_action_entry(entry_id, reason="invalid_command", entity_ids=entry.entity_ids)
            return ActionReplayRejected(entry_id=entry_id, reason="invalid_command")

        # Re-validation: check visibility for every entity referenced by the
        # cached entry, not just the primary action target.
        cached_agent_id = entry.agent_id if entry.agent_id is not None else requesting_agent_id
        if check_agent is not None:
            try:
                agent_ok = bool(await check_agent(cached_agent_id))
            except Exception:
                logger.warning("Action cache agent check failed", exc_info=True)
                agent_ok = False
            if not agent_ok:
                await self._invalidate_action_entry(entry_id, reason="agent_unavailable", entity_ids=entry.entity_ids)
                track_cache_event_background(tier="action", hit_type="miss")
                return ActionReplayRejected(entry_id=entry_id, reason="agent_unavailable")
        entity_ids_to_check = [
            eid for eid in dict.fromkeys([entry.cached_action.entity_id, *(entry.entity_ids or [])]) if eid
        ]
        try:
            visibility_results = await asyncio.gather(
                *[check_visibility(cached_agent_id, eid) for eid in entity_ids_to_check]
            )
            rejected_entity_ids = [
                eid for eid, visible in zip(entity_ids_to_check, visibility_results, strict=False) if not visible
            ]
        except Exception:
            logger.warning("Action cache visibility recheck failed", exc_info=True)
            rejected_entity_ids = list(entity_ids_to_check) or [""]
        if rejected_entity_ids:
            # Directive 2: the row is invalidated and the turn runs live.
            # While the entity index has not loaded its first snapshot the
            # helper keeps the row (metadata gap, not proof of staleness);
            # the replay is refused either way.
            deleted = await self._invalidate_action_entry(
                entry_id, reason="visibility_rejected", entity_ids=rejected_entity_ids
            )
            deferred = not deleted and not self.entity_index_ready
            reject_reason = "visibility_deferred_index_not_ready" if deferred else "visibility_rejected"
            logger.info(
                "Action cache entry %s rejected by visibility recheck (agent=%s entity=%s reason=%s)",
                entry_id,
                cached_agent_id,
                rejected_entity_ids[0],
                reject_reason,
            )
            track_cache_event_background(tier="action", hit_type="miss")
            return ActionReplayRejected(
                entry_id=entry_id,
                reason=reject_reason,
                details={"entity_id": rejected_entity_ids[0]},
                allow_routing_skip=True,
            )

        # The ha_action span wraps the ACTUAL replay REST call here (it used
        # to be emitted downstream in finalize_action_replay_hit, where it
        # wrapped nothing because the replay had already happened).
        async with _optional_span(span_collector, "ha_action", agent_id=cached_agent_id) as ha_span:
            ha_span["metadata"]["action"] = entry.cached_action.service
            ha_span["metadata"]["entity"] = entry.cached_action.entity_id
            ha_span["metadata"]["cached"] = True
            approval_blocked = False
            replay_error: BaseException | None = None
            try:
                replay_result = await execute_cached_action(entry.cached_action)
            except SensitiveActionBlocked:
                # Sensitive actions need an approval on every request: drop
                # the entry (e.g. stored before the approval gate existed)
                # so the live pipeline asks for it.
                logger.info("Action cache entry %s holds a sensitive action; invalidated", entry_id)
                ha_span["metadata"]["approval_blocked"] = True
                await self._invalidate_action_entry(
                    entry_id, reason="sensitive", entity_ids=[entry.cached_action.entity_id]
                )
                approval_blocked = True
                replay_result = None
            except Exception as exc:
                replay_error = exc
                replay_result = None
            ha_span["metadata"]["success"] = replay_result is not None
        if replay_result is None:
            details: dict[str, Any] = {"entity_id": entry.cached_action.entity_id}
            if approval_blocked:
                details["approval_blocked"] = True
            else:
                # The row is kept: a failed REST call is usually transient.
                logger.warning(
                    "Action cache replay failed for entry %s (service=%s entity=%s); falling through",
                    entry_id,
                    entry.cached_action.service,
                    entry.cached_action.entity_id,
                    exc_info=replay_error,
                )
            track_cache_event_background(tier="action", hit_type="miss")
            return ActionReplayRejected(
                entry_id=entry_id,
                reason="replay_failed",
                details=details,
                allow_routing_skip=True,
            )

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

    async def _invalidate_action_entry(
        self,
        entry_id: str | None,
        *,
        reason: str = "replay_rejected",
        entity_ids: list[str] | None = None,
    ) -> bool:
        """Delete one action row found by a lookup; returns True when deleted.

        Every deletion is logged with ``reason``. An index-dependent rejection
        (``visibility_rejected``) while the entity index has not loaded its
        first snapshot keeps the row: the caller still refuses the replay, and
        the next lookup rechecks visibility against the loaded index.
        """
        if entry_id is None:
            return False
        if reason in _INDEX_DEPENDENT_DELETION_REASONS and not self.entity_index_ready:
            with self._deletion_stats_lock:
                self._action_deletions_deferred += 1
            logger.info(
                "Action cache row kept: reason=%s key=%s entity_ids=%s (entity index not loaded yet)",
                reason,
                entry_id,
                entity_ids or [],
            )
            return False
        try:
            await asyncio.to_thread(self._action_cache.invalidate_by_entry_id, entry_id)
        except Exception:
            logger.warning("Action cache row delete failed: reason=%s key=%s", reason, entry_id, exc_info=True)
            return False
        self._record_action_deletions(reason, [(entry_id, None, list(entity_ids or []))])
        return True

    def set_entity_index_readiness_probe(self, probe: Callable[[], bool] | None) -> None:
        """Register the entity-index readiness signal (see ``entity_index_ready``)."""
        self._entity_index_ready_probe = probe

    @property
    def entity_index_ready(self) -> bool:
        """False while the entity index has not loaded its first HA snapshot.

        Without a registered probe the index is treated as ready, which keeps
        the fail-closed delete-on-rejection behaviour. A failing probe counts
        as not ready: rows are kept, replay is still refused.
        """
        probe = self._entity_index_ready_probe
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception:
            logger.debug("Entity index readiness probe failed", exc_info=True)
            return False

    def _record_action_deletions(
        self,
        reason: str,
        rows: list[tuple[str, str | None, list[str]]],
        *,
        count: int | None = None,
        detail: str = "",
    ) -> None:
        """Count and log action-cache deletions.

        ``rows`` are ``(key, query_text, entity_ids)`` of the deleted rows when
        known; ``count`` overrides ``len(rows)`` for bulk paths that only
        report a number. Each known row logs at INFO; more than
        ``_BULK_ACTION_DELETION_WARN_THRESHOLD`` rows at once also log one
        WARNING summary so the event survives restarts.
        """
        deleted = len(rows) if count is None else (count if isinstance(count, int) else 0)
        if deleted <= 0:
            return
        with self._deletion_stats_lock:
            self._action_deletions[reason] += deleted
        suffix = f" ({detail})" if detail else ""
        for entry_id, query_text, entity_ids in rows:
            logger.info(
                "Action cache row deleted: reason=%s key=%s query=%r entity_ids=%s%s",
                reason,
                entry_id,
                query_text,
                entity_ids,
                suffix,
            )
        if not rows:
            logger.info("Action cache rows deleted: reason=%s count=%d%s", reason, deleted, suffix)
        if deleted > _BULK_ACTION_DELETION_WARN_THRESHOLD:
            keys = [row[0] for row in rows[:_MAX_LOGGED_BULK_KEYS]]
            logger.warning(
                "Bulk action-cache deletion: %d rows, reason=%s%s keys=%s",
                deleted,
                reason,
                suffix,
                keys,
            )

    def _snapshot_action_rows(
        self,
        *,
        entry_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
    ) -> list[tuple[str, str | None, list[str]]] | None:
        """Read ``(key, query_text, entity_ids)`` of action rows about to be deleted.

        Rows are selected by ``entry_ids`` or, through the store's indexed
        entity sidecar, by ``entity_ids``. Returns None when the store cannot
        answer (no sidecar, read error); deletion then proceeds and is logged
        by count only.
        """
        try:
            if entry_ids is None:
                finder = _store_method(self._cache_store, "find_entries_by_entity_ids")
                if finder is None or not entity_ids:
                    return None
                entry_ids = list(finder(COLLECTION_ACTION_CACHE, entity_ids) or [])
            if not entry_ids:
                return []
            page = self._cache_store.get(
                COLLECTION_ACTION_CACHE, ids=list(entry_ids), include=["metadatas", "documents"]
            )
            ids = page.get("ids") or []
            documents = page.get("documents") or []
            metadatas = page.get("metadatas") or []
        except Exception:
            logger.debug("Could not snapshot action rows before deletion", exc_info=True)
            return None
        rows: list[tuple[str, str | None, list[str]]] = []
        for index, row_id in enumerate(ids):
            document = documents[index] if index < len(documents) else None
            meta = (metadatas[index] if index < len(metadatas) else None) or {}
            rows.append((str(row_id), document, _parse_entity_ids(meta.get("entity_ids"))))
        return rows

    def get_deletion_stats(self) -> dict[str, Any]:
        """Action-cache deletions by reason since process start."""
        with self._deletion_stats_lock:
            return {
                "deletions": dict(self._action_deletions),
                "deletions_deferred_index_not_ready": self._action_deletions_deferred,
            }

    @staticmethod
    def _cached_command_is_structurally_valid(cached_action: CachedAction) -> bool:
        """Reject structurally malformed cached commands before any HA call."""
        service = str(cached_action.service or "")
        if "/" not in service or not cached_action.entity_id or not isinstance(cached_action.service_data, dict):
            return False
        domain, action = service.split("/", 1)
        if not domain or not action:
            return False
        # The executor owns the authoritative alias map.  This only verifies
        # that a persisted canonical service targets the same HA domain as its
        # entity, without recreating that mapping in the cache layer.
        entity_domain = cached_action.entity_id.split(".", 1)[0]
        return domain == entity_domain

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
            self._schedule_semantic_backfill(entry_id, query_text, entry.embedding_model)
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
                lookup_ms=(time.perf_counter() - t0) * 1000,
                source_entry_id=entry_id or "",
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
                query_entry_id = make_routing_entry_id(query_text, language=language)
                try:
                    engine = await get_embedding_engine()
                    query_embedding = await engine.embed(query_text)
                    sem_id, sem_entry, sem_similarity = await asyncio.to_thread(
                        self._routing_cache.lookup_semantic,
                        query_embedding,
                        language=language,
                        embedding_model=_engine_model_id(engine),
                        exclude_ids=self._suppressed_neighbours(query_entry_id),
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
                        entry_id=make_semantic_entry_token(query_entry_id, sem_id) if sem_id else "",
                        agent_id=sem_entry.agent_id,
                        condensed_task=query_text,
                        similarity=sem_similarity,
                        language=sem_entry.language,
                        entity_ids=sem_entry.entity_ids or [],
                        lookup_ms=(time.perf_counter() - t0) * 1000,
                        source_entry_id=sem_id or "",
                    )

        track_cache_event_background(tier="routing", hit_type="miss")
        return None

    def _schedule_semantic_backfill(
        self,
        entry_id: str | None,
        query_text: str,
        embedding_model: str | None = None,
    ) -> None:
        """Schedule a background embedding backfill for an entry lacking a current one."""
        if not entry_id or not self._routing_cache.semantic_available():
            return
        if entry_id in self._backfill_inflight:
            return
        self._backfill_inflight.add(entry_id)
        spawn(
            self._backfill_routing_embedding(entry_id, query_text, embedding_model),
            name="routing-embedding-backfill",
        )

    async def _backfill_routing_embedding(
        self,
        entry_id: str,
        query_text: str,
        embedding_model: str | None = None,
    ) -> None:
        """Embed an existing routing entry's query text and store the vector.

        Re-embeds when the entry has no vector or its vector came from a
        different embedding model than the current engine's.
        """
        try:
            engine = await get_embedding_engine()
            model_id = _engine_model_id(engine)
            if embedding_model == model_id and await asyncio.to_thread(self._routing_cache.has_embedding, entry_id):
                return
            embedding = await engine.embed(query_text)
            await asyncio.to_thread(
                self._routing_cache.store_embedding,
                entry_id,
                embedding,
                model_id=model_id,
            )
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
        a single LLM call.

        When rewriting is disabled, or the rewrite fails or returns nothing,
        the stored original agent speech (``original_response_text``; legacy
        rows without it fall back to ``response_text``) is returned -- never
        mediated speech, which can carry per-turn additions or the requesting
        person's name. The caller appends the current turn's reminder itself.
        A failed attempt leaves ``rewrite_applied`` False and sets
        ``rewrite_failed``.
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
        except Exception:
            logger.warning("Rewrite failed, using the cached original agent speech", exc_info=True)
            rewritten = None
        rewrite_ms = (time.perf_counter() - t0) * 1000
        result.rewrite_latency_ms = rewrite_ms
        if rewritten:
            result.response_text = rewritten
            result.rewrite_applied = True
            result.rewrite_failed = False
            result.original_response_text = source_text
            await track_rewrite(latency_ms=rewrite_ms, success=True)
            return rewritten
        result.rewrite_applied = False
        result.rewrite_failed = True
        await track_rewrite(latency_ms=rewrite_ms, success=False)
        return fallback_text

    def store_routing(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """Store a routing decision after dispatch or read-only handling."""
        entry = RoutingCacheEntry(
            query_text=query_text,
            language=language,
            agent_id=agent_id,
            confidence=confidence,
            entity_ids=list(entity_ids or []),
            embedding_model=embedding_model if embedding else None,
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
    ) -> None:
        self.store_routing(
            query_text,
            agent_id,
            confidence,
            language=language,
            entity_ids=entity_ids,
        )

    async def store_routing_async(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
    ) -> None:
        """Async wrapper around ``store_routing``.

        Computes the query embedding up front (CPU-bound encode is offloaded
        off the event loop inside the engine, Directive 9) so the vec row is
        written in the same store transaction. An embedding failure degrades
        to an exact-only entry -- the routing cache keeps working.
        """
        embedding: list[float] | None = None
        embedding_model: str | None = None
        if self._routing_cache.semantic_available():
            try:
                engine = await get_embedding_engine()
                embedding = await engine.embed(query_text)
                embedding_model = _engine_model_id(engine)
            except Exception:
                logger.warning("Routing embedding compute failed; storing without embedding", exc_info=True)
        await asyncio.to_thread(
            self.store_routing,
            query_text,
            agent_id,
            confidence,
            language=language,
            entity_ids=entity_ids,
            embedding=embedding,
            embedding_model=embedding_model,
        )

    async def store_routing_only_async(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        *,
        language: str = "en",
        entity_ids: list[str] | None = None,
    ) -> None:
        await self.store_routing_async(
            query_text,
            agent_id,
            confidence,
            language=language,
            entity_ids=entity_ids,
        )

    def store_action(self, entry: ActionCacheEntry) -> None:
        self._action_cache.store(entry)

    async def store_action_async(self, entry: ActionCacheEntry) -> None:
        await asyncio.to_thread(self.store_action, entry)

    def invalidate_action(
        self,
        entry_id: str,
        *,
        expected_created_at: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Delete an action row; with ``expected_created_at`` only while it is unchanged.

        Background maintenance passes the ``created_at`` of its snapshot so a
        row re-stored by a live turn after the snapshot is kept. ``reason``
        defaults to ``validator`` for snapshot deletes and ``manual_delete``
        otherwise (admin API).
        """
        reason = reason or ("validator" if expected_created_at is not None else "manual_delete")
        rows = self._snapshot_action_rows(entry_ids=[entry_id])
        if expected_created_at is not None:
            deleted = self._action_cache.invalidate_if_unchanged(entry_id, expected_created_at=expected_created_at)
        else:
            deleted = self._action_cache.invalidate_by_entry_id(entry_id)
        if deleted and rows != []:
            self._record_action_deletions(reason, rows or [(entry_id, None, [])])
        return deleted

    def invalidate_routing(self, entry_id: str) -> None:
        """Invalidate a served routing entry.

        Exact-hit handles delete the row. A semantic-hit token never deletes
        the neighbour row it borrowed (that row may route its own wording
        correctly); the neighbour is suppressed for the failing query text
        instead, so the next identical query re-classifies live.
        """
        parsed = parse_semantic_entry_token(entry_id)
        if parsed is not None:
            self._suppress_semantic_neighbour(*parsed)
            return
        self._routing_cache.invalidate_by_entry_id(entry_id)

    def _suppress_semantic_neighbour(self, query_entry_id: str, neighbour_entry_id: str) -> None:
        with self._semantic_suppressions_lock:
            neighbours = self._semantic_suppressions.setdefault(query_entry_id, set())
            neighbours.add(neighbour_entry_id)
            self._semantic_suppressions.move_to_end(query_entry_id)
            while len(self._semantic_suppressions) > _MAX_SEMANTIC_SUPPRESSIONS:
                self._semantic_suppressions.popitem(last=False)

    def _suppressed_neighbours(self, query_entry_id: str) -> frozenset[str]:
        with self._semantic_suppressions_lock:
            return frozenset(self._semantic_suppressions.get(query_entry_id, ()))

    async def invalidate_by_entity_id(
        self,
        entity_ids,
        *,
        reason: str = "entity_changed",
        detail: str = "",
    ) -> dict[str, int]:
        """Delete every action and routing row that references ``entity_ids``.

        ``reason`` / ``detail`` describe the trigger (e.g. the registry event)
        and are logged with each deleted action row.
        """
        unique_ids = [str(entity_id) for entity_id in dict.fromkeys(entity_ids or []) if entity_id]
        if not unique_ids:
            return {"action": 0, "routing": 0}

        def _invalidate(cache) -> int:
            # Pass the full id set so the underlying scan paginates the
            # collection once instead of N times for N entity ids.
            return cache.invalidate_by_entity_id(unique_ids)

        def _invalidate_action() -> tuple[list[tuple[str, str | None, list[str]]] | None, int]:
            rows = self._snapshot_action_rows(entity_ids=unique_ids)
            return rows, _invalidate(self._action_cache)

        (action_rows, action_count), routing_count = await asyncio.gather(
            asyncio.to_thread(_invalidate_action),
            asyncio.to_thread(_invalidate, self._routing_cache),
        )
        trigger = f"trigger_entity_ids={unique_ids}"
        self._record_action_deletions(
            reason,
            action_rows or [],
            count=action_count,
            detail=f"{detail}; {trigger}" if detail else trigger,
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

    def flush(self, tier: str | None = None, *, reason: str = "flush") -> None:
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
                self._record_action_deletions(reason, [], count=count, detail=f"tier={tier or 'all'}")
            logger.info("Action cache flushed")

    def flush_pending(self) -> None:
        """Flush buffered hit-count updates (call at shutdown)."""
        self._routing_cache.flush_pending()
        self._action_cache.flush_pending()

    def get_stats(self) -> dict[str, Any]:
        """Return combined stats for both tiers (blocking COUNT queries)."""
        action_stats = self._action_cache.get_stats()
        if isinstance(action_stats, dict):
            action_stats = {**action_stats, **self.get_deletion_stats()}
        return {
            "routing": self._routing_cache.get_stats(),
            "action": action_stats,
        }

    async def get_stats_async(self) -> dict[str, Any]:
        """``get_stats`` off the event loop (Directive 9)."""
        return await asyncio.to_thread(self.get_stats)

    async def purge_readonly_entries(self) -> int:
        """Purge legacy read-only rows that should now live in routing cache."""
        purged = await asyncio.to_thread(self._action_cache.purge_readonly_entries)
        self._record_action_deletions("startup_readonly_purge", [], count=purged)
        return purged

    def iter_action_entries(self, *, page_size: int = 1000):
        """Yield action-cache entries, paginating through the underlying store."""
        return self._action_cache.iterate_entries(page_size=page_size)

    async def update_action_entry(self, entry: ActionCacheEntry) -> bool:
        """Persist validator results for a snapshot entry (compare-and-swap).

        Only ``response_text``, ``original_response_text`` and
        ``validated_at`` are written, and only while the row still exists
        with the snapshot's ``created_at``. A row deleted meanwhile is never
        resurrected, and a row re-stored by a live turn is never reverted to
        the stale snapshot. Returns True when the row was updated.
        """
        entry_id = self._action_cache.make_entry_id(entry.query_text, language=entry.language)
        patch = {
            "response_text": entry.response_text,
            "original_response_text": entry.original_response_text or "",
            "validated_at": entry.validated_at or "",
        }
        updated = await asyncio.to_thread(
            self._action_cache.patch_if_unchanged,
            entry_id,
            expected_created_at=entry.created_at,
            patch=patch,
        )
        if not updated:
            logger.debug("Skipped stale validator update for action entry %s", entry_id)
        return updated
