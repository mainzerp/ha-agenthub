"""Recently used agents: recency tracker, ``recent_agents`` and the activity endpoints."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.analytics import agent_recency
from app.analytics.agent_recency import (
    DISPATCH_SPAN_NAMES,
    EXCLUDED_AGENT_IDS,
    MAX_TRACKED_AGENTS,
    recent_agents,
    record_agent_use,
    reset_recency,
    tracked_agents,
)
from app.analytics.tracer import SpanCollector
from app.api.routes import sse
from app.api.routes.sse import ACTIVITY_TOPIC, publish_span_activity
from app.models.agent import AgentCard
from app.security.auth import require_admin_session
from tests.conftest import build_integration_test_app
from tests.scenarios.runner import _temp_db

API_KEY = "activity-test-key"
_AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture(autouse=True)
def _clean_state():
    reset_recency()
    sse._brokers.pop(ACTIVITY_TOPIC, None)
    sse._topic_loops.pop(ACTIVITY_TOPIC, None)
    yield
    reset_recency()
    sse._brokers.pop(ACTIVITY_TOPIC, None)
    sse._topic_loops.pop(ACTIVITY_TOPIC, None)


class _Registry:
    """Minimal stand-in for the A2A registry (only ``list_agents`` is used)."""

    def __init__(self, *agent_ids: str) -> None:
        self.cards = [
            AgentCard(agent_id=aid, name=aid.replace("-", " ").title(), description="test") for aid in agent_ids
        ]

    async def list_agents(self) -> list[AgentCard]:
        return list(self.cards)


@pytest_asyncio.fixture()
async def temp_db(tmp_path):
    """Real SQLite schema on a temporary file with every repository patched to it."""
    async with _temp_db(tmp_path / "activity.db"):
        yield


async def _insert_span(name: str, agent_id: str | None, start_time: str) -> None:
    from app.db.repository import TraceSpanRepository

    await TraceSpanRepository.insert(
        trace_id="t-" + start_time, span_name=name, start_time=start_time, duration_ms=1.0, agent_id=agent_id
    )


# ---------------------------------------------------------------------------
# Recency tracker
# ---------------------------------------------------------------------------


def test_tracker_orders_most_recent_first_and_moves_reused_agent_to_front():
    record_agent_use("dispatch", "light-agent", "2026-10-21T08:00:00+00:00")
    record_agent_use("dispatch", "music-agent", "2026-10-21T08:01:00+00:00")
    record_agent_use("dispatch_content", "climate-agent", "2026-10-21T08:02:00+00:00")
    record_agent_use("dispatch_fallback", "light-agent", "2026-10-21T08:03:00+00:00")

    assert tracked_agents() == [
        ("light-agent", "2026-10-21T08:03:00+00:00"),
        ("climate-agent", "2026-10-21T08:02:00+00:00"),
        ("music-agent", "2026-10-21T08:01:00+00:00"),
    ]


def test_tracker_is_bounded_and_drops_least_recent():
    for index in range(MAX_TRACKED_AGENTS + 16):
        record_agent_use("dispatch", f"agent-{index}")
    tracked = tracked_agents()
    assert len(tracked) == MAX_TRACKED_AGENTS
    assert tracked[0][0] == f"agent-{MAX_TRACKED_AGENTS + 15}"
    assert tracked[-1][0] == "agent-16"


@pytest.mark.parametrize("name", sorted(DISPATCH_SPAN_NAMES))
def test_tracker_records_every_dispatch_span_name(name):
    record_agent_use(name, "light-agent")
    assert [agent_id for agent_id, _ts in tracked_agents()] == ["light-agent"]


@pytest.mark.parametrize("name", ["classify", "classify.agents", "cache_lookup", "rewrite", "return", "llm_call", None])
def test_tracker_ignores_non_dispatch_spans(name):
    record_agent_use(name, "light-agent")
    assert tracked_agents() == []


@pytest.mark.parametrize("agent_id", [None, "", 42, b"light-agent", ["light-agent"], *sorted(EXCLUDED_AGENT_IDS)])
def test_tracker_ignores_bad_or_internal_agent_ids_without_raising(agent_id):
    record_agent_use("dispatch", agent_id)
    assert tracked_agents() == []


def test_tracker_defaults_timestamp_to_utc_now_on_bad_timestamp():
    record_agent_use("dispatch", "light-agent", 12345)
    ((_aid, ts),) = tracked_agents()
    assert ts.endswith("+00:00")


def test_tracker_never_raises_even_when_internals_fail():
    with patch.object(agent_recency, "_recent", None):
        record_agent_use("dispatch", "light-agent")


async def test_span_start_updates_tracker_without_subscribers():
    assert not sse.has_activity_subscribers()
    collector = SpanCollector("trace-recency", source="chat")
    async with collector.start_span("dispatch", agent_id="light-agent"):
        pass
    async with collector.start_span("classify", agent_id="orchestrator"):
        pass
    async with collector.start_span("llm_call", agent_id="music-agent"):
        pass
    assert [agent_id for agent_id, _ts in tracked_agents()] == ["light-agent"]


async def test_tracker_uses_span_start_time():
    collector = SpanCollector("trace-recency-ts")
    async with collector.start_span("dispatch", agent_id="light-agent"):
        pass
    ((_aid, ts),) = tracked_agents()
    assert ts == collector.get_spans()[0]["start_time"]


async def test_discarded_collector_does_not_track_agents():
    collector = SpanCollector("trace-recency-discard")
    collector.discard()
    async with collector.start_span("dispatch", agent_id="light-agent"):
        pass
    assert tracked_agents() == []


def test_span_end_does_not_record_and_bad_span_does_not_raise():
    publish_span_activity("span_end", {"span_name": "dispatch", "agent_id": "light-agent"}, "chat")
    assert tracked_agents() == []
    publish_span_activity("span_start", "not a dict", "chat")  # type: ignore[arg-type]
    assert tracked_agents() == []


# ---------------------------------------------------------------------------
# recent_agents
# ---------------------------------------------------------------------------


async def test_recent_agents_filters_unregistered_and_internal_ids_and_uses_card_names(temp_db):
    registry = _Registry("light-agent", "music-agent", "orchestrator", "rewrite-agent", "noise")
    record_agent_use("dispatch", "light-agent", "2026-10-21T08:00:00+00:00")
    record_agent_use("dispatch", "ghost-agent", "2026-10-21T08:00:01+00:00")
    record_agent_use("dispatch", "orchestrator", "2026-10-21T08:00:01+00:00")
    record_agent_use("dispatch", "music-agent", "2026-10-21T08:00:02+00:00")
    # Internal ids that bypass record_agent_use (e.g. tracker populated before an upgrade).
    agent_recency._recent["noise"] = "2026-10-21T09:00:00+00:00"
    agent_recency._recent["rewrite-agent"] = "2026-10-21T09:00:01+00:00"

    result = await recent_agents(7, registry=registry)

    assert result == [
        {"agent_id": "music-agent", "name": "Music Agent", "last_used": "2026-10-21T08:00:02+00:00"},
        {"agent_id": "light-agent", "name": "Light Agent", "last_used": "2026-10-21T08:00:00+00:00"},
    ]


async def test_recent_agents_honours_limit_and_clamps(temp_db):
    registry = _Registry(*(f"agent-{i}" for i in range(30)))
    for index in range(30):
        record_agent_use("dispatch", f"agent-{index}")

    five = await recent_agents(5, registry=registry)
    assert [a["agent_id"] for a in five] == [f"agent-{i}" for i in (29, 28, 27, 26, 25)]
    assert len(await recent_agents(1, registry=registry)) == 1
    assert await recent_agents(0, registry=registry) == []
    assert await recent_agents("nope", registry=registry) == []  # type: ignore[arg-type]
    assert len(await recent_agents(500, registry=registry)) == agent_recency.MAX_RECENT_LIMIT


async def test_recent_agents_empty_registry_returns_nothing(temp_db):
    record_agent_use("dispatch", "light-agent")
    assert await recent_agents(7, registry=_Registry()) == []


async def test_recent_agents_defaults_to_global_registry_when_none_given(temp_db):
    record_agent_use("dispatch", "light-agent")
    fake = _Registry("light-agent")
    with patch("app.a2a.registry.registry", fake):
        result = await recent_agents(3)
    assert [a["agent_id"] for a in result] == ["light-agent"]


async def test_recent_agents_tops_up_from_persisted_dispatch_spans(temp_db):
    registry = _Registry("light-agent", "music-agent", "climate-agent", "general-agent", "scene-agent")
    await _insert_span("dispatch", "light-agent", "2026-10-21T07:00:00+00:00")
    await _insert_span("dispatch", "light-agent", "2026-10-21T07:30:00+00:00")  # latest start wins
    await _insert_span("dispatch_content", "music-agent", "2026-10-21T07:10:00+00:00")
    await _insert_span("dispatch_fallback", "general-agent", "2026-10-21T06:00:00+00:00")
    await _insert_span("dispatch", "orchestrator", "2026-10-21T07:55:00+00:00")  # internal
    await _insert_span("dispatch", "ghost-agent", "2026-10-21T07:56:00+00:00")  # unregistered
    await _insert_span("classify", "climate-agent", "2026-10-21T07:57:00+00:00")  # not a dispatch span
    await _insert_span("dispatch", None, "2026-10-21T07:58:00+00:00")
    # Fresh in-memory use beats the persisted history and is not duplicated.
    record_agent_use("dispatch", "general-agent", "2026-10-22T00:00:00+00:00")

    result = await recent_agents(7, registry=registry)

    assert [(a["agent_id"], a["last_used"]) for a in result] == [
        ("general-agent", "2026-10-22T00:00:00+00:00"),
        ("light-agent", "2026-10-21T07:30:00+00:00"),
        ("music-agent", "2026-10-21T07:10:00+00:00"),
    ]


async def test_recent_agents_span_topup_ignores_rows_outside_the_window(temp_db):
    from app.db.repositories import trace as trace_repo

    registry = _Registry("light-agent", "music-agent")
    await _insert_span("dispatch", "light-agent", "2026-10-21T07:00:00+00:00")
    await _insert_span("dispatch", "music-agent", "2026-01-01T07:00:00+00:00")
    async with trace_repo.get_db_write() as db:
        await db.execute(
            "UPDATE trace_spans SET created_at = datetime('now', '-45 days') WHERE agent_id = 'music-agent'"
        )

    result = await recent_agents(7, registry=registry)

    assert [a["agent_id"] for a in result] == ["light-agent"]


async def test_recent_agents_falls_back_to_usage_counts_when_still_short(temp_db):
    from app.db.repository import AnalyticsRepository

    registry = _Registry("light-agent", "music-agent", "climate-agent", "general-agent")
    await _insert_span("dispatch", "light-agent", "2026-10-21T07:00:00+00:00")
    for aid, count in (("music-agent", 3), ("climate-agent", 1), ("light-agent", 9), ("orchestrator", 20)):
        for _ in range(count):
            await AnalyticsRepository.insert("request", aid)
    await AnalyticsRepository.insert("cache_miss", "general-agent")  # not a request event
    await AnalyticsRepository.insert("request", "ghost-agent")

    result = await recent_agents(7, registry=registry)

    assert [(a["agent_id"], a["last_used"]) for a in result] == [
        ("light-agent", "2026-10-21T07:00:00+00:00"),
        ("music-agent", None),
        ("climate-agent", None),
    ]


async def test_recent_agents_survives_database_failures():
    registry = _Registry("light-agent", "music-agent")
    record_agent_use("dispatch", "light-agent")
    with (
        patch("app.db.repository.TraceSpanRepository.recent_dispatch_agents", AsyncMock(side_effect=OSError("db"))),
        patch("app.db.repository.AnalyticsRepository.agent_usage_counts", AsyncMock(side_effect=OSError("db"))),
    ):
        result = await recent_agents(7, registry=registry)
    assert [a["agent_id"] for a in result] == ["light-agent"]


async def test_recent_agents_skips_database_when_memory_fills_the_limit():
    registry = _Registry("light-agent", "music-agent")
    record_agent_use("dispatch", "light-agent")
    record_agent_use("dispatch", "music-agent")
    spans = AsyncMock(return_value=[])
    usage = AsyncMock(return_value=[])
    with (
        patch("app.db.repository.TraceSpanRepository.recent_dispatch_agents", spans),
        patch("app.db.repository.AnalyticsRepository.agent_usage_counts", usage),
    ):
        result = await recent_agents(2, registry=registry)
    assert len(result) == 2
    spans.assert_not_called()
    usage.assert_not_called()


# ---------------------------------------------------------------------------
# Endpoints (full app wiring)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def app(temp_db):
    from app.security.encryption import store_secret

    await store_secret("container_api_key", API_KEY)
    application = build_integration_test_app(registry=_Registry("light-agent", "music-agent", "climate-agent"))
    with patch("app.db.repository.SetupStateRepository.is_complete", new_callable=AsyncMock, return_value=True):
        yield application


@pytest_asyncio.fixture()
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture()
def admin_session(app):
    app.dependency_overrides[require_admin_session] = lambda: {"username": "admin"}
    yield
    app.dependency_overrides.pop(require_admin_session, None)


async def _open_stream(app, path: str, headers: dict[str, str] | None = None):
    """Drive the ASGI app by hand (httpx buffers infinite streams); return (messages, disconnect, task)."""
    sent: asyncio.Queue = asyncio.Queue()
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        await sent.put(message)

    raw_headers = [(b"host", b"testserver")] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    return sent, disconnect, task


async def _close_stream(disconnect: asyncio.Event, task: asyncio.Task) -> None:
    disconnect.set()
    try:
        await asyncio.wait_for(task, timeout=3)
    except TimeoutError:
        task.cancel()
        raise


@pytest.mark.parametrize("path", ["/api/admin/activity/recent", "/api/activity/recent"])
async def test_recent_endpoints_reject_missing_credentials(client, path):
    assert (await client.get(path)).status_code == 401


async def test_api_recent_rejects_wrong_bearer_and_wrong_scheme(client):
    assert (await client.get("/api/activity/recent", headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await client.get("/api/activity/recent", headers={"Authorization": f"Basic {API_KEY}"})).status_code == 401


async def test_api_recent_does_not_accept_an_admin_session_alone(client, admin_session):
    assert (await client.get("/api/activity/recent")).status_code == 401


async def test_admin_recent_does_not_accept_api_key(client):
    assert (await client.get("/api/admin/activity/recent", headers=_AUTH)).status_code == 401


@pytest.mark.parametrize("path", ["/api/admin/activity/recent", "/api/activity/recent"])
async def test_recent_endpoints_return_agents_most_recent_first(client, app, admin_session, path):
    record_agent_use("dispatch", "light-agent", "2026-10-21T08:00:00+00:00")
    record_agent_use("dispatch", "ghost-agent", "2026-10-21T08:01:00+00:00")
    record_agent_use("dispatch_send", "climate-agent", "2026-10-21T08:02:00+00:00")

    response = await client.get(path, params={"limit": 2}, headers=_AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "agents": [
            {"agent_id": "climate-agent", "name": "Climate Agent", "last_used": "2026-10-21T08:02:00+00:00"},
            {"agent_id": "light-agent", "name": "Light Agent", "last_used": "2026-10-21T08:00:00+00:00"},
        ]
    }


@pytest.mark.parametrize("path", ["/api/admin/activity/recent", "/api/activity/recent"])
async def test_recent_endpoints_default_limit_is_seven(client, app, admin_session, path):
    app.state.registry = _Registry(*(f"agent-{i}" for i in range(12)))
    for index in range(12):
        record_agent_use("dispatch", f"agent-{index}")

    body = (await client.get(path, headers=_AUTH)).json()

    assert [a["agent_id"] for a in body["agents"]] == [f"agent-{i}" for i in range(11, 4, -1)]


@pytest.mark.parametrize("path", ["/api/admin/activity/recent", "/api/activity/recent"])
@pytest.mark.parametrize("limit", ["0", "21", "-1", "abc"])
async def test_recent_endpoints_validate_limit(client, admin_session, path, limit):
    response = await client.get(path, params={"limit": limit}, headers=_AUTH)
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/api/admin/activity/recent", "/api/activity/recent"])
async def test_recent_endpoints_accept_limit_bounds(client, admin_session, path):
    assert (await client.get(path, params={"limit": 1}, headers=_AUTH)).status_code == 200
    assert (await client.get(path, params={"limit": 20}, headers=_AUTH)).status_code == 200


async def test_recent_endpoints_leak_nothing_but_ids_names_and_timestamps(client):
    record_agent_use("dispatch", "light-agent")
    body = (await client.get("/api/activity/recent", headers=_AUTH)).json()
    assert set(body) == {"agents"}
    assert all(set(entry) == {"agent_id", "name", "last_used"} for entry in body["agents"])


def test_api_stream_requires_api_key_before_subscribing():
    bare = FastAPI()
    bare.include_router(sse.api_router)
    with TestClient(bare) as http_client:
        response = http_client.get("/api/activity/stream")
    assert response.status_code == 401
    assert not sse.has_activity_subscribers()


async def test_admin_stream_still_requires_admin_session(client):
    assert (await client.get("/api/admin/activity/stream", headers=_AUTH)).status_code == 401


@pytest.mark.parametrize("path", ["/api/activity/recent", "/api/activity/stream"])
async def test_api_activity_routes_share_the_admin_rate_limit(client, path):
    from app.middleware import rate_limit

    rate_limit._rate_limit_store["admin:127.0.0.1"] = (300, time.time())
    response = await client.get(path, headers=_AUTH)
    assert response.status_code == 429
    assert not sse.has_activity_subscribers()


async def test_api_stream_end_to_end_with_api_key(app):
    sent, disconnect, task = await _open_stream(app, "/api/activity/stream", _AUTH)

    start = await asyncio.wait_for(sent.get(), timeout=3)
    assert start["type"] == "http.response.start"
    assert start["status"] == 200
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["x-accel-buffering"] == "no"
    assert "no-cache" in headers["cache-control"]
    assert "x-trace-id" in headers

    for _ in range(50):
        if sse.has_activity_subscribers():
            break
        await asyncio.sleep(0.01)
    assert sse.has_activity_subscribers()

    collector = SpanCollector("trace-api-e2e", source="ha")
    async with collector.start_span("dispatch", agent_id="light-agent"):
        pass

    body = await asyncio.wait_for(sent.get(), timeout=3)
    frame = body["body"].decode()
    assert frame.startswith("event: activity\ndata: ")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["type"] == "span_start"
    assert payload["span_name"] == "dispatch"
    assert payload["agent_id"] == "light-agent"
    assert payload["source"] == "ha"

    await _close_stream(disconnect, task)
    assert not sse.has_activity_subscribers()
    assert [aid for aid, _ts in tracked_agents()] == ["light-agent"]


async def test_api_stream_with_wrong_key_returns_401_without_subscribing(app):
    sent, disconnect, task = await _open_stream(app, "/api/activity/stream", {"Authorization": "Bearer nope"})
    start = await asyncio.wait_for(sent.get(), timeout=3)
    assert start["status"] == 401
    await _close_stream(disconnect, task)
    assert not sse.has_activity_subscribers()


async def test_admin_stream_end_to_end_unchanged(app, admin_session):
    sent, disconnect, task = await _open_stream(app, "/api/admin/activity/stream")
    start = await asyncio.wait_for(sent.get(), timeout=3)
    assert start["status"] == 200
    for _ in range(50):
        if sse.has_activity_subscribers():
            break
        await asyncio.sleep(0.01)

    collector = SpanCollector("trace-admin-e2e", source="chat")
    async with collector.start_span("classify", agent_id="orchestrator"):
        pass

    body = await asyncio.wait_for(sent.get(), timeout=3)
    assert body["body"].decode().startswith("event: activity\ndata: ")
    await _close_stream(disconnect, task)
    assert not sse.has_activity_subscribers()


async def test_stream_handlers_share_the_same_response_builder():
    admin = await sse.activity_stream(request=MagicMock(), _session={"username": "admin"})
    api = await sse.api_activity_stream(request=MagicMock())
    for response in (admin, api):
        assert response.media_type == "text/event-stream"
        assert response.headers["x-accel-buffering"] == "no"
        assert "no-cache" in response.headers["cache-control"]
    assert len(sse._brokers[ACTIVITY_TOPIC]) == 2
