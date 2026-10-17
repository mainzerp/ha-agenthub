"""Action-cache deletions are observable and wait for the entity index.

Covers:
- every deletion path logs reason, key and entity ids at INFO and counts the
  reason in the action-tier cache stats;
- bulk deletions log one WARNING summary (persisted across restarts);
- a visibility rejection while the entity index has not loaded its first HA
  snapshot keeps the row (it is still never replayed);
- the entity bootstrap publishes that readiness signal.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache.cache_manager import CacheManager
from app.cache.sqlite_cache_store import COLLECTION_ACTION_CACHE, SqliteCacheStore
from app.models.cache import ActionCacheEntry, CachedAction


def _entry(query: str, entity_id: str) -> ActionCacheEntry:
    domain = entity_id.split(".", 1)[0]
    return ActionCacheEntry(
        query_text=query,
        language="de",
        agent_id="light-agent",
        confidence=1.0,
        response_text="ok",
        cached_action=CachedAction(service=f"{domain}/turn_on", entity_id=entity_id, service_data={}),
        entity_ids=[entity_id],
    )


@pytest.fixture()
def manager(tmp_path):
    store = SqliteCacheStore(str(tmp_path / "cache.db"))
    yield CacheManager(store)
    store.close()


def _key(manager: CacheManager, query: str) -> str:
    return manager.action_cache.make_entry_id(query, language="de")


def _row_exists(manager: CacheManager, query: str) -> bool:
    page = manager._cache_store.get(COLLECTION_ACTION_CACHE, ids=[_key(manager, query)], include=["metadatas"])
    return bool(page.get("ids"))


class TestDeletionLogging:
    @pytest.mark.asyncio
    async def test_entity_invalidation_logs_each_row_and_counts_reason(self, manager, caplog):
        manager.store_action(_entry("Kueche ausschalten.", "light.kuche"))
        manager.store_action(_entry("Keller einschalten.", "light.keller"))

        with caplog.at_level(logging.INFO, logger="app.cache.cache_manager"):
            counts = await manager.invalidate_by_entity_id(
                ["light.kuche"], reason="registry_event", detail="entity_registry_updated action=remove"
            )

        assert counts["action"] == 1
        assert not _row_exists(manager, "Kueche ausschalten.")
        assert _row_exists(manager, "Keller einschalten.")
        deleted = [r for r in caplog.records if "Action cache row deleted" in r.getMessage()]
        assert len(deleted) == 1
        message = deleted[0].getMessage()
        assert deleted[0].levelno == logging.INFO
        assert "reason=registry_event" in message
        assert _key(manager, "Kueche ausschalten.") in message
        assert "light.kuche" in message
        assert "entity_registry_updated action=remove" in message
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert manager.get_stats()["action"]["deletions"] == {"registry_event": 1}

    @pytest.mark.asyncio
    async def test_bulk_invalidation_logs_single_warning_summary(self, manager, caplog):
        queries = [f"Licht {i} an." for i in range(5)]
        for query in queries:
            manager.store_action(_entry(query, "light.kuche"))

        with caplog.at_level(logging.INFO, logger="app.cache.cache_manager"):
            counts = await manager.invalidate_by_entity_id(["light.kuche"], reason="registry_event")

        assert counts["action"] == 5
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "Bulk action-cache deletion: 5 rows" in warnings[0].getMessage()
        assert "reason=registry_event" in warnings[0].getMessage()
        assert len([r for r in caplog.records if "Action cache row deleted" in r.getMessage()]) == 5

    def test_manual_delete_and_flush_are_logged_and_counted(self, manager, caplog):
        manager.store_action(_entry("Kueche ausschalten.", "light.kuche"))
        for i in range(4):
            manager.store_action(_entry(f"Licht {i} an.", "light.keller"))

        with caplog.at_level(logging.INFO, logger="app.cache.cache_manager"):
            assert manager.invalidate_action(_key(manager, "Kueche ausschalten."))
            manager.flush("action")

        text = caplog.text
        assert "reason=manual_delete" in text
        assert "light.kuche" in text
        assert "Bulk action-cache deletion: 4 rows, reason=flush" in text
        assert manager.get_deletion_stats()["deletions"] == {"manual_delete": 1, "flush": 4}

    def test_validator_snapshot_delete_defaults_to_validator_reason(self, manager):
        manager.store_action(_entry("Kueche ausschalten.", "light.kuche"))
        key = _key(manager, "Kueche ausschalten.")
        created_at = manager._cache_store.get(COLLECTION_ACTION_CACHE, ids=[key], include=["metadatas"])["metadatas"][
            0
        ]["created_at"]

        assert manager.invalidate_action(key, expected_created_at=created_at)

        assert manager.get_deletion_stats()["deletions"] == {"validator": 1}

    @pytest.mark.asyncio
    async def test_lookup_rejection_delete_is_logged_with_reason(self, manager, caplog):
        manager.store_action(_entry("Kueche ausschalten.", "light.kuche"))
        key = _key(manager, "Kueche ausschalten.")

        with caplog.at_level(logging.INFO, logger="app.cache.cache_manager"):
            deleted = await manager._invalidate_action_entry(key, reason="legacy_schema", entity_ids=["light.kuche"])

        assert deleted is True
        assert not _row_exists(manager, "Kueche ausschalten.")
        assert f"reason=legacy_schema key={key}" in caplog.text
        assert manager.get_deletion_stats()["deletions"] == {"legacy_schema": 1}


class TestEntityIndexReadiness:
    @pytest.mark.asyncio
    async def test_visibility_rejection_keeps_row_while_index_not_loaded(self, manager, caplog):
        """Root-cause regression: right after a restart the in-memory entity
        index is empty, every visibility recheck fails on the metadata gap and
        the row used to be deleted although it was valid."""
        manager.store_action(_entry("Kueche ausschalten.", "light.kuche"))
        key = _key(manager, "Kueche ausschalten.")
        loaded = False
        manager.set_entity_index_readiness_probe(lambda: loaded)

        with caplog.at_level(logging.INFO, logger="app.cache.cache_manager"):
            deleted = await manager._invalidate_action_entry(
                key, reason="visibility_rejected", entity_ids=["light.kuche"]
            )

        assert deleted is False
        assert _row_exists(manager, "Kueche ausschalten.")
        assert "entity index not loaded yet" in caplog.text
        assert manager.get_deletion_stats() == {"deletions": {}, "deletions_deferred_index_not_ready": 1}

        loaded = True
        assert await manager._invalidate_action_entry(key, reason="visibility_rejected", entity_ids=["light.kuche"])
        assert not _row_exists(manager, "Kueche ausschalten.")
        assert manager.get_deletion_stats()["deletions"] == {"visibility_rejected": 1}

    @pytest.mark.asyncio
    async def test_index_independent_rejections_still_delete_during_warmup(self, manager):
        manager.store_action(_entry("Kueche ausschalten.", "light.kuche"))
        manager.set_entity_index_readiness_probe(lambda: False)

        assert await manager._invalidate_action_entry(_key(manager, "Kueche ausschalten."), reason="invalid_command")
        assert not _row_exists(manager, "Kueche ausschalten.")

    def test_failing_probe_counts_as_not_ready(self, manager):
        def _boom() -> bool:
            raise RuntimeError("probe failed")

        assert manager.entity_index_ready is True
        manager.set_entity_index_readiness_probe(_boom)
        assert manager.entity_index_ready is False


class TestBootstrapReadinessSignal:
    @pytest.mark.asyncio
    async def test_new_index_is_unloaded_until_full_sync(self):
        from app.bootstrap import _entity as entity_bootstrap

        index = SimpleNamespace()
        app = SimpleNamespace(state=SimpleNamespace())
        with (
            patch.object(entity_bootstrap, "EntityIndex", return_value=index),
            patch.object(entity_bootstrap, "register_entity_index"),
            patch.object(entity_bootstrap, "schedule_entity_index_prime", new=AsyncMock(return_value=True)),
        ):
            await entity_bootstrap.setup_entity_index(app, "test", MagicMock(), MagicMock())

        assert entity_bootstrap.entity_index_snapshot_loaded(index) is False

        index.sync_async = AsyncMock(return_value={"added": 1, "updated": 0, "removed": 0, "unchanged": 0})
        index.mutation_generation = lambda: 0
        app.state.ha_client = MagicMock()
        app.state.entity_index = index
        snapshot = [SimpleNamespace(entity_id="light.kuche")]
        with patch.object(entity_bootstrap, "build_entity_snapshot", new=AsyncMock(return_value=snapshot)):
            await entity_bootstrap.resync_entity_index(app, reason="periodic")

        assert entity_bootstrap.entity_index_snapshot_loaded(index) is True

    @pytest.mark.asyncio
    async def test_empty_snapshot_does_not_mark_index_loaded(self):
        from app.bootstrap import _entity as entity_bootstrap

        index = SimpleNamespace(
            initial_snapshot_loaded=False,
            sync_async=AsyncMock(return_value={"added": 0, "updated": 0, "removed": 0, "unchanged": 0}),
            mutation_generation=lambda: 0,
        )
        app = SimpleNamespace(state=SimpleNamespace(ha_client=MagicMock(), entity_index=index))
        with patch.object(entity_bootstrap, "build_entity_snapshot", new=AsyncMock(return_value=[])):
            await entity_bootstrap.resync_entity_index(app, reason="ws_reconnect")

        assert entity_bootstrap.entity_index_snapshot_loaded(index) is False

    def test_untracked_index_counts_as_loaded(self):
        from app.bootstrap._entity import entity_index_snapshot_loaded

        assert entity_index_snapshot_loaded(SimpleNamespace()) is True
        assert entity_index_snapshot_loaded(None) is False

    @pytest.mark.asyncio
    async def test_observers_register_readiness_probe_on_cache_manager(self, manager):
        from app.bootstrap import _entity as entity_bootstrap

        index = SimpleNamespace(initial_snapshot_loaded=False)
        app = SimpleNamespace(state=SimpleNamespace(ws_client=MagicMock(), sync_task=MagicMock(done=lambda: False)))

        def _close(_app, coro, _name):
            coro.close()

        with patch.object(entity_bootstrap, "spawn_background", side_effect=_close):
            await entity_bootstrap.setup_entity_observers(app, "test", MagicMock(), index, manager)

        assert manager.entity_index_ready is False
        index.initial_snapshot_loaded = True
        assert manager.entity_index_ready is True
