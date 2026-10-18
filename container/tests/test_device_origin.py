"""Satellite origin context: device_id -> area_id from the HA device registry.

HA voice satellites send only ``device_id``. The container keeps an
in-memory device directory (refreshed by the entity-index registry sync)
and applies the device's area / name to the turn context at the start of
the orchestrator prelude, before cache replay, routing and dispatch.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ha_client.devices import (
    DeviceDirectory,
    DeviceOrigin,
    apply_device_origin,
    get_device_directory,
    parse_device_rows,
)
from app.models.agent import AgentCard, IngressTask, TaskContext


def _directory(**devices: DeviceOrigin) -> DeviceDirectory:
    directory = DeviceDirectory()
    directory.replace(dict(devices), {"kitchen": "Kitchen", "office": "Office"})
    return directory


@pytest.fixture(autouse=True)
def _reset_global_directory():
    get_device_directory().reset()
    yield
    get_device_directory().reset()


def test_device_id_sets_area_and_names():
    directory = _directory(sat1=DeviceOrigin(name="Kitchen Satellite", area_id="kitchen"))
    context = TaskContext(device_id="sat1")

    assert apply_device_origin(context, directory) is True

    assert context.area_id == "kitchen"
    assert context.area_name == "Kitchen"
    assert context.device_name == "Kitchen Satellite"


def test_explicit_area_id_wins():
    directory = _directory(sat1=DeviceOrigin(name="Kitchen Satellite", area_id="kitchen"))
    context = TaskContext(device_id="sat1", area_id="office", area_name="Study")

    assert apply_device_origin(context, directory) is False

    assert context.area_id == "office"
    assert context.area_name == "Study"
    # The device name is pure metadata and still filled when missing.
    assert context.device_name == "Kitchen Satellite"


def test_explicit_device_name_is_kept():
    directory = _directory(sat1=DeviceOrigin(name="Kitchen Satellite", area_id="kitchen"))
    context = TaskContext(device_id="sat1", device_name="My Satellite")

    apply_device_origin(context, directory)

    assert context.device_name == "My Satellite"
    assert context.area_id == "kitchen"


def test_unknown_device_leaves_context_unchanged():
    directory = _directory(sat1=DeviceOrigin(name="Kitchen Satellite", area_id="kitchen"))
    context = TaskContext(device_id="unknown")

    assert apply_device_origin(context, directory) is False

    assert context.area_id is None
    assert context.area_name is None
    assert context.device_name is None


def test_device_without_area_sets_only_the_name():
    directory = _directory(sat1=DeviceOrigin(name="Hallway Satellite", area_id=None))
    context = TaskContext(device_id="sat1")

    assert apply_device_origin(context, directory) is False

    assert context.area_id is None
    assert context.area_name is None
    assert context.device_name == "Hallway Satellite"


def test_no_device_id_or_context_is_a_noop():
    directory = _directory(sat1=DeviceOrigin(name="Kitchen Satellite", area_id="kitchen"))
    context = TaskContext()

    assert apply_device_origin(context, directory) is False
    assert apply_device_origin(None, directory) is False
    assert context.area_id is None


def test_parse_device_rows_skips_malformed_rows():
    devices = parse_device_rows(
        {
            "sat1": {"name": " Kitchen Satellite ", "area_id": "kitchen"},
            "sat2": {"name": None, "area_id": ""},
            "": {"name": "x", "area_id": "y"},
            "sat3": "not-a-row",
        }
    )

    assert devices == {
        "sat1": DeviceOrigin(name="Kitchen Satellite", area_id="kitchen"),
        "sat2": DeviceOrigin(name=None, area_id=None),
    }


@pytest.mark.asyncio
async def test_registry_update_refresh_moves_device_to_new_area():
    directory = DeviceDirectory()
    ha = MagicMock()
    ha.get_device_registry = AsyncMock(return_value={"sat1": {"name": "Satellite", "area_id": "kitchen"}})
    ha.get_area_registry = AsyncMock(return_value={"kitchen": "Kitchen", "office": "Office"})

    assert await directory.refresh(ha) is True
    first = TaskContext(device_id="sat1")
    apply_device_origin(first, directory)
    assert (first.area_id, first.area_name) == ("kitchen", "Kitchen")

    # device_registry_updated: the satellite was moved to the office.
    ha.get_device_registry = AsyncMock(return_value={"sat1": {"name": "Satellite", "area_id": "office"}})
    assert await directory.refresh(ha) is True
    second = TaskContext(device_id="sat1")
    apply_device_origin(second, directory)
    assert (second.area_id, second.area_name) == ("office", "Office")


@pytest.mark.asyncio
async def test_failed_refresh_keeps_previous_map():
    directory = DeviceDirectory()
    ha = MagicMock()
    ha.get_device_registry = AsyncMock(return_value={"sat1": {"name": "Satellite", "area_id": "kitchen"}})
    ha.get_area_registry = AsyncMock(return_value={"kitchen": "Kitchen"})
    await directory.refresh(ha)

    ha.get_device_registry = AsyncMock(side_effect=RuntimeError("HA down"))
    assert await directory.refresh(ha) is False
    ha.get_device_registry = AsyncMock(return_value=None)
    assert await directory.refresh(ha) is False

    assert directory.lookup("sat1") == DeviceOrigin(name="Satellite", area_id="kitchen")


@pytest.mark.asyncio
async def test_rest_client_get_device_registry_parses_template_output():
    from app.ha_client.rest import HARestClient

    client = HARestClient()
    rendered = json.dumps(
        [
            {"id": "sat1", "name": "Kitchen Satellite", "area": "kitchen"},
            {"id": "dev2", "name": None, "area": None},
        ]
    )
    client.render_template = AsyncMock(return_value=rendered)

    result = await client.get_device_registry()

    assert result == {
        "sat1": {"name": "Kitchen Satellite", "area_id": "kitchen"},
        "dev2": {"name": None, "area_id": None},
    }


@pytest.mark.asyncio
async def test_entity_sync_refreshes_global_device_directory():
    from app.bootstrap import _entity as entity_bootstrap

    ha = MagicMock()
    ha.get_device_registry = AsyncMock(return_value={"sat1": {"name": "Satellite", "area_id": "kitchen"}})

    await entity_bootstrap._refresh_device_directory(ha, {"kitchen": "Kitchen"})

    context = TaskContext(device_id="sat1")
    apply_device_origin(context)
    assert (context.area_id, context.area_name, context.device_name) == ("kitchen", "Kitchen", "Satellite")


def _make_orchestrator():
    from app.agents.orchestrator import OrchestratorAgent

    registry = AsyncMock()
    registry.list_agents = AsyncMock(
        return_value=[AgentCard(agent_id="light-agent", name="Light Agent", description="", skills=["light"])]
    )
    orch = OrchestratorAgent(dispatcher=AsyncMock(), registry=registry, cache_manager=MagicMock())
    orch._is_background_turn = MagicMock(return_value=False)
    orch._get_turns = AsyncMock(return_value=[])
    orch._resolve_language = AsyncMock(return_value="en")
    orch._get_bool_setting = AsyncMock(side_effect=lambda _key, default: default)
    orch._cache_orchestrator._get_bool_setting_impl = AsyncMock(side_effect=lambda _key, default: default)
    orch._finalize_action_replay_hit = AsyncMock(
        return_value={"speech": "Cached", "routed_to": "light-agent", "action_executed": None}
    )
    return orch


@pytest.mark.asyncio
async def test_prelude_applies_device_area_before_cache_replay():
    from app.cache.cache_manager import ActionReplayOutcome

    get_device_directory().replace(
        {"sat1": DeviceOrigin(name="Kitchen Satellite", area_id="kitchen")}, {"kitchen": "Kitchen"}
    )
    task = IngressTask(
        description="turn on the light",
        conversation_id="conv-device-origin",
        context=TaskContext(device_id="sat1", source="ha"),
    )
    seen_area: list[str | None] = []

    async def _replay(*_args, **_kwargs):
        seen_area.append(task.context.area_id)
        hit = ActionReplayOutcome(
            kind="full_hit",
            entry_id="action-1",
            agent_id="light-agent",
            response_text="Cached",
            replay_result={"success": True},
            similarity=1.0,
        )
        return hit, None

    orch = _make_orchestrator()
    orch._cache_orchestrator.try_cache_replay = AsyncMock(side_effect=_replay)

    await orch._run_pipeline_prelude(task)

    assert seen_area == ["kitchen"]
    assert task.context.area_name == "Kitchen"
    assert task.context.device_name == "Kitchen Satellite"
