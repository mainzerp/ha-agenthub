"""Unit tests for app.entity.naming: own-name rule, collision guard, display helper.

Home Assistant 2026.11 builds an entity's ``friendly_name`` as
``"<device name> <user-set name>"``. The entity index uses the user-set name
only when HA's friendly_name is exactly that composition; every other case
keeps HA's friendly_name.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.entity.index import EntityIndex
from app.entity.ingest import parse_ha_states, state_to_entity_index_entry
from app.entity.naming import (
    choose_entity_name,
    compute_name_suppression,
    entity_display_name,
    name_fold_key,
    stripped_own_name,
)
from app.ha_client.websocket import EntityRegistryInfo, HAWebSocketClient, parse_entity_registry_rows
from app.models.entity_index import EntityIndexEntry

DIMMER = "Dimmer (Unterverteilung Gäste) (ABB700C7FF30)"


# ---------------------------------------------------------------------------
# Own-name rule
# ---------------------------------------------------------------------------


class TestStrippedOwnName:
    def test_prefixed_user_name_is_stripped(self):
        assert stripped_own_name(f"{DIMMER} Wohnzimmer", "Wohnzimmer", DIMMER) == "Wohnzimmer"

    def test_short_device_name_is_stripped(self):
        assert stripped_own_name("_ Wohnzimmer", "Wohnzimmer", "_") == "Wohnzimmer"

    def test_compat_name_without_prefix_keeps_ha_name(self):
        """Names frozen by HA's migration (compat_name) are already unprefixed."""
        assert stripped_own_name("Innenhof", "Innenhof", "Aktor (Unterverteilung Gäste) (ABB2B9192751)") is None
        assert choose_entity_name("Innenhof", "Innenhof", "Aktor (X) (S2)") == "Innenhof"

    def test_integration_name_keeps_ha_name(self):
        """No user-set name (registry name None): the integration name stays."""
        assert stripped_own_name(f"{DIMMER} Kanal A", None, DIMMER) is None
        assert choose_entity_name(f"{DIMMER} Kanal A", None, DIMMER) == f"{DIMMER} Kanal A"

    @pytest.mark.parametrize("user_name", ["", "   ", None])
    def test_main_entity_keeps_device_name(self, user_name):
        """A device's main entity has no own name; its friendly_name is the device name."""
        assert stripped_own_name(DIMMER, user_name, DIMMER) is None
        assert choose_entity_name(DIMMER, user_name, DIMMER) == DIMMER

    @pytest.mark.parametrize("device_name", [None, "", "  "])
    def test_entity_without_device_keeps_ha_name(self, device_name):
        assert stripped_own_name("Küche", "Küche", device_name) is None
        assert choose_entity_name("Küche", "Küche", device_name) == "Küche"

    def test_customize_override_keeps_ha_name(self):
        """``homeassistant: customize`` friendly_name differs from device + user name."""
        assert stripped_own_name("Mein Licht", "Wohnzimmer", DIMMER) is None
        assert choose_entity_name("Mein Licht", "Wohnzimmer", DIMMER) == "Mein Licht"

    def test_older_ha_user_name_is_the_whole_friendly_name(self):
        """Before 2026.11 a user-set name is the full friendly_name, never prefixed."""
        assert stripped_own_name("Wohnzimmer Licht", "Wohnzimmer Licht", "Wohnzimmer") is None

    def test_case_difference_is_not_stripped(self):
        assert stripped_own_name(f"{DIMMER.lower()} Wohnzimmer", "Wohnzimmer", DIMMER) is None

    @pytest.mark.parametrize(
        ("friendly_name", "user_name", "device_name"),
        [
            ("Dimmer  (X)   Wohnzimmer ", " Wohnzimmer", "Dimmer (X)"),
            ("Dimmer (X) Wohnzimmer", "Wohnzimmer  ", " Dimmer (X) "),
            ("Dimmer (X)\tWohnzimmer", "Wohnzimmer", "Dimmer (X)"),
        ],
    )
    def test_whitespace_variants_are_stripped(self, friendly_name, user_name, device_name):
        assert stripped_own_name(friendly_name, user_name, device_name) == "Wohnzimmer"

    def test_non_string_inputs_keep_ha_name(self):
        assert stripped_own_name(None, "Wohnzimmer", DIMMER) is None
        assert stripped_own_name(f"{DIMMER} Wohnzimmer", 7, DIMMER) is None
        assert choose_entity_name(None, "Wohnzimmer", DIMMER) == ""

    def test_suppressed_keeps_ha_name(self):
        assert choose_entity_name(f"{DIMMER} Wohnzimmer", "Wohnzimmer", DIMMER, suppressed=True) == (
            f"{DIMMER} Wohnzimmer"
        )


# ---------------------------------------------------------------------------
# Collision guard
# ---------------------------------------------------------------------------


class TestNameSuppression:
    def test_same_domain_stripped_names_both_keep_ha_names(self):
        names = [
            ("media_player.echo_a", "Echo A Küche"),
            ("media_player.echo_b", "Echo B Küche"),
        ]
        users = {"media_player.echo_a": "Küche", "media_player.echo_b": "Küche"}
        devices = {"media_player.echo_a": "Echo A", "media_player.echo_b": "Echo B"}
        assert compute_name_suppression(names, users, devices) == {"media_player.echo_a", "media_player.echo_b"}

    def test_different_domains_do_not_collide(self):
        names = [("light.kuche", "Dimmer Küche"), ("switch.kuche", "Aktor Küche")]
        users = {"light.kuche": "Küche", "switch.kuche": "Küche"}
        devices = {"light.kuche": "Dimmer", "switch.kuche": "Aktor"}
        assert compute_name_suppression(names, users, devices) == frozenset()

    def test_stripped_name_equal_to_unstripped_name_is_suppressed(self):
        """Only the stripped member reverts; the entity already named so keeps its name."""
        names = [("light.a", "Dimmer Küche"), ("light.b", "Küche")]
        users = {"light.a": "Küche"}
        devices = {"light.a": "Dimmer"}
        assert compute_name_suppression(names, users, devices) == {"light.a"}

    def test_collision_uses_shared_folding(self):
        names = [("light.a", "Dimmer Küche"), ("light.b", "Kueche")]
        users = {"light.a": "Küche"}
        devices = {"light.a": "Dimmer"}
        assert name_fold_key("Küche") == name_fold_key("  KUECHE ")
        assert compute_name_suppression(names, users, devices) == {"light.a"}

    def test_reverted_name_collision_repeats_until_stable(self):
        names = [
            ("light.a", "Dev X"),  # stripped -> "X"
            ("light.b", "Other X"),  # stripped -> "X" (collides with a)
            ("light.c", "Foo Dev X"),  # stripped -> "Dev X" (collides with a's reverted name)
        ]
        users = {"light.a": "X", "light.b": "X", "light.c": "Dev X"}
        devices = {"light.a": "Dev", "light.b": "Other", "light.c": "Foo"}
        assert compute_name_suppression(names, users, devices) == {"light.a", "light.b", "light.c"}

    def test_unique_stripped_name_is_kept(self):
        names = [("light.wohnzimmer", f"{DIMMER} Wohnzimmer"), ("light.lichtband", "Aktor Wohnzimmer Lichtband")]
        users = {"light.wohnzimmer": "Wohnzimmer", "light.lichtband": "Wohnzimmer Lichtband"}
        devices = {"light.wohnzimmer": DIMMER, "light.lichtband": "Aktor"}
        assert compute_name_suppression(names, users, devices) == frozenset()

    def test_no_user_names_means_no_suppression(self):
        assert compute_name_suppression([("light.a", "Küche"), ("light.b", "Küche")], {}, {}) == frozenset()


# ---------------------------------------------------------------------------
# Entity registry rows (WS client)
# ---------------------------------------------------------------------------


class TestRegistryRows:
    ROWS: ClassVar[list] = [
        {"entity_id": "light.wohnzimmer", "name": "Wohnzimmer", "hidden_by": None, "disabled_by": None},
        {"entity_id": "light.main", "name": None, "original_name": None},
        {"entity_id": "light.blank", "name": "  "},
        {"entity_id": "light.empty", "name": ""},
        {"entity_id": "light.hidden", "name": "Versteckt", "hidden_by": "user"},
        {"entity_id": "light.disabled", "disabled_by": "integration"},
        {"name": "No id"},
        "not-a-row",
    ]

    def test_parse_extracts_hidden_ids_and_user_names(self):
        info = parse_entity_registry_rows(self.ROWS)
        assert info.hidden_ids == {"light.hidden", "light.disabled"}
        assert info.user_names == {"light.wohnzimmer": "Wohnzimmer", "light.hidden": "Versteckt"}

    @pytest.mark.asyncio
    async def test_ws_client_one_round_trip_serves_both_views(self):
        client = HAWebSocketClient()
        client.send_command = AsyncMock(return_value=self.ROWS)

        info = await client.get_entity_registry()
        assert isinstance(info, EntityRegistryInfo)
        assert info.user_names["light.wohnzimmer"] == "Wohnzimmer"
        assert await client.get_hidden_entity_ids() == {"light.hidden", "light.disabled"}
        client.send_command.assert_awaited_with("config/entity_registry/list")

    @pytest.mark.asyncio
    async def test_ws_client_failure_returns_none(self):
        client = HAWebSocketClient()
        client.send_command = AsyncMock(return_value=None)
        assert await client.get_entity_registry() is None
        assert await client.get_hidden_entity_ids() is None


# ---------------------------------------------------------------------------
# Ingest and index entry
# ---------------------------------------------------------------------------


def _state(entity_id: str, friendly_name: str, state: str = "off") -> dict:
    return {"entity_id": entity_id, "state": state, "attributes": {"friendly_name": friendly_name}}


class TestIngest:
    def test_entry_carries_own_name_and_ha_name(self):
        entry = state_to_entity_index_entry(
            _state("light.wohnzimmer", f"{DIMMER} Wohnzimmer"),
            device_lookup={"light.wohnzimmer": DIMMER},
            user_name_lookup={"light.wohnzimmer": "Wohnzimmer"},
        )
        assert entry.friendly_name == "Wohnzimmer"
        assert entry.ha_friendly_name == f"{DIMMER} Wohnzimmer"
        assert entry.device_name == DIMMER
        assert entry.raw_friendly_name == f"{DIMMER} Wohnzimmer"

    def test_suppressed_entry_keeps_ha_name(self):
        entry = state_to_entity_index_entry(
            _state("light.wohnzimmer", f"{DIMMER} Wohnzimmer"),
            device_lookup={"light.wohnzimmer": DIMMER},
            user_name_lookup={"light.wohnzimmer": "Wohnzimmer"},
            name_suppressed=frozenset({"light.wohnzimmer"}),
        )
        assert entry.friendly_name == f"{DIMMER} Wohnzimmer"

    def test_parse_ha_states_computes_suppression_from_snapshot(self):
        states = [
            _state("media_player.echo_a", "Echo A Küche"),
            _state("media_player.echo_b", "Echo B Küche"),
            _state("light.wohnzimmer", f"{DIMMER} Wohnzimmer"),
            _state("light.hidden", "Echo C Wohnzimmer"),
        ]
        users = {
            "media_player.echo_a": "Küche",
            "media_player.echo_b": "Küche",
            "light.wohnzimmer": "Wohnzimmer",
            "light.hidden": "Wohnzimmer",
        }
        devices = {
            "media_player.echo_a": "Echo A",
            "media_player.echo_b": "Echo B",
            "light.wohnzimmer": DIMMER,
            "light.hidden": "Echo C",
        }
        entries = parse_ha_states(states, device_lookup=devices, user_name_lookup=users, hidden_ids={"light.hidden"})
        names = {e.entity_id: e.friendly_name for e in entries}
        # Hidden entities do not take part in the collision guard.
        assert names == {
            "media_player.echo_a": "Echo A Küche",
            "media_player.echo_b": "Echo B Küche",
            "light.wohnzimmer": "Wohnzimmer",
        }

    def test_content_hash_unchanged_when_name_does_not_change(self):
        """No re-embed for entries the own-name rule leaves alone."""
        state = _state("light.innenhof", "Innenhof")
        before = state_to_entity_index_entry(state, device_lookup={"light.innenhof": "Aktor"})
        after = state_to_entity_index_entry(
            state,
            device_lookup={"light.innenhof": "Aktor"},
            user_name_lookup={"light.innenhof": "Innenhof"},
            name_suppressed=frozenset(),
        )
        legacy = EntityIndexEntry(
            entity_id="light.innenhof",
            friendly_name="Innenhof",
            domain="light",
            device_name="Aktor",
            id_tokens=["innenhof"],
            state="off",
        )
        assert before.content_hash == after.content_hash == legacy.content_hash
        assert before.embedding_text == after.embedding_text == legacy.embedding_text

    def test_ha_friendly_name_is_not_hashed_or_embedded(self):
        base = EntityIndexEntry(entity_id="light.a", friendly_name="Wohnzimmer", domain="light")
        audited = base.model_copy(update={"ha_friendly_name": "Dimmer Wohnzimmer"})
        assert base.content_hash == audited.content_hash
        assert "Dimmer" not in audited.embedding_text

    def test_with_name_drops_cached_hash(self):
        entry = EntityIndex._entry_from_metadata(
            "light.a",
            {"friendly_name": "Dimmer Wohnzimmer", "domain": "light", "content_hash": "stale"},
        )
        renamed = entry.with_name("Wohnzimmer")
        assert renamed.friendly_name == "Wohnzimmer"
        assert renamed.content_hash != "stale"


class TestIndexMetadata:
    def test_metadata_round_trip_keeps_ha_name(self):
        entry = EntityIndexEntry(
            entity_id="light.a", friendly_name="Wohnzimmer", ha_friendly_name="Dimmer Wohnzimmer", domain="light"
        )
        meta = EntityIndex._build_metadata(entry)
        assert meta["ha_friendly_name"] == "Dimmer Wohnzimmer"
        assert EntityIndex._entry_from_metadata("light.a", meta).ha_friendly_name == "Dimmer Wohnzimmer"

    def test_older_metadata_without_ha_name_loads(self):
        entry = EntityIndex._entry_from_metadata("light.a", {"friendly_name": "Küche", "domain": "light"})
        assert entry.ha_friendly_name is None
        assert entry.raw_friendly_name == "Küche"

    def test_sync_reports_renamed_entities_only(self):
        store = MagicMock()
        index = EntityIndex(store)
        old = EntityIndexEntry(entity_id="light.a", friendly_name=f"{DIMMER} Wohnzimmer", domain="light")
        same = EntityIndexEntry(entity_id="light.b", friendly_name="Innenhof", domain="light")
        store.get.return_value = {
            "ids": ["light.a", "light.b"],
            "documents": [old.embedding_text, same.embedding_text],
            "metadatas": [EntityIndex._build_metadata(old), EntityIndex._build_metadata(same)],
        }
        new = old.with_name("Wohnzimmer")

        result = index.sync([new, same])

        assert result["renamed"] == ["light.a"]
        assert result["updated"] == 1
        assert result["unchanged"] == 1
        upserted = store.upsert.call_args.kwargs["ids"]
        assert upserted == ["light.a"]

    def test_batch_add_and_add_report_name_changes(self):
        store = MagicMock()
        index = EntityIndex(store)
        old = EntityIndexEntry(entity_id="light.a", friendly_name=f"{DIMMER} Wohnzimmer", domain="light")
        store.get.return_value = {
            "ids": ["light.a"],
            "documents": [old.embedding_text],
            "metadatas": [EntityIndex._build_metadata(old)],
        }
        assert index.batch_add([old]) == []
        assert index.batch_add([old.with_name("Wohnzimmer")]) == ["light.a"]
        assert index.add(old.with_name("Wohnzimmer")) is True
        assert index.add(old) is False


# ---------------------------------------------------------------------------
# Display helper
# ---------------------------------------------------------------------------


class TestEntityDisplayName:
    def test_prefers_index_name(self):
        index = MagicMock()
        index.get_by_id.return_value = SimpleNamespace(friendly_name="Wohnzimmer")
        attrs = {"friendly_name": f"{DIMMER} Wohnzimmer"}
        assert entity_display_name(index, "light.wohnzimmer", attrs) == "Wohnzimmer"

    def test_falls_back_to_attribute_then_entity_id(self):
        index = MagicMock()
        index.get_by_id.return_value = None
        assert entity_display_name(index, "light.a", {"friendly_name": "HA Name"}) == "HA Name"
        assert entity_display_name(index, "light.a", {}) == "light.a"
        assert entity_display_name(None, "light.a", {"friendly_name": "HA Name"}) == "HA Name"

    def test_mock_entries_and_errors_fall_back(self):
        index = MagicMock()  # get_by_id returns a MagicMock without a string name
        assert entity_display_name(index, "light.a", {"friendly_name": "HA Name"}) == "HA Name"
        index.get_by_id.side_effect = RuntimeError("boom")
        assert entity_display_name(index, "light.a", {"friendly_name": "HA Name"}) == "HA Name"

    def test_async_double_is_not_awaited(self, recwarn):
        index = MagicMock()
        index.get_by_id = AsyncMock(return_value=SimpleNamespace(friendly_name="Wohnzimmer"))
        assert entity_display_name(index, "light.a", {"friendly_name": "HA Name"}) == "HA Name"
        assert not [w for w in recwarn.list if "never awaited" in str(w.message)]
