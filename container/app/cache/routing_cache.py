"""Routing skip cache tier for intent-to-agent decisions."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from app.cache._base_cache import (
    _BaseCache,
    _extract_single,
    _normalize_language,
    _parse_entity_ids,
    _store_method,
    make_text_id,
)
from app.cache.sqlite_cache_store import COLLECTION_ROUTING_CACHE, SqliteCacheStore
from app.defaults import CACHE_DEFAULTS
from app.models.cache import RoutingCacheEntry

logger = logging.getLogger(__name__)

_ROUTING_CACHE_SCHEMA_VERSION = 5

# Conservative default for the semantic routing tier: reuses the project's
# historical routing threshold (no production data exists yet to tune it;
# tighten via the cache.routing.semantic_threshold setting only after
# reviewing semantic_hit / semantic_invalid telemetry).
_SEMANTIC_THRESHOLD_DEFAULT = float(CACHE_DEFAULTS["cache.routing.semantic_threshold"])

# k-NN width for the semantic tier. Candidates are post-filtered by language
# and threshold in Python, so k must be wide enough to survive filtering.
_SEMANTIC_K = 8


def make_routing_entry_id(query_text: str, *, language: str = "en") -> str:
    return make_text_id(query_text, language)


def _parse_entity_candidates(raw: object) -> list[tuple[str, str, float]]:
    """Parse the JSON ``entity_candidates`` metadata key into tuples.

    Defensive: any malformed payload degrades to ``[]`` (Tier 1), never
    crashes. Items must be list/tuple of len 3 with a non-empty str id;
    name is coerced to str, score to float.
    """
    if not raw:
        return []
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    candidates: list[tuple[str, str, float]] = []
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        entity_id, name, score = item
        if not isinstance(entity_id, str) or not entity_id:
            continue
        try:
            candidates.append((entity_id, str(name or ""), float(score or 0.0)))
        except (TypeError, ValueError):
            continue
    return candidates


class RoutingCache(_BaseCache[RoutingCacheEntry]):
    """Stores routing decisions keyed by raw user text + language."""

    def __init__(self, cache_store: SqliteCacheStore) -> None:
        super().__init__(
            cache_store,
            collection_name=COLLECTION_ROUTING_CACHE,
            default_max_entries=50000,
        )
        self._exact_match_only: bool = True
        # Semantic tier (P4): disabled until load_config applies the DB
        # setting (default enabled there). _exact_match_only is derived from
        # the effective flag in load_config.
        self._semantic_enabled: bool = False
        self._semantic_threshold: float = _SEMANTIC_THRESHOLD_DEFAULT

    async def load_config(self) -> None:
        await self._load_common_config(
            enabled_key="cache.routing.enabled",
            enabled_default=True,
            max_entries_key="cache.routing.max_entries",
            max_entries_default=50000,
        )
        semantic_raw = await self._get_setting(
            "cache.routing.semantic_enabled",
            "true" if CACHE_DEFAULTS["cache.routing.semantic_enabled"] else "false",
        )
        self._semantic_enabled = self._coerce_bool(
            semantic_raw,
            bool(CACHE_DEFAULTS["cache.routing.semantic_enabled"]),
        )
        threshold_raw = await self._get_setting(
            "cache.routing.semantic_threshold",
            str(_SEMANTIC_THRESHOLD_DEFAULT),
        )
        self._semantic_threshold = self._coerce_float(threshold_raw, _SEMANTIC_THRESHOLD_DEFAULT)
        self._exact_match_only = not self._semantic_enabled

    def semantic_available(self) -> bool:
        """True when the semantic tier can actually run for this instance.

        Requires the tier to be enabled and the backing store to provide a
        loaded sqlite-vec extension (SqliteCacheStore). Stores without vec
        support (legacy VectorStore doubles in tests) keep exact-only
        behavior.
        """
        if not self._enabled or not self._semantic_enabled:
            return False
        if _store_method(self._store, "search_routing_embeddings") is None:
            return False
        return bool(getattr(self._store, "vec_available", False))

    async def reload_config(self) -> None:
        await self.load_config()

    def lookup(
        self,
        query_text: str,
        *,
        language: str = "en",
    ) -> tuple[RoutingCacheEntry | None, float | None]:
        _entry_id, entry, similarity = self._lookup_common(query_text, language=language)
        if entry is None:
            return None, None
        return entry, similarity

    def lookup_with_id(
        self,
        query_text: str,
        *,
        language: str = "en",
    ) -> tuple[str | None, RoutingCacheEntry | None, float | None]:
        """Like lookup() but also returns the computed entry_id."""
        entry_id, entry, similarity = self._lookup_common(query_text, language=language)
        if entry is None:
            return entry_id, None, None
        return entry_id, entry, similarity

    def lookup_semantic(
        self,
        query_embedding: list[float],
        *,
        language: str = "en",
    ) -> tuple[str | None, RoutingCacheEntry | None, float | None]:
        """Semantic (k-NN cosine) routing lookup over stored query embeddings.

        Runs ONLY after an exact-hash miss. Pure embedding similarity --
        no keyword, substring, or regex matching anywhere (Directive 11).
        The tier decides only the target agent; the agent's own LLM call
        still resolves action parameters and entities from the original
        text. Candidates are post-filtered by language (embeddings are
        multilingual, so a German query must not hit an English entry) and
        by the configured cosine-similarity threshold.

        Returns ``(entry_id, entry, similarity)`` on hit, else ``(None, None, None)``.
        """
        if not self._enabled or not self._semantic_enabled:
            return None, None, None
        search = _store_method(self._store, "search_routing_embeddings")
        if search is None or not query_embedding:
            return None, None, None
        lang = _normalize_language(language)
        try:
            candidates = search(query_embedding, _SEMANTIC_K)
        except Exception:
            logger.warning("Routing semantic search failed", exc_info=True)
            return None, None, None
        for entry_id, distance in candidates:
            similarity = 1.0 - float(distance)
            if similarity < self._semantic_threshold:
                # k-NN results are ordered by ascending distance.
                break
            row = self._store.get(
                self._collection_name,
                ids=[entry_id],
                include=["metadatas", "documents"],
            )
            row_ids = row.get("ids") or []
            if not row_ids:
                continue  # stale vec row whose routing entry is gone
            meta = _extract_single(row.get("metadatas")) or {}
            if _normalize_language(meta.get("language")) != lang:
                continue
            entry = self._hydrate_hit(
                entry_id,
                _extract_single(row.get("documents")),
                meta,
                similarity=similarity,
            )
            if entry is not None:
                return entry_id, entry, similarity
        return None, None, None

    def get_stats(self) -> dict[str, object]:
        stats = super().get_stats()
        stats["exact_match_only"] = self._exact_match_only
        stats["semantic_enabled"] = self._semantic_enabled
        stats["semantic_threshold"] = self._semantic_threshold
        return stats

    def store(
        self,
        entry: RoutingCacheEntry | None = None,
        *,
        query_text: str | None = None,
        language: str = "en",
        agent_id: str | None = None,
        entity_ids: list[str] | None = None,
        entity_candidates: list[tuple[str, str, float]] | None = None,
        confidence: float = 0.0,
        embedding: list[float] | None = None,
    ) -> None:
        if entry is None:
            if query_text is None or agent_id is None:
                raise ValueError("RoutingCache.store requires either an entry or full routing-cache fields")
            # Keep entity_ids a superset of the candidate ids: the
            # invalidation scan matches only the entity_ids metadata key.
            merged_ids = list(dict.fromkeys([*(entity_ids or []), *(c[0] for c in entity_candidates or [])]))
            entry = RoutingCacheEntry(
                query_text=query_text,
                language=language,
                agent_id=agent_id,
                entity_ids=merged_ids,
                entity_candidates=entity_candidates or [],
                confidence=confidence,
            )
        super().store(entry, embedding=embedding)

    def store_embedding(self, entry_id: str, embedding: list[float]) -> bool:
        """Store (or refresh) the embedding for an existing entry.

        Used by the lazy backfill path: entries stored before the semantic
        tier existed get their embedding on the next exact-hash hit.
        """
        if not embedding:
            return False
        writer = _store_method(self._store, "store_routing_embedding")
        if writer is None:
            return False
        try:
            return bool(writer(entry_id, embedding))
        except Exception:
            logger.warning("Failed to store routing embedding for %s", entry_id, exc_info=True)
            return False

    def has_embedding(self, entry_id: str) -> bool:
        """True when the entry already has a stored embedding."""
        checker = _store_method(self._store, "has_routing_embedding")
        if checker is None:
            return True
        try:
            return bool(checker(entry_id))
        except Exception:
            logger.debug("Routing embedding presence check failed", exc_info=True)
            return True

    @staticmethod
    def make_entry_id(query_text: str, *, language: str = "en") -> str:
        return make_routing_entry_id(query_text, language=language)

    def _serialize_metadata(self, entry: RoutingCacheEntry) -> dict:
        now = datetime.now(UTC).isoformat()
        created_at = entry.created_at or now
        last_accessed = entry.last_accessed or created_at
        return {
            "agent_id": entry.agent_id,
            "language": _normalize_language(entry.language),
            "confidence": str(entry.confidence),
            "entity_ids": json.dumps(entry.entity_ids or []),
            "entity_candidates": json.dumps([list(c) for c in entry.entity_candidates or []]),
            "created_at": created_at,
            "last_accessed": last_accessed,
            "hit_count": str(entry.hit_count),
            "schema_version": str(_ROUTING_CACHE_SCHEMA_VERSION),
        }

    def _deserialize_entry(self, document: str, metadata: dict, *, similarity: float) -> RoutingCacheEntry | None:
        return RoutingCacheEntry(
            query_text=document,
            language=metadata.get("language", "en"),
            agent_id=metadata.get("agent_id", ""),
            confidence=self._coerce_float(metadata.get("confidence"), 0.0),
            entity_ids=_parse_entity_ids(metadata.get("entity_ids")),
            entity_candidates=_parse_entity_candidates(metadata.get("entity_candidates")),
            created_at=metadata.get("created_at") or None,
            last_accessed=metadata.get("last_accessed") or None,
            hit_count=self._coerce_int(metadata.get("hit_count"), 0),
            schema_version=self._coerce_int(metadata.get("schema_version"), _ROUTING_CACHE_SCHEMA_VERSION),
        )
