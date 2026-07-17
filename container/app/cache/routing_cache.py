"""Routing skip cache tier for intent-to-agent decisions."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from app.cache._base_cache import _BaseCache, _normalize_language, _parse_entity_ids, make_text_id
from app.cache.sqlite_cache_store import COLLECTION_ROUTING_CACHE, SqliteCacheStore
from app.models.cache import RoutingCacheEntry

logger = logging.getLogger(__name__)

_ROUTING_CACHE_SCHEMA_VERSION = 4


def make_routing_entry_id(query_text: str, *, language: str = "en") -> str:
    return make_text_id(query_text, language)


class RoutingCache(_BaseCache[RoutingCacheEntry]):
    """Stores routing decisions keyed by raw user text + language."""

    def __init__(self, cache_store: SqliteCacheStore) -> None:
        super().__init__(
            cache_store,
            collection_name=COLLECTION_ROUTING_CACHE,
            default_max_entries=50000,
        )
        self._exact_match_only: bool = True

    async def load_config(self) -> None:
        await self._load_common_config(
            enabled_key="cache.routing.enabled",
            enabled_default=True,
            max_entries_key="cache.routing.max_entries",
            max_entries_default=50000,
        )

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

    def get_stats(self) -> dict[str, object]:
        stats = super().get_stats()
        stats["exact_match_only"] = self._exact_match_only
        return stats

    def store(
        self,
        entry: RoutingCacheEntry | None = None,
        *,
        query_text: str | None = None,
        language: str = "en",
        agent_id: str | None = None,
        entity_ids: list[str] | None = None,
        confidence: float = 0.0,
    ) -> None:
        if entry is None:
            if query_text is None or agent_id is None:
                raise ValueError("RoutingCache.store requires either an entry or full routing-cache fields")
            entry = RoutingCacheEntry(
                query_text=query_text,
                language=language,
                agent_id=agent_id,
                entity_ids=entity_ids or [],
                confidence=confidence,
            )
        super().store(entry)

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
            created_at=metadata.get("created_at") or None,
            last_accessed=metadata.get("last_accessed") or None,
            hit_count=self._coerce_int(metadata.get("hit_count"), 0),
            schema_version=self._coerce_int(metadata.get("schema_version"), _ROUTING_CACHE_SCHEMA_VERSION),
        )
