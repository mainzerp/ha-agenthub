"""Unit tests for SqliteCacheStore.

Tests against a real temp-file SQLite DB.
"""

from __future__ import annotations

import os
import tempfile

from app.cache.sqlite_cache_store import COLLECTION_ACTION_CACHE, COLLECTION_ROUTING_CACHE, SqliteCacheStore


class TestSqliteCacheStore:
    def test_init_connect_close_and_ensure_conn(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        store = SqliteCacheStore(db_path=path)
        assert store._conn is not None

        # _ensure_conn should return the same connection when open
        conn = store._ensure_conn()
        assert conn is store._conn

        store.close()
        assert store._conn is None

        # _ensure_conn should reconnect after close
        conn2 = store._ensure_conn()
        assert conn2 is not None
        assert conn2 is store._conn

        store.close()
        os.unlink(path)

    def test_upsert_and_get_variants(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store = SqliteCacheStore(db_path=path)

        # Basic upsert + get
        store.upsert(
            COLLECTION_ROUTING_CACHE,
            ids=["entry1"],
            documents=["doc1"],
            metadatas=[{"key": "val"}],
        )
        result = store.get(COLLECTION_ROUTING_CACHE, ids=["entry1"])
        assert result["ids"] == ["entry1"]
        assert result["documents"] == ["doc1"]
        assert result["metadatas"] == [{"key": "val"}]

        # Get without include returns everything
        result_all = store.get(COLLECTION_ROUTING_CACHE, ids=["entry1"])
        assert "documents" in result_all
        assert "metadatas" in result_all

        # Get with include filtering
        result_ids_only = store.get(COLLECTION_ROUTING_CACHE, ids=["entry1"], include=[])
        assert "documents" not in result_ids_only
        assert "metadatas" not in result_ids_only

        result_docs_only = store.get(COLLECTION_ROUTING_CACHE, ids=["entry1"], include=["documents"])
        assert "documents" in result_docs_only
        assert "metadatas" not in result_docs_only

        # Get without ids returns all rows
        store.upsert(
            COLLECTION_ROUTING_CACHE,
            ids=["entry2"],
            documents=["doc2"],
            metadatas=[{"key2": "val2"}],
        )
        result_all_rows = store.get(COLLECTION_ROUTING_CACHE)
        assert len(result_all_rows["ids"]) == 2

        # Limit and offset
        result_limited = store.get(COLLECTION_ROUTING_CACHE, limit=1)
        assert len(result_limited["ids"]) == 1

        result_offset = store.get(COLLECTION_ROUTING_CACHE, limit=1, offset=1)
        assert len(result_offset["ids"]) == 1

        # Upsert into action_cache collection
        store.upsert(
            COLLECTION_ACTION_CACHE,
            ids=["action1"],
            documents=["action_doc"],
            metadatas=[{"action": "true"}],
        )
        result_action = store.get(COLLECTION_ACTION_CACHE, ids=["action1"])
        assert result_action["ids"] == ["action1"]
        assert result_action["documents"] == ["action_doc"]

        # Default metadata when metadatas is None
        store.upsert(
            COLLECTION_ROUTING_CACHE,
            ids=["entry3"],
            documents=["doc3"],
        )
        result_default_meta = store.get(COLLECTION_ROUTING_CACHE, ids=["entry3"])
        assert result_default_meta["metadatas"] == [{}]

        store.close()
        os.unlink(path)

    def test_delete_count_update_metadata_delete_oldest_delete_all(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store = SqliteCacheStore(db_path=path)

        # Seed data
        for i in range(5):
            store.upsert(
                COLLECTION_ROUTING_CACHE,
                ids=[f"entry{i}"],
                documents=[f"doc{i}"],
                metadatas=[{"idx": i}],
            )

        # count
        assert store.count(COLLECTION_ROUTING_CACHE) == 5
        assert store.count(COLLECTION_ACTION_CACHE) == 0

        # delete
        store.delete(COLLECTION_ROUTING_CACHE, ids=["entry0", "entry1"])
        assert store.count(COLLECTION_ROUTING_CACHE) == 3

        # delete with empty ids is a no-op
        store.delete(COLLECTION_ROUTING_CACHE, ids=[])
        assert store.count(COLLECTION_ROUTING_CACHE) == 3

        # update_metadata
        store.update_metadata(
            COLLECTION_ROUTING_CACHE,
            ids=["entry2", "entry3"],
            metadatas=[{"updated": True}, {"updated": False}],
        )
        result = store.get(COLLECTION_ROUTING_CACHE, ids=["entry2", "entry3"])
        assert result["metadatas"] == [{"updated": True}, {"updated": False}]

        # update_metadata with empty ids is a no-op
        store.update_metadata(COLLECTION_ROUTING_CACHE, ids=[], metadatas=[])
        assert store.count(COLLECTION_ROUTING_CACHE) == 3

        # delete_oldest
        deleted = store.delete_oldest(COLLECTION_ROUTING_CACHE, n=1)
        assert deleted == 1
        assert store.count(COLLECTION_ROUTING_CACHE) == 2

        # delete_oldest with n <= 0 is a no-op
        assert store.delete_oldest(COLLECTION_ROUTING_CACHE, n=0) == 0
        assert store.delete_oldest(COLLECTION_ROUTING_CACHE, n=-1) == 0

        # delete_all
        deleted_all = store.delete_all(COLLECTION_ROUTING_CACHE)
        assert deleted_all == 2
        assert store.count(COLLECTION_ROUTING_CACHE) == 0

        # delete_all on empty collection
        assert store.delete_all(COLLECTION_ROUTING_CACHE) == 0

        store.close()
        os.unlink(path)
