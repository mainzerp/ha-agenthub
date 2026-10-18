"""Tests for the drop-and-rebuild logic in `_prime_entity_index`.

Covers the 0.23.0 behaviour added to `app.bootstrap._entity` that drops and
re-creates the vector `entity_index` collection when the embedding
model identifier or `INDEX_SCHEMA_VERSION` differs from what was
persisted on the previous successful build. Without this, swapping the
default embedding model (e.g. to `intfloat/multilingual-e5-small`)
leaves the on-disk sqlite-vec `vec0` table locked to the old vector
dimension and every upsert fails with a vec0 dimension-mismatch error.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache.vector_store import COLLECTION_ENTITY_INDEX
from app.defaults import DEFAULT_LOCAL_EMBEDDING_MODEL


def _make_app_state() -> MagicMock:
    """Minimal FastAPI-app stub; runtime_setup only touches `state.*`."""
    app = MagicMock()
    app.state = MagicMock()
    return app


def _make_ha_client() -> MagicMock:
    ha = MagicMock()
    ha.get_states = AsyncMock(return_value=[])
    ha.get_areas = AsyncMock(return_value=[])
    ha.get_devices = AsyncMock(return_value=[])
    ha.get_entity_registry = AsyncMock(return_value=[])
    ha.get_hidden_entity_ids = AsyncMock(return_value=set())
    return ha


def _make_entity_index() -> MagicMock:
    ei = MagicMock()
    ei.populate_async = AsyncMock(return_value=None)
    ei.sync_async = AsyncMock(return_value={"added": 0, "updated": 0, "removed": 0, "unchanged": 0})
    return ei


def _make_vector_store(*, count: int = 5) -> MagicMock:
    vs = MagicMock()
    vs.count = MagicMock(return_value=count)
    vs.delete_collection = MagicMock(return_value=None)
    return vs


def _patch_entity_pipeline(entities: list | None = None):
    """Patch the HA-lookup + parse_ha_states helpers used by _prime."""
    if entities is None:
        entities = [MagicMock(name="entity-stub")]
    return [
        patch(
            "app.bootstrap._entity._gather_ha_lookups",
            new=AsyncMock(return_value=({}, {}, {}, {})),
        ),
        patch("app.bootstrap._entity.parse_ha_states", return_value=entities),
        patch(
            "app.entity.user_aliases.load_user_aliases",
            new=AsyncMock(return_value=None),
        ),
    ]


def _patch_settings(values: dict[str, str]):
    """Patch SettingsRepository.get_value/set_value used by _prime."""
    set_calls: list[tuple[str, str]] = []

    async def _get(key: str, default: str | None = None) -> str:
        return values.get(key, default if default is not None else "")

    async def _set(key: str, value: str) -> None:
        set_calls.append((key, value))
        values[key] = value

    return (
        patch("app.bootstrap._entity.SettingsRepository.get_value", new=AsyncMock(side_effect=_get)),
        patch("app.bootstrap._entity.SettingsRepository.set", new=AsyncMock(side_effect=_set)),
        set_calls,
    )


@pytest.mark.asyncio
async def test_build_entity_snapshot_filters_hidden_and_stores_lookups():
    """build_entity_snapshot applies hidden-entity filtering and publishes
    the fresh registry lookups + hidden ids on app.state."""
    from app.bootstrap import _entity as entity_bootstrap

    app = _make_app_state()
    ha = _make_ha_client()
    ha.get_states = AsyncMock(
        return_value=[
            {"entity_id": "light.visible", "state": "on", "attributes": {"friendly_name": "Visible"}},
            {"entity_id": "light.hidden", "state": "on", "attributes": {"friendly_name": "Hidden"}},
        ]
    )
    ha.get_hidden_entity_ids = AsyncMock(return_value={"light.hidden"})

    with patch(
        "app.bootstrap._entity._gather_ha_lookups",
        new=AsyncMock(return_value=({"kitchen": "Kitchen"}, {}, {}, {})),
    ):
        entries = await entity_bootstrap.build_entity_snapshot(app, ha)

    assert [e.entity_id for e in entries] == ["light.visible"]
    assert app.state.hidden_entity_ids == {"light.hidden"}
    assert app.state.entity_lookups["area"] == {"kitchen": "Kitchen"}


@pytest.mark.asyncio
async def test_resolve_active_embedding_model_uses_multilingual_default_when_setting_missing():
    from app.bootstrap import _entity as runtime_setup

    async def _get_value(key: str, default: str | None = None) -> str:
        if key == "embedding.provider":
            return "local"
        if key == "embedding.local_model":
            return default if default is not None else ""
        return default if default is not None else ""

    with patch(
        "app.bootstrap._entity.SettingsRepository.get_value",
        new=AsyncMock(side_effect=_get_value),
    ):
        resolved = await runtime_setup._resolve_active_embedding_model()

    assert resolved == DEFAULT_LOCAL_EMBEDDING_MODEL


@pytest.mark.asyncio
async def test_prime_drops_collection_when_embedding_model_changed():
    from app.bootstrap import _entity as runtime_setup

    app = _make_app_state()
    ha = _make_ha_client()
    ei = _make_entity_index()
    vs = _make_vector_store(count=42)

    # Stored model differs from the active model -> must drop & rebuild.
    settings_values = {
        "embedding.provider": "local",
        "embedding.local_model": "intfloat/multilingual-e5-small",
        "entity_index.schema_version": "2",  # matches current INDEX_SCHEMA_VERSION
        "entity_index.embedding_model": "all-MiniLM-L6-v2",
    }
    get_patch, set_patch, set_calls = _patch_settings(settings_values)
    pipeline_patches = _patch_entity_pipeline()

    # Force INDEX_SCHEMA_VERSION to the value we wrote above so only
    # the model mismatch drives the rebuild decision.
    with (
        get_patch,
        set_patch,
        pipeline_patches[0],
        pipeline_patches[1],
        pipeline_patches[2],
        patch("app.entity.index.INDEX_SCHEMA_VERSION", 2),
    ):
        await runtime_setup._prime_entity_index(app, ha, ei, vs)

    assert vs.delete_collection.call_count == 1
    assert vs.delete_collection.call_args.args[0] == COLLECTION_ENTITY_INDEX
    ei.populate_async.assert_awaited_once()
    ei.sync_async.assert_not_called()
    # New model identifier must be persisted after a successful rebuild.
    assert (
        "entity_index.embedding_model",
        "intfloat/multilingual-e5-small",
    ) in set_calls


@pytest.mark.asyncio
async def test_prime_does_not_drop_when_schema_and_model_match():
    from app.bootstrap import _entity as runtime_setup

    app = _make_app_state()
    ha = _make_ha_client()
    ei = _make_entity_index()
    vs = _make_vector_store(count=42)

    settings_values = {
        "embedding.provider": "local",
        "embedding.local_model": "intfloat/multilingual-e5-small",
        "entity_index.schema_version": "2",
        "entity_index.embedding_model": "intfloat/multilingual-e5-small",
    }
    get_patch, set_patch, _ = _patch_settings(settings_values)
    pipeline_patches = _patch_entity_pipeline()

    with (
        get_patch,
        set_patch,
        pipeline_patches[0],
        pipeline_patches[1],
        pipeline_patches[2],
        patch("app.entity.index.INDEX_SCHEMA_VERSION", 2),
    ):
        await runtime_setup._prime_entity_index(app, ha, ei, vs)

    vs.delete_collection.assert_not_called()
    # Existing collection with matching schema+model takes the sync path.
    ei.sync_async.assert_awaited_once()
    ei.populate_async.assert_not_called()


@pytest.mark.asyncio
async def test_prime_drops_and_retries_on_vector_dimension_error():
    """First populate raises a vec0 dimension-mismatch error; code drops + retries once."""
    from app.bootstrap import _entity as runtime_setup

    app = _make_app_state()
    ha = _make_ha_client()
    ei = _make_entity_index()
    # Empty existing collection -> populate path (no model mismatch needed).
    vs = _make_vector_store(count=0)

    err = RuntimeError("vec0 INSERT failed: Vector has 768 dimensions, expected 384")
    ei.populate_async = AsyncMock(side_effect=[err, None])

    settings_values = {
        "embedding.provider": "local",
        "embedding.local_model": "intfloat/multilingual-e5-small",
        "entity_index.schema_version": "2",
        "entity_index.embedding_model": "intfloat/multilingual-e5-small",
    }
    get_patch, set_patch, _ = _patch_settings(settings_values)
    pipeline_patches = _patch_entity_pipeline()

    with (
        get_patch,
        set_patch,
        pipeline_patches[0],
        pipeline_patches[1],
        pipeline_patches[2],
        patch("app.entity.index.INDEX_SCHEMA_VERSION", 2),
    ):
        await runtime_setup._prime_entity_index(app, ha, ei, vs)

    # One drop happens up-front because count==0 with entities to index;
    # a second drop happens after the dimension error during populate.
    assert vs.delete_collection.call_count == 2
    assert ei.populate_async.await_count == 2


class _RegistryGatedHaClient:
    """HA client double whose hidden list loads only once the WS is connected."""

    def __init__(self, app, states: list[dict], hidden: set[str]) -> None:
        self._app = app
        self._states = states
        self._hidden = hidden
        self.hidden_entity_ids_loaded = False
        self.calls: list[str] = []

    async def get_hidden_entity_ids(self) -> set[str]:
        ws = getattr(self._app.state, "ws_client", None)
        if ws is not None and ws.is_connected():
            self.hidden_entity_ids_loaded = True
            self.calls.append("hidden:loaded")
            return set(self._hidden)
        self.calls.append("hidden:unknown")
        return set()

    async def get_states(self) -> list[dict]:
        self.calls.append("states")
        return list(self._states)


def _states_with_hidden() -> list[dict]:
    return [
        {"entity_id": "light.visible", "state": "on", "attributes": {"friendly_name": "Visible"}},
        {"entity_id": "light.hidden", "state": "on", "attributes": {"friendly_name": "Hidden"}},
    ]


async def _run_prime(app, ha, ei, vs) -> None:
    from app.bootstrap import _entity as runtime_setup

    settings_values = {
        "embedding.provider": "local",
        "embedding.local_model": "intfloat/multilingual-e5-small",
        "entity_index.schema_version": "2",
        "entity_index.embedding_model": "intfloat/multilingual-e5-small",
    }
    get_patch, set_patch, _ = _patch_settings(settings_values)
    with (
        get_patch,
        set_patch,
        patch("app.bootstrap._entity._gather_ha_lookups", new=AsyncMock(return_value=({}, {}, {}, {}))),
        patch("app.entity.user_aliases.load_user_aliases", new=AsyncMock(return_value=None)),
        patch("app.entity.index.INDEX_SCHEMA_VERSION", 2),
    ):
        await runtime_setup._prime_entity_index(app, ha, ei, vs)


@pytest.mark.asyncio
async def test_first_snapshot_never_contains_hidden_entities():
    """The prime waits for the WS-only hidden list before fetching the states
    snapshot, so hidden entities are never searchable in the first snapshot."""
    import asyncio
    from types import SimpleNamespace

    app = SimpleNamespace(state=SimpleNamespace(ws_client=None))
    ha = _RegistryGatedHaClient(app, _states_with_hidden(), {"light.hidden"})
    ei = _make_entity_index()
    vs = _make_vector_store(count=0)

    async def _connect_ws_later() -> None:
        await asyncio.sleep(0.1)
        app.state.ws_client = SimpleNamespace(is_connected=lambda: True)

    connector = asyncio.create_task(_connect_ws_later())
    await _run_prime(app, ha, ei, vs)
    await connector

    ei.populate_async.assert_awaited_once()
    entries = ei.populate_async.await_args.args[0]
    assert [e.entity_id for e in entries] == ["light.visible"]
    assert app.state.hidden_entity_ids == {"light.hidden"}
    # The states snapshot is fetched only after the hidden list loaded.
    assert ha.calls.index("hidden:loaded") < ha.calls.index("states")


@pytest.mark.asyncio
async def test_prime_degrades_when_hidden_list_stays_unavailable():
    """Without a readable entity registry the prime still installs the snapshot
    after the bounded wait (degraded) instead of leaving the index empty."""
    from types import SimpleNamespace

    app = SimpleNamespace(state=SimpleNamespace(ws_client=None))
    ha = _RegistryGatedHaClient(app, _states_with_hidden(), {"light.hidden"})
    ei = _make_entity_index()
    vs = _make_vector_store(count=0)

    with patch("app.bootstrap._entity._FIRST_SNAPSHOT_HIDDEN_WAIT_SEC", 0.05):
        await _run_prime(app, ha, ei, vs)

    ei.populate_async.assert_awaited_once()
    assert "hidden:loaded" not in ha.calls


@pytest.mark.asyncio
async def test_state_events_not_indexed_before_hidden_list_and_first_snapshot():
    """WS state events arriving before the hidden list and the first snapshot
    are not indexed: their hidden flag cannot be checked yet."""
    import contextlib
    from types import SimpleNamespace

    from app.bootstrap import _entity as entity_bootstrap

    handlers: dict[str, object] = {}
    ws = SimpleNamespace(
        on_event=lambda event_type, cb: handlers.__setitem__(event_type, cb),
        on_reconnect=lambda cb: None,
        run=AsyncMock(return_value=None),
        is_connected=lambda: False,
    )
    ha = MagicMock()
    ha.hidden_entity_ids_loaded = False
    entity_index = MagicMock()
    entity_index.initial_snapshot_loaded = False
    cache_manager = MagicMock()
    app = SimpleNamespace(state=SimpleNamespace(ws_client=None, sync_task=MagicMock(done=lambda: False)))

    def _close(_app, coro, _name):
        with contextlib.suppress(Exception):
            coro.close()

    with (
        patch("app.ha_client.websocket.HAWebSocketClient", return_value=ws),
        patch.object(entity_bootstrap, "spawn_background", side_effect=_close),
    ):
        await entity_bootstrap.setup_entity_observers(app, "test", ha, entity_index, cache_manager)

    event = {
        "data": {
            "entity_id": "light.hidden",
            "old_state": None,
            "new_state": {"entity_id": "light.hidden", "state": "on", "attributes": {}},
        }
    }
    with patch.object(entity_bootstrap, "state_to_entity_index_entry") as to_entry:
        await handlers["state_changed"](event)
        to_entry.assert_not_called()

        # Once the hidden list is loaded, events are indexed again.
        ha.hidden_entity_ids_loaded = True
        await handlers["state_changed"](event)
        to_entry.assert_called_once()
