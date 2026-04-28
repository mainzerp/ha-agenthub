"""Tests for registry-event cache invalidation fan-out."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache.cache_manager import CacheManager
from app.cache.vector_store import VectorStore


async def _initialize_registry_runtime(*, entity_entries=None, cache_counts=None):
    from app.runtime_setup import _initialize_setup_dependent_services

    app = SimpleNamespace()
    app.state = SimpleNamespace()

    class FakeRegistry:
        async def register(self, _agent):
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

    def _fake_create_task(coro):
        try:
            coro.close()
        except Exception:
            pass
        return MagicMock()

    patches = [
        patch("app.runtime_setup.HARestClient", return_value=fake_ha_client),
        patch("app.runtime_setup.EntityIndex", return_value=fake_entity_index),
        patch("app.runtime_setup.get_embedding_engine", new_callable=AsyncMock),
        patch("app.runtime_setup.get_vector_store", new_callable=AsyncMock, return_value=fake_vector_store),
        patch("app.runtime_setup.schedule_entity_index_prime", new_callable=AsyncMock, return_value=True),
        patch("app.runtime_setup.home_context_provider"),
        patch("app.runtime_setup.AliasResolver"),
        patch("app.runtime_setup.EntityMatcher"),
        patch("app.runtime_setup.RewriteAgent"),
        patch("app.runtime_setup.CacheManager", return_value=fake_cache),
        patch("app.db.repository.McpServerRepository.get", new_callable=AsyncMock, return_value={"name": "duckduckgo-search"}),
        patch("app.runtime_setup.OrchestratorAgent"),
        patch("app.runtime_setup.GeneralAgent"),
        patch("app.runtime_setup.LightAgent"),
        patch("app.runtime_setup.MusicAgent"),
        patch("app.runtime_setup.FillerAgent"),
        patch("app.runtime_setup.CustomAgentLoader"),
        patch("app.runtime_setup.AgentConfigRepository.get", new_callable=AsyncMock, return_value=None),
        patch("app.ha_client.websocket.HAWebSocketClient", return_value=ws_inst),
        patch("app.agents.alarm_monitor.AlarmMonitor"),
        patch("app.agents.timer_scheduler.TimerScheduler"),
        patch("app.runtime_setup.asyncio.create_task", side_effect=_fake_create_task),
    ]

    with contextlib.ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        (
            _ha_cls,
            _ei_cls,
            _embed,
            _vs,
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
        mock_orch_cls.return_value = MagicMock(agent_card=SimpleNamespace(agent_id="orchestrator"), initialize=AsyncMock())
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

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.kitchen_new", "light.kitchen_old"])
    assert ha_client.get_state.await_count == 2


@pytest.mark.asyncio
async def test_entity_remove_invalidates_action_and_routing():
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime()

    await handlers["entity_registry_updated"]({"data": {"entity_id": "light.kitchen", "action": "remove"}})

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.kitchen"])
    ha_client.get_state.assert_awaited_once_with("light.kitchen")


@pytest.mark.asyncio
async def test_device_registry_update_fans_out_to_both_caches():
    entries = [
        SimpleNamespace(entity_id="light.kitchen", area="kitchen", device_name="Kitchen Lamp"),
        SimpleNamespace(entity_id="switch.kitchen_socket", area="kitchen", device_name="Kitchen Lamp"),
    ]
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime(entity_entries=entries)

    await handlers["device_registry_updated"]({"data": {"device_name": "Kitchen Lamp"}})

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.kitchen", "switch.kitchen_socket"])
    assert ha_client.get_state.await_count == 2


@pytest.mark.asyncio
async def test_area_registry_update_fans_out_to_both_caches():
    entries = [
        SimpleNamespace(entity_id="light.kitchen", area="kitchen", device_name="Kitchen Lamp"),
        SimpleNamespace(entity_id="switch.kitchen_socket", area="kitchen", device_name="Kitchen Lamp"),
        SimpleNamespace(entity_id="light.garage", area="garage", device_name="Garage Lamp"),
    ]
    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime(entity_entries=entries)

    await handlers["area_registry_updated"]({"data": {"area_id": "kitchen"}})

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(["light.kitchen", "switch.kitchen_socket"])
    assert ha_client.get_state.await_count == 2


@pytest.mark.asyncio
async def test_invalidation_runs_before_index_sync():
    order: list[str] = []

    async def _invalidate(_entity_ids):
        order.append("invalidate")
        return {"action": 1, "routing": 2}

    async def _get_state(_entity_id):
        order.append("refresh")

    handlers, cache_manager, ha_client, _entity_index = await _initialize_registry_runtime()
    cache_manager.invalidate_by_entity_id.side_effect = _invalidate
    ha_client.get_state.side_effect = _get_state

    await handlers["entity_registry_updated"]({"data": {"entity_id": "light.kitchen"}})

    assert order == ["invalidate", "refresh"]


@pytest.mark.asyncio
async def test_invalidate_by_entity_id_returns_per_cache_counts():
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
    manager = CacheManager(store)
    manager._action_cache.invalidate_by_entity_id = MagicMock(side_effect=[2, 1])
    manager._routing_cache.invalidate_by_entity_id = MagicMock(side_effect=[3, 2])

    counts = await manager.invalidate_by_entity_id(["light.kitchen", "switch.garage"])

    assert counts == {"action": 3, "routing": 5}
