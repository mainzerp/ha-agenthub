"""Schema 6 action-cache origin binding: global rows by default, area binding when needed.

Covers how ``CacheOrchestrator.store_after_dispatch`` decides the binding
from the executed entity's area, the store/replay round trip across
satellites, and the startup purge of schema-5 rows.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.cache_orchestrator import CacheOrchestrator
from app.cache.action_cache import _ACTION_CACHE_SCHEMA_VERSION
from app.cache.cache_manager import ActionReplayOutcome, ActionReplayRejected, CacheManager
from app.cache.sqlite_cache_store import COLLECTION_ACTION_CACHE, SqliteCacheStore
from app.models.agent import IngressTask, TaskContext
from tests.helpers import make_action_cache_entry, make_entity_index_entry

_WRITE_RESULT = {
    "success": True,
    "action": "turn_off",
    "entity_id": "light.kitchen",
    "service_data": {},
}


class _FakeIndex:
    """Minimal entity index exposing the async by-id lookup used at store time."""

    def __init__(self, entries) -> None:
        self._entries = {entry.entity_id: entry for entry in entries}

    async def get_by_id_async(self, entity_id: str):
        return self._entries.get(entity_id)


def _kitchen_light(area: str | None = "kitchen", area_name: str | None = "Kitchen"):
    return make_entity_index_entry(entity_id="light.kitchen", friendly_name="Kitchen", area=area, area_name=area_name)


async def _store(co: CacheOrchestrator, context: TaskContext | None, text: str = "kitchen off"):
    task = IngressTask(description=text, context=context)
    with patch.object(co, "_get_bool_setting_impl", new=AsyncMock(return_value=True)):
        return await co.store_after_dispatch(
            user_text=text,
            language="en",
            target_agent="light-agent",
            condensed_task=text,
            confidence=0.9,
            speech="Done.",
            original_response_text="Done.",
            action_executed=dict(_WRITE_RESULT),
            has_error=False,
            task=task,
            used_origin_context=bool(context and (context.area_id or context.device_id)),
        )


def _mock_orchestrator(entity_index) -> tuple[CacheOrchestrator, MagicMock]:
    cm = MagicMock()
    cm.store_action_async = AsyncMock()
    cm.store_routing_async = AsyncMock()
    return CacheOrchestrator(cache_manager=cm, entity_index=entity_index), cm


def _stored_entry(cm: MagicMock):
    cm.store_action_async.assert_awaited_once()
    return cm.store_action_async.await_args.args[0]


class TestStoreBinding:
    @pytest.mark.asyncio
    async def test_target_outside_origin_area_is_stored_global(self):
        co, cm = _mock_orchestrator(_FakeIndex([_kitchen_light()]))

        result = await _store(co, TaskContext(area_id="living", area_name="Living", device_id="satellite.living"))

        assert result == (True, False)
        entry = _stored_entry(cm)
        assert entry.origin_required is False
        assert entry.origin_area_id is None
        assert entry.origin_device_id is None
        assert entry.schema_version == _ACTION_CACHE_SCHEMA_VERSION == 6

    @pytest.mark.asyncio
    async def test_target_inside_origin_area_is_area_bound_never_device_bound(self):
        co, cm = _mock_orchestrator(_FakeIndex([_kitchen_light()]))

        result = await _store(co, TaskContext(area_id="kitchen", area_name="Kitchen", device_id="satellite.kitchen"))

        assert result == (True, False)
        entry = _stored_entry(cm)
        assert entry.origin_required is True
        assert entry.origin_area_id == "kitchen"
        assert entry.origin_device_id is None

    @pytest.mark.asyncio
    async def test_device_only_turn_is_stored_global(self):
        co, cm = _mock_orchestrator(_FakeIndex([_kitchen_light()]))

        await _store(co, TaskContext(device_id="satellite.kitchen"))

        entry = _stored_entry(cm)
        assert entry.origin_required is False
        assert entry.origin_area_id is None
        assert entry.origin_device_id is None

    @pytest.mark.asyncio
    async def test_origin_less_turn_is_stored_global(self):
        co, cm = _mock_orchestrator(_FakeIndex([_kitchen_light()]))

        await _store(co, TaskContext())

        entry = _stored_entry(cm)
        assert entry.origin_required is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entity_index", [None, _FakeIndex([])])
    async def test_undeterminable_target_area_binds_to_area(self, entity_index):
        co, cm = _mock_orchestrator(entity_index)

        await _store(co, TaskContext(area_id="living", device_id="satellite.living"))

        entry = _stored_entry(cm)
        assert entry.origin_required is True
        assert entry.origin_area_id == "living"
        assert entry.origin_device_id is None

    @pytest.mark.asyncio
    async def test_target_without_area_is_stored_global(self):
        co, cm = _mock_orchestrator(_FakeIndex([_kitchen_light(area=None, area_name=None)]))

        await _store(co, TaskContext(area_id="kitchen", area_name="Kitchen"))

        entry = _stored_entry(cm)
        assert entry.origin_required is False

    @pytest.mark.asyncio
    async def test_area_name_only_match_without_area_id_stores_routing_row_only(self):
        """The prompt saw the area name but no area id exists to bind to: no action row."""
        co, cm = _mock_orchestrator(_FakeIndex([_kitchen_light(area="kitchen", area_name="Küche")]))

        result = await _store(co, TaskContext(area_name="Kueche"))

        assert result == (False, True)
        cm.store_action_async.assert_not_called()
        cm.store_routing_async.assert_awaited_once()


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_global_row_learned_on_one_satellite_replays_on_another(self, tmp_path):
        store = SqliteCacheStore(str(tmp_path / "cache.db"))
        manager = CacheManager(store)
        manager._action_cache._enabled = True
        co = CacheOrchestrator(cache_manager=manager, entity_index=_FakeIndex([_kitchen_light()]))

        await _store(co, TaskContext(area_id="living", device_id="satellite.living"), text="Kitchen off.")
        replay = AsyncMock(return_value={"success": True})
        with patch("app.cache.cache_manager.track_cache_event_background"):
            outcome = await manager.try_replay_action(
                # Different satellite, different area, STT punctuation variant.
                query_text="Kitchen, off",
                language="en",
                origin_area_id="bedroom",
                origin_device_id="satellite.bedroom",
                check_visibility=AsyncMock(return_value=True),
                execute_cached_action=replay,
            )

        assert isinstance(outcome, ActionReplayOutcome)
        replay.assert_awaited_once()
        store.close()

    @pytest.mark.asyncio
    async def test_area_bound_row_replays_only_in_its_area(self, tmp_path):
        store = SqliteCacheStore(str(tmp_path / "cache.db"))
        manager = CacheManager(store)
        manager._action_cache._enabled = True
        co = CacheOrchestrator(cache_manager=manager, entity_index=_FakeIndex([_kitchen_light()]))

        await _store(co, TaskContext(area_id="kitchen", device_id="satellite.kitchen_a"), text="light off")
        check = AsyncMock(return_value=True)
        with patch("app.cache.cache_manager.track_cache_event_background"):
            other_satellite_same_area = await manager.try_replay_action(
                query_text="light off",
                language="en",
                origin_area_id="kitchen",
                origin_device_id="satellite.kitchen_b",
                check_visibility=check,
                execute_cached_action=AsyncMock(return_value={"success": True}),
            )
            other_area = await manager.try_replay_action(
                query_text="light off",
                language="en",
                origin_area_id="bedroom",
                origin_device_id="satellite.kitchen_a",
                check_visibility=check,
                execute_cached_action=AsyncMock(return_value={"success": True}),
            )

        assert isinstance(other_satellite_same_area, ActionReplayOutcome)
        assert isinstance(other_area, ActionReplayRejected)
        assert other_area.reason == "origin_mismatch"
        assert other_area.details == {"expected_area_id": "kitchen", "actual_area_id": "bedroom"}
        store.close()


class TestSchemaPurge:
    @pytest.mark.asyncio
    async def test_initialize_purges_schema_5_action_rows(self, tmp_path):
        store = SqliteCacheStore(str(tmp_path / "cache.db"))
        manager = CacheManager(store)
        manager._action_cache._enabled = True
        legacy = make_action_cache_entry(query_text="kitchen off")
        legacy.schema_version = 5
        legacy.origin_required = True
        legacy.origin_device_id = "satellite.kitchen"
        current = make_action_cache_entry(query_text="living room off")
        manager._action_cache.store(legacy)
        manager._action_cache.store(current)
        assert store.count(COLLECTION_ACTION_CACHE) == 2

        with patch("app.cache._base_cache.SettingsRepository") as settings:
            settings.get_value = AsyncMock(return_value=None)
            await manager.initialize()

        remaining = store.get(COLLECTION_ACTION_CACHE, include=["documents"])
        assert remaining["documents"] == ["living room off"]
        store.close()
