"""Routing cache tier for intent-to-agent routing decisions."""

from __future__ import annotations

import hashlib
import heapq
import logging
import re
from datetime import UTC, datetime

from app.cache._state import _CacheState
from app.cache.vector_store import COLLECTION_ROUTING_CACHE, VectorStore
from app.db.repository import SettingsRepository
from app.models.cache import RoutingCacheEntry

logger = logging.getLogger(__name__)

# P1-3: pagination batch for ``_enforce_lru``. Large enough to amortise
# Chroma-side overhead, small enough that the transient memory footprint
# stays bounded even on very large collections (50k+ entries).
_LRU_PAGE_SIZE = 1000
# Only actually run the LRU sweep when the collection is past this
# fraction of the configured max; below that the eviction work is
# pure overhead since no entry would be dropped.
_LRU_TRIGGER_FRACTION = 0.95

# Defensive: pre-fix entries written by an older parser may still carry
# an embedded classification fragment in their condensed_task (e.g.
# ``"climate-agent (96%): living room temperature"``). Reject those on
# lookup so corrupted entries self-heal as the LLM is asked to
# re-classify.
_CORRUPTED_CONDENSED_RE = re.compile(
    r"\b[\w-]+\s*\(\s*\d+\s*%?\s*\)\s*:\s*",
)


def make_routing_entry_id(query_text: str, *, language: str = "en") -> str:
    """Return the deterministic routing-cache key for a query/language pair."""
    lang = (language or "en").lower()
    return hashlib.sha256(f"{lang}\n{query_text}".encode()).hexdigest()[:16]


def _condensed_task_is_corrupted(text: str | None) -> bool:
    if not text:
        return False
    return _CORRUPTED_CONDENSED_RE.search(text) is not None


class RoutingCache:
    """Routing cache tier mapping user text to agent routing decisions."""

    def __init__(self, vector_store: VectorStore) -> None:
        self._store = vector_store
        self._threshold: float = 0.92
        self._max_entries: int = 50000
        self._eviction_interval: int = 100
        self._flush_interval: int = 5
        # FLOW-MED-1 / P1-3: counters, pending map, and invalidation
        # generation all live in ``_state``; every mutation happens
        # under its lock. ChromaDB I/O itself stays outside the lock.
        self._state = _CacheState()

    def prepare_for_flush(self) -> None:
        """Invalidate in-flight routing writes before vector store delete.

        Admin ``flush`` clears Chroma; without this, a worker thread still
        running :meth:`store` could ``upsert`` after delete and repopulate
        the routing tier so the next request immediately hits ``routing_hit``.
        """
        self._state.invalidate()

    async def load_config(self) -> None:
        """Load thresholds from settings table."""
        self._threshold = float(await SettingsRepository.get_value("cache.routing.threshold", "0.92"))
        self._max_entries = int(await SettingsRepository.get_value("cache.routing.max_entries", "50000"))

    async def reload_config(self) -> None:
        """Reload thresholds from DB without restart."""
        await self.load_config()

    def lookup(
        self,
        query_text: str,
        *,
        language: str = "en",
    ) -> tuple[RoutingCacheEntry | None, float | None]:
        """Query routing cache. Returns (entry, similarity).

        ChromaDB returns distance (0=identical). Similarity = 1 - distance.
        Returns the best similarity score even on a miss.

        FLOW-HIGH-4: scopes the vector query to entries with matching
        language metadata so cross-language hits cannot leak.
        """
        lang = (language or "en").lower()
        result = self._store.query(
            COLLECTION_ROUTING_CACHE,
            query_texts=[query_text],
            n_results=1,
            where={"language": lang},
            include=["metadatas", "distances", "documents"],
        )
        if not result["ids"] or not result["ids"][0]:
            return (None, None)

        distance = result["distances"][0][0]
        similarity = 1.0 - distance

        if similarity < self._threshold:
            return (None, similarity)

        meta = result["metadatas"][0][0]
        entry_id = result["ids"][0][0]
        condensed_task = meta.get("condensed_task")
        if _condensed_task_is_corrupted(condensed_task):
            logger.warning(
                "Routing cache entry %s rejected: corrupted condensed_task=%s",
                entry_id,
                repr((condensed_task or "")[:200]),
            )
            return (None, similarity)
        now = datetime.now(UTC).isoformat()
        hit_count = int(meta.get("hit_count", 0)) + 1
        should_flush = self._state.record_pending_update(
            entry_id,
            result["documents"][0][0],
            {**meta, "last_accessed": now, "hit_count": str(hit_count)},
            self._flush_interval,
        )
        if should_flush:
            self._flush_pending_updates()

        return (
            RoutingCacheEntry(
                query_text=result["documents"][0][0],
                agent_id=meta["agent_id"],
                confidence=similarity,
                hit_count=hit_count,
                condensed_task=meta.get("condensed_task"),
                created_at=meta.get("created_at"),
                last_accessed=now,
                language=meta.get("language", "en"),
            ),
            similarity,
        )

    def store(
        self,
        query_text: str,
        agent_id: str,
        confidence: float,
        condensed_task: str = "",
        *,
        language: str = "en",
    ) -> None:
        """Store a new routing decision in the cache."""
        gen_at_start = self._state.current_generation()
        if self._state.record_store(self._eviction_interval):
            self._enforce_lru()
        now = datetime.now(UTC).isoformat()
        lang = (language or "en").lower()
        # FLOW-HIGH-4: prefix the key with language so identical text
        # in different languages produces distinct entries.
        entry_id = make_routing_entry_id(query_text, language=lang)
        self._flush_pending_updates()
        if not self._state.matches_generation(gen_at_start):
            logger.info("Skipping routing store -- cache was flushed during write")
            return
        self._store.upsert(
            COLLECTION_ROUTING_CACHE,
            ids=[entry_id],
            documents=[query_text],
            metadatas=[
                {
                    "agent_id": agent_id,
                    "confidence": str(confidence),
                    "hit_count": "0",
                    "condensed_task": condensed_task,
                    "created_at": now,
                    "last_accessed": now,
                    "language": lang,
                }
            ],
        )

    def invalidate(self, entry_id: str) -> None:
        """Remove one routing entry and drop any queued metadata updates for it."""
        self._state.discard_pending(entry_id)
        self._store.delete(COLLECTION_ROUTING_CACHE, ids=[entry_id])
        logger.info("Routing cache entry invalidated: %s", entry_id)

    def _enforce_lru(self) -> None:
        """Evict oldest entries if collection exceeds max_entries.

        P1-3: paginate through Chroma instead of loading the entire
        collection in one ``get()`` call, and only keep
        ``(last_accessed, id)`` pairs. ``heapq.nsmallest`` caps memory
        at roughly ``overage`` tuples plus one page of metadata at a
        time, so the sweep stays bounded even on very large
        collections.
        """
        self._flush_pending_updates()
        count = self._store.count(COLLECTION_ROUTING_CACHE)
        if count <= int(self._max_entries * _LRU_TRIGGER_FRACTION):
            return
        if count <= self._max_entries:
            return
        overage = count - self._max_entries + int(self._max_entries * 0.1)

        def _iter_all():
            offset = 0
            while True:
                page = self._store.get(
                    COLLECTION_ROUTING_CACHE,
                    include=["metadatas"],
                    limit=_LRU_PAGE_SIZE,
                    offset=offset,
                )
                ids = page.get("ids") or []
                if not ids:
                    return
                metas = page.get("metadatas") or []
                for entry_id, meta in zip(ids, metas, strict=False):
                    yield ((meta or {}).get("last_accessed", ""), entry_id)
                if len(ids) < _LRU_PAGE_SIZE:
                    return
                offset += _LRU_PAGE_SIZE

        oldest = heapq.nsmallest(overage, _iter_all(), key=lambda pair: pair[0])
        to_delete = [pair[1] for pair in oldest]
        if to_delete:
            for i in range(0, len(to_delete), 500):
                self._store.delete(COLLECTION_ROUTING_CACHE, ids=to_delete[i : i + 500])
            logger.info("Routing cache LRU evicted %d entries", len(to_delete))

    def _flush_pending_updates(self) -> None:
        """Batch-flush pending hit count updates to ChromaDB (metadata only)."""
        pending = self._state.swap_pending()
        if not pending:
            return
        ids = list(pending.keys())
        metas = [pending[i][1] for i in ids]
        try:
            self._store.update_metadata(COLLECTION_ROUTING_CACHE, ids=ids, metadatas=metas)
        except Exception:
            # P1-3: do not drop updates on Chroma failure; re-queue so
            # the next flush retries. Newer updates for the same id
            # that arrived in the meantime take precedence.
            self._state.requeue_failed(pending)
            logger.warning("Failed to flush routing cache hit updates; re-queued", exc_info=True)

    def flush_pending(self) -> None:
        """Public flush for shutdown hook."""
        self._flush_pending_updates()

    def purge_entries_without_language(self) -> int:
        """Remove entries missing the ``language`` metadata field.

        FLOW-HIGH-4 migration: pre-0.18.0 entries have no ``language``
        field and their keys were not language-scoped. Since the new
        lookup filters on ``language`` they would be unreachable
        anyway; purge them to keep the collection tidy. Returns the
        number of purged entries.
        """
        all_data = self._store.get(
            COLLECTION_ROUTING_CACHE,
            include=["metadatas"],
        )
        if not all_data["ids"]:
            return 0
        to_delete = [
            eid
            for eid, meta in zip(all_data["ids"], all_data["metadatas"], strict=False)
            if not (meta or {}).get("language")
        ]
        if to_delete:
            for i in range(0, len(to_delete), 500):
                self._store.delete(COLLECTION_ROUTING_CACHE, ids=to_delete[i : i + 500])
            logger.info(
                "Routing cache: purged %d pre-0.18.0 entries without language metadata",
                len(to_delete),
            )
        return len(to_delete)

    def get_stats(self) -> dict:
        """Return routing cache stats."""
        return {
            "count": self._store.count(COLLECTION_ROUTING_CACHE),
            "max_entries": self._max_entries,
            "threshold": self._threshold,
        }
