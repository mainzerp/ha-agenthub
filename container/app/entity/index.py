"""Pre-embedded entity index using ChromaDB."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from functools import partial

from app.cache.vector_store import COLLECTION_ENTITY_INDEX, VectorStore
from app.models.entity_index import EntityIndexEntry

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

# 0.23.0: bump when EntityIndexEntry / embedding_text shape changes so
# stale ChromaDB collections (built before the new fields existed) are
# fully rebuilt on startup. Persisted under setting key
# ``entity_index.schema_version``.
#
# v3: adds ``content_hash`` to Chroma metadata so the WebSocket push
# path can short-circuit redundant re-embeddings when only the runtime
# state of an entity changes (its identity-bearing fields are
# unchanged). Existing v2 collections are dropped and rebuilt on first
# startup by ``_prime_entity_index``.
# v4: 0.23.0 area-id resolution fix populates entry.area /
# entry.area_name for production states (HA /api/states never carries
# area_id in attributes), changing content_hash for previously
# indexed entities; force a one-time rebuild.
# v5: index the runtime alarm fields (state, has_date, has_time) for
# input_datetime entities so AlarmMonitor can read them from EntityIndex.
INDEX_SCHEMA_VERSION = 5


class EntityIndex:
    """Pre-embedded entity index backed by ChromaDB."""

    def __init__(self, vector_store: VectorStore) -> None:
        self._store = vector_store
        self._last_refresh: str | None = None
        self._status: dict = {
            "state": "ready",
            "progress": 0,
            "total": 0,
            "processed": 0,
            "error": None,
        }
        self._sync_stats: dict = {
            "added": 0,
            "updated": 0,
            "removed": 0,
            "unchanged": 0,
            "last_sync": None,
            "last_sync_duration_ms": 0,
        }

    @staticmethod
    def _build_metadata(entry: EntityIndexEntry) -> dict:
        """Build ChromaDB metadata dict from an EntityIndexEntry."""
        import json as _json

        return {
            "friendly_name": entry.friendly_name,
            "domain": entry.domain,
            "area": entry.area or "",
            "area_name": entry.area_name or "",
            "device_class": entry.device_class or "",
            "aliases": _json.dumps(entry.aliases or []),
            "device_name": entry.device_name or "",
            "id_tokens": _json.dumps(entry.id_tokens or []),
            "state": entry.state or "",
            "has_date": "1" if entry.has_date else "0",
            "has_time": "1" if entry.has_time else "0",
            "content_hash": entry.content_hash,
        }

    @staticmethod
    def _stored_hash(meta: dict | None) -> str | None:
        """Extract the stored ``content_hash`` from Chroma metadata, if any."""
        if not meta:
            return None
        value = meta.get("content_hash")
        return value or None

    @staticmethod
    def _entry_from_metadata(entity_id: str, meta: dict) -> EntityIndexEntry:
        """Build an EntityIndexEntry from stored Chroma metadata."""
        import json as _json

        aliases_raw = meta.get("aliases", "") or ""
        id_tokens_raw = meta.get("id_tokens", "") or ""
        # Support both legacy comma-separated and new JSON-encoded lists.
        if aliases_raw.startswith("["):
            try:
                aliases = _json.loads(aliases_raw)
            except _json.JSONDecodeError:
                aliases = []
        else:
            aliases = [a for a in aliases_raw.split(",") if a]
        if id_tokens_raw.startswith("["):
            try:
                id_tokens = _json.loads(id_tokens_raw)
            except _json.JSONDecodeError:
                id_tokens = []
        else:
            id_tokens = [t for t in id_tokens_raw.split(",") if t]
        entry = EntityIndexEntry(
            entity_id=entity_id,
            friendly_name=meta.get("friendly_name", ""),
            domain=meta.get("domain", ""),
            area=meta.get("area", "") or None,
            area_name=meta.get("area_name", "") or None,
            device_class=meta.get("device_class", "") or None,
            aliases=aliases,
            device_name=meta.get("device_name", "") or None,
            id_tokens=id_tokens,
            state=meta.get("state", "") or None,
            has_date=str(meta.get("has_date", "0")) == "1",
            has_time=str(meta.get("has_time", "0")) == "1",
        )
        entry._content_hash = meta.get("content_hash") or None
        return entry

    def populate(self, entities: list[EntityIndexEntry]) -> None:
        """Bulk upsert all HA entities into the entity_index collection.

        Called at startup after fetching GET /api/states.
        """
        if not entities:
            return
        total = len(entities)
        self._status = {
            "state": "building",
            "progress": 0,
            "total": total,
            "processed": 0,
            "error": None,
        }
        try:
            for start in range(0, total, BATCH_SIZE):
                batch = entities[start : start + BATCH_SIZE]
                ids = [e.entity_id for e in batch]
                documents = [e.embedding_text for e in batch]
                metadatas = [self._build_metadata(e) for e in batch]
                self._store.upsert(
                    COLLECTION_ENTITY_INDEX,
                    ids=ids,
                    documents=documents,
                    metadatas=metadatas,
                )
                self._status["processed"] = min(start + len(batch), total)
                self._status["progress"] = int(self._status["processed"] / total * 100)
            self._last_refresh = datetime.now(UTC).isoformat()
            self._status["state"] = "ready"
            self._status["progress"] = 100
            logger.info("Entity index populated with %d entities", total)
        except Exception as exc:
            self._status["state"] = "error"
            self._status["error"] = str(exc)
            logger.error("Entity index populate failed: %s", exc)
            raise

    def search(self, query: str, n_results: int = 5) -> list[tuple[EntityIndexEntry, float]]:
        """Vector similarity search. Returns list of (entry, distance) tuples.

        Lower distance = more similar (cosine distance, 0.0 = identical).
        """
        result = self._store.query(
            COLLECTION_ENTITY_INDEX,
            query_texts=[query],
            n_results=n_results,
            include=["metadatas", "distances", "documents"],
        )
        entries: list[tuple[EntityIndexEntry, float]] = []
        if not result["ids"] or not result["ids"][0]:
            return entries
        for i, eid in enumerate(result["ids"][0]):
            meta = result["metadatas"][0][i]
            distance = result["distances"][0][i]
            entry = self._entry_from_metadata(eid, meta)
            entries.append((entry, distance))
        return entries

    def add(self, entry: EntityIndexEntry) -> None:
        """Add or update a single entity.

        Skips the upsert when the stored ``content_hash`` already
        matches the entry's hash, so HA ``state_changed`` pushes whose
        identity is unchanged do not re-embed the row. Any error while
        reading current metadata falls through to the upsert (fail
        open -- never silently drop a write).
        """
        try:
            current = self._store.get(
                COLLECTION_ENTITY_INDEX,
                ids=[entry.entity_id],
                include=["metadatas"],
            )
            ids = current.get("ids") or []
            metas = current.get("metadatas") or []
            if ids and metas and self._stored_hash(metas[0]) == entry.content_hash:
                return
        except Exception:
            logger.debug(
                "add() pre-fetch failed for %s -- falling through to upsert",
                entry.entity_id,
                exc_info=True,
            )
        self._store.upsert(
            COLLECTION_ENTITY_INDEX,
            ids=[entry.entity_id],
            documents=[entry.embedding_text],
            metadatas=[self._build_metadata(entry)],
        )

    def remove(self, entity_id: str) -> None:
        """Remove an entity from the index."""
        self._store.delete(COLLECTION_ENTITY_INDEX, ids=[entity_id])

    def get_by_id(self, entity_id: str) -> EntityIndexEntry | None:
        """Retrieve a single entity by its ID, or None if not found."""
        data = self._store.get(
            COLLECTION_ENTITY_INDEX,
            ids=[entity_id],
            include=["metadatas"],
        )
        if not data["ids"]:
            return None
        meta = data["metadatas"][0]
        return self._entry_from_metadata(entity_id, meta)

    def get_by_ids(self, entity_ids: list[str]) -> dict[str, EntityIndexEntry]:
        """Retrieve multiple entities by ID. Returns a mapping of entity_id -> entry."""
        if not entity_ids:
            return {}
        data = self._store.get(
            COLLECTION_ENTITY_INDEX,
            ids=entity_ids,
            include=["metadatas"],
        )
        result: dict[str, EntityIndexEntry] = {}
        for eid, meta in zip(data.get("ids", []), data.get("metadatas", []), strict=False):
            if meta is not None:
                result[eid] = self._entry_from_metadata(eid, meta)
        return result

    def list_entries(self, domains: set[str] | frozenset[str] | None = None) -> list[EntityIndexEntry]:
        """Return all indexed entities, optionally filtered by domain."""
        where: dict | None = None
        if domains:
            dlist = list(domains)
            where = {"domain": dlist[0]} if len(dlist) == 1 else {"domain": {"$in": dlist}}
        data = self._store.get(
            COLLECTION_ENTITY_INDEX,
            include=["metadatas"],
            where=where,
        )
        entries: list[EntityIndexEntry] = []
        for entity_id, meta in zip(data.get("ids", []), data.get("metadatas", []), strict=False):
            entry = self._entry_from_metadata(entity_id, meta)
            if domains and entry.domain not in domains:
                continue
            entries.append(entry)
        return entries

    def clear(self) -> None:
        """Remove all entities from the index."""
        count = self._store.count(COLLECTION_ENTITY_INDEX)
        if count > 0:
            all_data = self._store.get(COLLECTION_ENTITY_INDEX, include=[])
            if all_data["ids"]:
                self._store.delete(COLLECTION_ENTITY_INDEX, ids=all_data["ids"])
        logger.info("Entity index cleared")

    def refresh(self, entities: list[EntityIndexEntry]) -> None:
        """Clear and re-populate from a fresh entity list."""
        self._status["state"] = "building"
        self._status["progress"] = 0
        self.clear()
        self.populate(entities)

    def sync(self, entities: list[EntityIndexEntry]) -> dict:
        """Smart diff sync: upsert changed/new, remove deleted, skip unchanged.

        Returns dict with counts: added, updated, removed, unchanged.
        """
        import time as _time

        start = _time.monotonic()

        if not entities:
            return {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}

        prev_state = self._status["state"]
        self._status["state"] = "syncing"

        try:
            # Build map of incoming entities
            ha_map: dict[str, EntityIndexEntry] = {e.entity_id: e for e in entities}

            # Fetch all current entries from ChromaDB
            current_data = self._store.get(
                COLLECTION_ENTITY_INDEX,
                include=["documents", "metadatas"],
            )
            current_ids = current_data.get("ids", [])
            current_docs = current_data.get("documents", [])
            current_metas = current_data.get("metadatas", [])

            # Build lookup: entity_id -> (document, metadata)
            chroma_map: dict[str, tuple[str, dict]] = {}
            for i, eid in enumerate(current_ids):
                chroma_map[eid] = (current_docs[i], current_metas[i])

            to_upsert: list[EntityIndexEntry] = []
            added = 0
            updated = 0
            unchanged = 0

            for entity_id, entry in ha_map.items():
                if entity_id in chroma_map:
                    old_doc, old_meta = chroma_map[entity_id]
                    new_doc = entry.embedding_text
                    # Use content_hash (excludes volatile state) instead of full
                    # metadata comparison to avoid re-embedding on every restart.
                    if new_doc != old_doc or entry.content_hash != self._stored_hash(old_meta):
                        to_upsert.append(entry)
                        updated += 1
                    else:
                        unchanged += 1
                else:
                    to_upsert.append(entry)
                    added += 1

            # Find entities to remove (in ChromaDB but not in HA)
            to_remove = [eid for eid in current_ids if eid not in ha_map]
            removed = len(to_remove)

            # Batch upsert changed/new entities
            total_entities = len(ha_map)
            self._status["total"] = total_entities
            self._status["processed"] = unchanged + removed
            self._status["progress"] = int(self._status["processed"] / total_entities * 100) if total_entities else 0
            if to_upsert:
                for start_idx in range(0, len(to_upsert), BATCH_SIZE):
                    batch = to_upsert[start_idx : start_idx + BATCH_SIZE]
                    ids = [e.entity_id for e in batch]
                    documents = [e.embedding_text for e in batch]
                    metadatas = [self._build_metadata(e) for e in batch]
                    self._store.upsert(
                        COLLECTION_ENTITY_INDEX,
                        ids=ids,
                        documents=documents,
                        metadatas=metadatas,
                    )
                    self._status["processed"] = min(unchanged + removed + start_idx + len(batch), total_entities)
                    self._status["progress"] = (
                        int(self._status["processed"] / total_entities * 100) if total_entities else 0
                    )

            # Batch delete removed entities
            if to_remove:
                self._store.delete(COLLECTION_ENTITY_INDEX, ids=to_remove)

            elapsed_ms = int((_time.monotonic() - start) * 1000)

            self._last_refresh = datetime.now(UTC).isoformat()
            self._status["state"] = "ready"
            self._status["processed"] = total_entities
            self._status["progress"] = 100

            self._sync_stats = {
                "added": added,
                "updated": updated,
                "removed": removed,
                "unchanged": unchanged,
                "last_sync": self._last_refresh,
                "last_sync_duration_ms": elapsed_ms,
            }

            logger.info(
                "Entity sync complete: +%d ~%d -%d =%d (%dms)",
                added,
                updated,
                removed,
                unchanged,
                elapsed_ms,
            )
            return {"added": added, "updated": updated, "removed": removed, "unchanged": unchanged}

        except Exception as exc:
            self._status["state"] = prev_state if prev_state != "syncing" else "ready"
            self._status["error"] = str(exc)
            logger.error("Entity sync failed: %s", exc)
            raise

    def get_embedding_status(self) -> dict:
        """Return current embedding status."""
        return dict(self._status)

    def get_stats(self) -> dict:
        """Return index statistics."""
        return {
            "count": self._store.count(COLLECTION_ENTITY_INDEX),
            "last_refresh": self._last_refresh,
            "embedding_status": dict(self._status),
            "sync": dict(self._sync_stats),
        }

    # ------------------------------------------------------------------
    # Batch add (used by async queue flush)
    # ------------------------------------------------------------------

    def batch_add(self, entries: list[EntityIndexEntry]) -> None:
        """Add or update multiple entities, skipping unchanged embedding text."""
        if not entries:
            return
        seen: dict[str, EntityIndexEntry] = {}
        for e in entries:
            seen[e.entity_id] = e
        deduped = list(seen.values())

        # Fetch current docs/metadata from ChromaDB to diff
        all_ids = [e.entity_id for e in deduped]
        try:
            current = self._store.get(
                COLLECTION_ENTITY_INDEX,
                ids=all_ids,
                include=["documents", "metadatas"],
            )
            current_map: dict[str, tuple[str, dict]] = {}
            for i, eid in enumerate(current.get("ids", [])):
                current_map[eid] = (
                    current["documents"][i],
                    current["metadatas"][i],
                )
        except Exception:
            current_map = {}

        # Split into: needs re-embed (doc changed) vs metadata-only vs new
        to_upsert: list[EntityIndexEntry] = []
        meta_only_ids: list[str] = []
        meta_only_metas: list[dict] = []

        for entry in deduped:
            if entry.entity_id in current_map:
                old_doc, old_meta = current_map[entry.entity_id]
                # Hash-first short-circuit: if the stored content_hash
                # matches, identity is unchanged -- skip entirely. This
                # is the dominant path for HA state_changed pushes.
                if self._stored_hash(old_meta) == entry.content_hash:
                    continue
                new_doc = entry.embedding_text
                new_meta = self._build_metadata(entry)
                if new_doc == old_doc and new_meta == old_meta:
                    continue  # Unchanged -- skip entirely
                if new_doc == old_doc:
                    # Only metadata changed -- no re-embedding needed
                    meta_only_ids.append(entry.entity_id)
                    meta_only_metas.append(new_meta)
                else:
                    to_upsert.append(entry)
            else:
                to_upsert.append(entry)

        if to_upsert:
            ids = [e.entity_id for e in to_upsert]
            documents = [e.embedding_text for e in to_upsert]
            metadatas = [self._build_metadata(e) for e in to_upsert]
            self._store.upsert(
                COLLECTION_ENTITY_INDEX,
                ids=ids,
                documents=documents,
                metadatas=metadatas,
            )

        if meta_only_ids:
            self._store.update_metadata(
                COLLECTION_ENTITY_INDEX,
                ids=meta_only_ids,
                metadatas=meta_only_metas,
            )

    # ------------------------------------------------------------------
    # Async wrappers (offload to thread pool via run_in_executor)
    # ------------------------------------------------------------------

    async def add_async(self, entry: EntityIndexEntry) -> None:
        """Async wrapper -- offloads add() to thread pool."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.add, entry)

    async def remove_async(self, entity_id: str) -> None:
        """Async wrapper -- offloads remove() to thread pool."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.remove, entity_id)

    async def populate_async(self, entities: list[EntityIndexEntry]) -> None:
        """Async wrapper -- offloads populate() to thread pool."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.populate, entities)

    async def sync_async(self, entities: list[EntityIndexEntry]) -> dict:
        """Async wrapper -- offloads sync() to thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.sync, entities)

    async def search_async(self, query: str, n_results: int = 5) -> list[tuple[EntityIndexEntry, float]]:
        """Async wrapper -- offloads search() to thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(self.search, query, n_results=n_results))

    async def get_by_id_async(self, entity_id: str) -> EntityIndexEntry | None:
        """Async wrapper -- offloads get_by_id() to thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_by_id, entity_id)

    async def list_entries_async(
        self,
        domains: set[str] | frozenset[str] | None = None,
    ) -> list[EntityIndexEntry]:
        """Async wrapper -- offloads list_entries() to thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(self.list_entries, domains=domains))
