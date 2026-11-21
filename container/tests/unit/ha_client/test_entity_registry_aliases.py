"""HA entity-registry aliases and entity categories end to end (issue 238).

HA keeps per-entity aliases in the entity registry only:
``config/entity_registry/list`` omits them, ``config/entity_registry/get_entries``
returns them (``null`` items are placeholders for the entity's own name).
The list rows carry ``entity_category``; config / diagnostic entities are
tagged on the index entry so agent keyword recall can skip them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bootstrap import _entity as entity_bootstrap
from app.entity.index import EntityIndex
from app.entity.ingest import state_to_entity_index_entry
from app.entity.tokens import entry_field_tokens
from app.ha_client.rest import HARestClient
from app.ha_client.websocket import (
    ENTITY_REGISTRY_ENTRIES_BATCH,
    EntityRegistryInfo,
    HAWebSocketClient,
    parse_entity_registry_aliases,
    parse_entity_registry_rows,
)
from app.models.entity_index import EntityIndexEntry


def _state(entity_id: str, friendly_name: str, state: str = "off") -> dict:
    return {"entity_id": entity_id, "state": state, "attributes": {"friendly_name": friendly_name}}


class TestParsing:
    def test_parse_rows_collects_categories_and_visible_ids(self):
        info = parse_entity_registry_rows(
            [
                {"entity_id": "light.girlande", "entity_category": None},
                {"entity_id": "light.led_ring", "entity_category": "config"},
                {"entity_id": "sensor.rssi", "entity_category": "diagnostic"},
                {"entity_id": "light.hidden", "hidden_by": "user", "entity_category": "config"},
                {"entity_id": "sensor.odd", "entity_category": "system"},
            ]
        )
        assert info.entity_categories == {
            "light.led_ring": "config",
            "sensor.rssi": "diagnostic",
            "light.hidden": "config",
        }
        assert info.entity_ids == {"light.girlande", "light.led_ring", "sensor.rssi", "sensor.odd"}

    def test_parse_aliases_skips_null_placeholders(self):
        result = {
            "light.girlande": {"entity_id": "light.girlande", "aliases": [None, "gelande schlafzimmer"]},
            "light.plain": {"entity_id": "light.plain", "aliases": [None]},
            "light.dupes": {"entity_id": "light.dupes", "aliases": ["Spot", "Spot", " ", 3]},
            "light.unknown": None,
            "light.no_id": {"aliases": ["Kugel"]},
        }
        assert parse_entity_registry_aliases(result) == {
            "light.girlande": ["gelande schlafzimmer"],
            "light.dupes": ["Spot"],
            "light.no_id": ["Kugel"],
        }

    def test_parse_aliases_accepts_entry_list_and_rejects_garbage(self):
        assert parse_entity_registry_aliases([{"entity_id": "light.a", "aliases": ["A"]}, "x"]) == {"light.a": ["A"]}
        assert parse_entity_registry_aliases(None) == {}


class TestWebSocketAliasRead:
    async def test_get_entity_aliases_batches_get_entries(self):
        client = HAWebSocketClient()
        ids = [f"light.l{i:04d}" for i in range(ENTITY_REGISTRY_ENTRIES_BATCH * 2 + 1)]

        async def _send(msg_type, **kwargs):
            assert msg_type == "config/entity_registry/get_entries"
            return {eid: {"entity_id": eid, "aliases": [None, f"alias {eid}"]} for eid in kwargs["entity_ids"]}

        client.send_command = AsyncMock(side_effect=_send)

        aliases = await client.get_entity_aliases(set(ids))

        sizes = [len(call.kwargs["entity_ids"]) for call in client.send_command.await_args_list]
        assert sizes == [ENTITY_REGISTRY_ENTRIES_BATCH, ENTITY_REGISTRY_ENTRIES_BATCH, 1]
        assert len(aliases) == len(ids)
        assert aliases["light.l0000"] == ["alias light.l0000"]

    async def test_get_entity_aliases_failure_returns_none(self):
        client = HAWebSocketClient()
        client.send_command = AsyncMock(return_value=None)
        assert await client.get_entity_aliases({"light.a"}) is None


class TestRestAliasLookup:
    def _client(self, aliases_result) -> tuple[HARestClient, MagicMock]:
        client = HARestClient()
        ws = MagicMock()
        ws.is_connected.return_value = True
        ws.get_entity_registry = AsyncMock(
            return_value=EntityRegistryInfo(entity_ids=frozenset({"light.girlande", "light.plain"}))
        )
        ws.get_entity_aliases = AsyncMock(return_value=aliases_result)
        client._state_observer = ws
        return client, ws

    async def test_aliases_read_for_registry_ids_and_cached(self):
        client, ws = self._client({"light.girlande": ["gelande schlafzimmer"]})

        assert await client.get_entity_aliases() == {"light.girlande": ["gelande schlafzimmer"]}
        assert await client.get_entity_aliases() == {"light.girlande": ["gelande schlafzimmer"]}

        ws.get_entity_aliases.assert_awaited_once()
        assert set(ws.get_entity_aliases.await_args.args[0]) == {"light.girlande", "light.plain"}

    async def test_failed_read_keeps_last_good_and_is_not_cached(self):
        client, ws = self._client({"light.girlande": ["gelande schlafzimmer"]})
        await client.get_entity_aliases()
        client.clear_area_registry_cache()
        ws.get_entity_aliases = AsyncMock(return_value=None)

        assert await client.get_entity_aliases() == {"light.girlande": ["gelande schlafzimmer"]}
        assert "entity_aliases" not in client._registry_cache

    async def test_no_websocket_continues_without_aliases(self):
        client = HARestClient()
        assert await client.get_entity_aliases() == {}


class TestIngestAndIndex:
    def test_aliases_and_category_reach_the_index_entry(self):
        entry = state_to_entity_index_entry(
            _state("light.girlande", "Lichterkette"),
            alias_lookup={"light.girlande": ["gelande schlafzimmer"]},
            entity_category_lookup={"light.girlande": "config"},
        )
        assert entry.aliases == ["gelande schlafzimmer"]
        assert entry.entity_category == "config"
        name_tokens, _identity, _area = entry_field_tokens(entry)
        assert {"gelande", "schlafzimmer"} <= name_tokens

    def test_entity_category_round_trips_through_metadata(self):
        entry = EntityIndexEntry(entity_id="light.led", friendly_name="LED", domain="light", entity_category="config")
        restored = EntityIndex._entry_from_metadata("light.led", EntityIndex._build_metadata(entry))
        assert restored.entity_category == "config"
        assert restored.content_hash == entry.content_hash

    def test_category_change_changes_content_hash(self):
        """A category change must re-upsert the row (``add`` skips rows whose hash matches)."""
        plain = EntityIndexEntry(entity_id="light.a", friendly_name="A", domain="light")
        tagged = plain.model_copy(update={"entity_category": "diagnostic"})
        tagged._content_hash = None
        assert plain.content_hash != tagged.content_hash


class _FakeHa:
    def __init__(self) -> None:
        self.hidden_entity_ids_loaded = True
        self.registry = EntityRegistryInfo(
            entity_categories={"light.led_ring": "config"},
            entity_ids=frozenset({"light.girlande", "light.led_ring"}),
        )

    async def get_states(self) -> list[dict]:
        return [_state("light.girlande", "Lichterkette"), _state("light.led_ring", "LED Ring")]

    async def get_entity_registry(self) -> EntityRegistryInfo:
        return self.registry

    async def get_area_registry(self) -> dict:
        return {}

    async def get_entity_aliases(self) -> dict:
        return {"light.girlande": ["gelande schlafzimmer"]}

    async def get_device_names(self) -> dict:
        return {}

    async def get_entity_areas(self) -> dict:
        return {}


async def test_snapshot_carries_registry_aliases_and_categories():
    app = SimpleNamespace(state=SimpleNamespace())

    with patch("app.bootstrap._entity._refresh_device_directory", new=AsyncMock()):
        entries = await entity_bootstrap.build_entity_snapshot(app, _FakeHa())

    by_id = {entry.entity_id: entry for entry in entries}
    assert by_id["light.girlande"].aliases == ["gelande schlafzimmer"]
    assert by_id["light.girlande"].entity_category is None
    assert by_id["light.led_ring"].entity_category == "config"
    assert app.state.entity_lookups["entity_category"] == {"light.led_ring": "config"}
    assert app.state.entity_lookups["alias"] == {"light.girlande": ["gelande schlafzimmer"]}
