"""Tests for the drop-and-rebuild logic in `_prime_entity_index`.

Covers the 0.23.0 behaviour added to `app.runtime_setup` that drops and
re-creates the Chroma `entity_index` collection when the embedding
model identifier or `INDEX_SCHEMA_VERSION` differs from what was
persisted on the previous successful build. Without this, swapping the
default embedding model (e.g. to `intfloat/multilingual-e5-small`)
leaves the on-disk HNSW segment locked to the old vector dimension and
every upsert fails with a Chroma compaction error.
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
async def test_prime_drops_and_retries_on_chroma_compaction_error():
    """First populate raises a compaction/HNSW error; code drops + retries once."""
    from app.bootstrap import _entity as runtime_setup

    app = _make_app_state()
    ha = _make_ha_client()
    ei = _make_entity_index()
    # Empty existing collection -> populate path (no model mismatch needed).
    vs = _make_vector_store(count=0)

    err = RuntimeError("Error in compaction: Failed to apply logs to the hnsw segment writer")
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
    # a second drop happens after the compaction error during populate.
    assert vs.delete_collection.call_count == 2
    assert ei.populate_async.await_count == 2
