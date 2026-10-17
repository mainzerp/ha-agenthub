"""Tests for registry-event cache invalidation fan-out."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from app.cache.cache_manager import CacheManager
from app.cache.vector_store import VectorStore

REGISTRY_KWARGS = {"reason": "registry_event", "detail": ANY}


async def _initialize_registry_runtime(*, entity_entries=None, cache_counts=None, lookups=None):
    from app.runtime_setup import _initialize_setup_dependent_services

    app = SimpleNamespace()
    app.state = SimpleNamespace()
    if lookups is not None:
        app.state.entity_lookups = lookups

    class FakeRegistry:
        async def register(self, _agent, *, replace=False):
            return None

        async def list_agents(self):
            return []

    app.state.registry = FakeRegistry()
    app.state.dispatcher = MagicMock()
    app.state.mcp_registry = MagicMock(load_from_db=AsyncMock(), add_server=AsyncMock(return_value=False))
    app.state.mcp_tool_manager = MagicMock()

    fake_ha_client = MagicMock()
    fake_ha_client.initialize = AsyncMock()
    fake_ha_client.reload = AsyncMock()
    fake_ha_client.set_state_observer = MagicMock()
    fake_ha_client.get_state = AsyncMock(return_value=None)
    fake_ha_client.get_hidden_entity_ids = AsyncMock(return_value=set())

    fake_entity_index = MagicMock()
    fake_entity_index.list_entries.return_value = entity_entries or []
    fake_entity_index.add_async = AsyncMock()
    fake_entity_index.remove_async = AsyncMock()

    fake_vector_store = MagicMock()
    fake_cache = MagicMock()
    fake_cache.initialize = AsyncMock()
    fake_cache.purge_readonly_entries = AsyncMock(return_value=0)
    fake_cache.invalidate_by_entity_id = AsyncMock(return_value=cache_counts or {"action": 1, "routing": 2})

    ws_inst = MagicMock()
    ws_inst.run = AsyncMock(return_value=None)
    ws_inst.on_event = MagicMock()

    def _fake_create_task(coro, **kwargs):
        with contextlib.suppress(Exception):
            coro.close()
        return MagicMock()

    patches = [
        patch("app.bootstrap._ha_client.HARestClient", return_value=fake_ha_client),
        patch("app.bootstrap._entity.EntityIndex", return_value=fake_entity_index),
        patch("app.bootstrap._ha_client.get_embedding_engine", new_callable=AsyncMock),
        patch("app.bootstrap._ha_client.get_vector_store", new_callable=AsyncMock, return_value=fake_vector_store),
        patch("app.bootstrap._ha_client.SqliteCacheStore", return_value=MagicMock()),
        patch("app.bootstrap._entity.schedule_entity_index_prime", new_callable=AsyncMock, return_value=True),
        patch("app.bootstrap._ha_client.home_context_provider"),
        patch("app.bootstrap._ha_client.AliasResolver"),
        patch("app.bootstrap._entity_matcher.EntityMatcher"),
        patch("app.bootstrap._agents.RewriteAgent"),
        patch("app.bootstrap._cache.CacheManager", return_value=fake_cache),
        patch(
            "app.db.repository.McpServerRepository.get",
            new_callable=AsyncMock,
            return_value={"name": "duckduckgo-search"},
        ),
        patch("app.agents.orchestrator.OrchestratorAgent"),
        patch("app.agents.general.GeneralAgent"),
        patch("app.agents.actionable.LightAgent"),
        patch("app.agents.actionable.MusicAgent"),
        patch("app.agents.filler.FillerAgent"),
        patch("app.bootstrap._agents.CustomAgentLoader"),
        patch("app.db.repository.AgentConfigRepository.get", new_callable=AsyncMock, return_value=None),
        patch("app.ha_client.websocket.HAWebSocketClient", return_value=ws_inst),
        patch("app.agents.alarm_monitor.AlarmMonitor"),
        patch("app.agents.timer_scheduler.TimerScheduler"),
        patch("app.util.tasks.asyncio.create_task", side_effect=_fake_create_task),
    ]

    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        (
            _ha_cls,
            _ei_cls,
            _embed,
            _vs,
            _sqlite_cache,
            _prime,
            mock_home_ctx,
            mock_alias_cls,
            mock_matcher_cls,
            mock_rewrite_cls,
            _cache_cls,
            _ddg_get,
            mock_orch_cls,
            mock_general_cls,
            mock_light_cls,
            mock_music_cls,
            mock_filler_cls,
            mock_custom_cls,
            _agent_cfg,
            _ws_cls,
            mock_alarm_cls,
            mock_timer_cls,
            _create_task,
        ) = mocks

        mock_home_ctx.refresh = AsyncMock()
        alias_inst = MagicMock(load=AsyncMock())
        matcher_inst = MagicMock(load_config=AsyncMock())
        mock_alias_cls.return_value = alias_inst
        mock_matcher_cls.return_value = matcher_inst
        mock_rewrite_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="rewrite-agent"))
        mock_orch_cls.return_value = MagicMock(
            agent_card=SimpleNamespace(agent_id="orchestrator"), initialize=AsyncMock()
        )
        mock_general_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="general-agent"))
        mock_light_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="light-agent"))
        mock_music_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="music-agent"))
        mock_filler_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="filler-agent"))
        loader_inst = MagicMock(load_all=AsyncMock())
        mock_custom_cls.return_value = loader_inst
        mock_alarm_cls.return_value = MagicMock(start=AsyncMock())
        mock_timer_cls.return_value = MagicMock(start=AsyncMock())

        await _initialize_setup_dependent_services(app, source="test-registry")

    handlers = {call.args[0]: call.args[1] for call in ws_inst.on_event.call_args_list}
    return handlers, fake_cache, fake_ha_client, fake_entity_index


@pytest.mark.asyncio
async def test_entity_rename_invalidates_action_and_routing():
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"](
        {
            "data": {
                "entity_id": "light.kitchen_new",
                "changes": {"entity_id": "light.kitchen_old"},
            }
        }
    )

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(
        ["light.kitchen_new", "light.kitchen_old"], **REGISTRY_KWARGS
    )
    assert ha_client.get_state.await_count == 2


@pytest.mark.asyncio
async def test_entity_remove_invalidates_action_and_routing():
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"]({"data": {"entity_id": "light.kitchen", "action": "remove"}})

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.kitchen"], **REGISTRY_KWARGS)
    ha_client.get_state.assert_awaited_once_with("light.kitchen")


@pytest.mark.asyncio
async def test_irrelevant_registry_change_does_not_invalidate():
    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"](
        {
            "data": {
                "entity_id": "light.kitchen",
                "changes": {"last_changed": "2024-01-01T00:00:00Z"},
            }
        }
    )

    cache_manager.invalidate_by_entity_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_relevant_registry_change_does_invalidate():
    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"](
        {
            "data": {
                "entity_id": "light.kitchen",
                "changes": {"name": "Kitchen Overhead"},
            }
        }
    )

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.kitchen"], **REGISTRY_KWARGS)


@pytest.mark.asyncio
async def test_empty_changes_with_entity_id_does_not_invalidate():
    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"]({"data": {"entity_id": "light.kitchen", "changes": {}}})

    cache_manager.invalidate_by_entity_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_changes_with_entity_id_does_not_invalidate():
    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"]({"data": {"entity_id": "light.kitchen"}})

    cache_manager.invalidate_by_entity_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_invalidation_is_logged(caplog):
    import logging

    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime()
    cache_manager.invalidate_by_entity_id.return_value = {"action": 3, "routing": 5}

    with caplog.at_level(logging.INFO):
        await handlers["entity_registry_updated"](
            {"data": {"entity_id": "light.kitchen", "changes": {"name": "New Name"}}}
        )

    assert "Cache invalidation succeeded" in caplog.text
    assert "action=3" in caplog.text
    assert "routing=5" in caplog.text
    assert "light.kitchen" in caplog.text


_KITCHEN_ENTRIES = [
    SimpleNamespace(entity_id="light.kitchen", area="kitchen", device_name="Kitchen Lamp"),
    SimpleNamespace(entity_id="light.kitchen_ceiling", area="kitchen", device_name="Ceiling Spot"),
    SimpleNamespace(entity_id="light.garage", area="garage", device_name="Garage Lamp"),
]


@pytest.mark.asyncio
async def test_device_rename_invalidates_only_that_devices_entities():
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime(
        entity_entries=_KITCHEN_ENTRIES
    )
    ha_client.render_template = AsyncMock(return_value='["light.kitchen", "switch.kitchen_socket"]')

    await handlers["device_registry_updated"](
        {"data": {"action": "update", "device_id": "dev-lamp", "changes": {"name_by_user": "Kitchen Lamp"}}}
    )

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(
        ["light.kitchen", "switch.kitchen_socket"], **REGISTRY_KWARGS
    )
    assert ha_client.render_template.await_args.kwargs == {"variables": {"device_id": "dev-lamp"}}
    assert ha_client.get_state.await_count == 2


@pytest.mark.asyncio
async def test_device_area_change_does_not_invalidate_every_entity_in_the_old_area():
    """Root-cause regression: a device moving out of ``kitchen`` used to
    invalidate every action row of every entity in ``kitchen``."""
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime(
        entity_entries=_KITCHEN_ENTRIES
    )
    ha_client.render_template = AsyncMock(return_value='["light.kitchen"]')

    await handlers["device_registry_updated"](
        {"data": {"action": "update", "device_id": "dev-lamp", "changes": {"area_id": "kitchen"}}}
    )

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.kitchen"], **REGISTRY_KWARGS)


@pytest.mark.asyncio
async def test_device_update_falls_back_to_broad_match_when_device_lookup_fails():
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime(
        entity_entries=_KITCHEN_ENTRIES
    )
    ha_client.render_template = AsyncMock(return_value=None)

    await handlers["device_registry_updated"](
        {"data": {"action": "update", "device_id": "dev-lamp", "changes": {"area_id": "kitchen"}}}
    )

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(
        ["light.kitchen", "light.kitchen_ceiling"], **REGISTRY_KWARGS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {"action": "update", "device_id": "dev-lamp", "changes": {"sw_version": "1.0.0"}},
        {"action": "update", "device_id": "dev-lamp", "changes": {"labels": {"x"}, "modified_at": "t"}},
        {"action": "create", "device_id": "dev-new"},
        {"action": "remove", "device_id": "dev-lamp"},
    ],
)
async def test_irrelevant_device_events_keep_cache(data):
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime(
        entity_entries=_KITCHEN_ENTRIES
    )
    ha_client.render_template = AsyncMock(return_value='["light.kitchen"]')

    await handlers["device_registry_updated"]({"data": data})

    cache_manager.invalidate_by_entity_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_cosmetic_area_update_keeps_cache():
    """Root-cause regression: HA sends ``area_registry_updated`` without a
    change set for icon / picture / alias / floor / label edits. Each one used
    to delete every cached action of every entity in that area."""
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime(
        entity_entries=_KITCHEN_ENTRIES,
        lookups={"area": {"kitchen": "Kueche"}, "alias": {}, "device": {}, "area_id": {}},
    )
    ha_client.get_area_registry = AsyncMock(return_value={"kitchen": "Kueche"})

    await handlers["area_registry_updated"]({"data": {"action": "update", "area_id": "kitchen"}})

    cache_manager.invalidate_by_entity_id.assert_not_awaited()
    ha_client.get_state.assert_not_awaited()
    ha_client.clear_area_registry_cache.assert_called_once()


@pytest.mark.asyncio
async def test_area_rename_invalidates_entities_in_that_area():
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime(
        entity_entries=_KITCHEN_ENTRIES,
        lookups={"area": {"kitchen": "Kueche"}, "alias": {}, "device": {}, "area_id": {}},
    )
    ha_client.get_area_registry = AsyncMock(return_value={"kitchen": "Kochbereich"})

    await handlers["area_registry_updated"]({"data": {"action": "update", "area_id": "kitchen"}})

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(
        ["light.kitchen", "light.kitchen_ceiling"], **REGISTRY_KWARGS
    )
    assert ha_client.get_state.await_count == 2


@pytest.mark.asyncio
async def test_area_remove_invalidates_entities_in_that_area():
    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime(
        entity_entries=_KITCHEN_ENTRIES
    )

    await handlers["area_registry_updated"]({"data": {"action": "remove", "area_id": "garage"}})

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.garage"], **REGISTRY_KWARGS)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "reorder"])
async def test_area_create_and_reorder_keep_cache(action):
    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime(
        entity_entries=_KITCHEN_ENTRIES
    )

    await handlers["area_registry_updated"]({"data": {"action": action, "area_id": "kitchen"}})

    cache_manager.invalidate_by_entity_id.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"icon": "mdi:lamp"},
        {"labels": {"evening"}},
        {"original_name": "Kitchen", "translation_key": "light", "capabilities": {"min_mireds": 153}},
        {"options": {"light": {"favorite_colors": []}}},
    ],
)
async def test_cosmetic_entity_update_keeps_cache(changes):
    """Root-cause regression: icon / label edits and integration-driven
    ``original_*`` rewrites (HA upgrades) do not change what a cached action
    targets and must not delete it."""
    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"](
        {"data": {"action": "update", "entity_id": "light.kitchen", "changes": changes}}
    )

    cache_manager.invalidate_by_entity_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_entity_create_keeps_cache():
    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"]({"data": {"action": "create", "entity_id": "light.kitchen"}})

    cache_manager.invalidate_by_entity_id.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [{"disabled_by": None}, {"hidden_by": None}, {"area_id": "kitchen"}, {"aliases": set()}, {"device_id": "x"}],
)
async def test_replay_relevant_entity_update_invalidates(changes):
    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"](
        {"data": {"action": "update", "entity_id": "light.kitchen", "changes": changes}}
    )

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.kitchen"], **REGISTRY_KWARGS)


@pytest.mark.asyncio
async def test_entity_id_rename_with_old_entity_id_invalidates_both_ids():
    handlers, cache_manager, _ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"](
        {"data": {"action": "update", "entity_id": "light.kueche", "old_entity_id": "light.kuche", "changes": {}}}
    )

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.kuche", "light.kueche"], **REGISTRY_KWARGS)


@pytest.mark.asyncio
async def test_invalidation_runs_before_index_sync():
    order: list[str] = []

    async def _invalidate(_entity_ids, **_kwargs):
        order.append("invalidate")
        return {"action": 1, "routing": 2}

    async def _get_state(_entity_id):
        order.append("refresh")

    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime()
    cache_manager.invalidate_by_entity_id.side_effect = _invalidate
    ha_client.get_state.side_effect = _get_state

    await handlers["entity_registry_updated"](
        {"data": {"entity_id": "light.kitchen", "changes": {"name": "Kitchen Light"}}}
    )

    assert order == ["invalidate", "refresh"]


@pytest.mark.asyncio
async def test_registry_listener_schedules_refresh_off_receive_loop():
    """Registry listeners must return immediately instead of awaiting the
    refresh inline -- the refresh issues send_command calls whose replies
    only the WS receive loop can deliver."""
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime()

    gate = asyncio.Event()

    async def _blocked_hidden_ids():
        await gate.wait()
        return set()

    ha_client.get_hidden_entity_ids = AsyncMock(side_effect=_blocked_hidden_ids)

    task = handlers["entity_registry_updated"](
        {"data": {"entity_id": "light.kitchen", "changes": {"name": "Kitchen Overhead"}}}
    )

    assert isinstance(task, asyncio.Task)
    for _ in range(20):
        await asyncio.sleep(0)
        if ha_client.get_hidden_entity_ids.await_count:
            break
    assert not task.done()

    gate.set()
    await asyncio.wait_for(task, timeout=2)

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.kitchen"], **REGISTRY_KWARGS)
    ha_client.get_state.assert_awaited_once_with("light.kitchen")


@pytest.mark.asyncio
async def test_invalidate_by_entity_id_returns_per_cache_counts():
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
    manager = CacheManager(store)
    # I6: invalidate_by_entity_id now batches all ids into one underlying call
    # per cache so the collection is paginated once instead of N times.
    manager._action_cache.invalidate_by_entity_id = MagicMock(return_value=3)
    manager._routing_cache.invalidate_by_entity_id = MagicMock(return_value=5)

    counts = await manager.invalidate_by_entity_id(["light.kitchen", "switch.garage"])

    assert counts == {"action": 3, "routing": 5}
    manager._action_cache.invalidate_by_entity_id.assert_called_once_with(["light.kitchen", "switch.garage"])
    manager._routing_cache.invalidate_by_entity_id.assert_called_once_with(["light.kitchen", "switch.garage"])
