"""SQLite-based cache store replacing ChromaDB for routing/action cache tiers.

Uses Python's stdlib ``sqlite3`` with ``check_same_thread=False`` and WAL
journal mode so the existing ``asyncio.to_thread()`` pattern works without
any cascading async changes elsewhere.  The public interface mirrors the
cache-relevant subset of ``VectorStore``.

Schema versioning: the main app database has its own migration-ladder
registry (``app.db.schema._migrations``). This store manages a SEPARATE
database file (``cache.db``); its schema is versioned via
``PRAGMA user_version`` (see :meth:`SqliteCacheStore._run_migrations`).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
import threading
from collections.abc import Iterable
from datetime import UTC, datetime

from app.models.cache import CachedAction

logger = logging.getLogger(__name__)

COLLECTION_ROUTING_CACHE = "routing_cache"
COLLECTION_ACTION_CACHE = "action_cache"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_cache (
    entry_id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    last_accessed TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_cache (
    entry_id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    last_accessed TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_routing_cache_last_accessed ON routing_cache(last_accessed);
CREATE INDEX IF NOT EXISTS idx_action_cache_last_accessed ON action_cache(last_accessed);
"""

# Version 1: invalidation sidecars for the action cache (P2). Entity
# invalidation becomes an indexed join (O(matches)) instead of a paged
# full scan with per-row JSON parsing.
_SIDECAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_cache_entities (
    entry_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY (entry_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_action_cache_entities_entity ON action_cache_entities(entity_id);

CREATE TABLE IF NOT EXISTS action_cache_entry_index (
    entry_id TEXT PRIMARY KEY,
    language TEXT NOT NULL DEFAULT '',
    is_readonly INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_action_cache_entry_index_readonly ON action_cache_entry_index(is_readonly);
CREATE INDEX IF NOT EXISTS idx_action_cache_entry_index_language ON action_cache_entry_index(language);
CREATE INDEX IF NOT EXISTS idx_action_cache_entry_index_schema ON action_cache_entry_index(schema_version);
"""

_SCHEMA_VERSION = 2

# Version 2: semantic routing tier (P4). Metadata sidecar mapping routing
# entry_id -> vec0 rowid; the vec0 virtual table itself is created lazily on
# first embedding write because its dimension is only known once an embedding
# vector exists (the embedding model is configurable).
_ROUTING_EMBEDDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_cache_embeddings (
    entry_id TEXT PRIMARY KEY,
    vec_rowid INTEGER NOT NULL,
    dim INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS routing_cache_vec_dim (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    dim INTEGER NOT NULL
);
"""

_ROUTING_VEC_TABLE = "routing_cache_vec"


def _serialize_vec(vector: list[float]) -> bytes:
    """Serialize a float vector into the compact little-endian f32 BLOB sqlite-vec expects."""
    return struct.pack(f"{len(vector)}f", *vector)


def _meta_entity_ids(meta: dict) -> list[str]:
    """Extract lowercased entity ids from a metadata dict (sidecar form)."""
    raw = (meta or {}).get("entity_ids")
    if not raw:
        return []
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(parsed, list):
        return []
    return sorted({str(value).strip().lower() for value in parsed if str(value).strip()})


def _meta_is_readonly(meta: dict) -> bool:
    """Mirror ``action_cache._is_readonly_action`` for sidecar maintenance.

    Entries whose cached_action is missing or fails validation count as
    read-only (they are purge candidates); otherwise the service name
    decides (``query_*``/``list_*``).
    """
    raw = (meta or {}).get("cached_action")
    service = ""
    if raw:
        try:
            action = CachedAction.model_validate_json(raw) if isinstance(raw, str) else CachedAction.model_validate(raw)
            service = action.service or ""
        except Exception:
            service = ""
    if not service:
        return True
    action_name = service.split("/", 1)[1] if "/" in service else service
    return action_name.strip().lower().startswith(("query_", "list_"))


def _meta_schema_version(meta: dict) -> int:
    try:
        return int(str((meta or {}).get("schema_version") or 0))
    except (TypeError, ValueError):
        return 0


class SqliteCacheStore:
    """Sync SQLite store that implements the same cache interface as VectorStore."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._vec_available: bool = False
        self._connect()

    @property
    def vec_available(self) -> bool:
        """True when the sqlite-vec extension loaded successfully for this store."""
        return self._vec_available

    def _connect(self) -> None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._vec_available = True
        except Exception:
            self._vec_available = False
            logger.warning(
                "sqlite-vec unavailable for cache.db; routing semantic tier disabled",
                exc_info=True,
            )
        conn.executescript(_SCHEMA)
        self._run_migrations(conn)
        conn.commit()
        self._conn = conn
        logger.info("SqliteCacheStore opened at %s", self._db_path)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply cache.db-local schema migrations (PRAGMA user_version ladder).

        Version 1 creates the action-cache invalidation sidecars and
        backfills them from existing rows, so entity invalidation keeps
        working for entries stored before the sidecar existed.
        Version 2 creates the routing-embedding sidecar tables for the
        semantic routing tier (the vec0 table itself is created lazily,
        see :meth:`_ensure_routing_vec`). Backfill of embeddings for
        pre-existing routing entries is lazy: entries are embedded on
        their next exact-hash hit or re-store.
        """
        row = conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row else 0
        if version >= _SCHEMA_VERSION:
            return
        if version < 1:
            conn.executescript(_SIDECAR_SCHEMA)
            cursor = conn.execute("SELECT entry_id, metadata_json FROM action_cache")
            backfilled = 0
            for entry_id, metadata_json in cursor.fetchall():
                try:
                    meta = json.loads(metadata_json) if metadata_json else {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                self._write_sidecar_rows(conn, entry_id, meta)
                backfilled += 1
            if backfilled:
                logger.info("Backfilled action-cache invalidation sidecars for %d entries", backfilled)
        if version < 2:
            conn.executescript(_ROUTING_EMBEDDINGS_SCHEMA)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    # ------------------------------------------------------------------
    # sidecar maintenance (action cache only)
    # ------------------------------------------------------------------

    def _write_sidecar_rows(self, conn: sqlite3.Connection, entry_id: str, meta: dict) -> None:
        """(Re)write the invalidation sidecar rows for one action-cache entry."""
        conn.execute("DELETE FROM action_cache_entities WHERE entry_id = ?", (entry_id,))
        entity_ids = _meta_entity_ids(meta)
        if entity_ids:
            conn.executemany(
                "INSERT OR IGNORE INTO action_cache_entities (entry_id, entity_id) VALUES (?, ?)",
                [(entry_id, entity_id) for entity_id in entity_ids],
            )
        conn.execute(
            "INSERT OR REPLACE INTO action_cache_entry_index (entry_id, language, is_readonly, schema_version) "
            "VALUES (?, ?, ?, ?)",
            (
                entry_id,
                str((meta or {}).get("language") or ""),
                1 if _meta_is_readonly(meta) else 0,
                _meta_schema_version(meta),
            ),
        )

    def _drop_sidecar_rows(self, conn: sqlite3.Connection, entry_ids: list[str]) -> None:
        if not entry_ids:
            return
        placeholders = ", ".join("?" for _ in entry_ids)
        conn.execute(f"DELETE FROM action_cache_entities WHERE entry_id IN ({placeholders})", entry_ids)
        conn.execute(f"DELETE FROM action_cache_entry_index WHERE entry_id IN ({placeholders})", entry_ids)

    # ------------------------------------------------------------------
    # routing-embedding sidecar (semantic routing tier, P4)
    # ------------------------------------------------------------------

    def _routing_vec_table_exists(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_ROUTING_VEC_TABLE,),
        ).fetchone()
        return row is not None

    def _ensure_routing_vec(self, conn: sqlite3.Connection, dim: int) -> bool:
        """Create the routing vec0 table for ``dim``, rebuilding on a model/dim change.

        Returns False when sqlite-vec is unavailable (semantic tier disabled,
        exact-hash routing keeps working). On a dimension change the stale
        vectors are unusable, so the vec table and its sidecar rows are
        dropped; entries are re-embedded lazily on their next exact hit or
        re-store.
        """
        if not self._vec_available:
            return False
        row = conn.execute("SELECT dim FROM routing_cache_vec_dim WHERE id = 1").fetchone()
        known_dim = int(row[0]) if row else None
        if known_dim is not None and known_dim != dim:
            conn.execute(f"DROP TABLE IF EXISTS {_ROUTING_VEC_TABLE}")
            conn.execute("DELETE FROM routing_cache_embeddings")
            conn.execute("DELETE FROM routing_cache_vec_dim")
            logger.info(
                "Routing embedding dimension changed (%d -> %d); semantic entries reset for lazy re-embed",
                known_dim,
                dim,
            )
            known_dim = None
        if known_dim is None:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {_ROUTING_VEC_TABLE} USING vec0("
                f"embedding float[{dim}] distance_metric=cosine)"
            )
            conn.execute("INSERT OR REPLACE INTO routing_cache_vec_dim (id, dim) VALUES (1, ?)", (dim,))
        return True

    def _write_routing_embedding(self, conn: sqlite3.Connection, entry_id: str, embedding: list[float]) -> None:
        """Insert-or-replace the vec row for one routing entry (caller holds the lock)."""
        if not embedding or not self._ensure_routing_vec(conn, len(embedding)):
            return
        existing = conn.execute(
            "SELECT vec_rowid FROM routing_cache_embeddings WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        if existing is not None:
            conn.execute(f"DELETE FROM {_ROUTING_VEC_TABLE} WHERE rowid = ?", (existing[0],))
        cursor = conn.execute(
            f"INSERT INTO {_ROUTING_VEC_TABLE}(embedding) VALUES (?)",
            (_serialize_vec(embedding),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO routing_cache_embeddings (entry_id, vec_rowid, dim) VALUES (?, ?, ?)",
            (entry_id, cursor.lastrowid, len(embedding)),
        )

    def store_routing_embedding(self, entry_id: str, embedding: list[float]) -> bool:
        """Store the embedding for an existing routing entry (lazy backfill path).

        Only writes when the routing entry itself still exists, so no orphan
        vec rows are left behind. Returns True when a vec row was written.
        """
        if not self._vec_available or not embedding:
            return False
        conn = self._ensure_conn()
        with self._lock:
            row = conn.execute(
                f"SELECT 1 FROM {COLLECTION_ROUTING_CACHE} WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            if row is None:
                return False
            self._write_routing_embedding(conn, entry_id, embedding)
            conn.commit()
        return True

    def has_routing_embedding(self, entry_id: str) -> bool:
        """True when a vec row exists for the entry (or vec support is off)."""
        if not self._vec_available:
            # Treat as present so the lazy backfill does not spam embeds.
            return True
        conn = self._ensure_conn()
        with self._lock:
            row = conn.execute(
                "SELECT 1 FROM routing_cache_embeddings WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
        return row is not None

    def search_routing_embeddings(self, embedding: list[float], k: int) -> list[tuple[str, float]]:
        """k-NN cosine search over stored routing embeddings.

        Returns ``[(entry_id, cosine_distance), ...]`` ordered by ascending
        distance. Returns an empty list when vec support is unavailable or no
        embeddings are stored yet.
        """
        if not self._vec_available or not embedding:
            return []
        conn = self._ensure_conn()
        with self._lock:
            if not self._ensure_routing_vec(conn, len(embedding)):
                return []
            if not self._routing_vec_table_exists(conn):
                return []
            rows = conn.execute(
                f"WITH knn AS (SELECT rowid, distance FROM {_ROUTING_VEC_TABLE} "
                f"WHERE embedding MATCH ? AND k = ?) "
                f"SELECT m.entry_id, knn.distance "
                f"FROM knn JOIN routing_cache_embeddings m ON m.vec_rowid = knn.rowid",
                (_serialize_vec(embedding), max(1, int(k))),
            ).fetchall()
        return [(str(entry_id), float(distance)) for entry_id, distance in rows]

    def _drop_routing_embedding_rows(self, conn: sqlite3.Connection, entry_ids: list[str]) -> None:
        """Remove vec + sidecar rows for deleted routing entries (caller holds the lock)."""
        if not self._vec_available or not entry_ids:
            return
        placeholders = ", ".join("?" for _ in entry_ids)
        if self._routing_vec_table_exists(conn):
            rows = conn.execute(
                f"SELECT vec_rowid FROM routing_cache_embeddings WHERE entry_id IN ({placeholders})",
                entry_ids,
            ).fetchall()
            rowids = [row[0] for row in rows]
            if rowids:
                rowid_placeholders = ", ".join("?" for _ in rowids)
                conn.execute(f"DELETE FROM {_ROUTING_VEC_TABLE} WHERE rowid IN ({rowid_placeholders})", rowids)
        conn.execute(f"DELETE FROM routing_cache_embeddings WHERE entry_id IN ({placeholders})", entry_ids)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                logger.info("SqliteCacheStore closed")

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._connect()
        return self._conn  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # cache-relevant VectorStore interface
    # ------------------------------------------------------------------

    def upsert(
        self,
        collection: str,
        ids: list[str],
        documents: list[str] | None = None,
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict] | None = None,
    ) -> None:
        """INSERT OR REPLACE a single entry.

        ``embeddings`` is honoured only for the routing collection (semantic
        tier, P4): when a vector is supplied, the vec0 sidecar row is written
        in the same transaction. For the action collection it is ignored.
        """
        entry_id = ids[0]
        document = (documents or [""])[0]
        meta = (metadatas or [{}])[0]
        now = datetime.now(UTC).isoformat()
        metadata_json = json.dumps(meta)
        last_accessed = now
        created_at = meta.get("created_at") or now
        embedding = embeddings[0] if embeddings else None
        conn = self._ensure_conn()
        with self._lock:
            conn.execute(
                f"INSERT OR REPLACE INTO {collection} (entry_id, document, metadata_json, last_accessed, created_at) VALUES (?, ?, ?, ?, ?)",
                (entry_id, document, metadata_json, last_accessed, created_at),
            )
            if collection == COLLECTION_ACTION_CACHE:
                self._write_sidecar_rows(conn, entry_id, meta)
            if collection == COLLECTION_ROUTING_CACHE and embedding:
                self._write_routing_embedding(conn, entry_id, embedding)
            conn.commit()

    def get(
        self,
        collection: str,
        ids: list[str] | None = None,
        include: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        """Return entries matching the ChromaDB result shape."""
        conn = self._ensure_conn()
        want_documents = (include is None) or "documents" in include
        want_metadatas = (include is None) or "metadatas" in include

        select_cols = ["entry_id"]
        if want_documents:
            select_cols.append("document")
        if want_metadatas:
            select_cols.append("metadata_json")

        query = f"SELECT {', '.join(select_cols)} FROM {collection}"
        params: list = []

        if ids:
            placeholders = ", ".join("?" for _ in ids)
            query += f" WHERE entry_id IN ({placeholders})"
            params.extend(ids)

        if limit is not None:
            query += f" LIMIT {int(limit)}"
        if offset is not None:
            query += f" OFFSET {int(offset)}"

        result: dict = {"ids": []}
        if want_documents:
            result["documents"] = []
        if want_metadatas:
            result["metadatas"] = []

        with self._lock:
            rows = conn.execute(query, params).fetchall()

        for row in rows:
            result["ids"].append(row[0])
            idx = 1
            if want_documents:
                result["documents"].append(row[idx])
                idx += 1
            if want_metadatas:
                raw_meta = row[idx]
                try:
                    result["metadatas"].append(json.loads(raw_meta) if raw_meta else {})
                except (json.JSONDecodeError, TypeError):
                    result["metadatas"].append({})

        return result

    def delete(self, collection: str, ids: list[str]) -> None:
        if not ids:
            return
        conn = self._ensure_conn()
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            conn.execute(f"DELETE FROM {collection} WHERE entry_id IN ({placeholders})", ids)
            if collection == COLLECTION_ACTION_CACHE:
                self._drop_sidecar_rows(conn, ids)
            if collection == COLLECTION_ROUTING_CACHE:
                self._drop_routing_embedding_rows(conn, ids)
            conn.commit()

    def count(self, collection: str) -> int:
        conn = self._ensure_conn()
        with self._lock:
            row = conn.execute(f"SELECT COUNT(*) FROM {collection}").fetchone()
        return int(row[0]) if row else 0

    def update_metadata(self, collection: str, ids: list[str], metadatas: list[dict]) -> None:
        """Batch-update metadata_json and last_accessed in a single transaction."""
        if not ids:
            return
        conn = self._ensure_conn()
        now = datetime.now(UTC).isoformat()
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for entry_id, meta in zip(ids, metadatas, strict=False):
                    metadata_json = json.dumps(meta)
                    conn.execute(
                        f"UPDATE {collection} SET metadata_json = ?, last_accessed = ? WHERE entry_id = ?",
                        (metadata_json, now, entry_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------
    # LRU + maintenance helpers (not part of VectorStore interface)
    # ------------------------------------------------------------------

    def delete_oldest(self, collection: str, n: int) -> int:
        """Delete the *n* least-recently-accessed entries.  Returns count deleted."""
        if n <= 0:
            return 0
        conn = self._ensure_conn()
        with self._lock:
            rows = conn.execute(
                f"SELECT entry_id FROM {collection} ORDER BY last_accessed ASC LIMIT ?",
                (n,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if not ids:
                return 0
            placeholders = ", ".join("?" for _ in ids)
            cursor = conn.execute(f"DELETE FROM {collection} WHERE entry_id IN ({placeholders})", ids)
            if collection == COLLECTION_ACTION_CACHE:
                self._drop_sidecar_rows(conn, ids)
            if collection == COLLECTION_ROUTING_CACHE:
                self._drop_routing_embedding_rows(conn, ids)
            conn.commit()
            return cursor.rowcount

    def delete_all(self, collection: str) -> int:
        """Delete all entries from a collection.  Returns count deleted."""
        conn = self._ensure_conn()
        with self._lock:
            cursor = conn.execute(f"DELETE FROM {collection}")
            if collection == COLLECTION_ACTION_CACHE:
                conn.execute("DELETE FROM action_cache_entities")
                conn.execute("DELETE FROM action_cache_entry_index")
            if collection == COLLECTION_ROUTING_CACHE:
                # Reset the vec table too so a stale dimension does not
                # survive a flush; it is recreated lazily on next write.
                conn.execute(f"DROP TABLE IF EXISTS {_ROUTING_VEC_TABLE}")
                conn.execute("DELETE FROM routing_cache_embeddings")
                conn.execute("DELETE FROM routing_cache_vec_dim")
            conn.commit()
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Indexed invalidation lookups (action-cache sidecars; P2)
    #
    # Each returns None when the collection has no sidecar support so
    # callers can fall back to the legacy paged full scan (routing cache
    # and VectorStore-backed collections).
    # ------------------------------------------------------------------

    def find_entries_by_entity_ids(self, collection: str, entity_ids: Iterable[str]) -> list[str] | None:
        """Indexed entity->entry lookup; O(matches) instead of a full scan."""
        if collection != COLLECTION_ACTION_CACHE:
            return None
        targets = sorted({str(entity_id).strip().lower() for entity_id in entity_ids if str(entity_id).strip()})
        if not targets:
            return []
        placeholders = ", ".join("?" for _ in targets)
        conn = self._ensure_conn()
        with self._lock:
            rows = conn.execute(
                f"SELECT DISTINCT entry_id FROM action_cache_entities WHERE entity_id IN ({placeholders})",
                targets,
            ).fetchall()
        return [row[0] for row in rows]

    def find_entries_without_language(self, collection: str) -> list[str] | None:
        if collection != COLLECTION_ACTION_CACHE:
            return None
        conn = self._ensure_conn()
        with self._lock:
            rows = conn.execute("SELECT entry_id FROM action_cache_entry_index WHERE language = ''").fetchall()
        return [row[0] for row in rows]

    def find_entries_below_schema_version(self, collection: str, min_schema_version: int) -> list[str] | None:
        if collection != COLLECTION_ACTION_CACHE:
            return None
        conn = self._ensure_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT entry_id FROM action_cache_entry_index WHERE schema_version < ?",
                (int(min_schema_version),),
            ).fetchall()
        return [row[0] for row in rows]

    def find_readonly_entries(self, collection: str) -> list[str] | None:
        if collection != COLLECTION_ACTION_CACHE:
            return None
        conn = self._ensure_conn()
        with self._lock:
            rows = conn.execute("SELECT entry_id FROM action_cache_entry_index WHERE is_readonly = 1").fetchall()
        return [row[0] for row in rows]
