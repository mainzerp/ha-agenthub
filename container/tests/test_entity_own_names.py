"""Own entity names (HA 2026.11 device-prefixed friendly names) end to end.

Covers the snapshot, state_changed and registry-event ingest paths, the
action-cache invalidation when an entity's index name changes, keyword
recall, and executor speech. HA is mocked; the entity index is a real
EntityIndex over a mocked vector store.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from app.agents.actionable import LightAgent
from app.agents.light_executor import execute_light_action
from app.bootstrap import _entity as entity_bootstrap
from app.entity.index import EntityIndex
from app.entity.ingest import parse_ha_states, state_to_entity_index_entry
from app.entity.matcher import EntityMatcher
from app.ha_client.websocket import EntityRegistryInfo
from app.models.agent import DispatchTask, TaskContext
from tests.helpers import attach_expect_state_shim

DIMMER = "Dimmer (X) (S1)"
AKTOR = "Aktor (X) (S2)"


def _state(entity_id: str, friendly_name: str, state: str = "off") -> dict:
    return {"entity_id": entity_id, "state": state, "attributes": {"friendly_name": friendly_name}}


# HA 2026.11 install: two user-named lights on different devices, one entity
# with a frozen compat name, and a main entity (no own name).
STATES = [
    _state("light.wohnzimmer", f"{DIMMER} Wohnzimmer"),
    _state("light.wohnzimmer_lichtband", f"{AKTOR} Wohnzimmer Lichtband"),
    _state("light.innenhof", "Innenhof"),
    _state("light.flur", "Flur Dimmer"),
]
USER_NAMES = {
    "light.wohnzimmer": "Wohnzimmer",
    "light.wohnzimmer_lichtband": "Wohnzimmer Lichtband",
    "light.innenhof": "Innenhof",
}
DEVICES = {
    "light.wohnzimmer": DIMMER,
    "light.wohnzimmer_lichtband": AKTOR,
    "light.innenhof": "Aktor (X) (S3)",
    "light.flur": "Flur Dimmer",
}


class _FakeHa:
    """HA REST client double: states, per-entity state and one registry read."""

    def __init__(self, states: list[dict], user_names: dict[str, str], hidden: set[str] | None = None) -> None:
        self.states = {s["entity_id"]: s for s in states}
        self.registry = EntityRegistryInfo(hidden_ids=frozenset(hidden or ()), user_names=dict(user_names))
        self.hidden_entity_ids_loaded = True
        self.clear_area_registry_cache = MagicMock()
        self.set_state_observer = MagicMock()

    async def get_states(self) -> list[dict]:
        return list(self.states.values())

    async def get_state(self, entity_id: str) -> dict | None:
        return self.states.get(entity_id)

    async def get_entity_registry(self) -> EntityRegistryInfo:
        return self.registry

    async def get_hidden_entity_ids(self) -> set[str]:
        return set(self.registry.hidden_ids)


def _lookups(devices: dict[str, str] | None = None):
    return patch(
        "app.bootstrap._entity._gather_ha_lookups",
        new=AsyncMock(return_value=({}, {}, dict(devices if devices is not None else DEVICES), {})),
    )


def _index_with(entries) -> tuple[EntityIndex, MagicMock]:
    """Real EntityIndex whose mocked store already holds ``entries``."""
    store = MagicMock()
    index = EntityIndex(store)
    _store_rows(store, entries)
    index._install_snapshot(list(entries), 0)
    return index, store


def _store_rows(store: MagicMock, entries) -> None:
    store.get.return_value = {
        "ids": [e.entity_id for e in entries],
        "documents": [e.embedding_text for e in entries],
        "metadatas": [EntityIndex._build_metadata(e) for e in entries],
    }


def _ha_named_entries():
    """Entries as indexed before the own-name rule (HA friendly_names)."""
    return parse_ha_states(STATES, device_lookup=DEVICES)


# ---------------------------------------------------------------------------
# Snapshot path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_uses_own_names_and_publishes_naming_lookups():
    app = SimpleNamespace(state=SimpleNamespace())
    ha = _FakeHa(STATES, USER_NAMES)

    with _lookups():
        entries = await entity_bootstrap.build_entity_snapshot(app, ha)

    names = {e.entity_id: e.friendly_name for e in entries}
    assert names == {
        "light.wohnzimmer": "Wohnzimmer",
        "light.wohnzimmer_lichtband": "Wohnzimmer Lichtband",
        "light.innenhof": "Innenhof",
        "light.flur": "Flur Dimmer",
    }
    by_id = {e.entity_id: e for e in entries}
    assert by_id["light.wohnzimmer"].ha_friendly_name == f"{DIMMER} Wohnzimmer"
    assert app.state.entity_lookups["user_name"] == USER_NAMES
    assert app.state.entity_lookups["name_suppressed"] == frozenset()


@pytest.mark.asyncio
async def test_first_sync_after_name_change_invalidates_renamed_entities_only():
    """The first sync after the own-name rule ships finds the old (prefixed)
    names in the stored rows: only those entities re-embed and lose their
    action-cache rows."""
    index, store = _index_with(_ha_named_entries())
    cache_manager = MagicMock()
    cache_manager.invalidate_by_entity_id = AsyncMock(return_value={"action": 2, "routing": 1})
    app = SimpleNamespace(
        state=SimpleNamespace(ha_client=_FakeHa(STATES, USER_NAMES), entity_index=index, cache_manager=cache_manager)
    )

    with _lookups():
        result = await entity_bootstrap.resync_entity_index(app, reason="periodic")

    assert result["renamed"] == ["light.wohnzimmer", "light.wohnzimmer_lichtband"]
    assert result["unchanged"] == 2
    upserted = store.upsert.call_args.kwargs["ids"]
    assert sorted(upserted) == ["light.wohnzimmer", "light.wohnzimmer_lichtband"]
    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(
        ["light.wohnzimmer", "light.wohnzimmer_lichtband"],
        reason="entity_name_changed",
        detail="path=sync:periodic",
    )
    assert index.get_by_id("light.wohnzimmer").friendly_name == "Wohnzimmer"


@pytest.mark.asyncio
async def test_sync_without_name_change_keeps_cache():
    entries = parse_ha_states(STATES, device_lookup=DEVICES, user_name_lookup=USER_NAMES)
    index, store = _index_with(entries)
    cache_manager = MagicMock()
    cache_manager.invalidate_by_entity_id = AsyncMock()
    app = SimpleNamespace(
        state=SimpleNamespace(ha_client=_FakeHa(STATES, USER_NAMES), entity_index=index, cache_manager=cache_manager)
    )

    with _lookups():
        result = await entity_bootstrap.resync_entity_index(app, reason="periodic")

    assert result["renamed"] == []
    assert result["unchanged"] == 4
    store.upsert.assert_not_called()
    cache_manager.invalidate_by_entity_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_registry_unavailable_falls_back_to_ha_names():
    app = SimpleNamespace(state=SimpleNamespace())
    ha = MagicMock()
    ha.get_states = AsyncMock(return_value=STATES)
    ha.get_entity_registry = AsyncMock(return_value=None)  # not an EntityRegistryInfo
    ha.get_hidden_entity_ids = AsyncMock(return_value=set())

    with _lookups():
        entries = await entity_bootstrap.build_entity_snapshot(app, ha)

    assert {e.friendly_name for e in entries} == {s["attributes"]["friendly_name"] for s in STATES}


# ---------------------------------------------------------------------------
# Observers: state_changed and registry events
# ---------------------------------------------------------------------------


async def _setup_observers(app, ha, entity_index, cache_manager, *, run_flush: bool = False):
    handlers: dict[str, object] = {}
    ws = SimpleNamespace(
        on_event=lambda event_type, cb: handlers.__setitem__(event_type, cb),
        on_reconnect=lambda cb: None,
        run=AsyncMock(return_value=None),
        is_connected=lambda: False,
    )
    tasks: list[asyncio.Task] = []

    def _spawn(_app, coro, name):
        if run_flush and name == "flush_task":
            tasks.append(asyncio.ensure_future(coro))
            return
        with contextlib.suppress(Exception):
            coro.close()

    with (
        patch("app.ha_client.websocket.HAWebSocketClient", return_value=ws),
        patch.object(entity_bootstrap, "spawn_background", side_effect=_spawn),
    ):
        await entity_bootstrap.setup_entity_observers(app, "test", ha, entity_index, cache_manager)
    return handlers, tasks


@pytest.mark.asyncio
async def test_state_changed_keeps_own_name_and_suppression():
    entries = parse_ha_states(STATES, device_lookup=DEVICES, user_name_lookup=USER_NAMES)
    index, _store = _index_with(entries)
    app = SimpleNamespace(state=SimpleNamespace(ws_client=None, sync_task=MagicMock(done=lambda: False)))
    entity_bootstrap._store_entity_lookups(
        app,
        {},
        {},
        {**DEVICES, "light.wohnzimmer_2": "Dimmer (X) (S9)"},
        {},
        user_name_lookup={**USER_NAMES, "light.wohnzimmer_2": "Wohnzimmer"},
        name_suppressed=frozenset({"light.wohnzimmer_2"}),
    )
    handlers, _tasks = await _setup_observers(app, _FakeHa(STATES, USER_NAMES), index, MagicMock())

    built = []
    real = entity_bootstrap.state_to_entity_index_entry

    def _capture(*args, **kwargs):
        entry = real(*args, **kwargs)
        built.append(entry)
        return entry

    with patch.object(entity_bootstrap, "state_to_entity_index_entry", side_effect=_capture):
        for entity_id, name in (
            ("light.wohnzimmer", f"{DIMMER} Wohnzimmer"),
            ("light.wohnzimmer_2", "Dimmer (X) (S9) Wohnzimmer"),
        ):
            await handlers["state_changed"](
                {
                    "data": {
                        "entity_id": entity_id,
                        "old_state": _state(entity_id, name),
                        "new_state": _state(entity_id, name, state="on"),
                    }
                }
            )

    assert [e.friendly_name for e in built] == ["Wohnzimmer", "Dimmer (X) (S9) Wohnzimmer"]


@pytest.mark.asyncio
async def test_state_changed_name_change_invalidates_cache_rows():
    """A push whose index name differs from the stored row deletes the entity's
    cached rows on the background flush (never on the turn path)."""
    index, _store = _index_with(_ha_named_entries())
    cache_manager = MagicMock()
    cache_manager.invalidate_by_entity_id = AsyncMock(return_value={"action": 1, "routing": 0})
    app = SimpleNamespace(state=SimpleNamespace(ws_client=None, sync_task=MagicMock(done=lambda: False)))
    entity_bootstrap._store_entity_lookups(app, {}, {}, DEVICES, {}, user_name_lookup=USER_NAMES)

    with patch.object(entity_bootstrap, "_ENTITY_UPDATE_FLUSH_INTERVAL_SEC", 0.01):
        handlers, tasks = await _setup_observers(app, _FakeHa(STATES, USER_NAMES), index, cache_manager, run_flush=True)
        try:
            await handlers["state_changed"](
                {
                    "data": {
                        "entity_id": "light.wohnzimmer",
                        "old_state": STATES[0],
                        "new_state": _state("light.wohnzimmer", f"{DIMMER} Wohnzimmer", state="on"),
                    }
                }
            )
            for _ in range(200):
                if cache_manager.invalidate_by_entity_id.await_count:
                    break
                await asyncio.sleep(0.01)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(
        ["light.wohnzimmer"], reason="entity_name_changed", detail="path=state_changed"
    )
    assert index.get_by_id("light.wohnzimmer").friendly_name == "Wohnzimmer"


@pytest.mark.asyncio
async def test_registry_rename_updates_name_and_invalidates_rows():
    entries = parse_ha_states(STATES, device_lookup=DEVICES, user_name_lookup=USER_NAMES)
    index, _store = _index_with(entries)
    cache_manager = MagicMock()
    cache_manager.invalidate_by_entity_id = AsyncMock(return_value={"action": 1, "routing": 0})
    renamed_states = [s for s in STATES if s["entity_id"] != "light.wohnzimmer"]
    renamed_states.append(_state("light.wohnzimmer", f"{DIMMER} Wohnzimmer Decke"))
    ha = _FakeHa(renamed_states, {**USER_NAMES, "light.wohnzimmer": "Wohnzimmer Decke"})
    app = SimpleNamespace(state=SimpleNamespace(ws_client=None, sync_task=MagicMock(done=lambda: False)))
    handlers, _tasks = await _setup_observers(app, ha, index, cache_manager)

    with _lookups():
        task = handlers["entity_registry_updated"](
            {
                "event_type": "entity_registry_updated",
                "data": {"action": "update", "entity_id": "light.wohnzimmer", "changes": {"name": "Wohnzimmer"}},
            }
        )
        await asyncio.wait_for(task, timeout=2)

    cache_manager.invalidate_by_entity_id.assert_awaited_once_with(
        ["light.wohnzimmer"], reason="registry_event", detail=ANY
    )
    assert index.get_by_id("light.wohnzimmer").friendly_name == "Wohnzimmer Decke"
    assert app.state.entity_lookups["user_name"]["light.wohnzimmer"] == "Wohnzimmer Decke"


@pytest.mark.asyncio
async def test_registry_rename_collision_flips_and_invalidates_the_other_entity():
    """Renaming a second media player to an own name another one already has
    makes both keep their HA names; the untouched one loses its cached rows."""
    states = [_state("media_player.echo_a", "Echo A Küche"), _state("media_player.echo_b", "Echo B")]
    devices = {"media_player.echo_a": "Echo A", "media_player.echo_b": "Echo B"}
    entries = parse_ha_states(states, device_lookup=devices, user_name_lookup={"media_player.echo_a": "Küche"})
    assert [e.friendly_name for e in entries] == ["Küche", "Echo B"]
    index, _store = _index_with(entries)

    ha = _FakeHa(
        [states[0], _state("media_player.echo_b", "Echo B Küche")],
        {"media_player.echo_a": "Küche", "media_player.echo_b": "Küche"},
    )
    cache_manager = MagicMock()
    cache_manager.invalidate_by_entity_id = AsyncMock(return_value={"action": 1, "routing": 0})
    app = SimpleNamespace(state=SimpleNamespace(ws_client=None, sync_task=MagicMock(done=lambda: False)))
    handlers, _tasks = await _setup_observers(app, ha, index, cache_manager)

    with _lookups(devices):
        task = handlers["entity_registry_updated"](
            {
                "event_type": "entity_registry_updated",
                "data": {"action": "update", "entity_id": "media_player.echo_b", "changes": {"name": None}},
            }
        )
        await asyncio.wait_for(task, timeout=2)

    calls = cache_manager.invalidate_by_entity_id.await_args_list
    assert [(c.args[0], c.kwargs["reason"]) for c in calls] == [
        (["media_player.echo_b"], "registry_event"),
        (["media_player.echo_a"], "entity_name_changed"),
    ]
    assert "name collision guard" in calls[1].kwargs["detail"]
    assert index.get_by_id("media_player.echo_a").friendly_name == "Echo A Küche"
    assert index.get_by_id("media_player.echo_b").friendly_name == "Echo B Küche"
    assert app.state.entity_lookups["name_suppressed"] == {"media_player.echo_a", "media_player.echo_b"}


# ---------------------------------------------------------------------------
# Keyword recall and executor speech
# ---------------------------------------------------------------------------


def _recall_agent(entries) -> LightAgent:
    agent = LightAgent()
    index = MagicMock()
    index.list_entries_async = AsyncMock(return_value=entries)
    agent._entity_index = index
    agent._entity_matcher = MagicMock()
    return agent


def _visible_passthrough():
    return patch(
        "app.agents.actionable.filter_visible_results",
        new=AsyncMock(side_effect=lambda _agent_id, entries, _index: entries),
    )


@pytest.mark.asyncio
async def test_recall_ranks_own_name_uniquely_first():
    entries = parse_ha_states(STATES, device_lookup=DEVICES, user_name_lookup=USER_NAMES)
    agent = _recall_agent(entries)
    task = DispatchTask(description="Wohnzimmer ausschalten", context=TaskContext())

    with _visible_passthrough():
        block, scored = await agent._build_query_candidate_context(task)

    assert scored[0][0].entity_id == "light.wohnzimmer"
    assert scored[0][1] > scored[1][1]
    assert "ambiguous" not in block
    assert "light.wohnzimmer — Wohnzimmer (off)" in block
    assert DIMMER not in block


@pytest.mark.asyncio
async def test_recall_with_device_prefixed_names_ties():
    """Control: the same install indexed with HA's prefixed names ties."""
    agent = _recall_agent(_ha_named_entries())
    task = DispatchTask(description="Wohnzimmer ausschalten", context=TaskContext())

    with _visible_passthrough():
        block, _scored = await agent._build_query_candidate_context(task)

    assert "ambiguous" in block


@pytest.fixture()
def _fast_verify(monkeypatch):
    from app.agents import action_executor as _ae

    async def _fast(key, *, default):
        return {
            "state_verify.ws_timeout_sec": 0.05,
            "state_verify.poll_interval_sec": 0.01,
            "state_verify.poll_max_sec": 0.05,
        }.get(key, default)

    monkeypatch.setattr(_ae, "_settings_float", _fast)
    monkeypatch.setattr("app.entity.visibility.EntityVisibilityRepository.get_rules", AsyncMock(return_value=[]))


def _speech_fixture():
    entries = parse_ha_states(STATES, device_lookup=DEVICES, user_name_lookup=USER_NAMES)
    index, _store = _index_with(entries)
    matcher = MagicMock(spec=EntityMatcher)
    matcher.match = AsyncMock(return_value=[])
    matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
    ha = AsyncMock()
    ha.get_state = AsyncMock(return_value=_state("light.wohnzimmer", f"{DIMMER} Wohnzimmer", state="off"))
    ha.call_service = AsyncMock(return_value=[{"entity_id": "light.wohnzimmer", "state": "on"}])
    return index, matcher, attach_expect_state_shim(ha)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_verify")
@pytest.mark.parametrize(
    "action",
    [
        {"action": "turn_on", "entity": "Wohnzimmer", "parameters": {}},
        {"action": "turn_on", "entity": "Wohnzimmer", "entity_id": "light.wohnzimmer", "parameters": {}},
    ],
)
async def test_executor_confirmation_speaks_own_name(action):
    index, matcher, ha = _speech_fixture()

    result = await execute_light_action(action, ha, index, matcher, agent_id="light-agent")

    assert result["success"] is True
    assert result["entity_id"] == "light.wohnzimmer"
    assert "Wohnzimmer" in result["speech"]
    assert "Dimmer" not in result["speech"]
    matcher.match.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_verify")
async def test_read_and_list_replies_speak_own_names():
    index, matcher, ha = _speech_fixture()
    ha.get_states = AsyncMock(return_value=STATES)

    read = await execute_light_action(
        {"action": "query_light_state", "entity": "Wohnzimmer"}, ha, index, matcher, agent_id="light-agent"
    )
    listed = await execute_light_action({"action": "list_lights"}, ha, index, matcher, agent_id="light-agent")

    assert read["speech"] == "Wohnzimmer is off."
    assert "Wohnzimmer Lichtband" in listed["speech"]
    assert "Dimmer (X)" not in listed["speech"]
    assert "Aktor (X)" not in listed["speech"]


def test_state_to_entry_without_naming_lookups_is_unchanged():
    """Callers that pass no naming lookups keep HA's friendly_name (pre-rule behavior)."""
    entry = state_to_entity_index_entry(STATES[0], device_lookup=DEVICES)
    assert entry.friendly_name == f"{DIMMER} Wohnzimmer"
