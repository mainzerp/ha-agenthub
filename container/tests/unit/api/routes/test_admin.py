"""Unit tests for admin GET routes.

Lightweight tests that assert 200 OK and verify response schema.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from tests.conftest import build_integration_test_app


def _build_app(**kwargs):
    """Build test app with admin session overridden."""
    return build_integration_test_app(
        setup_complete=True,
        override_api_key=True,
        override_admin_session=True,
        **kwargs,
    )


async def _client_for(app):
    """Return an httpx client with SetupState patched to complete."""
    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=True,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.mark.asyncio
class TestGetEntityMatchingWeights:
    async def test_get_entity_matching_weights_returns_200(self, db_repository):
        app = _build_app()
        with patch(
            "app.api.routes.admin.EntityMatchingConfigRepository.get_all",
            new_callable=AsyncMock,
            return_value=[
                {"key": "weight.levenshtein", "value": "0.3"},
                {"key": "weight.jaro_winkler", "value": "0.2"},
            ],
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/entity-matching-weights")

        assert resp.status_code == 200
        data = resp.json()
        assert "weights" in data
        assert data["weights"]["weight.levenshtein"] == "0.3"
        assert data["weights"]["weight.jaro_winkler"] == "0.2"


@pytest.mark.asyncio
class TestGetAlarmMonitorStatus:
    async def test_get_alarm_monitor_status_active(self, db_repository):
        app = _build_app()
        alarm_monitor = MagicMock()
        alarm_monitor.fired_today = ["alarm-1"]
        app.state.alarm_monitor = alarm_monitor

        async for client in _client_for(app):
            resp = await client.get("/api/admin/alarm-monitor")

        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert data["fired_today"] == ["alarm-1"]
        assert data["check_interval"] == 30

    async def test_get_alarm_monitor_status_inactive_when_none(self, db_repository):
        app = _build_app()
        app.state.alarm_monitor = None

        async for client in _client_for(app):
            resp = await client.get("/api/admin/alarm-monitor")

        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False
        assert data["fired_today"] == []
        assert data["check_interval"] == 30


@pytest.mark.asyncio
class TestGetRecentlyExpiredTimers:
    async def test_get_recently_expired_timers_returns_empty_list(self, db_repository):
        app = _build_app()

        async for client in _client_for(app):
            resp = await client.get("/api/admin/timers/recently-expired")

        assert resp.status_code == 200
        data = resp.json()
        assert "recently_expired" in data
        assert data["recently_expired"] == []
