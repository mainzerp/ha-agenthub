"""Tests for the action-cache invalidation sidecars (P2).

Covers:
- Sidecar rows are maintained on store/delete and used for O(matches)
  entity invalidation (no full scan).
- Backfill migration for pre-existing rows (PRAGMA user_version ladder).
- LRU eviction and flush keep the sidecars consistent.
- Indexed readonly / language / schema-version purges.
- Collections without sidecars (routing cache) keep the scan fallback.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.cache.action_cache import ActionCache
from app.cache.routing_cache import RoutingCache
from app.cache.sqlite_cache_store import (
    COLLECTION_ACTION_CACHE,
    COLLECTION_ROUTING_CACHE,
    SqliteCacheStore,
)
from app.models.cache import ActionCacheEntry, CachedAction, RoutingCacheEntry


def _entry(query: str, entity_id: str, *, service: str | None = None, language: str = "en") -> ActionCacheEntry:
    domain = entity_id.split(".", 1)[0]
    action_name = (service or "turn_on").split("/", 1)[-1]
    return ActionCacheEntry(
        query_text=query,
        language=language,
        agent_id="light-agent",
        confidence=1.0,
        response_text="done",
        cached_action=CachedAction(
            service=f"{domain}/{action_name}",
            entity_id=entity_id,
            service_data={},
        ),
        entity_ids=[entity_id],
    )


@pytest.fixture()
def store(tmp_path):
    s = SqliteCacheStore(str(tmp_path / "cache.db"))
    yield s
    s.close()


def _sidecar_entity_rows(db_path: str) -> set[tuple[str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return set(conn.execute("SELECT entry_id, entity_id FROM action_cache_entities").fetchall())
    finally:
        conn.close()


class TestEntityInvalidationSidecar:
    def test_store_maintains_sidecar_rows(self, store, tmp_path):
        cache = ActionCache(store)
        entry = _entry("turn on the kitchen light", "light.kitchen")
        cache.store(entry)
        entry_id = cache.make_entry_id("turn on the kitchen light", language="en")
        assert _sidecar_entity_rows(str(tmp_path / "cache.db")) == {(entry_id, "light.kitchen")}

    def test_invalidate_by_entity_id_removes_exactly_matching_entries(self, store):
        cache = ActionCache(store)
        cache.store(_entry("turn on the kitchen light", "light.kitchen"))
        cache.store(_entry("turn on the desk light", "light.desk"))
        cache.store(_entry("toggle the kitchen switch", "switch.kitchen"))

        removed = cache.invalidate_by_entity_id(["LIGHT.Kitchen "])
        assert removed == 1

        assert cache.lookup("turn on the kitchen light", language="en")[0] is None
        assert cache.lookup("turn on the desk light", language="en")[0] is not None
        assert cache.lookup("toggle the kitchen switch", language="en")[0] is not None

    def test_invalidate_multiple_entities_and_sidecar_cleanup(self, store, tmp_path):
        cache = ActionCache(store)
        cache.store(_entry("turn on the kitchen light", "light.kitchen"))
        cache.store(_entry("turn on the desk light", "light.desk"))
        removed = cache.invalidate_by_entity_id(["light.kitchen", "light.desk"])
        assert removed == 2
        assert _sidecar_entity_rows(str(tmp_path / "cache.db")) == set()

    def test_invalidate_by_entry_id_cleans_sidecar(self, store, tmp_path):
        cache = ActionCache(store)
        cache.store(_entry("turn on the kitchen light", "light.kitchen"))
        entry_id = cache.make_entry_id("turn on the kitchen light", language="en")
        cache.invalidate_by_entry_id(entry_id)
        assert _sidecar_entity_rows(str(tmp_path / "cache.db")) == set()

    def test_delete_oldest_cleans_sidecar(self, store, tmp_path):
        cache = ActionCache(store)
        cache.store(_entry("turn on the kitchen light", "light.kitchen"))
        cache.store(_entry("turn on the desk light", "light.desk"))
        deleted = store.delete_oldest(COLLECTION_ACTION_CACHE, 1)
        assert deleted == 1
        rows = _sidecar_entity_rows(str(tmp_path / "cache.db"))
        assert len(rows) == 1  # exactly one entry's sidecar rows remain

    def test_delete_all_clears_sidecars(self, store, tmp_path):
        cache = ActionCache(store)
        cache.store(_entry("turn on the kitchen light", "light.kitchen"))
        store.delete_all(COLLECTION_ACTION_CACHE)
        assert _sidecar_entity_rows(str(tmp_path / "cache.db")) == set()
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        try:
            assert conn.execute("SELECT COUNT(*) FROM action_cache_entry_index").fetchone()[0] == 0
        finally:
            conn.close()


class TestSidecarBackfill:
    def test_existing_rows_are_backfilled_on_open(self, tmp_path):
        db_path = str(tmp_path / "cache.db")
        store = SqliteCacheStore(db_path)
        cache = ActionCache(store)
        cache.store(_entry("turn on the kitchen light", "light.kitchen"))
        entry_id = cache.make_entry_id("turn on the kitchen light", language="en")
        store.close()

        # Simulate a pre-sidecar database: drop the sidecars and rewind
        # the schema version, then reopen -> migration must backfill.
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE action_cache_entities")
        conn.execute("DROP TABLE action_cache_entry_index")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()

        reopened = SqliteCacheStore(db_path)
        try:
            found = reopened.find_entries_by_entity_ids(COLLECTION_ACTION_CACHE, ["light.kitchen"])
            assert found == [entry_id]
        finally:
            reopened.close()


class TestIndexedPurges:
    def test_purge_readonly_entries_uses_sidecar(self, store):
        cache = ActionCache(store)
        cache.store(_entry("are the kitchen lights on", "light.kitchen", service="light/query_state"))
        cache.store(_entry("turn on the kitchen light", "light.kitchen", service="light/turn_on"))

        removed = cache.purge_readonly_entries()
        assert removed == 1
        assert cache.lookup("are the kitchen lights on", language="en")[0] is None
        assert cache.lookup("turn on the kitchen light", language="en")[0] is not None

    def test_purge_entries_without_language_uses_sidecar(self, store):
        cache = ActionCache(store)
        cache.store(_entry("turn on the kitchen light", "light.kitchen"))
        entry_id = cache.make_entry_id("turn on the kitchen light", language="en")
        # Strip the language directly in metadata + sidecar to simulate a legacy row.
        page = store.get(COLLECTION_ACTION_CACHE, ids=[entry_id], include=["metadatas"])
        meta = page["metadatas"][0]
        del meta["language"]
        store.upsert(COLLECTION_ACTION_CACHE, ids=[entry_id], documents=["turn on the kitchen light"], metadatas=[meta])

        removed = cache.purge_entries_without_language()
        assert removed == 1

    def test_purge_legacy_schema_entries_uses_sidecar(self, store):
        cache = ActionCache(store)
        cache.store(_entry("turn on the kitchen light", "light.kitchen"))
        entry_id = cache.make_entry_id("turn on the kitchen light", language="en")
        page = store.get(COLLECTION_ACTION_CACHE, ids=[entry_id], include=["metadatas"])
        meta = page["metadatas"][0]
        meta["schema_version"] = "2"
        store.upsert(COLLECTION_ACTION_CACHE, ids=[entry_id], documents=["turn on the kitchen light"], metadatas=[meta])

        removed = cache.purge_legacy_schema_entries(4)
        assert removed == 1


class TestRoutingCacheFallback:
    def test_finders_return_none_for_routing_collection(self, store):
        assert store.find_entries_by_entity_ids(COLLECTION_ROUTING_CACHE, ["light.kitchen"]) is None
        assert store.find_entries_without_language(COLLECTION_ROUTING_CACHE) is None
        assert store.find_entries_below_schema_version(COLLECTION_ROUTING_CACHE, 4) is None
        assert store.find_readonly_entries(COLLECTION_ROUTING_CACHE) is None

    def test_routing_invalidate_by_entity_still_works_via_scan(self, store):
        cache = RoutingCache(store)
        cache.store(
            RoutingCacheEntry(
                query_text="are the lights on",
                language="en",
                agent_id="light-agent",
                confidence=1.0,
                entity_ids=["light.kitchen"],
            )
        )
        removed = cache.invalidate_by_entity_id(["light.kitchen"])
        assert removed == 1


class TestSidecarMetadataParsing:
    def test_entity_ids_normalization(self):
        from app.cache.sqlite_cache_store import _meta_entity_ids

        assert _meta_entity_ids({"entity_ids": json.dumps(["Light.Kitchen", " light.desk "])}) == [
            "light.desk",
            "light.kitchen",
        ]
        assert _meta_entity_ids({"entity_ids": "not-json"}) == []
        assert _meta_entity_ids({}) == []

    def test_readonly_flag_mirrors_action_cache_semantics(self):
        from app.cache.sqlite_cache_store import _meta_is_readonly

        write_action = {"cached_action": CachedAction(service="light/turn_on", entity_id="light.k").model_dump_json()}
        read_action = {
            "cached_action": CachedAction(service="light/query_state", entity_id="light.k").model_dump_json()
        }
        assert _meta_is_readonly(write_action) is False
        assert _meta_is_readonly(read_action) is True
        assert _meta_is_readonly({"cached_action": "broken json"}) is True
        assert _meta_is_readonly({}) is True
