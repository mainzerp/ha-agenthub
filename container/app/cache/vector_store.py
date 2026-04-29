"""ChromaDB wrapper managing cache and entity index collections."""

from __future__ import annotations

import asyncio
import logging
import threading

import chromadb
from chromadb.api.models.Collection import Collection

from app.cache.embedding import ChromaEmbeddingFunction, get_embedding_engine
from app.config import settings

logger = logging.getLogger(__name__)

COLLECTION_ENTITY_INDEX = "entity_index"
COLLECTION_ROUTING_CACHE = "routing_cache"
COLLECTION_ACTION_CACHE = "action_cache"
COLLECTION_RESPONSE_CACHE = "response_cache"


def _is_client_closed_error(exc: BaseException) -> bool:
    # Narrow the heuristic to chromadb client-closed / connection-closed wording
    # so unrelated errors whose stringification happens to contain "closed" do
    # not trigger a heavyweight PersistentClient reinit.
    msg = str(exc).lower()
    return (
        "client is closed" in msg
        or "client closed" in msg
        or "connection closed" in msg
        or "connection is closed" in msg
    )


class VectorStore:
    """Manages ChromaDB PersistentClient and all cache/index collections."""

    def __init__(self) -> None:
        self._client: chromadb.ClientAPI | None = None
        self._embedding_fn: ChromaEmbeddingFunction | None = None
        self._collections: dict[str, Collection] = {}
        # P3-3: serialise reinitialisation across worker threads so
        # concurrent callers (cache writes detecting a closed client at
        # the same moment) do not stomp on each other and create
        # multiple PersistentClient instances against the same path.
        self._reinit_lock: threading.Lock = threading.Lock()

    async def initialize(self) -> None:
        """Create PersistentClient and get/create all collections."""
        engine = await get_embedding_engine()
        self._embedding_fn = ChromaEmbeddingFunction(engine)
        self._client = chromadb.PersistentClient(path=settings.chromadb_persist_dir)
        for name in (COLLECTION_ENTITY_INDEX, COLLECTION_ROUTING_CACHE, COLLECTION_ACTION_CACHE):
            self._collections[name] = self._client.get_or_create_collection(
                name=name,
                embedding_function=self._embedding_fn,
                metadata={"hnsw:space": "cosine"},
            )
        self._delete_legacy_response_collection()
        logger.info(
            "VectorStore initialized with %d collections at %s",
            len(self._collections),
            settings.chromadb_persist_dir,
        )

    def _delete_legacy_response_collection(self) -> None:
        """Drop the obsolete v3 response cache collection after v4 init."""
        if self._client is None:
            return
        try:
            self._client.delete_collection(name=COLLECTION_RESPONSE_CACHE)
        except Exception as exc:
            # Accept both ChromaDB's NotFoundError and generic "does not exist" wording.
            msg = str(exc).lower()
            if ("not" in msg and "found" in msg) or "does not exist" in msg or "no such collection" in msg:
                return
            logger.debug("Ignoring legacy response_cache delete failure: %s", exc)
        else:
            logger.info("Dropped legacy Chroma collection %s", COLLECTION_RESPONSE_CACHE)

    def _is_alive(self) -> bool:
        """Check if the ChromaDB client is still responsive."""
        try:
            self._client.heartbeat()
            return True
        except Exception:
            return False

    def _reinitialize_sync(self) -> None:
        """Re-create the PersistentClient and re-fetch all collections.

        P3-3: guarded by ``_reinit_lock`` so parallel ``add`` / ``upsert``
        retries cannot both rebuild the client. The double-checked
        ``_is_alive`` guard inside the lock makes the second waiter a
        no-op when the first reinit already restored a working client.
        """
        with self._reinit_lock:
            if self._client is not None and self._is_alive():
                return
            logger.warning("ChromaDB client dead, reinitializing VectorStore")
            self._client = chromadb.PersistentClient(path=settings.chromadb_persist_dir)
            for name in (COLLECTION_ENTITY_INDEX, COLLECTION_ROUTING_CACHE, COLLECTION_ACTION_CACHE):
                self._collections[name] = self._client.get_or_create_collection(
                    name=name,
                    embedding_function=self._embedding_fn,
                    metadata={"hnsw:space": "cosine"},
                )
            self._delete_legacy_response_collection()
            logger.info("VectorStore reinitialized successfully")

    def close(self) -> None:
        """Close the underlying Chroma client and clear cached state."""
        client = self._client
        self._collections.clear()
        self._embedding_fn = None
        self._client = None
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.warning("VectorStore client close failed", exc_info=True)

    def get_collection(self, name: str) -> Collection:
        """Return a named collection. Must call initialize() first."""
        return self._collections[name]

    def delete_collection(self, name: str) -> None:
        """Delete a collection from the underlying client and re-create it empty.

        Used when an on-disk HNSW segment is incompatible with the current
        embedding model (e.g. dimension mismatch after switching models).
        Swallows NotFoundError so callers can treat the call as idempotent.
        """
        if self._client is None:
            return
        try:
            self._client.delete_collection(name=name)
        except Exception as exc:  # chromadb raises various NotFoundError variants
            if "not" in str(exc).lower() and "found" in str(exc).lower():
                logger.debug("delete_collection: %s did not exist", name)
            else:
                logger.warning(
                    "delete_collection(%s) raised %s: %s",
                    name,
                    type(exc).__name__,
                    exc,
                )
        # Re-create empty so subsequent get_collection() calls succeed.
        self._collections[name] = self._client.get_or_create_collection(
            name=name,
            embedding_function=self._embedding_fn,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("Recreated empty Chroma collection %s", name)

    def add(
        self,
        collection_name: str,
        ids: list[str],
        documents: list[str] | None = None,
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict] | None = None,
    ) -> None:
        """Add entries to a collection."""
        col = self.get_collection(collection_name)
        kwargs: dict = {"ids": ids}
        if documents is not None:
            kwargs["documents"] = documents
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        if metadatas is not None:
            kwargs["metadatas"] = metadatas
        try:
            col.add(**kwargs)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                self.get_collection(collection_name).add(**kwargs)
            else:
                raise

    def upsert(
        self,
        collection_name: str,
        ids: list[str],
        documents: list[str] | None = None,
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict] | None = None,
    ) -> None:
        """Upsert entries into a collection."""
        col = self.get_collection(collection_name)
        kwargs: dict = {"ids": ids}
        if documents is not None:
            kwargs["documents"] = documents
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        if metadatas is not None:
            kwargs["metadatas"] = metadatas
        try:
            col.upsert(**kwargs)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                self.get_collection(collection_name).upsert(**kwargs)
            else:
                raise

    def query(
        self,
        collection_name: str,
        query_texts: list[str] | None = None,
        query_embeddings: list[list[float]] | None = None,
        n_results: int = 5,
        where: dict | None = None,
        include: list[str] | None = None,
    ) -> dict:
        """Query a collection by text or embedding. Returns ChromaDB result dict."""
        col = self.get_collection(collection_name)
        kwargs: dict = {"n_results": n_results}
        if query_texts is not None:
            kwargs["query_texts"] = query_texts
        if query_embeddings is not None:
            kwargs["query_embeddings"] = query_embeddings
        if where is not None:
            kwargs["where"] = where
        if include is not None:
            kwargs["include"] = include
        try:
            return col.query(**kwargs)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                return self.get_collection(collection_name).query(**kwargs)
            raise

    def delete(self, collection_name: str, ids: list[str]) -> None:
        """Delete entries by ID from a collection."""
        try:
            self.get_collection(collection_name).delete(ids=ids)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                self.get_collection(collection_name).delete(ids=ids)
            else:
                raise

    def count(self, collection_name: str) -> int:
        """Return the number of entries in a collection."""
        try:
            return self.get_collection(collection_name).count()
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                return self.get_collection(collection_name).count()
            raise

    def update_metadata(
        self,
        collection_name: str,
        ids: list[str],
        metadatas: list[dict],
    ) -> None:
        """Update only metadata for existing entries (no re-embedding)."""
        col = self.get_collection(collection_name)
        try:
            col.update(ids=ids, metadatas=metadatas)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                self.get_collection(collection_name).update(ids=ids, metadatas=metadatas)
            else:
                raise

    def get(
        self,
        collection_name: str,
        ids: list[str] | None = None,
        where: dict | None = None,
        include: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        """Get entries by ID or filter from a collection."""
        col = self.get_collection(collection_name)
        kwargs: dict = {}
        if ids is not None:
            kwargs["ids"] = ids
        if where is not None:
            kwargs["where"] = where
        if include is not None:
            kwargs["include"] = include
        if limit is not None:
            kwargs["limit"] = limit
        if offset is not None:
            kwargs["offset"] = offset
        try:
            return col.get(**kwargs)
        except Exception as exc:
            if _is_client_closed_error(exc):
                self._reinitialize_sync()
                return self.get_collection(collection_name).get(**kwargs)
            raise

    # --- Async wrappers (run blocking ChromaDB ops in thread executor) ---

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


_store: VectorStore | None = None
_store_init_lock = asyncio.Lock()


async def get_vector_store() -> VectorStore:
    """Return the singleton VectorStore, initializing on first call."""
    global _store
    if _store is not None and not _store._is_alive():
        logger.warning("VectorStore singleton has dead client, resetting")
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
