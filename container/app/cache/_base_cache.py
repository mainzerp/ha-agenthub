"""Shared cache-tier primitives for vector-backed query caches."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel

from app.cache._state import _CacheState
from app.db.repository import SettingsRepository

logger = logging.getLogger(__name__)

TEntry = TypeVar("TEntry", bound=BaseModel)

_LRU_PAGE_SIZE = 5000
_LRU_TRIGGER_FRACTION = 0.95
_MAX_LEGACY_WARNING_KEYS = 1000
_LEGACY_WARNING_KEYS: deque[tuple[str, str]] = deque(maxlen=_MAX_LEGACY_WARNING_KEYS)
_WHITESPACE_RE = re.compile(r"\s+")
_ZERO_EMBEDDING_DIM = 384


def _warn_legacy_key_once(legacy_key: str, canonical_key: str) -> None:
    pair = (legacy_key, canonical_key)
    if pair in _LEGACY_WARNING_KEYS:
        return
    _LEGACY_WARNING_KEYS.append(pair)
    logger.warning("Using legacy cache setting %s; migrate to %s", legacy_key, canonical_key)


def _normalize_language(language: str | None) -> str:
    return (language or "en").strip().lower() or "en"


def normalize_text(text: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", (text or "").strip()).casefold()
    return normalized.rstrip(".!?;:,")


def make_text_id(text: str, language: str) -> str:
    payload = f"{normalize_text(text)}\n{_normalize_language(language)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _extract_single(value):
    if not value:
        return None
    if isinstance(value, list) and value and isinstance(value[0], list):
        inner = value[0]
        return inner[0] if inner else None
    if isinstance(value, list):
        return value[0]
    return value


def _parse_entity_ids(raw: object) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            return [item for item in text.split(",") if item]
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
    return []


class _BaseCache[TEntry](ABC):
    """Shared storage, LRU, and metadata-flush behavior for cache tiers."""

    def __init__(
        self,
        cache_store: Any,
        *,
        collection_name: str,
        default_max_entries: int,
    ) -> None:
        # Accepts VectorStore (entity_index) or SqliteCacheStore (routing/action) —
        # duck-typed on the cache-relevant methods: upsert, get, delete, count,
        # update_metadata, delete_oldest.
        self._store = cache_store
        self._collection_name = collection_name
        self._enabled: bool = True
        self._max_entries: int = default_max_entries
        self._eviction_interval: int = 100
        self._lru_trigger_fraction: float = _LRU_TRIGGER_FRACTION
        self._flush_interval: int = 5
        self._state = _CacheState()
        self._exact_match_only: bool = False

    @staticmethod
    def _coerce_bool(raw: str | None, default: bool) -> bool:
        if raw is None:
            return default
        normalized = str(raw).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    @staticmethod
    def _coerce_int(raw: str | None, default: int) -> int:
        try:
            return int(str(raw)) if raw is not None else default
        except Exception:
            return default

    @staticmethod
    def _coerce_float(raw: str | None, default: float) -> float:
        try:
            return float(str(raw)) if raw is not None else default
        except Exception:
            return default

    async def _get_setting(
        self,
        key: str,
        default: str | None,
        *,
        legacy_keys: tuple[str, ...] = (),
    ) -> str | None:
        value = await SettingsRepository.get_value(key, None)
        if value is not None:
            return value
        for legacy_key in legacy_keys:
            legacy_value = await SettingsRepository.get_value(legacy_key, None)
            if legacy_value is not None:
                _warn_legacy_key_once(legacy_key, key)
                return legacy_value
        return default

    async def _load_common_config(
        self,
        *,
        enabled_key: str,
        enabled_default: bool,
        max_entries_key: str,
        max_entries_default: int,
        legacy_enabled_keys: tuple[str, ...] = (),
        legacy_max_entries_keys: tuple[str, ...] = (),
    ) -> None:
        enabled_raw = await self._get_setting(
            enabled_key,
            "true" if enabled_default else "false",
            legacy_keys=legacy_enabled_keys,
        )
        max_entries_raw = await self._get_setting(
            max_entries_key,
            str(max_entries_default),
            legacy_keys=legacy_max_entries_keys,
        )
        self._enabled = self._coerce_bool(enabled_raw, enabled_default)
        self._max_entries = self._coerce_int(max_entries_raw, max_entries_default)
        trigger_raw = await self._get_setting("cache.lru.trigger_fraction", "0.95")
        self._lru_trigger_fraction = self._coerce_float(trigger_raw, _LRU_TRIGGER_FRACTION)
        interval_raw = await self._get_setting("cache.lru.eviction_interval", "100")
        self._eviction_interval = self._coerce_int(interval_raw, 100)

    def prepare_for_flush(self) -> None:
        self._state.invalidate()

    def flush_pending(self) -> None:
        self._flush_pending_updates()

    def store(self, entry: TEntry) -> None:
        if not self._enabled:
            return
        generation = self._state.current_generation()
        if self._state.record_store(self._eviction_interval):
            self._enforce_lru()
        self._flush_pending_updates()
        if not self._state.matches_generation(generation):
            logger.info("Skipping %s cache store after flush invalidation", self._collection_name)
            return
        entry_id = self.make_entry_id(entry.query_text, language=getattr(entry, "language", "en"))  # type: ignore[attr-defined]
        self._store.upsert(
            self._collection_name,
            ids=[entry_id],
            documents=[entry.query_text],  # type: ignore[attr-defined]
            embeddings=None,
            metadatas=[self._serialize_metadata(entry)],
        )

    def invalidate_by_entry_id(self, entry_id: str) -> bool:
        self._state.invalidate()
        self._state.discard_pending(entry_id)
        self._store.delete(self._collection_name, ids=[entry_id])
        return True

    def invalidate_by_entity_id(self, entity_ids: Iterable[str]) -> int:
        targets = {str(entity_id).strip().lower() for entity_id in entity_ids if entity_id}
        if not targets:
            return 0
        to_delete: list[str] = []
        offset = 0
        while True:
            page = self._store.get(
                self._collection_name,
                include=["metadatas"],
                limit=_LRU_PAGE_SIZE,
                offset=offset,
            )
            ids = page.get("ids") or []
            if not ids:
                break
            metas = page.get("metadatas") or []
            for entry_id, meta in zip(ids, metas, strict=False):
                row_entity_ids = _parse_entity_ids((meta or {}).get("entity_ids"))
                if targets.intersection({value.strip().lower() for value in row_entity_ids if value}):
                    to_delete.append(entry_id)
            if len(ids) < _LRU_PAGE_SIZE:
                break
            offset += _LRU_PAGE_SIZE
        if not to_delete:
            return 0
        for entry_id in to_delete:
            self._state.discard_pending(entry_id)
        for start in range(0, len(to_delete), 500):
            self._store.delete(self._collection_name, ids=to_delete[start : start + 500])
        logger.debug(
            "%s invalidated %d entries for entity_ids=%s",
            self.__class__.__name__,
            len(to_delete),
            sorted(targets),
        )
        return len(to_delete)

    def count(self) -> int:
        return self._store.count(self._collection_name)

    def get_rows(self, *, include: list[str], limit: int | None = None, offset: int | None = None) -> dict:
        return self._store.get(
            self._collection_name,
            include=include,
            limit=limit,
            offset=offset,
        )

    def purge_entries_without_language(self) -> int:
        page = self._store.get(self._collection_name, include=["metadatas"])
        ids = page.get("ids") or []
        metas = page.get("metadatas") or []
        to_delete = [entry_id for entry_id, meta in zip(ids, metas, strict=False) if not (meta or {}).get("language")]
        if not to_delete:
            return 0
        for start in range(0, len(to_delete), 500):
            self._store.delete(self._collection_name, ids=to_delete[start : start + 500])
        return len(to_delete)

    def purge_legacy_schema_entries(self, min_schema_version: int) -> int:
        page = self._store.get(self._collection_name, include=["metadatas"])
        ids = page.get("ids") or []
        metas = page.get("metadatas") or []
        to_delete: list[str] = []
        for entry_id, meta in zip(ids, metas, strict=False):
            schema_raw = (meta or {}).get("schema_version")
            try:
                schema_version = int(schema_raw or 0)
            except Exception:
                schema_version = 0
            if schema_version < min_schema_version:
                to_delete.append(entry_id)
        if not to_delete:
            return 0
        for start in range(0, len(to_delete), 500):
            self._store.delete(self._collection_name, ids=to_delete[start : start + 500])
        return len(to_delete)

    def get_stats(self) -> dict[str, object]:
        return {
            "count": self.count(),
            "enabled": self._enabled,
            "max_entries": self._max_entries,
        }

    def _lookup_common(
        self,
        query_text: str,
        *,
        language: str = "en",
    ) -> tuple[str | None, TEntry | None, float | None]:
        """Exact-match hash-based cache lookup.

        Computes a deterministic SHA-256 hash from ``(normalized_text, language)``
        and queries the vector store by ID. This is an O(1) key-value lookup --
        no semantic similarity or vector distance comparison is performed.

        Returns ``(entry_id, entry, 1.0)`` on cache hit, or ``(None, None, None)`` on miss.
        """
        if not self._enabled:
            return None, None, None
        lang = _normalize_language(language)
        exact_id = self.make_entry_id(query_text, language=lang)
        exact = self._store.get(
            self._collection_name,
            ids=[exact_id],
            include=["metadatas", "documents"],
        )
        exact_ids = exact.get("ids") or []
        if exact_ids:
            entry = self._hydrate_hit(
                exact_id,
                _extract_single(exact.get("documents")),
                _extract_single(exact.get("metadatas")),
                similarity=1.0,
            )
            if entry is not None:
                return exact_id, entry, 1.0

        return None, None, None

    def _hydrate_hit(
        self,
        entry_id: str,
        document: str | None,
        metadata: dict | None,
        *,
        similarity: float,
    ) -> TEntry | None:
        meta = dict(metadata or {})
        if not meta:
            return None
        now = datetime.now(UTC).isoformat()
        hit_count = self._coerce_int(str(meta.get("hit_count", 0)), 0) + 1
        meta["last_accessed"] = now
        meta["hit_count"] = str(hit_count)
        should_flush = self._state.record_pending_update(
            entry_id,
            document or "",
            meta,
            self._flush_interval,
        )
        if should_flush:
            self._flush_pending_updates()
        return self._deserialize_entry(document or "", meta, similarity=similarity)

    def _enforce_lru(self) -> None:
        self._flush_pending_updates()
        count = self._store.count(self._collection_name)
        trigger = int(self._max_entries * self._lru_trigger_fraction)
        if count <= trigger:
            return
        target = int(self._max_entries * 0.9)
        overage = count - target
        if overage <= 0:
            return
        deleted = 0
        if hasattr(self._store, "delete_oldest"):
            deleted = self._store.delete_oldest(self._collection_name, overage)
        else:
            # Fallback for VectorStore (entity_index): page scan
            to_delete: list[str] = []
            offset = 0
            while len(to_delete) < overage:
                page = self._store.get(
                    self._collection_name,
                    include=["metadatas"],
                    limit=_LRU_PAGE_SIZE,
                    offset=offset,
                )
                ids = page.get("ids") or []
                if not ids:
                    break
                metas = page.get("metadatas") or []
                for entry_id, _meta in zip(ids, metas, strict=False):
                    to_delete.append(entry_id)
                if len(ids) < _LRU_PAGE_SIZE:
                    break
                offset += _LRU_PAGE_SIZE
            to_delete = to_delete[:overage]
            for entry_id in to_delete:
                self._state.discard_pending(entry_id)
            for start in range(0, len(to_delete), 500):
                self._store.delete(self._collection_name, ids=to_delete[start : start + 500])
            deleted = len(to_delete)
        logger.info("%s LRU evicted %d entries", self.__class__.__name__, deleted)

    def _flush_pending_updates(self) -> None:
        pending = self._state.swap_pending()
        if not pending:
            return
        ids = list(pending.keys())
        metas = [pending[entry_id][1] for entry_id in ids]
        try:
            self._store.update_metadata(self._collection_name, ids=ids, metadatas=metas)
        except Exception:
            self._state.requeue_failed(pending)
            logger.warning("Failed to flush %s cache metadata updates; re-queued", self._collection_name, exc_info=True)

    @staticmethod
    @abstractmethod
    def make_entry_id(query_text: str, *, language: str = "en") -> str:
        raise NotImplementedError

    @abstractmethod
    def _serialize_metadata(self, entry: TEntry) -> dict:
        raise NotImplementedError

    @abstractmethod
    def _deserialize_entry(self, document: str, metadata: dict, *, similarity: float) -> TEntry | None:
        raise NotImplementedError
