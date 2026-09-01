"""Tests for the HA log-ingest API (POST /api/logs/ingest) and LogBuffer.add_entry."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest_asyncio

from tests.conftest import build_integration_test_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "timestamp": "2026-01-01T12:00:00.123456+00:00",
        "level": "INFO",
        "name": "custom_components.ha_agenthub.conversation",
        "message": "shipped message",
        "lineno": 380,
        "module": "conversation",
        "funcName": "_async_handle_message",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def ingest_client():
    """Client with API-key and admin-session overrides + a fresh log buffer."""
    from app.util.log_buffer import LogBuffer, get_log_buffer, set_log_buffer

    old_buffer = get_log_buffer()
    log_buffer = LogBuffer(capacity=100)
    set_log_buffer(log_buffer)

    app = build_integration_test_app(
        setup_complete=True,
        override_api_key=True,
        override_admin_session=True,
    )

    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=True,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, log_buffer

    set_log_buffer(old_buffer)


@pytest_asyncio.fixture()
async def unauthed_client(db_repository):
    """Client with NO auth overrides; real require_api_key runs against the temp DB."""
    from app.util.log_buffer import LogBuffer, get_log_buffer, set_log_buffer

    old_buffer = get_log_buffer()
    set_log_buffer(LogBuffer(capacity=100))

    app = build_integration_test_app(
        setup_complete=True,
        override_api_key=False,
        override_admin_session=False,
    )

    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=True,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client

    set_log_buffer(old_buffer)


# ---------------------------------------------------------------------------
# C1: LogBuffer.add_entry unit test
# ---------------------------------------------------------------------------


def test_add_entry_appends_verbatim() -> None:
    from app.util.log_buffer import LogBuffer

    buf = LogBuffer(capacity=10)
    entry = {
        "timestamp": "2026-01-01T12:00:00+00:00",
        "level": "ERROR",
        "name": "custom_components.ha_agenthub.conversation",
        "message": "shipped error",
        "lineno": 42,
        "module": "conversation",
        "funcName": "_async_handle_message",
        "source": "ha",
    }
    buf.add_entry(entry)

    result = buf.get_entries()
    assert result["total"] == 1
    assert result["entries"][0] == entry

    # Level filter treats the ingested entry like a local one (uppercase level).
    assert buf.get_entries(level="WARNING")["total"] == 1
    assert buf.get_entries(level="CRITICAL")["total"] == 0

    # Since filter parses the ISO timestamp without error.
    assert buf.get_entries(since="2026-01-01T11:00:00+00:00")["total"] == 1
    assert buf.get_entries(since="2026-01-01T13:00:00+00:00")["total"] == 0


# ---------------------------------------------------------------------------
# POST /api/logs/ingest
# ---------------------------------------------------------------------------


async def test_ingest_valid_batch(ingest_client) -> None:
    client, _buf = ingest_client
    records = [
        _record(trace_id="0123abcd5678ef01", conversation_id="01JABC"),
        _record(message="second message", lineno=381),
    ]
    resp = await client.post("/api/logs/ingest", json=records)
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 2}

    # Entries are retrievable via the admin logs API and marked as shipped.
    resp = await client.get("/api/admin/logs")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 2
    assert all(e["source"] == "ha" for e in entries)

    # Optional context keys are only present when provided.
    by_lineno = {e["lineno"]: e for e in entries}
    assert by_lineno[380]["trace_id"] == "0123abcd5678ef01"
    assert by_lineno[380]["conversation_id"] == "01JABC"
    assert "trace_id" not in by_lineno[381]
    assert "conversation_id" not in by_lineno[381]


async def test_ingest_requires_api_key(unauthed_client: httpx.AsyncClient) -> None:
    resp = await unauthed_client.post("/api/logs/ingest", json=[_record()])
    assert resp.status_code == 401

    resp = await unauthed_client.post(
        "/api/logs/ingest",
        json=[_record()],
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401


async def test_ingest_rejects_lowercase_or_unknown_level(ingest_client) -> None:
    client, _buf = ingest_client
    resp = await client.post("/api/logs/ingest", json=[_record(level="info")])
    assert resp.status_code == 422

    resp = await client.post("/api/logs/ingest", json=[_record(level="VERBOSE")])
    assert resp.status_code == 422


async def test_ingest_rejects_naive_timestamp(ingest_client) -> None:
    client, _buf = ingest_client
    resp = await client.post("/api/logs/ingest", json=[_record(timestamp="2026-01-01T12:00:00")])
    assert resp.status_code == 422


async def test_ingest_rejects_batch_over_100(ingest_client) -> None:
    client, _buf = ingest_client
    resp = await client.post("/api/logs/ingest", json=[_record() for _ in range(101)])
    assert resp.status_code == 422


async def test_ingest_rejects_oversized_body(ingest_client) -> None:
    client, _buf = ingest_client
    resp = await client.post(
        "/api/logs/ingest",
        content=b"[]",
        headers={"Content-Type": "application/json", "Content-Length": "300000"},
    )
    assert resp.status_code == 413


async def test_ingest_rejects_message_over_2000_chars(ingest_client) -> None:
    client, _buf = ingest_client
    resp = await client.post("/api/logs/ingest", json=[_record(message="x" * 2001)])
    assert resp.status_code == 422


async def test_ingest_returns_503_when_buffer_unavailable(ingest_client) -> None:
    client, _buf = ingest_client
    from app.util.log_buffer import get_log_buffer, set_log_buffer

    old_buffer = get_log_buffer()
    set_log_buffer(None)  # type: ignore[arg-type]
    try:
        resp = await client.post("/api/logs/ingest", json=[_record()])
        assert resp.status_code == 503
        assert resp.json()["detail"] == "log buffer unavailable"
    finally:
        set_log_buffer(old_buffer)


async def test_ingested_entry_passes_admin_level_filter(ingest_client) -> None:
    client, _buf = ingest_client
    resp = await client.post("/api/logs/ingest", json=[_record(level="ERROR")])
    assert resp.status_code == 200

    resp = await client.get("/api/admin/logs?level=WARNING")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["entries"][0]["level"] == "ERROR"
    assert data["entries"][0]["source"] == "ha"
