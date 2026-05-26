"""Semantic action replay cache tier."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime

from app.cache._base_cache import _BaseCache, _normalize_language, _parse_entity_ids, make_text_id
from app.cache.vector_store import COLLECTION_ACTION_CACHE, VectorStore
from app.models.cache import ActionCacheEntry, CachedAction

logger = logging.getLogger(__name__)

_ACTION_CACHE_SCHEMA_VERSION = 4


def make_action_entry_id(query_text: str, *, language: str = "en") -> str:
    return make_text_id(query_text, language)


def _as_cached_action(value) -> CachedAction | None:
    if value is None or value == "":
        return None
    if isinstance(value, CachedAction):
        return value
    if isinstance(value, str):
        try:
            return CachedAction.model_validate_json(value)
        except Exception:
            logger.debug("Failed to parse cached action metadata", exc_info=True)
            return None
    if isinstance(value, dict):
        try:
            return CachedAction.model_validate(value)
        except Exception:
            logger.debug("Failed to validate cached action metadata", exc_info=True)
            return None
    return None


def _is_readonly_service_name(service_name: str) -> bool:
    if not service_name:
        return False
    action = service_name.split("/", 1)[1] if "/" in service_name else service_name
    action = action.strip().lower()
    return action.startswith(("query_", "list_"))


def _is_readonly_action(action) -> bool:
    cached_action = _as_cached_action(action)
    if cached_action is None:
        return True
    return _is_readonly_service_name(cached_action.service)


class ActionCache(_BaseCache[ActionCacheEntry]):
    """Stores replayable action results keyed by raw user text + language."""

    def __init__(self, vector_store: VectorStore) -> None:
        super().__init__(
            vector_store,
            collection_name=COLLECTION_ACTION_CACHE,
            default_max_entries=50000,
        )
        self._semantic_threshold: float = 0.95

    async def load_config(self) -> None:
        await self._load_common_config(
            enabled_key="cache.action.enabled",
            enabled_default=True,
            max_entries_key="cache.action.max_entries",
            max_entries_default=50000,
            legacy_enabled_keys=("cache.response.enabled",),
            legacy_max_entries_keys=("cache.response.max_entries",),
        )
        threshold_raw = await self._get_setting(
            "cache.action.semantic_threshold",
            "0.95",
            legacy_keys=("cache.response.threshold",),
        )
        self._semantic_threshold = self._coerce_float(threshold_raw, 0.95)

    async def reload_config(self) -> None:
        await self.load_config()

    def lookup(
        self,
        query_text: str,
        *,
        language: str = "en",
    ) -> tuple[ActionCacheEntry | None, float | None]:
        _entry_id, entry, similarity = self._lookup_common(query_text, language=language)
        if entry is None or similarity is None:
            return None, similarity
        if similarity < self._semantic_threshold:
            return None, similarity
        if entry.cached_action is None:
            return None, similarity
        return entry, similarity

    def lookup_with_id(
        self,
        query_text: str,
        *,
        language: str = "en",
    ) -> tuple[str | None, ActionCacheEntry | None, float | None]:
        """Like lookup() but also returns the computed entry_id."""
        entry_id, entry, similarity = self._lookup_common(query_text, language=language)
        if entry is None or similarity is None:
            return entry_id, None, similarity
        if similarity < self._semantic_threshold:
            return entry_id, None, similarity
        if entry.cached_action is None:
            return entry_id, None, similarity
        return entry_id, entry, similarity

    def purge_readonly_entries(self) -> int:
        page = self._store.get(COLLECTION_ACTION_CACHE, include=["metadatas"])
        ids = page.get("ids") or []
        metas = page.get("metadatas") or []
        to_delete = [
            entry_id
            for entry_id, meta in zip(ids, metas, strict=False)
            if _is_readonly_action((meta or {}).get("cached_action"))
        ]
        if not to_delete:
            return 0
        for start in range(0, len(to_delete), 500):
            self._store.delete(COLLECTION_ACTION_CACHE, ids=to_delete[start : start + 500])
        return len(to_delete)

    def iterate_entries(
        self,
        *,
        page_size: int = 1000,
        include: list[str] | None = None,
    ) -> Iterator[ActionCacheEntry]:
        """Yield all action-cache entries, paginating through ChromaDB."""
        _include = include or ["documents", "metadatas"]
        offset = 0
        while True:
            page = self._store.get(
                COLLECTION_ACTION_CACHE,
                include=_include,
                limit=page_size,
                offset=offset,
            )
            ids = page.get("ids") or []
            documents = page.get("documents") or []
            metas = page.get("metadatas") or []
            if not ids:
                break
            for _entry_id, document, meta in zip(ids, documents, metas, strict=False):
                entry = self._deserialize_entry(document or "", meta or {}, similarity=1.0)
                if entry is not None:
                    yield entry
            if len(ids) < page_size:
                break
            offset += page_size

    def get_stats(self) -> dict[str, object]:
        stats = super().get_stats()
        stats["semantic_threshold"] = self._semantic_threshold
        return stats

    def store(
        self,
        entry: ActionCacheEntry | None = None,
        *,
        query_text: str | None = None,
        language: str = "en",
        agent_id: str | None = None,
        condensed_task: str | None = None,
        cached_action: CachedAction | None = None,
        response_text: str | None = None,
        entity_ids: list[str] | None = None,
        origin_area_id: str | None = None,
        origin_device_id: str | None = None,
        confidence: float = 0.0,
    ) -> None:
        if entry is None:
            if query_text is None or agent_id is None or cached_action is None or response_text is None:
                raise ValueError("ActionCache.store requires either an entry or full action-cache fields")
            entry = ActionCacheEntry(
                query_text=query_text,
                language=language,
                agent_id=agent_id,
                condensed_task=condensed_task,
                confidence=confidence,
                response_text=response_text,
                cached_action=cached_action,
                entity_ids=entity_ids or ([cached_action.entity_id] if cached_action.entity_id else []),
                origin_area_id=origin_area_id,
                origin_device_id=origin_device_id,
            )
        super().store(entry)

    @staticmethod
    def make_entry_id(query_text: str, *, language: str = "en") -> str:
        return make_action_entry_id(query_text, language=language)

    def _serialize_metadata(self, entry: ActionCacheEntry) -> dict:
        now = datetime.now(UTC).isoformat()
        created_at = entry.created_at or now
        last_accessed = entry.last_accessed or created_at
        entity_ids = entry.entity_ids or ([entry.cached_action.entity_id] if entry.cached_action.entity_id else [])
        return {
            "agent_id": entry.agent_id,
            "language": _normalize_language(entry.language),
            "condensed_task": entry.condensed_task or "",
            "confidence": str(entry.confidence),
            "response_text": entry.response_text,
            "cached_action": entry.cached_action.model_dump_json(),
            "entity_ids": json.dumps(entity_ids),
            "origin_area_id": entry.origin_area_id or "",
            "origin_device_id": entry.origin_device_id or "",
            "created_at": created_at,
            "last_accessed": last_accessed,
            "executed_at": entry.executed_at or created_at,
            "hit_count": str(entry.hit_count),
            "schema_version": str(_ACTION_CACHE_SCHEMA_VERSION),
            "original_response_text": entry.original_response_text or "",
            "rewrite_applied": str(entry.rewrite_applied).lower(),
            "rewrite_latency_ms": str(entry.rewrite_latency_ms or ""),
            "validated_at": entry.validated_at or "",
        }

    def _deserialize_entry(self, document: str, metadata: dict, *, similarity: float) -> ActionCacheEntry | None:
        cached_action = _as_cached_action(metadata.get("cached_action"))
        if cached_action is None:
            return None
        return ActionCacheEntry(
            query_text=document,
            language=metadata.get("language", "en"),
            agent_id=metadata.get("agent_id", ""),
            condensed_task=metadata.get("condensed_task") or None,
            confidence=similarity,
            response_text=metadata.get("response_text", ""),
            cached_action=cached_action,
            entity_ids=_parse_entity_ids(metadata.get("entity_ids")),
            origin_area_id=metadata.get("origin_area_id") or None,
            origin_device_id=metadata.get("origin_device_id") or None,
            created_at=metadata.get("created_at") or None,
            last_accessed=metadata.get("last_accessed") or None,
            executed_at=metadata.get("executed_at") or None,
            hit_count=self._coerce_int(metadata.get("hit_count"), 0),
            schema_version=self._coerce_int(metadata.get("schema_version"), _ACTION_CACHE_SCHEMA_VERSION),
            original_response_text=metadata.get("original_response_text") or None,
            rewrite_applied=self._coerce_bool(metadata.get("rewrite_applied"), False),
            rewrite_latency_ms=self._coerce_float(metadata.get("rewrite_latency_ms"), 0.0),
            validated_at=metadata.get("validated_at") or None,
        )
