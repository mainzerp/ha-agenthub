"""Tests for v4 cache export/import helpers."""

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
from app.cache.vector_store import COLLECTION_ACTION_CACHE, COLLECTION_ROUTING_CACHE, VectorStore
from tests.helpers import make_action_cache_entry, make_routing_cache_entry


def _empty_page() -> dict:
    return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}


def _make_manager(store: MagicMock | None = None) -> CacheManager:
    vector_store = store or MagicMock(spec=VectorStore)
    vector_store.count.return_value = 0
    vector_store.get.return_value = _empty_page()
    return CacheManager(vector_store)


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
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0

    def _get(collection_name, *, include, limit=None, offset=None, ids=None):
        if ids is not None:
            return _empty_page()
        pages = pages_by_collection.get(collection_name, [])
        if limit is None or limit <= 0:
            index = 0
        else:
            index = (offset or 0) // limit
        if index >= len(pages):
            return _empty_page()
        return pages[index]

    store.get.side_effect = _get
    return store


def _make_envelope(*, action_entries=None, routing_entries=None, format_version=SUPPORTED_FORMAT_VERSION, schema_version=SCHEMA_VERSION) -> dict:
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

    payload = json.loads(b"".join(iter_export_chunks(manager, ["action", "routing"], app_version="1.4.0")).decode("utf-8"))

    assert payload["format_version"] == SUPPORTED_FORMAT_VERSION
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["source_app_version"] == "1.4.0"
    assert set(payload["tiers"]) == {"action", "routing"}
    assert payload["tiers"]["action"][0]["query_text"] == action_entry.query_text
    assert payload["tiers"]["routing"][0]["condensed_task"] == "Read kitchen temperature"


def test_iter_export_chunks_paginates_until_short_page():
    bootstrap_manager = _make_manager()
    entries = [
        make_action_cache_entry(query_text=f"turn on light {index}")
        for index in range(3)
    ]
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
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
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
    assert store.upsert.call_args_list[0].kwargs["ids"] == [make_action_entry_id(action_entry.query_text, language=action_entry.language)]
    assert store.upsert.call_args_list[1].kwargs["ids"] == [make_routing_entry_id(routing_entry.query_text, language=routing_entry.language)]


@pytest.mark.asyncio
async def test_import_envelope_replace_flushes_requested_tier_first():
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
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
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
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
    store = MagicMock(spec=VectorStore)
    store.count.return_value = 0
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

'''
def _make_routing_entry(entry_id: str = "abc123") -> dict:
    return {
        "id": entry_id,
        "document": "turn on the kitchen light",
        "embedding": [0.1, 0.2, 0.3],
        "metadata": {
            "agent_id": "light-agent",
            "confidence": "0.95",
            "hit_count": "1",
            "condensed_task": "turn on light kitchen",
            "created_at": "2026-04-20T08:00:00+00:00",
            "last_accessed": "2026-04-22T07:55:12+00:00",
            "language": "en",
        },
    }


def _make_response_entry(entry_id: str = "def456") -> dict:
    structured_key = StructuredActionKey(
        language="en",
        target_agent="climate-agent",
        domain="climate",
        service="set_temperature",
        target_kind="entity_ids",
        target_value="climate.living_room",
        service_data_norm='{"temperature":21}',
    )
    return {
        "id": entry_id,
        "document": "set the living room to 21 degrees",
        "embedding": [0.4, 0.5, 0.6],
        "metadata": {
            "response_text": "Living room set to 21 degrees.",
            "agent_id": "climate-agent",
            "confidence": "0.97",
            "hit_count": "0",
            "entity_ids": "climate.living_room",
            "created_at": "2026-04-21T18:00:00+00:00",
            "last_accessed": "2026-04-22T09:00:00+00:00",
            "language": "en",
            "schema_version": "3",
            "cached_action": '{"service":"climate/set_temperature","entity_id":"climate.living_room","service_data":{"temperature":21}}',
            "structured_key": structured_key.model_dump_json(),
            "target_agent": "climate-agent",
            "domain": "climate",
            "service": "set_temperature",
            "target_kind": "entity_ids",
            "target_value": "climate.living_room",
            "service_data_norm": '{"temperature":21}',
            "executed_at": "2026-04-21T18:00:00+00:00",
            "origin_area_id": "",
            "origin_device_id": "",
        },
    }


def _make_envelope(
    routing_entries: list[dict] | None = None,
    response_entries: list[dict] | None = None,
    *,
    format_version: int = 1,
    action_schema_version: int = 3,
) -> dict:
    tiers: dict = {}
    if routing_entries is not None:
        tiers["routing"] = {
            "schema_version": 1,
            "count": len(routing_entries),
            "entries": routing_entries,
        }
    if response_entries is not None:
        tiers["response"] = {
            "schema_version": action_schema_version,
            "count": len(response_entries),
            "entries": response_entries,
        }
    return {
        "export_format": EXPORT_FORMAT_TAG,
        "format_version": format_version,
        "generated_at": "2026-04-22T10:15:00+00:00",
        "source": {
            "app_version": "0.20.0",
            "embedding_model": "all-MiniLM-L6-v2",
            "embedding_dim": 3,
        },
        "tiers": tiers,
    }


def _make_cache_manager(vector_store: MagicMock) -> MagicMock:
    """Return a MagicMock(spec=CacheManager) wired with the given vector store."""

    cm = MagicMock(spec=CacheManager)
    cm._vector_store = vector_store
    cm._routing_cache = MagicMock()
    cm._response_cache = MagicMock()
    cm.flush = MagicMock()
    return cm


def _vector_store_with_pages(
    pages_by_collection: dict[str, list[dict]],
) -> MagicMock:
    """Build a MagicMock(spec=VectorStore) that returns the given pages.

    ``pages_by_collection[name]`` is a list of pages; each page is a
    dict with ``ids`` / ``documents`` / ``metadatas`` / ``embeddings``
    keys mirroring Chroma. Pagination cursors only advance for full
    pagination calls (limit > 1); single-row probe calls used by
    ``_detect_embedding_dim`` are answered from page 0 without
    consuming it.
    """

    store = MagicMock(spec=VectorStore)
    counts = {name: sum(len(p.get("ids", [])) for p in pages) for name, pages in pages_by_collection.items()}
    cursors: dict[str, int] = {name: 0 for name in pages_by_collection}

    def _count(name):
        return counts.get(name, 0)

    def _get(name, **kwargs):
        pages = pages_by_collection.get(name, [])
        # Single-row probe (dim detection) does not consume the cursor.
        if kwargs.get("limit") == 1:
            if not pages or not pages[0].get("ids"):
                return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}
            first = pages[0]
            return {
                "ids": first["ids"][:1],
                "documents": (first.get("documents") or [None])[:1],
                "metadatas": (first.get("metadatas") or [None])[:1],
                "embeddings": (first.get("embeddings") or [None])[:1],
            }
        idx = cursors[name]
        cursors[name] += 1
        if idx >= len(pages):
            return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}
        return pages[idx]

    store.count.side_effect = _count
    store.get.side_effect = _get
    return store


# ---------------------------------------------------------------------------
# 7.1 Helper unit tests
# ---------------------------------------------------------------------------


def test_build_export_filename_uses_all_tag_for_full_export():
    from datetime import datetime

    name = build_export_filename(["routing", "response"], datetime(2026, 4, 22, 10, 15, 30))
    assert name == "agent-assist-cache-all-20260422101530.json"


def test_build_export_filename_uses_single_tier_tag():
    from datetime import datetime

    name = build_export_filename(["routing"], datetime(2026, 4, 22, 10, 15, 30))
    assert name == "agent-assist-cache-routing-20260422101530.json"


def test_iter_export_chunks_emits_valid_envelope():
    routing_pages = [
        {
            "ids": ["r1"],
            "documents": ["doc1"],
            "metadatas": [{"agent_id": "a1", "language": "en"}],
            "embeddings": [[0.1, 0.2]],
        }
    ]
    response_pages = [
        {
            "ids": ["s1"],
            "documents": ["doc2"],
            "metadatas": [{"agent_id": "b1", "language": "en"}],
            "embeddings": [[0.3, 0.4]],
        }
    ]
    store = _vector_store_with_pages(
        {COLLECTION_ROUTING_CACHE: routing_pages, COLLECTION_RESPONSE_CACHE: response_pages}
    )
    cm = _make_cache_manager(store)

    chunks = list(iter_export_chunks(cm, ["routing", "response"], app_version="0.20.0"))
    payload = b"".join(chunks).decode("utf-8")
    envelope = json.loads(payload)

    assert envelope["export_format"] == EXPORT_FORMAT_TAG
    assert envelope["format_version"] == SUPPORTED_FORMAT_VERSION
    assert envelope["source"]["app_version"] == "0.20.0"
    assert set(envelope["tiers"].keys()) == {"routing", "action"}
    assert envelope["tiers"]["routing"]["count"] == 1
    assert envelope["tiers"]["routing"]["entries"][0]["id"] == "r1"
    assert envelope["tiers"]["action"]["entries"][0]["id"] == "s1"


def test_iter_export_chunks_skips_unrequested_tier():
    routing_pages = [
        {
            "ids": ["r1"],
            "documents": ["doc1"],
            "metadatas": [{"agent_id": "a1"}],
            "embeddings": [[0.1]],
        }
    ]
    store = _vector_store_with_pages({COLLECTION_ROUTING_CACHE: routing_pages})
    cm = _make_cache_manager(store)

    payload = b"".join(iter_export_chunks(cm, ["routing"], app_version="0.20.0"))
    envelope = json.loads(payload)
    assert "routing" in envelope["tiers"]
    assert "action" not in envelope["tiers"]
    assert "response" not in envelope["tiers"]


def test_iter_export_chunks_paginates():
    # Two full pages plus a short tail to verify pagination loop exit.
    page1 = {
        "ids": [f"id{i}" for i in range(EXPORT_PAGE_SIZE)],
        "documents": [f"doc{i}" for i in range(EXPORT_PAGE_SIZE)],
        "metadatas": [{"agent_id": "a"} for _ in range(EXPORT_PAGE_SIZE)],
        "embeddings": [[0.0] for _ in range(EXPORT_PAGE_SIZE)],
    }
    page2 = {
        "ids": ["tail"],
        "documents": ["tail-doc"],
        "metadatas": [{"agent_id": "a"}],
        "embeddings": [[0.0]],
    }
    store = _vector_store_with_pages({COLLECTION_ROUTING_CACHE: [page1, page2]})
    cm = _make_cache_manager(store)

    list(iter_export_chunks(cm, ["routing"], app_version="0.20.0"))
    # Two get() calls for two pages.
    get_calls = [c for c in store.get.call_args_list if c.args[0] == COLLECTION_ROUTING_CACHE]
    # One detect-dim call + two pagination calls.
    pagination_calls = [c for c in get_calls if c.kwargs.get("limit") == EXPORT_PAGE_SIZE]
    assert len(pagination_calls) == 2
    offsets = [c.kwargs["offset"] for c in pagination_calls]
    assert offsets == [0, EXPORT_PAGE_SIZE]


def test_parse_envelope_rejects_wrong_format():
    raw = json.dumps(
        {"export_format": "other", "format_version": 1, "tiers": {"routing": {"schema_version": 1, "entries": []}}}
    ).encode()
    with pytest.raises(ImportValidationError):
        parse_envelope(raw)


def test_parse_envelope_rejects_future_format_version():
    raw = json.dumps(
        {
            "export_format": EXPORT_FORMAT_TAG,
            "format_version": SUPPORTED_FORMAT_VERSION + 1,
            "tiers": {"routing": {"schema_version": 1, "entries": []}},
        }
    ).encode()
    with pytest.raises(ImportValidationError):
        parse_envelope(raw)


def test_parse_envelope_rejects_future_schema_version():
    raw = json.dumps(
        {
            "export_format": EXPORT_FORMAT_TAG,
            "format_version": 1,
            "tiers": {"routing": {"schema_version": 99, "entries": []}},
        }
    ).encode()
    with pytest.raises(ImportValidationError):
        parse_envelope(raw)


def test_parse_envelope_accepts_minimal_valid():
    raw = json.dumps(
        {
            "export_format": EXPORT_FORMAT_TAG,
            "format_version": 1,
            "tiers": {"routing": {"schema_version": 1, "entries": []}},
        }
    ).encode()
    envelope = parse_envelope(raw)
    assert envelope["export_format"] == EXPORT_FORMAT_TAG


def test_parse_envelope_rejects_non_json():
    with pytest.raises(ImportValidationError):
        parse_envelope(b"not-json")


def test_parse_envelope_rejects_oversized():
    from app.cache import export_import as ei

    big = b"a" * (ei.MAX_IMPORT_BYTES + 1)
    with pytest.raises(ImportValidationError):
        parse_envelope(big)


@pytest.mark.asyncio
async def test_import_envelope_merge_calls_prepare_for_flush_then_upsert():
    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)

    envelope = _make_envelope(routing_entries=[_make_routing_entry()])
    summary = await import_envelope(cm, envelope, mode="merge", tiers=["routing"], re_embed=False)

    cm._routing_cache.prepare_for_flush.assert_called_once()
    cm.flush.assert_not_called()
    store.upsert.assert_called()
    cm._routing_cache._enforce_lru.assert_called_once()
    assert summary.tiers["routing"].imported == 1


@pytest.mark.asyncio
async def test_import_envelope_replace_calls_flush_first():
    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)

    envelope = _make_envelope(routing_entries=[_make_routing_entry()])
    await import_envelope(cm, envelope, mode="replace", tiers=["routing"], re_embed=False)

    cm.flush.assert_called_once_with("routing")
    store.upsert.assert_called()


@pytest.mark.asyncio
async def test_import_envelope_re_embed_drops_embeddings():
    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)

    envelope = _make_envelope(routing_entries=[_make_routing_entry()])
    summary = await import_envelope(cm, envelope, mode="merge", tiers=["routing"], re_embed=True)

    upsert_calls = store.upsert.call_args_list
    assert any(c.kwargs.get("embeddings") is None for c in upsert_calls)
    assert summary.tiers["routing"].re_embedded == 1


@pytest.mark.asyncio
async def test_import_envelope_dim_mismatch_forces_re_embed_with_warning():
    store = MagicMock(spec=VectorStore)
    # detect_embedding_dim sees a 384-dim entry already in collection.
    store.get.return_value = {
        "ids": ["existing"],
        "embeddings": [[0.0] * 384],
    }
    cm = _make_cache_manager(store)

    entry = _make_routing_entry()
    entry["embedding"] = [0.1, 0.2, 0.3]  # only 3 dims
    envelope = _make_envelope(routing_entries=[entry])
    summary = await import_envelope(cm, envelope, mode="merge", tiers=["routing"], re_embed=False)

    assert summary.tiers["routing"].re_embedded == 1
    assert any("dim mismatch" in w for w in summary.tiers["routing"].warnings)


@pytest.mark.asyncio
async def test_import_envelope_skips_missing_agent_id():
    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)

    bad = _make_routing_entry("bad")
    bad["metadata"] = dict(bad["metadata"])
    bad["metadata"]["agent_id"] = ""
    good1 = _make_routing_entry("good1")
    good2 = _make_routing_entry("good2")
    envelope = _make_envelope(routing_entries=[bad, good1, good2])
    summary = await import_envelope(cm, envelope, mode="merge", tiers=["routing"], re_embed=False)

    assert summary.tiers["routing"].imported == 2
    assert summary.tiers["routing"].skipped == 1
    assert any("agent_id" in w for w in summary.tiers["routing"].warnings)


@pytest.mark.asyncio
async def test_import_envelope_defaults_missing_language_to_en():
    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)

    entry = _make_routing_entry()
    entry["metadata"] = dict(entry["metadata"])
    entry["metadata"].pop("language", None)
    envelope = _make_envelope(routing_entries=[entry])
    summary = await import_envelope(cm, envelope, mode="merge", tiers=["routing"], re_embed=False)

    assert summary.tiers["routing"].imported == 1
    assert any("language" in w for w in summary.tiers["routing"].warnings)
    metadatas = store.upsert.call_args_list[0].kwargs["metadatas"]
    assert metadatas[0]["language"] == "en"


@pytest.mark.asyncio
async def test_import_envelope_drops_invalid_cached_action():
    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)

    bad = _make_response_entry("bad-resp")
    bad["metadata"] = dict(bad["metadata"])
    bad["metadata"]["cached_action"] = "{not-json"
    good = _make_response_entry("good-resp")
    envelope = _make_envelope(response_entries=[bad, good])
    summary = await import_envelope(cm, envelope, mode="merge", tiers=["response"], re_embed=False)

    assert summary.tiers["action"].imported == 1
    assert summary.tiers["action"].skipped == 1
    assert any("cached_action" in w for w in summary.tiers["action"].warnings)


@pytest.mark.asyncio
async def test_import_envelope_runs_enforce_lru_once_per_tier():
    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)

    envelope = _make_envelope(
        routing_entries=[_make_routing_entry("r1")],
        response_entries=[_make_response_entry("s1")],
    )
    await import_envelope(cm, envelope, mode="merge", tiers=["routing", "response"], re_embed=False)

    cm._routing_cache._enforce_lru.assert_called_once()
    cm._response_cache._enforce_lru.assert_called_once()


# ---------------------------------------------------------------------------
# 7.2 API tests
# ---------------------------------------------------------------------------


def _build_cache_api_app(cache_manager):
    """Build a minimal FastAPI app mounting only the cache router.

    Auth is overridden to a no-op admin session.
    """

    from fastapi import FastAPI

    from app.api.routes.cache_api import router as cache_router
    from app.security.auth import require_admin_session

    app = FastAPI()
    app.dependency_overrides[require_admin_session] = lambda: {"username": "admin"}
    app.state.cache_manager = cache_manager
    # ensure_setup_runtime_initialized expects this attribute.
    app.state.setup_runtime_initialized = True
    app.include_router(cache_router)
    return app


def _make_export_cache_manager() -> MagicMock:
    routing_pages = [
        {
            "ids": ["r1"],
            "documents": ["doc1"],
            "metadatas": [{"agent_id": "a1", "language": "en"}],
            "embeddings": [[0.1, 0.2]],
        }
    ]
    store = _vector_store_with_pages({COLLECTION_ROUTING_CACHE: routing_pages})
    return _make_cache_manager(store)


def _make_export_cache_manager_with_action() -> MagicMock:
    action_entry = _make_response_entry("s1")
    routing_pages = [
        {
            "ids": ["r1"],
            "documents": ["doc1"],
            "metadatas": [{"agent_id": "a1", "language": "en"}],
            "embeddings": [[0.1, 0.2]],
        }
    ]
    action_pages = [
        {
            "ids": [action_entry["id"]],
            "documents": [action_entry["document"]],
            "metadatas": [action_entry["metadata"]],
            "embeddings": [action_entry["embedding"]],
        }
    ]
    store = _vector_store_with_pages(
        {
            COLLECTION_ROUTING_CACHE: routing_pages,
            COLLECTION_RESPONSE_CACHE: action_pages,
        }
    )
    return _make_cache_manager(store)


def test_export_endpoint_returns_attachment_headers():
    from fastapi.testclient import TestClient

    cm = _make_export_cache_manager()
    app = _build_cache_api_app(cm)
    client = TestClient(app)

    resp = client.get("/api/admin/cache/export?tier=routing")
    assert resp.status_code == 200
    cd = resp.headers.get("content-disposition", "")
    assert cd.startswith('attachment; filename="agent-assist-cache-')
    payload = json.loads(resp.content.decode("utf-8"))
    assert payload["export_format"] == EXPORT_FORMAT_TAG
    assert "routing" in payload["tiers"]


def test_export_rejects_invalid_tier():
    from fastapi.testclient import TestClient

    cm = _make_export_cache_manager()
    app = _build_cache_api_app(cm)
    client = TestClient(app)

    resp = client.get("/api/admin/cache/export?tier=foo")
    assert resp.status_code == 422


def test_export_503_when_cache_manager_missing():
    from fastapi.testclient import TestClient

    app = _build_cache_api_app(None)
    client = TestClient(app)

    resp = client.get("/api/admin/cache/export")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "error"


def test_import_endpoint_happy_path():
    from fastapi.testclient import TestClient

    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)
    app = _build_cache_api_app(cm)
    client = TestClient(app)

    envelope = _make_envelope(routing_entries=[_make_routing_entry()])
    raw = json.dumps(envelope).encode("utf-8")
    resp = client.post(
        "/api/admin/cache/import",
        files={"file": ("envelope.json", io.BytesIO(raw), "application/json")},
        data={"mode": "merge", "tiers": "routing", "re_embed": "false"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "merge"
    assert body["tiers"]["routing"]["imported"] == 1


def test_import_rejects_invalid_mode():
    from fastapi.testclient import TestClient

    store = MagicMock(spec=VectorStore)
    cm = _make_cache_manager(store)
    app = _build_cache_api_app(cm)
    client = TestClient(app)

    envelope = _make_envelope(routing_entries=[_make_routing_entry()])
    raw = json.dumps(envelope).encode("utf-8")
    resp = client.post(
        "/api/admin/cache/import",
        files={"file": ("envelope.json", io.BytesIO(raw), "application/json")},
        data={"mode": "bogus", "tiers": "routing"},
    )
    assert resp.status_code == 400


def test_import_rejects_oversized_payload(monkeypatch):
    from fastapi.testclient import TestClient

    from app.cache import export_import as ei

    store = MagicMock(spec=VectorStore)
    cm = _make_cache_manager(store)
    app = _build_cache_api_app(cm)
    client = TestClient(app)

    from app.api.routes import cache_api as cache_api_mod

    monkeypatch.setattr(ei, "MAX_IMPORT_BYTES", 8)
    monkeypatch.setattr(cache_api_mod, "MAX_IMPORT_BYTES", 8)
    raw = b"x" * 32
    resp = client.post(
        "/api/admin/cache/import",
        files={"file": ("envelope.json", io.BytesIO(raw), "application/json")},
        data={"mode": "merge", "tiers": "routing"},
    )
    assert resp.status_code == 413


def test_import_rejects_bad_envelope():
    from fastapi.testclient import TestClient

    store = MagicMock(spec=VectorStore)
    cm = _make_cache_manager(store)
    app = _build_cache_api_app(cm)
    client = TestClient(app)

    raw = json.dumps({"foo": "bar"}).encode("utf-8")
    resp = client.post(
        "/api/admin/cache/import",
        files={"file": ("envelope.json", io.BytesIO(raw), "application/json")},
        data={"mode": "merge", "tiers": "routing"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "error"
    assert "export_format" in body["detail"]


def test_import_replace_calls_flush():
    from fastapi.testclient import TestClient

    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)
    app = _build_cache_api_app(cm)
    client = TestClient(app)

    envelope = _make_envelope(routing_entries=[_make_routing_entry()])
    raw = json.dumps(envelope).encode("utf-8")
    resp = client.post(
        "/api/admin/cache/import",
        files={"file": ("envelope.json", io.BytesIO(raw), "application/json")},
        data={"mode": "replace", "tiers": "routing"},
    )
    assert resp.status_code == 200, resp.text
    cm.flush.assert_called_once_with("routing")


def test_import_passes_re_embed_flag():
    from fastapi.testclient import TestClient

    store = MagicMock(spec=VectorStore)
    cm = _make_cache_manager(store)
    app = _build_cache_api_app(cm)
    client = TestClient(app)

    envelope = _make_envelope(routing_entries=[_make_routing_entry()])
    raw = json.dumps(envelope).encode("utf-8")

    async def _fake_import_envelope(*args, **kwargs):
        return ImportSummary(
            mode=kwargs["mode"],
            format_version=1,
            tiers={"routing": TierImportResult(imported=1)},
        )

    with patch(
        "app.api.routes.cache_api.import_envelope",
        side_effect=_fake_import_envelope,
    ) as patched:
        resp = client.post(
            "/api/admin/cache/import",
            files={"file": ("envelope.json", io.BytesIO(raw), "application/json")},
            data={"mode": "merge", "tiers": "routing", "re_embed": "true"},
        )
    assert resp.status_code == 200, resp.text
    patched.assert_called_once()
    assert patched.call_args.kwargs["re_embed"] is True


# ---------------------------------------------------------------------------
# 0.21.0 rename alias contract (action / response)
# ---------------------------------------------------------------------------


def test_export_envelope_emits_format_version_3_and_action_tier():
    cm = _make_export_cache_manager_with_action()
    payload = b"".join(iter_export_chunks(cm, ["routing", "response"], app_version="0.21.0"))
    envelope = json.loads(payload)
    assert envelope["format_version"] == 3
    assert SUPPORTED_FORMAT_VERSION == 3
    assert "action" in envelope["tiers"]
    assert "response" not in envelope["tiers"]
    assert envelope["tiers"]["action"]["schema_version"] == 3


@pytest.mark.asyncio
async def test_export_v3_round_trip_preserves_structured_key():
    export_cm = _make_export_cache_manager_with_action()
    payload = b"".join(iter_export_chunks(export_cm, ["response"], app_version="0.21.0"))
    envelope = parse_envelope(payload)

    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    import_cm = _make_cache_manager(store)

    summary = await import_envelope(import_cm, envelope, mode="merge", tiers=["action"], re_embed=False)

    assert summary.tiers["action"].imported == 1
    action_meta = store.upsert.call_args.kwargs["metadatas"][0]
    exported_meta = envelope["tiers"]["action"]["entries"][0]["metadata"]
    assert action_meta["structured_key"] == exported_meta["structured_key"]


@pytest.mark.asyncio
async def test_import_v2_envelope_routes_through_legacy_purge():
    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)
    legacy_entry = _make_response_entry("legacy-action")
    legacy_entry["metadata"] = dict(legacy_entry["metadata"])
    legacy_entry["metadata"].pop("structured_key", None)

    envelope = _make_envelope(
        response_entries=[legacy_entry],
        format_version=2,
        action_schema_version=2,
    )

    summary = await import_envelope(cm, envelope, mode="merge", tiers=["action"], re_embed=False)

    assert summary.tiers["action"].imported == 1
    assert summary.tiers["action"].re_embedded == 1
    assert any("startup purge" in warning for warning in summary.tiers["action"].warnings)
    assert store.upsert.call_args.kwargs["metadatas"][0]["schema_version"] == "2"


@pytest.mark.asyncio
async def test_import_v3_envelope_rejects_entry_without_structured_key():
    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)
    invalid_entry = _make_response_entry("invalid-action")
    invalid_entry["metadata"] = dict(invalid_entry["metadata"])
    invalid_entry["metadata"].pop("structured_key", None)

    envelope = _make_envelope(
        response_entries=[invalid_entry],
        format_version=3,
        action_schema_version=3,
    )

    summary = await import_envelope(cm, envelope, mode="merge", tiers=["action"], re_embed=False)

    assert summary.tiers["action"].imported == 0
    assert summary.tiers["action"].skipped == 1
    assert any("structured_key" in warning for warning in summary.tiers["action"].warnings)


@pytest.mark.asyncio
async def test_parse_envelope_v1_response_alias_round_trips_to_action():
    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)
    envelope = _make_envelope(response_entries=[_make_response_entry("alias-1")])
    assert envelope["format_version"] == 1
    assert "response" in envelope["tiers"]
    raw = json.dumps(envelope).encode("utf-8")
    parsed = parse_envelope(raw)
    assert "action" in parsed["tiers"]
    assert "response" not in parsed["tiers"]
    summary = await import_envelope(cm, parsed, mode="merge", tiers=["action"], re_embed=False)
    assert summary.tiers["action"].imported == 1
    cm._response_cache.prepare_for_flush.assert_called()


def test_api_export_accepts_legacy_tier_response():
    from fastapi.testclient import TestClient

    cm = _make_export_cache_manager_with_action()
    app = _build_cache_api_app(cm)
    client = TestClient(app)
    resp_legacy = client.get("/api/admin/cache/export?tier=response")
    assert resp_legacy.status_code == 200
    resp_canonical = client.get("/api/admin/cache/export?tier=action")
    assert resp_canonical.status_code == 200


def test_api_import_accepts_legacy_tiers_field_response():
    from fastapi.testclient import TestClient

    store = MagicMock(spec=VectorStore)
    store.get.return_value = {"ids": [], "embeddings": []}
    cm = _make_cache_manager(store)
    app = _build_cache_api_app(cm)
    client = TestClient(app)

    envelope = _make_envelope(response_entries=[_make_response_entry()])
    raw = json.dumps(envelope).encode("utf-8")
    resp = client.post(
        "/api/admin/cache/import",
        files={"file": ("envelope.json", io.BytesIO(raw), "application/json")},
        data={"mode": "merge", "tiers": "routing,response", "re_embed": "false"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "action" in body["tiers"]
    assert body["tiers"]["action"]["imported"] == 1


def test_api_flush_accepts_legacy_tier_response():
    from fastapi.testclient import TestClient

    store = MagicMock(spec=VectorStore)
    cm = _make_cache_manager(store)
    cm.flush = MagicMock()
    app = _build_cache_api_app(cm)
    client = TestClient(app)
    resp = client.post("/api/admin/cache/flush", json={"tier": "response"})
    assert resp.status_code == 200, resp.text
    cm.flush.assert_called_with("action")
'''
