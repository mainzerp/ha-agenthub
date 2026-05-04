"""Tests for the remote logs API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from tests.conftest import build_integration_test_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_log_record(
    name: str = "test.logger",
    level: int = logging.INFO,
    message: str = "Test message",
    module: str = "test_module",
    func_name: str = "test_func",
    lineno: int = 42,
    created: float | None = None,
) -> logging.LogRecord:
    if created is None:
        created = datetime.now(UTC).timestamp()
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=f"/app/{module}.py",
        lineno=lineno,
        msg=message,
        args=(),
        exc_info=None,
        func=func_name,
    )
    record.created = created
    return record


def _add_entries(log_buffer: Any) -> None:
    """Seed a log buffer with predictable test entries."""
    base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    entries = [
        ("app.core", logging.DEBUG, "debug entry", base_time),
        ("app.core", logging.INFO, "info entry one", base_time + timedelta(seconds=1)),
        ("app.api", logging.INFO, "info entry two", base_time + timedelta(seconds=2)),
        ("app.api", logging.WARNING, "warning entry", base_time + timedelta(seconds=3)),
        ("app.db", logging.ERROR, "error entry", base_time + timedelta(seconds=4)),
        ("app.db", logging.CRITICAL, "critical entry", base_time + timedelta(seconds=5)),
    ]
    for name, level, msg, ts in entries:
        log_buffer.add(_make_log_record(name=name, level=level, message=msg, created=ts.timestamp()))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def authed_client():
    from app.util.log_buffer import LogBuffer, get_log_buffer, set_log_buffer

    old_buffer = get_log_buffer()
    log_buffer = LogBuffer(capacity=100)
    set_log_buffer(log_buffer)

    _add_entries(log_buffer)

    app = build_integration_test_app(
        setup_complete=True,
        override_admin_session=True,
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


@pytest_asyncio.fixture()
async def unauthed_client():
    from app.util.log_buffer import LogBuffer, get_log_buffer, set_log_buffer

    old_buffer = get_log_buffer()
    log_buffer = LogBuffer(capacity=100)
    set_log_buffer(log_buffer)

    _add_entries(log_buffer)

    app = build_integration_test_app(
        setup_complete=True,
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_logs_returns_200(authed_client: httpx.AsyncClient) -> None:
    resp = await authed_client.get("/api/admin/logs")
    assert resp.status_code == 200
    data = resp.json()
    assert "entries" in data
    assert "total" in data
    assert data["total"] == 6
    assert len(data["entries"]) == 6


@pytest.mark.asyncio
async def test_filter_by_level(authed_client: httpx.AsyncClient) -> None:
    resp = await authed_client.get("/api/admin/logs?level=WARNING")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    for entry in data["entries"]:
        assert entry["level"] in ("WARNING", "ERROR", "CRITICAL")


@pytest.mark.asyncio
async def test_filter_by_logger_name(authed_client: httpx.AsyncClient) -> None:
    resp = await authed_client.get("/api/admin/logs?logger=app.api")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    for entry in data["entries"]:
        assert "app.api" in entry["name"]


@pytest.mark.asyncio
async def test_search_parameter(authed_client: httpx.AsyncClient) -> None:
    resp = await authed_client.get("/api/admin/logs?search=entry one")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["entries"][0]["message"] == "info entry one"


@pytest.mark.asyncio
async def test_pagination_limit_offset(authed_client: httpx.AsyncClient) -> None:
    resp = await authed_client.get("/api/admin/logs?limit=2&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["entries"]) == 2
    assert data["total"] == 6

    resp = await authed_client.get("/api/admin/logs?limit=2&offset=2")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["entries"]) == 2

    resp = await authed_client.get("/api/admin/logs?limit=10&offset=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["entries"]) == 0


@pytest.mark.asyncio
async def test_get_log_levels(authed_client: httpx.AsyncClient) -> None:
    resp = await authed_client.get("/api/admin/logs/levels")
    assert resp.status_code == 200
    data = resp.json()
    assert "root_level" in data
    assert "loggers" in data


@pytest.mark.asyncio
async def test_update_log_level(authed_client: httpx.AsyncClient) -> None:
    resp = await authed_client.post(
        "/api/admin/logs/levels",
        json={"logger_name": "app.test_logger", "level": "DEBUG"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["level"] == "DEBUG"

    logger = logging.getLogger("app.test_logger")
    assert logger.level == logging.DEBUG


@pytest.mark.asyncio
async def test_unauthorized_without_admin_session(unauthed_client: httpx.AsyncClient) -> None:
    resp = await unauthed_client.get("/api/admin/logs")
    assert resp.status_code == 401

    resp = await unauthed_client.get("/api/admin/logs/levels")
    assert resp.status_code == 401

    resp = await unauthed_client.post(
        "/api/admin/logs/levels",
        json={"logger_name": "app.test", "level": "DEBUG"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_max_limit_enforcement(authed_client: httpx.AsyncClient) -> None:
    resp = await authed_client.get("/api/admin/logs?limit=5000")
    # FastAPI query validation rejects limit > 1000 with 422
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_invalid_level_query_param(authed_client: httpx.AsyncClient) -> None:
    resp = await authed_client.get("/api/admin/logs?level=INVALID")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_invalid_level_post_body(authed_client: httpx.AsyncClient) -> None:
    resp = await authed_client.post(
        "/api/admin/logs/levels",
        json={"logger_name": "app.test", "level": "INVALID"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_since_filter(authed_client: httpx.AsyncClient) -> None:
    since = "2026-01-01T12:00:03%2B00:00"
    resp = await authed_client.get(f"/api/admin/logs?since={since}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    for entry in data["entries"]:
        assert entry["level"] in ("ERROR", "CRITICAL")
