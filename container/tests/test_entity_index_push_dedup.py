"""Regression tests for the entity index push dedup path.

Covers both halves of the fix:

- PART A: ``on_state_changed`` reuses the cached HA registry lookups
  on ``app.state.entity_lookups`` so incremental updates produce the
  same enriched ``EntityIndexEntry`` shape as the snapshot path.
- PART B: ``EntityIndex.batch_add`` / ``add`` short-circuit upserts
  when the stored ``content_hash`` already matches the entry's hash.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.bootstrap._entity import _store_entity_lookups
from app.entity.index import EntityIndex
from app.entity.ingest import state_to_entity_index_entry


def _state_payload(entity_id: str, state: str, area_id: str | None = "kitchen") -> dict:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {
            "friendly_name": "Kitchen Ceiling",
            "area_id": area_id,
        },
    }


def test_store_entity_lookups_atomic_assignment():
    """_store_entity_lookups publishes a complete snapshot in one assignment."""
    app = SimpleNamespace(state=SimpleNamespace())
    _store_entity_lookups(
        app,
        {"kitchen": "Kitchen"},
        {"light.kitchen_ceiling": ["main"]},
        {"light.kitchen_ceiling": "Kitchen Ceiling Device"},
        {"light.kitchen_ceiling": "kitchen"},
    )
    lookups = app.state.entity_lookups
    assert set(lookups.keys()) == {"area", "alias", "device", "area_id"}
    assert lookups["area"] == {"kitchen": "Kitchen"}
    assert lookups["alias"] == {"light.kitchen_ceiling": ["main"]}
    assert lookups["device"] == {"light.kitchen_ceiling": "Kitchen Ceiling Device"}
    assert lookups["area_id"] == {"light.kitchen_ceiling": "kitchen"}


def test_store_entity_lookups_replaces_previous():
    """Subsequent calls fully replace the previous lookup snapshot."""
    app = SimpleNamespace(state=SimpleNamespace())
    _store_entity_lookups(app, {"a": "A"}, {}, {}, {})
    _store_entity_lookups(app, {"b": "B"}, {"x": ["y"]}, {}, {"e": "b"})
    assert app.state.entity_lookups["area"] == {"b": "B"}
    assert app.state.entity_lookups["alias"] == {"x": ["y"]}
    assert app.state.entity_lookups["area_id"] == {"e": "b"}


def test_store_entity_lookups_handles_none_inputs():
    """None inputs are coerced to empty dicts for safe lookup access."""
    app = SimpleNamespace(state=SimpleNamespace())
    _store_entity_lookups(app, None, None, None, None)  # type: ignore[arg-type]
    assert app.state.entity_lookups == {
        "area": {},
        "alias": {},
        "device": {},
        "area_id": {},
    }


def test_state_to_entity_index_entry_area_id_lookup_overrides_missing_attrs():
    """area_id_lookup populates entry.area when attrs has no area_id."""
    state = {
        "entity_id": "light.kitchen_ceiling",
        "state": "on",
        "attributes": {"friendly_name": "Kitchen Ceiling"},
    }
    entry = state_to_entity_index_entry(
        state,
        entity_id="light.kitchen_ceiling",
        area_lookup={"kitchen": "Kitchen"},
        area_id_lookup={"light.kitchen_ceiling": "kitchen"},
    )
    assert entry.area == "kitchen"
    assert entry.area_name == "Kitchen"


def test_state_to_entity_index_entry_attrs_area_id_used_when_lookup_missing():
    """attrs-provided area_id remains a fallback when no lookup is supplied."""
    state = {
        "entity_id": "light.kitchen_ceiling",
        "state": "on",
        "attributes": {"friendly_name": "Kitchen Ceiling", "area_id": "kitchen"},
    }
    entry = state_to_entity_index_entry(
        state,
        entity_id="light.kitchen_ceiling",
        area_lookup={"kitchen": "Kitchen"},
        area_id_lookup=None,
    )
    assert entry.area == "kitchen"
    assert entry.area_name == "Kitchen"


def test_state_changed_with_cached_lookups_produces_enriched_entry():
    """Enrichment kwargs must reach state_to_entity_index_entry."""
    area_lookup = {"kitchen": "Kitchen"}
    alias_lookup = {"light.kitchen_ceiling": ["overhead"]}
    device_lookup = {"light.kitchen_ceiling": "Kitchen Bridge"}

    entry = state_to_entity_index_entry(
        _state_payload("light.kitchen_ceiling", "on"),
        entity_id="light.kitchen_ceiling",
        area_lookup=area_lookup,
        alias_lookup=alias_lookup,
        device_lookup=device_lookup,
    )
    assert entry.area_name == "Kitchen"
    assert "overhead" in entry.aliases
    assert entry.device_name == "Kitchen Bridge"


def test_state_change_only_does_not_change_content_hash():
    """Two state_changed events that only flip the runtime state must
    produce entries whose content_hash is identical, so PART B's
    short-circuit kicks in."""
    area_lookup = {"kitchen": "Kitchen"}
    alias_lookup = {"light.kitchen_ceiling": ["overhead"]}
    device_lookup = {"light.kitchen_ceiling": "Kitchen Bridge"}

    on_entry = state_to_entity_index_entry(
        _state_payload("light.kitchen_ceiling", "on"),
        entity_id="light.kitchen_ceiling",
        area_lookup=area_lookup,
        alias_lookup=alias_lookup,
        device_lookup=device_lookup,
    )
    off_entry = state_to_entity_index_entry(
        _state_payload("light.kitchen_ceiling", "off"),
        entity_id="light.kitchen_ceiling",
        area_lookup=area_lookup,
        alias_lookup=alias_lookup,
        device_lookup=device_lookup,
    )
    assert on_entry.content_hash == off_entry.content_hash


def test_two_state_changed_events_trigger_a_single_upsert():
    """End-to-end: two state-only changes flushed through batch_add only
    embed once, because the content_hash is unchanged on the second
    flush.
    """
    store = MagicMock()
    index = EntityIndex(store)

    area_lookup = {"kitchen": "Kitchen"}
    alias_lookup: dict[str, list[str]] = {}
    device_lookup: dict[str, str] = {}

    def make_entry(state: str):
        return state_to_entity_index_entry(
            _state_payload("light.kitchen_ceiling", state),
            entity_id="light.kitchen_ceiling",
            area_lookup=area_lookup,
            alias_lookup=alias_lookup,
            device_lookup=device_lookup,
        )

    first = make_entry("on")

    # First flush: collection is empty.
    store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    index.batch_add([first])
    assert store.upsert.call_count == 1

    # Simulate that the upserted row is now persisted with content_hash.
    persisted_meta = EntityIndex._build_metadata(first)
    store.get.return_value = {
        "ids": ["light.kitchen_ceiling"],
        "documents": [first.embedding_text],
        "metadatas": [persisted_meta],
    }

    second = make_entry("off")
    index.batch_add([second])

    # No additional upsert and no metadata-only update for state-only flip.
    assert store.upsert.call_count == 1
    store.update_metadata.assert_not_called()


def test_friendly_name_change_after_state_change_triggers_upsert():
    """When identity actually changes (e.g. friendly_name renamed in HA),
    the second flush must upsert."""
    store = MagicMock()
    index = EntityIndex(store)

    area_lookup = {"kitchen": "Kitchen"}
    first = state_to_entity_index_entry(
        _state_payload("light.kitchen_ceiling", "on"),
        entity_id="light.kitchen_ceiling",
        area_lookup=area_lookup,
        alias_lookup={},
        device_lookup={},
    )

    store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    index.batch_add([first])
    assert store.upsert.call_count == 1

    persisted_meta = EntityIndex._build_metadata(first)
    store.get.return_value = {
        "ids": ["light.kitchen_ceiling"],
        "documents": [first.embedding_text],
        "metadatas": [persisted_meta],
    }

    renamed_state = _state_payload("light.kitchen_ceiling", "off")
    renamed_state["attributes"]["friendly_name"] = "Kitchen Ceiling Renamed"
    second = state_to_entity_index_entry(
        renamed_state,
        entity_id="light.kitchen_ceiling",
        area_lookup=area_lookup,
        alias_lookup={},
        device_lookup={},
    )

    index.batch_add([second])
    assert store.upsert.call_count == 2
