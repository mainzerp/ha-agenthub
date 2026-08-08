"""Dedicated sqlite-vec store for session-memory turn embeddings.

Lives in its own database file (``session_memory.db`` next to the main app
DB) so the main-DB layer never loads the sqlite-vec extension -- this mirrors
how ``SqliteCacheStore`` isolates ``routing_cache_embeddings`` from the
entity ``VectorStore``. The relational session/turn metadata lives in the
main DB (``memory_sessions`` / ``memory_turns``, migration 40); this store
only maps ``memory_turns.id`` -> vec0 rowid and serves k-NN search.

Uses Python's stdlib ``sqlite3`` with ``check_same_thread=False`` and WAL
journal mode so the ``asyncio.to_thread()`` async wrappers work without any
cascading async changes elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import struct
import threading
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_VEC_TABLE = "memory_turn_embeddings"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS memory_turn_vec_map (
    memory_turn_id INTEGER PRIMARY KEY,
    vec_rowid INTEGER NOT NULL
);
"""


def _serialize_vec(vector: list[float]) -> bytes:
    """Serialize a float vector into the compact little-endian f32 BLOB sqlite-vec expects."""
    return struct.pack(f"{len(vector)}f", *vector)


class SessionMemoryVectorStore:
    """Sync sqlite-vec store mapping memory_turns rows to embedding vectors."""

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
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
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
                "sqlite-vec unavailable for session_memory.db; session memory disabled",
                exc_info=True,
            )
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        logger.info("SessionMemoryVectorStore opened at %s", self._db_path)

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._connect()
        return self._conn  # type: ignore[return-value]

    def _vec_table_exists(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_VEC_TABLE,),
        ).fetchone()
        return row is not None

    def _meta_value(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM memory_meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def _reset_locked(self, conn: sqlite3.Connection) -> None:
        """Drop the vec table and clear the id map (caller holds the lock)."""
        conn.execute(f"DROP TABLE IF EXISTS {_VEC_TABLE}")
        conn.execute("DELETE FROM memory_turn_vec_map")

    def _ensure_vec(self, conn: sqlite3.Connection, dim: int) -> bool:
        """Create the vec0 table for ``dim``, rebuilding on a dimension change.

        Returns False when sqlite-vec is unavailable (session memory disabled).
        On a dimension change the stale vectors are unusable, so the vec table
        and its id map are dropped; turns are re-embedded lazily by the
        startup backfill (routing ``_ensure_routing_vec`` precedent).
        """
        if not self._vec_available:
            return False
        raw_dim = self._meta_value(conn, "vec_dim")
        known_dim = int(raw_dim) if raw_dim else None
        if known_dim is not None and known_dim != dim:
            self._reset_locked(conn)
            logger.info(
                "Session-memory embedding dimension changed (%d -> %d); vectors reset for lazy re-embed",
                known_dim,
                dim,
            )
            known_dim = None
        if known_dim is None:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VEC_TABLE} USING vec0("
                f"embedding float[{dim}] distance_metric=cosine)"
            )
            conn.execute("INSERT OR REPLACE INTO memory_meta (key, value) VALUES ('vec_dim', ?)", (str(dim),))
        return True

    def ensure_active_model(self, model: str, dim: int) -> bool:
        """Reset stored vectors when the active embedding model/dim changed.

        Called once at service init (model/dim are only known then). Returns
        True when a reset happened so the caller can NULL the relational
        ``memory_turns.vec_rowid`` references and let the startup backfill
        re-embed with the active model.
        """
        if not self._vec_available:
            return False
        conn = self._ensure_conn()
        with self._lock:
            raw_dim = self._meta_value(conn, "vec_dim")
            known_dim = int(raw_dim) if raw_dim else None
            known_model = self._meta_value(conn, "vec_model")
            reset = (known_dim is not None and known_dim != dim) or (
                known_model is not None and bool(model) and known_model != model
            )
            if reset:
                self._reset_locked(conn)
                logger.info(
                    "Session-memory embedding model changed (%s/%s -> %s/%d); vectors reset for lazy re-embed",
                    known_model,
                    known_dim,
                    model,
                    dim,
                )
            if reset or known_dim is None:
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VEC_TABLE} USING vec0("
                    f"embedding float[{int(dim)}] distance_metric=cosine)"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO memory_meta (key, value) VALUES ('vec_dim', ?)",
                    (str(int(dim)),),
                )
            if model:
                conn.execute("INSERT OR REPLACE INTO memory_meta (key, value) VALUES ('vec_model', ?)", (model,))
            conn.commit()
        return reset

    def store_embedding(self, memory_turn_id: int, vector: list[float]) -> int | None:
        """Insert-or-replace the vec row for one memory turn. Returns the vec rowid."""
        if not self._vec_available or not vector:
            return None
        conn = self._ensure_conn()
        with self._lock:
            if not self._ensure_vec(conn, len(vector)):
                return None
            existing = conn.execute(
                "SELECT vec_rowid FROM memory_turn_vec_map WHERE memory_turn_id = ?",
                (int(memory_turn_id),),
            ).fetchone()
            if existing is not None:
                conn.execute(f"DELETE FROM {_VEC_TABLE} WHERE rowid = ?", (existing[0],))
            cursor = conn.execute(
                f"INSERT INTO {_VEC_TABLE}(embedding) VALUES (?)",
                (_serialize_vec(vector),),
            )
            vec_rowid = cursor.lastrowid
            conn.execute(
                "INSERT OR REPLACE INTO memory_turn_vec_map (memory_turn_id, vec_rowid) VALUES (?, ?)",
                (int(memory_turn_id), vec_rowid),
            )
            conn.commit()
        return int(vec_rowid) if vec_rowid is not None else None

    def has_embedding(self, memory_turn_id: int) -> bool:
        """True when a vec row exists for the turn (or vec support is off)."""
        if not self._vec_available:
            # Treat as present so the lazy backfill does not spam embeds.
            return True
        conn = self._ensure_conn()
        with self._lock:
            row = conn.execute(
                "SELECT 1 FROM memory_turn_vec_map WHERE memory_turn_id = ?",
                (int(memory_turn_id),),
            ).fetchone()
        return row is not None

    def search(self, vector: list[float], k: int) -> list[tuple[int, float]]:
        """k-NN cosine search over stored turn embeddings.

        Returns ``[(memory_turn_id, cosine_distance), ...]`` ordered by
        ascending distance. Returns an empty list when vec support is
        unavailable or no embeddings are stored yet.
        """
        if not self._vec_available or not vector:
            return []
        conn = self._ensure_conn()
        with self._lock:
            if not self._ensure_vec(conn, len(vector)):
                return []
            if not self._vec_table_exists(conn):
                return []
            rows = conn.execute(
                f"WITH knn AS (SELECT rowid, distance FROM {_VEC_TABLE} "
                f"WHERE embedding MATCH ? AND k = ?) "
                f"SELECT m.memory_turn_id, knn.distance "
                f"FROM knn JOIN memory_turn_vec_map m ON m.vec_rowid = knn.rowid",
                (_serialize_vec(vector), max(1, int(k))),
            ).fetchall()
        return [(int(turn_id), float(distance)) for turn_id, distance in rows]

    def reset_embeddings(self) -> None:
        """Drop the vec table and clear the id map + recorded dim/model."""
        conn = self._ensure_conn()
        with self._lock:
            self._reset_locked(conn)
            conn.execute("DELETE FROM memory_meta WHERE key IN ('vec_dim', 'vec_model')")
            conn.commit()

    async def store_embedding_async(self, memory_turn_id: int, vector: list[float]) -> int | None:
        return await asyncio.to_thread(self.store_embedding, memory_turn_id, vector)

    async def search_async(self, vector: list[float], k: int) -> list[tuple[int, float]]:
        return await asyncio.to_thread(self.search, vector, k)

    async def has_embedding_async(self, memory_turn_id: int) -> bool:
        return await asyncio.to_thread(self.has_embedding, memory_turn_id)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                logger.info("SessionMemoryVectorStore closed")


_store: SessionMemoryVectorStore | None = None
_store_lock = threading.Lock()


def get_memory_vector_store() -> SessionMemoryVectorStore | None:
    """Return the singleton store, opening ``session_memory.db`` on first call.

    Failure-contained: returns None when the store cannot be opened so the
    request path is never broken by a memory-storage problem.
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                try:
                    db_path = Path(settings.sqlite_db_path).parent / "session_memory.db"
                    _store = SessionMemoryVectorStore(str(db_path))
                except Exception:
                    logger.warning("Failed to open session-memory vector store", exc_info=True)
                    return None
    return _store
