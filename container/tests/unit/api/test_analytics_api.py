"""Unit tests for analytics_api routes.

Mocks AnalyticsRepository and ConversationRepository to test all analytics
endpoints without a real database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
class TestAnalyticsOverview:
    async def test_analytics_overview(self, db_repository):
        app = _build_app()

        request_events = [
            {"event_type": "request", "created_at": "2024-01-01T12:00:00", "data": {"latency_ms": 120}},
            {"event_type": "request", "created_at": "2024-01-01T12:01:00", "data": {"latency_ms": 80}},
        ]
        cache_events = [
            {"event_type": "routing_hit", "created_at": "2024-01-01T12:00:00", "data": {}},
            {"event_type": "miss", "created_at": "2024-01-01T12:01:00", "data": {}},
        ]

        async def _fake_query_by_range(*, event_type=None, start=None, limit=None):
            if event_type == "request":
                return request_events
            return cache_events

        with (
            patch(
                "app.api.routes.analytics_api.AnalyticsRepository.query_by_range",
                new_callable=AsyncMock,
                side_effect=_fake_query_by_range,
            ),
            patch(
                "app.api.routes.analytics_api.ConversationRepository.count",
                new_callable=AsyncMock,
                return_value=5,
            ),
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/analytics/overview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requests"] == 2
        assert data["avg_latency_ms"] == 100.0
        assert data["cache_hit_rate"] == 50.0
        assert data["total_conversations"] == 5
        assert "p50" in data
        assert "p95" in data
        assert "p99" in data


@pytest.mark.asyncio
class TestAnalyticsRequests:
    async def test_analytics_requests(self, db_repository):
        app = _build_app()

        events = [
            {"event_type": "request", "created_at": "2024-01-01T12:00:00", "data": {"latency_ms": 100}},
            {"event_type": "request", "created_at": "2024-01-01T12:00:00", "data": {"latency_ms": 200}},
        ]

        with patch(
            "app.api.routes.analytics_api.AnalyticsRepository.query_by_range",
            new_callable=AsyncMock,
            return_value=events,
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/analytics/requests?bucket_minutes=60")

        assert resp.status_code == 200
        data = resp.json()
        assert "labels" in data
        assert "datasets" in data
        assert data["datasets"][0]["label"] == "Requests"
        assert len(data["datasets"][0]["data"]) == len(data["labels"])


@pytest.mark.asyncio
class TestAnalyticsAgents:
    async def test_analytics_agents(self, db_repository):
        app = _build_app()

        events = [
            {
                "event_type": "request",
                "created_at": "2024-01-01T12:00:00",
                "agent_id": "light-agent",
                "data": {"latency_ms": 100},
            },
            {
                "event_type": "request",
                "created_at": "2024-01-01T12:01:00",
                "agent_id": "light-agent",
                "data": {"latency_ms": 200},
            },
            {
                "event_type": "request",
                "created_at": "2024-01-01T12:02:00",
                "agent_id": "climate-agent",
                "data": {"latency_ms": 50},
            },
        ]

        with patch(
            "app.api.routes.analytics_api.AnalyticsRepository.query_by_range",
            new_callable=AsyncMock,
            return_value=events,
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/analytics/agents")

        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert len(data["agents"]) == 2
        agent_ids = {a["agent_id"] for a in data["agents"]}
        assert agent_ids == {"climate-agent", "light-agent"}
        light = next(a for a in data["agents"] if a["agent_id"] == "light-agent")
        assert light["request_count"] == 2
        assert light["avg_latency_ms"] == 150.0
        assert "p50" in light
        assert "p95" in light


@pytest.mark.asyncio
class TestAnalyticsCacheAndTiers:
    async def test_analytics_cache_and_cache_tiers(self, db_repository):
        app = _build_app()

        events = [
            {"event_type": "routing_hit", "created_at": "2024-01-01T12:00:00", "data": {}},
            {"event_type": "action_hit", "created_at": "2024-01-01T12:00:00", "data": {}},
            {"event_type": "miss", "created_at": "2024-01-01T12:01:00", "data": {}},
        ]

        with patch(
            "app.api.routes.analytics_api.AnalyticsRepository.query_by_range",
            new_callable=AsyncMock,
            return_value=events,
        ):
            async for client in _client_for(app):
                resp_cache = await client.get("/api/admin/analytics/cache?bucket_minutes=60")
                resp_tiers = await client.get("/api/admin/analytics/cache/tiers?bucket_minutes=60")

        assert resp_cache.status_code == 200
        data_cache = resp_cache.json()
        assert "labels" in data_cache
        assert "datasets" in data_cache
        assert data_cache["datasets"][0]["label"] == "Cache Hit Rate (%)"

        assert resp_tiers.status_code == 200
        data_tiers = resp_tiers.json()
        assert "labels" in data_tiers
        assert len(data_tiers["datasets"]) == 3
        labels = {d["label"] for d in data_tiers["datasets"]}
        assert labels == {"Routing Hits", "Action Hits", "Misses"}


@pytest.mark.asyncio
class TestAnalyticsTokens:
    async def test_analytics_tokens(self, db_repository):
        app = _build_app()

        events = [
            {
                "event_type": "token_usage",
                "created_at": "2024-01-01T12:00:00",
                "agent_id": "light-agent",
                "data": {
                    "provider": "openrouter",
                    "tokens_in": 10,
                    "tokens_out": 20,
                    "ttft_ms": 150,
                    "tps": 25.5,
                },
            },
            {
                "event_type": "token_usage",
                "created_at": "2024-01-01T12:01:00",
                "agent_id": "light-agent",
                "data": {
                    "provider": "openrouter",
                    "tokens_in": 5,
                    "tokens_out": 15,
                    "ttft_ms": 200,
                    "tps": 30.0,
                },
            },
        ]

        with patch(
            "app.api.routes.analytics_api.AnalyticsRepository.query_by_range",
            new_callable=AsyncMock,
            return_value=events,
        ):
            async for client in _client_for(app):
                resp = await client.get("/api/admin/analytics/tokens")

        assert resp.status_code == 200
        data = resp.json()
        assert "by_agent" in data
        assert "by_provider" in data
        light = data["by_agent"]["light-agent"]
        assert light["tokens_in"] == 15
        assert light["tokens_out"] == 35
        assert light["calls"] == 2
        assert light["avg_ttft_ms"] == 175.0
        assert light["avg_tps"] == 27.75
        provider = data["by_provider"]["openrouter"]
        assert provider["calls"] == 2


@pytest.mark.asyncio
class TestAnalyticsErrorsAndRewrite:
    async def test_analytics_errors_and_rewrite(self, db_repository):
        app = _build_app()

        error_events = [
            {
                "event_type": "error",
                "created_at": "2024-01-01T12:00:00",
                "agent_id": "light-agent",
                "data": {"error_type": "timeout"},
            },
            {
                "event_type": "error",
                "created_at": "2024-01-01T12:01:00",
                "agent_id": "light-agent",
                "data": {"error_type": "timeout"},
            },
            {
                "event_type": "error",
                "created_at": "2024-01-01T12:02:00",
                "agent_id": "climate-agent",
                "data": {"error_type": "connection"},
            },
        ]

        rewrite_events = [
            {
                "event_type": "rewrite_invocation",
                "created_at": "2024-01-01T12:00:00",
                "data": {"success": True, "latency_ms": 100},
            },
            {
                "event_type": "rewrite_invocation",
                "created_at": "2024-01-01T12:01:00",
                "data": {"success": False, "latency_ms": 200},
            },
        ]

        async def _fake_query_by_range(*, event_type=None, start=None, limit=None):
            if event_type == "error":
                return error_events
            if event_type == "rewrite_invocation":
                return rewrite_events
            return []

        with patch(
            "app.api.routes.analytics_api.AnalyticsRepository.query_by_range",
            new_callable=AsyncMock,
            side_effect=_fake_query_by_range,
        ):
            async for client in _client_for(app):
                resp_errors = await client.get("/api/admin/analytics/errors")
                resp_rewrite = await client.get("/api/admin/analytics/rewrite")

        assert resp_errors.status_code == 200
        data_errors = resp_errors.json()
        assert "labels" in data_errors
        assert "datasets" in data_errors
        assert data_errors["by_agent"]["light-agent"] == 2
        assert data_errors["by_agent"]["climate-agent"] == 1

        assert resp_rewrite.status_code == 200
        data_rewrite = resp_rewrite.json()
        assert data_rewrite["total"] == 2
        assert data_rewrite["successes"] == 1
        assert data_rewrite["failures"] == 1
        assert data_rewrite["avg_latency_ms"] == 150.0
