"""SQLite-based cache store replacing ChromaDB for routing/action cache tiers.

Uses Python's stdlib ``sqlite3`` with ``check_same_thread=False`` and WAL
journal mode so the existing ``asyncio.to_thread()`` pattern works without
any cascading async changes elsewhere.  The public interface mirrors the
cache-relevant subset of ``VectorStore``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime

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


class SqliteCacheStore:
    """Sync SQLite store that implements the same cache interface as VectorStore."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        logger.info("SqliteCacheStore opened at %s", self._db_path)

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
        """INSERT OR REPLACE a single entry.  ``embeddings`` is accepted but ignored."""
        entry_id = ids[0]
        document = (documents or [""])[0]
        meta = (metadatas or [{}])[0]
        now = datetime.now(UTC).isoformat()
        metadata_json = json.dumps(meta)
        last_accessed = now
        created_at = meta.get("created_at") or now
        conn = self._ensure_conn()
        with self._lock:
            conn.execute(
                f"INSERT OR REPLACE INTO {collection} (entry_id, document, metadata_json, last_accessed, created_at) VALUES (?, ?, ?, ?, ?)",
                (entry_id, document, metadata_json, last_accessed, created_at),
            )
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
            cursor = conn.execute(
                f"DELETE FROM {collection} WHERE entry_id IN (SELECT entry_id FROM {collection} ORDER BY last_accessed ASC LIMIT ?)",
                (n,),
            )
            conn.commit()
            return cursor.rowcount

    def delete_all(self, collection: str) -> int:
        """Delete all entries from a collection.  Returns count deleted."""
        conn = self._ensure_conn()
        with self._lock:
            cursor = conn.execute(f"DELETE FROM {collection}")
            conn.commit()
            return cursor.rowcount
