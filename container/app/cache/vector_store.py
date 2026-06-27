"""sqlite-vec (``vec0``) wrapper managing the entity index collection.

The entity index is the ONLY runtime consumer of this store (the routing
and action caches moved to ``SqliteCacheStore`` in v1.37.0). It backs the
``EmbeddingSignal`` similarity fallback that runs AFTER deterministic
entity resolution (Prime Directives 4 and 5).

Public method signatures are preserved from the previous ChromaDB-backed
implementation so ``entity/index.py`` is unchanged. ``query``/``get``
return the same result-dict shapes ChromaDB produced.

The ``vec0`` virtual table stores only the raw float vector keyed by an
integer rowid. A sidecar metadata table maps ``entity_id`` <-> ``vec_rowid``
and stores the document text + JSON metadata. KNN search uses the
``distance_metric=cosine`` table option, so the returned ``distance`` is a
cosine distance (0.0 = identical), matching the ChromaDB cosine contract
that ``signals.py`` relied on (``similarity = 1 - distance``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import sqlite3
import struct
import threading

from app.cache.embedding import get_embedding_engine
from app.config import settings

logger = logging.getLogger(__name__)

COLLECTION_ENTITY_INDEX = "entity_index"
COLLECTION_RESPONSE_CACHE = "response_cache"

# Default embedding dimension for the local model (intfloat/multilingual-e5-small).
_DEFAULT_DIM = 384


def _serialize_vec(vector: list[float]) -> bytes:
    """Serialize a float vector into the compact little-endian f32 BLOB sqlite-vec expects."""
    return struct.pack(f"{len(vector)}f", *vector)


def _is_client_closed_error(exc: BaseException) -> bool:
    # sqlite3 raises "Cannot operate on a closed database" when a connection
    # was torn down. Match that wording narrowly so unrelated errors do not
    # trigger a connection reopen.
    msg = str(exc).lower()
    return "closed database" in msg or "cannot operate on a closed" in msg


class VectorStore:
    """Manages a sqlite-vec ``vec0`` store for the entity index."""

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._engine = None
        self._dim: int = _DEFAULT_DIM
        # sqlite3 connections opened with check_same_thread=False are not safe
        # for concurrent use; serialise every operation. The store is called
        # from worker threads (EntityIndex offloads via run_in_executor).
        self._lock: threading.Lock = threading.Lock()
        # P3-3: serialise reinitialisation across worker threads so concurrent
        # callers cannot both reopen the connection.
        self._reinit_lock: threading.Lock = threading.Lock()
        self._db_path: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open the sqlite database, load the sqlite-vec extension, create tables."""
        engine = await get_embedding_engine()
        self._engine = engine
        dim = engine.get_info().get("dimensions")
        self._dim = int(dim) if dim else _DEFAULT_DIM

        os.makedirs(settings.chromadb_persist_dir, exist_ok=True)
        self._db_path = os.path.join(settings.chromadb_persist_dir, "entity_vectors.db")
        self._open_connection()
        self._ensure_collection(COLLECTION_ENTITY_INDEX)
        self._delete_legacy_response_collection()
        logger.info(
            "VectorStore initialized with sqlite-vec (dim=%d) at %s",
            self._dim,
            self._db_path,
        )

    def _open_connection(self) -> None:
        """Open the sqlite connection and load the sqlite-vec extension."""
        import sqlite_vec

        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        # Hardened mode: writes are durable; reads are fast. WAL keeps the
        # single connection from blocking itself on concurrent reads.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            logger.debug("Could not set WAL/synchronous pragmas", exc_info=True)
        self._conn = conn

    def _table_names(self, collection_name: str) -> tuple[str, str, str]:
        """Return (vec0 virtual table, metadata table, vec_rowid index) for a collection."""
        safe = collection_name.replace('"', "")
        return f'"{safe}_vec"', f'"{safe}_meta"', f'"idx_{safe}_vec_rowid"'

    def _ensure_collection(self, collection_name: str) -> None:
        """Create the vec0 + metadata tables for a collection if they do not exist."""
        if self._conn is None:
            return
        vec_t, meta_t, idx = self._table_names(collection_name)
        cur = self._conn.cursor()
        cur.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {vec_t} USING vec0("
            f"embedding float[{self._dim}] distance_metric=cosine)"
        )
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {meta_t}("
            f"entity_id TEXT PRIMARY KEY, vec_rowid INTEGER NOT NULL, "
            f"document TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{{}}')"
        )
        cur.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {meta_t}(vec_rowid)")
        self._conn.commit()

    def _delete_legacy_response_collection(self) -> None:
        """Drop the obsolete v3 response cache tables if present (idempotent)."""
        if self._conn is None:
            return
        vec_t, meta_t, idx = self._table_names(COLLECTION_RESPONSE_CACHE)
        try:
            cur = self._conn.cursor()
            cur.execute(f"DROP TABLE IF EXISTS {vec_t}")
            cur.execute(f"DROP TABLE IF EXISTS {meta_t}")
            cur.execute(f"DROP INDEX IF EXISTS {idx}")
            self._conn.commit()
            logger.info("Dropped legacy response_cache tables if present")
        except Exception:
            logger.debug("Ignoring legacy response_cache drop failure", exc_info=True)

    def _ensure_open(self) -> None:
        """Reopen the connection if it was lost (defensive; sqlite rarely needs this)."""
        if self._conn is None:
            self._reinitialize_sync()

    def _reinitialize_sync(self) -> None:
        """Re-open the database connection.

        P3-3: guarded by ``_reinit_lock`` so parallel retries cannot both
        reopen the connection. The double-checked ``_conn is not None``
        guard makes the second waiter a no-op.
        """
        with self._reinit_lock:
            if self._conn is not None:
                return
            logger.warning("VectorStore connection missing, reopening")
            self._open_connection()
            self._ensure_collection(COLLECTION_ENTITY_INDEX)
            logger.info("VectorStore reinitialized successfully")

    def close(self) -> None:
        """Close the underlying sqlite connection."""
        conn = self._conn
        self._conn = None
        self._engine = None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            logger.warning("VectorStore connection close failed", exc_info=True)

    def get_collection(self, name: str):
        """Return a lightweight handle for a collection (interface parity).

        The sqlite-vec backend has no Chroma-style collection object; this
        returns the store itself keyed by name for callers that only need a
        reference. No production code path relies on the return value.
        """
        self._ensure_open()
        self._ensure_collection(name)
        return self

    def delete_collection(self, name: str) -> None:
        """Drop a collection's tables and recreate it empty with the current dimension.

        Used on dimension/schema/model mismatch so a stale on-disk ``vec0``
        table built with a different embedding dimension is rebuilt cleanly.
        """
        self._ensure_open()
        if self._conn is None:
            return
        vec_t, meta_t, idx = self._table_names(name)
        with self._lock:
            try:
                cur = self._conn.cursor()
                cur.execute(f"DROP TABLE IF EXISTS {vec_t}")
                cur.execute(f"DROP TABLE IF EXISTS {meta_t}")
                cur.execute(f"DROP INDEX IF EXISTS {idx}")
                self._conn.commit()
            except Exception:
                logger.warning("delete_collection(%s) drop failed", name, exc_info=True)
        # Recreate empty with the active dimension so subsequent calls succeed.
        self._ensure_collection(name)
        logger.info("Recreated empty vector collection %s", name)

    # ------------------------------------------------------------------
    # Embedding helper
    # ------------------------------------------------------------------

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Embed texts from a synchronous context.

        Mirrors the previous ``ChromaEmbeddingFunction`` shim: when invoked
        from a worker thread (the common path -- EntityIndex offloads to
        ``run_in_executor``), there is no running loop so ``asyncio.run`` is
        safe. When invoked from the event-loop thread, the coroutine is run
        in a throwaway thread to avoid blocking/deadlocking the loop.
        """
        coro = self._engine.embed_batch(texts)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    # ------------------------------------------------------------------
    # CRUD (ChromaDB-compatible signatures + result shapes)
    # ------------------------------------------------------------------

    def add(
        self,
        collection_name: str,
        ids: list[str],
        documents: list[str] | None = None,
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict] | None = None,
    ) -> None:
        """Add entries to a collection (idempotent insert-or-replace)."""
        self._write(collection_name, ids, documents, embeddings, metadatas)

    def upsert(
        self,
        collection_name: str,
        ids: list[str],
        documents: list[str] | None = None,
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict] | None = None,
    ) -> None:
        """Upsert entries into a collection."""
        try:
            self._write(collection_name, ids, documents, embeddings, metadatas)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                self._write(collection_name, ids, documents, embeddings, metadatas)
            else:
                raise

    def _write(
        self,
        collection_name: str,
        ids: list[str],
        documents: list[str] | None,
        embeddings: list[list[float]] | None,
        metadatas: list[dict] | None,
    ) -> None:
        self._ensure_open()
        self._ensure_collection(collection_name)
        if self._conn is None:
            raise RuntimeError("VectorStore connection is not open")
        vec_t, meta_t, _idx = self._table_names(collection_name)

        docs = list(documents) if documents is not None else [""] * len(ids)
        metas = list(metadatas) if metadatas is not None else [{}] * len(ids)
        if embeddings is None:
            embeddings = self._embed_sync(docs)

        with self._lock:
            cur = self._conn.cursor()
            for eid, emb, doc, meta in zip(ids, embeddings, docs, metas, strict=False):
                meta_json = json.dumps(meta, ensure_ascii=False)
                existing = cur.execute(f"SELECT vec_rowid FROM {meta_t} WHERE entity_id = ?", (eid,)).fetchone()
                if existing is not None:
                    cur.execute(f"DELETE FROM {vec_t} WHERE rowid = ?", (existing[0],))
                    cur.execute(f"INSERT INTO {vec_t}(embedding) VALUES (?)", (_serialize_vec(emb),))
                    new_rowid = cur.lastrowid
                    cur.execute(
                        f"UPDATE {meta_t} SET vec_rowid = ?, document = ?, metadata = ? WHERE entity_id = ?",
                        (new_rowid, doc, meta_json, eid),
                    )
                else:
                    cur.execute(f"INSERT INTO {vec_t}(embedding) VALUES (?)", (_serialize_vec(emb),))
                    new_rowid = cur.lastrowid
                    cur.execute(
                        f"INSERT INTO {meta_t}(entity_id, vec_rowid, document, metadata) VALUES (?, ?, ?, ?)",
                        (eid, new_rowid, doc, meta_json),
                    )
            self._conn.commit()

    def query(
        self,
        collection_name: str,
        query_texts: list[str] | None = None,
        query_embeddings: list[list[float]] | None = None,
        n_results: int = 5,
        where: dict | None = None,
        include: list[str] | None = None,
    ) -> dict:
        """k-NN cosine query. Returns a ChromaDB-shaped result dict.

        ``where`` (metadata prefilter) is NOT pushed into the vector layer;
        per Prime Directive 5, visibility/domain filtering stays in Python.
        If a ``where`` is supplied it is applied as a post-filter here so
        the public contract is honoured, but production callers do not pass one.
        """
        try:
            return self._query(collection_name, query_texts, query_embeddings, n_results, where, include)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                return self._query(collection_name, query_texts, query_embeddings, n_results, where, include)
            raise

    def _query(
        self,
        collection_name: str,
        query_texts: list[str] | None,
        query_embeddings: list[list[float]] | None,
        n_results: int,
        where: dict | None,
        include: list[str] | None,
    ) -> dict:
        self._ensure_open()
        self._ensure_collection(collection_name)
        if self._conn is None:
            return {"ids": [[]], "metadatas": [[]], "distances": [[]], "documents": [[]]}

        include = include if include is not None else ["metadatas", "distances", "documents"]
        if query_embeddings is None:
            qtexts = query_texts or [""]
            query_embeddings = self._embed_sync(qtexts)

        vec_t, meta_t, _idx = self._table_names(collection_name)
        all_ids: list[list[str]] = []
        all_metas: list[list[dict]] = []
        all_docs: list[list[str]] = []
        all_dists: list[list[float]] = []
        with self._lock:
            cur = self._conn.cursor()
            for qemb in query_embeddings:
                rows = cur.execute(
                    f"WITH knn AS (SELECT rowid, distance FROM {vec_t} "
                    f"WHERE embedding MATCH ? AND k = ?) "
                    f"SELECT knn.distance, m.entity_id, m.document, m.metadata "
                    f"FROM knn JOIN {meta_t} m ON m.vec_rowid = knn.rowid",
                    (_serialize_vec(qemb), max(1, n_results)),
                ).fetchall()
                ids: list[str] = []
                metas: list[dict] = []
                docs: list[str] = []
                dists: list[float] = []
                for distance, eid, doc, meta_json in rows:
                    meta = json.loads(meta_json) if meta_json else {}
                    if where is not None and not _matches_where(meta, where):
                        continue
                    ids.append(eid)
                    docs.append(doc or "")
                    try:
                        dists.append(float(distance))
                    except (TypeError, ValueError):
                        dists.append(0.0)
                    metas.append(meta)
                all_ids.append(ids)
                all_metas.append(metas)
                all_docs.append(docs)
                all_dists.append(dists)

        result: dict = {"ids": all_ids}
        if "metadatas" in include or where is not None:
            result["metadatas"] = all_metas
        if "documents" in include or where is not None:
            result["documents"] = all_docs
        if "distances" in include or where is not None:
            result["distances"] = all_dists
        return result

    def delete(self, collection_name: str, ids: list[str]) -> None:
        """Delete entries by ID from a collection."""
        try:
            self._delete(collection_name, ids)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                self._delete(collection_name, ids)
            else:
                raise

    def _delete(self, collection_name: str, ids: list[str]) -> None:
        self._ensure_open()
        self._ensure_collection(collection_name)
        if self._conn is None:
            return
        vec_t, meta_t, _idx = self._table_names(collection_name)
        with self._lock:
            cur = self._conn.cursor()
            for eid in ids:
                existing = cur.execute(f"SELECT vec_rowid FROM {meta_t} WHERE entity_id = ?", (eid,)).fetchone()
                if existing is not None:
                    cur.execute(f"DELETE FROM {vec_t} WHERE rowid = ?", (existing[0],))
                    cur.execute(f"DELETE FROM {meta_t} WHERE entity_id = ?", (eid,))
            self._conn.commit()

    def count(self, collection_name: str) -> int:
        """Return the number of entries in a collection."""
        try:
            return self._count(collection_name)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                return self._count(collection_name)
            raise

    def _count(self, collection_name: str) -> int:
        self._ensure_open()
        self._ensure_collection(collection_name)
        if self._conn is None:
            return 0
        _vec_t, meta_t, _idx = self._table_names(collection_name)
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) FROM {meta_t}").fetchone()
        return int(row[0]) if row else 0

    def update_metadata(
        self,
        collection_name: str,
        ids: list[str],
        metadatas: list[dict],
    ) -> None:
        """Update only metadata for existing entries (no re-embedding)."""
        try:
            self._update_metadata(collection_name, ids, metadatas)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                self._update_metadata(collection_name, ids, metadatas)
            else:
                raise

    def _update_metadata(self, collection_name: str, ids: list[str], metadatas: list[dict]) -> None:
        self._ensure_open()
        self._ensure_collection(collection_name)
        if self._conn is None:
            return
        _vec_t, meta_t, _idx = self._table_names(collection_name)
        with self._lock:
            cur = self._conn.cursor()
            for eid, meta in zip(ids, metadatas, strict=False):
                cur.execute(
                    f"UPDATE {meta_t} SET metadata = ? WHERE entity_id = ?",
                    (json.dumps(meta, ensure_ascii=False), eid),
                )
            self._conn.commit()

    def get(
        self,
        collection_name: str,
        ids: list[str] | None = None,
        where: dict | None = None,
        include: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        """Get entries by ID or full scan. Returns a ChromaDB-shaped result dict."""
        try:
            return self._get(collection_name, ids, where, include, limit, offset)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                return self._get(collection_name, ids, where, include, limit, offset)
            raise

    def _get(
        self,
        collection_name: str,
        ids: list[str] | None,
        where: dict | None,
        include: list[str] | None,
        limit: int | None,
        offset: int | None,
    ) -> dict:
        self._ensure_open()
        self._ensure_collection(collection_name)
        include = include if include is not None else ["documents", "metadatas"]
        out_ids: list[str] = []
        out_docs: list[str] = []
        out_metas: list[dict] = []
        if self._conn is None or (ids is not None and len(ids) == 0):
            result: dict = {"ids": out_ids}
            if "documents" in include:
                result["documents"] = out_docs
            if "metadatas" in include:
                result["metadatas"] = out_metas
            return result

        _vec_t, meta_t, _idx = self._table_names(collection_name)
        cols = "entity_id, document, metadata"
        sql = f"SELECT {cols} FROM {meta_t}"
        params: list = []
        if ids is not None:
            placeholders = ",".join("?" for _ in ids)
            sql += f" WHERE entity_id IN ({placeholders})"
            params.extend(ids)
        sql += " ORDER BY entity_id"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if offset:
            sql += f" OFFSET {int(offset)}"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        for eid, doc, meta_json in rows:
            meta = json.loads(meta_json) if meta_json else {}
            if where is not None and not _matches_where(meta, where):
                continue
            out_ids.append(eid)
            out_docs.append(doc or "")
            out_metas.append(meta)
        result = {"ids": out_ids}
        if "documents" in include:
            result["documents"] = out_docs
        if "metadatas" in include:
            result["metadatas"] = out_metas
        return result

    async def _is_alive(self) -> bool:
        """Check if the sqlite connection is open."""
        return self._conn is not None

    # --- Async wrappers (run blocking sqlite ops in thread executor) ---

    async def aquery(self, collection_name: str, **kwargs):
        """Async wrapper for query()."""
        return await asyncio.to_thread(self.query, collection_name, **kwargs)

    async def aupsert(self, collection_name: str, **kwargs):
        """Async wrapper for upsert()."""
        return await asyncio.to_thread(self.upsert, collection_name, **kwargs)

    async def aadd(self, collection_name: str, **kwargs):
        """Async wrapper for add()."""
        return await asyncio.to_thread(self.add, collection_name, **kwargs)

    async def adelete(self, collection_name: str, **kwargs):
        """Async wrapper for delete()."""
        return await asyncio.to_thread(self.delete, collection_name, **kwargs)

    async def acount(self, collection_name: str):
        """Async wrapper for count()."""
        return await asyncio.to_thread(self.count, collection_name)

    async def aget(self, collection_name: str, **kwargs):
        """Async wrapper for get()."""
        return await asyncio.to_thread(self.get, collection_name, **kwargs)


def _matches_where(meta: dict, where: dict) -> bool:
    """Minimal metadata equality / ``$in`` / ``$eq`` filter (parity with prior contract)."""
    for key, val in where.items():
        if isinstance(val, dict):
            if "$in" in val and meta.get(key) not in set(val["$in"]):
                return False
            if "$eq" in val and meta.get(key) != val["$eq"]:
                return False
        elif meta.get(key) != val:
            return False
    return True


_store: VectorStore | None = None
_store_init_lock = asyncio.Lock()


async def get_vector_store() -> VectorStore:
    """Return the singleton VectorStore, initializing on first call."""
    global _store
    if _store is not None and not await _store._is_alive():
        logger.warning("VectorStore singleton has dead connection, resetting")
        _store = None
    if _store is None:
        async with _store_init_lock:
            if _store is None:
                _store = VectorStore()
                await _store.initialize()
    return _store


def close_vector_store() -> None:
    """Close and reset the singleton VectorStore, if one exists."""
    global _store
    if _store is None:
        return
    try:
        _store.close()
    finally:
        _store = None
