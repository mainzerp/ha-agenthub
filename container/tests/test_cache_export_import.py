"""Tests for v4 cache export/import helpers.

# Phase 1 triage (F1) — dead-block recovery (test_cache_export_import.py)
# The triple-quoted block that wrapped lines 253-1069 has been removed.
# Promoted (valid against v4 API, no changes needed): 0
#   All dead-block tests used pre-v4 APIs: EXPORT_FORMAT_TAG (removed), StructuredActionKey
#   (removed), re_embed parameter in import_envelope (removed), _make_cache_manager using
#   _response_cache (removed), response tier alias in envelope (removed in v4), io module not
#   imported, old _make_routing_entry/_make_response_entry helper format.
# Rewritten (ported to v4 API): 1
#   test_build_export_filename_uses_single_tier_tag — simple helper test, valid concept, added below.
# Deleted (target removed surfaces or already covered by live section): all others
#   test_api_flush_accepts_legacy_tier_response: flush("response") raises ValueError in v4,
#   test must NOT be promoted as-is.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.cache.action_cache import make_action_entry_id
from app.cache.cache_manager import CacheManager
from app.cache.export_import import (
    SCHEMA_VERSION,
    SUPPORTED_FORMAT_VERSION,
    ImportValidationError,
    build_export_filename,
    import_envelope,
    iter_export_chunks,
    parse_envelope,
)
from app.cache.routing_cache import make_routing_entry_id
from app.cache.sqlite_cache_store import COLLECTION_ACTION_CACHE, COLLECTION_ROUTING_CACHE
from tests.helpers import make_action_cache_entry, make_routing_cache_entry


def _empty_page() -> dict:
    return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}


def _make_manager(store: MagicMock | None = None) -> CacheManager:
    cache_store = store or MagicMock()
    cache_store.count.return_value = 0
    cache_store.get.return_value = _empty_page()
    cache_store.delete_oldest.return_value = 0
    cache_store.delete_all.return_value = 0
    return CacheManager(cache_store)


def _page_from_action_entries(manager: CacheManager, entries: list) -> dict:
    return {
        "ids": [make_action_entry_id(entry.query_text, language=entry.language) for entry in entries],
        "documents": [entry.query_text for entry in entries],
        "metadatas": [manager.action_cache._serialize_metadata(entry) for entry in entries],
    }


def _page_from_routing_entries(manager: CacheManager, entries: list) -> dict:
    return {
        "ids": [make_routing_entry_id(entry.query_text, language=entry.language) for entry in entries],
        "documents": [entry.query_text for entry in entries],
        "metadatas": [manager._routing_cache._serialize_metadata(entry) for entry in entries],
    }


def _vector_store_with_pages(pages_by_collection: dict[str, list[dict]]) -> MagicMock:
    store = MagicMock()
    store.count.return_value = 0
    store.delete_oldest.return_value = 0
    store.delete_all.return_value = 0

    def _get(collection_name, *, include, limit=None, offset=None, ids=None):
        if ids is not None:
            return _empty_page()
        pages = pages_by_collection.get(collection_name, [])
        index = 0 if limit is None or limit <= 0 else (offset or 0) // limit
        if index >= len(pages):
            return _empty_page()
        return pages[index]

    store.get.side_effect = _get
    return store


def _make_envelope(
    *, action_entries=None, routing_entries=None, format_version=SUPPORTED_FORMAT_VERSION, schema_version=SCHEMA_VERSION
) -> dict:
    tiers: dict[str, list[dict]] = {}
    if action_entries is not None:
        tiers["action"] = [entry.model_dump() if hasattr(entry, "model_dump") else entry for entry in action_entries]
    if routing_entries is not None:
        tiers["routing"] = [entry.model_dump() if hasattr(entry, "model_dump") else entry for entry in routing_entries]
    return {
        "format_version": format_version,
        "exported_at": datetime.now(UTC).isoformat(),
        "schema_version": schema_version,
        "source_app_version": "1.4.0",
        "tiers": tiers,
    }


def test_build_export_filename_uses_all_tag_for_full_export():
    name = build_export_filename(["routing", "action"], datetime(2026, 4, 22, 10, 15, 30, tzinfo=UTC))

    assert name == "agent-assist-cache-all-20260422101530.json"


def test_build_export_filename_uses_single_tier_tag():
    name = build_export_filename(["routing"], datetime(2026, 4, 22, 10, 15, 30, tzinfo=UTC))

    assert name == "agent-assist-cache-routing-20260422101530.json"


def test_iter_export_chunks_emits_v4_action_and_routing_tiers():
    bootstrap_manager = _make_manager()
    action_entry = make_action_cache_entry(query_text="turn on kitchen light")
    routing_entry = make_routing_cache_entry(
        query_text="what is the kitchen temperature",
        condensed_task="Read kitchen temperature",
    )
    store = _vector_store_with_pages(
        {
            COLLECTION_ACTION_CACHE: [_page_from_action_entries(bootstrap_manager, [action_entry])],
            COLLECTION_ROUTING_CACHE: [_page_from_routing_entries(bootstrap_manager, [routing_entry])],
        }
    )
    manager = _make_manager(store)

    payload = json.loads(
        b"".join(iter_export_chunks(manager, ["action", "routing"], app_version="1.4.0")).decode("utf-8")
    )

    assert payload["format_version"] == SUPPORTED_FORMAT_VERSION
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["source_app_version"] == "1.4.0"
    assert set(payload["tiers"]) == {"action", "routing"}
    assert payload["tiers"]["action"][0]["query_text"] == action_entry.query_text
    assert payload["tiers"]["routing"][0]["condensed_task"] == "Read kitchen temperature"


def test_iter_export_chunks_paginates_until_short_page():
    bootstrap_manager = _make_manager()
    entries = [make_action_cache_entry(query_text=f"turn on light {index}") for index in range(3)]
    store = _vector_store_with_pages(
        {
            COLLECTION_ACTION_CACHE: [
                _page_from_action_entries(bootstrap_manager, entries[:2]),
                _page_from_action_entries(bootstrap_manager, entries[2:]),
            ]
        }
    )
    manager = _make_manager(store)

    with patch("app.cache.export_import.EXPORT_PAGE_SIZE", 2):
        payload = json.loads(b"".join(iter_export_chunks(manager, ["action"], app_version="1.4.0")).decode("utf-8"))

    assert [entry["query_text"] for entry in payload["tiers"]["action"]] == [entry.query_text for entry in entries]


def test_parse_envelope_accepts_valid_v4_envelope():
    envelope = _make_envelope(
        action_entries=[make_action_cache_entry()],
        routing_entries=[make_routing_cache_entry()],
    )

    parsed = parse_envelope(json.dumps(envelope).encode("utf-8"))

    assert parsed["format_version"] == SUPPORTED_FORMAT_VERSION
    assert set(parsed["tiers"]) == {"action", "routing"}


def test_parse_envelope_rejects_legacy_v3_format():
    envelope = _make_envelope(action_entries=[make_action_cache_entry()], format_version=3)

    with pytest.raises(ImportValidationError, match="legacy v<=3 export not supported"):
        parse_envelope(json.dumps(envelope).encode("utf-8"))


def test_parse_envelope_rejects_response_tier_alias():
    envelope = _make_envelope(action_entries=[make_action_cache_entry()])
    envelope["tiers"] = {"response": envelope["tiers"]["action"]}

    with pytest.raises(ImportValidationError, match="legacy v<=3 export not supported"):
        parse_envelope(json.dumps(envelope).encode("utf-8"))


@pytest.mark.asyncio
async def test_import_envelope_merge_upserts_action_and_routing_entries():
    store = MagicMock()
    store.count.return_value = 0
    store.upsert = MagicMock()
    store.delete_oldest.return_value = 0
    store.delete_all.return_value = 0
    manager = _make_manager(store)
    manager.action_cache._enforce_lru = MagicMock()
    manager._routing_cache._enforce_lru = MagicMock()
    action_entry = make_action_cache_entry(query_text="turn on kitchen light")
    routing_entry = make_routing_cache_entry(
        query_text="what is the kitchen temperature",
        condensed_task="Read kitchen temperature",
    )

    summary = await import_envelope(
        manager,
        _make_envelope(action_entries=[action_entry], routing_entries=[routing_entry]),
        mode="merge",
        tiers=["action", "routing"],
    )

    assert summary.tiers["action"].imported == 1
    assert summary.tiers["routing"].imported == 1
    assert [call.args[0] for call in store.upsert.call_args_list] == [COLLECTION_ACTION_CACHE, COLLECTION_ROUTING_CACHE]
    assert store.upsert.call_args_list[0].kwargs["ids"] == [
        make_action_entry_id(action_entry.query_text, language=action_entry.language)
    ]
    assert store.upsert.call_args_list[1].kwargs["ids"] == [
        make_routing_entry_id(routing_entry.query_text, language=routing_entry.language)
    ]


@pytest.mark.asyncio
async def test_import_envelope_replace_flushes_requested_tier_first():
    store = MagicMock()
    store.count.return_value = 0
    store.upsert = MagicMock()
    store.delete_oldest.return_value = 0
    store.delete_all.return_value = 0
    manager = _make_manager(store)
    manager.flush = MagicMock()
    manager.action_cache._enforce_lru = MagicMock()

    summary = await import_envelope(
        manager,
        _make_envelope(action_entries=[make_action_cache_entry()]),
        mode="replace",
        tiers=["action"],
    )

    assert summary.tiers["action"].imported == 1
    manager.flush.assert_called_once_with("action")


@pytest.mark.asyncio
async def test_import_envelope_skips_invalid_entries_and_records_warning():
    store = MagicMock()
    store.count.return_value = 0
    store.upsert = MagicMock()
    store.delete_oldest.return_value = 0
    store.delete_all.return_value = 0
    manager = _make_manager(store)
    manager.action_cache._enforce_lru = MagicMock()
    valid_entry = make_action_cache_entry(query_text="turn on kitchen light")

    summary = await import_envelope(
        manager,
        _make_envelope(action_entries=[valid_entry.model_dump(), {"query_text": "broken"}]),
        mode="merge",
        tiers=["action"],
    )

    assert summary.tiers["action"].imported == 1
    assert summary.tiers["action"].skipped == 1
    assert len(summary.tiers["action"].warnings) == 1
    store.upsert.assert_called_once()


@pytest.mark.asyncio
async def test_import_envelope_warns_when_requested_tier_is_missing():
    store = MagicMock()
    store.count.return_value = 0
    store.upsert = MagicMock()
    store.delete_oldest.return_value = 0
    store.delete_all.return_value = 0
    manager = _make_manager(store)

    summary = await import_envelope(
        manager,
        _make_envelope(action_entries=[make_action_cache_entry()]),
        mode="merge",
        tiers=["routing"],
    )

    assert summary.tiers["routing"].imported == 0
    assert summary.warnings == ["tier 'routing' not present in envelope"]
    store.upsert.assert_not_called()
