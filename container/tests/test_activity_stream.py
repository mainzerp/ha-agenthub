"""Live agent activity stream: span hook, privacy allowlist and SSE endpoint."""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.analytics.tracer import SpanCollector
from app.api.routes import sse
from app.api.routes.sse import (
    ACTIVITY_SPAN_NAMES,
    ACTIVITY_TOPIC,
    build_activity_event,
    has_activity_subscribers,
    publish_activity,
    publish_span_activity,
)

_EVENT_KEYS = {"type", "span_id", "trace_id", "span_name", "agent_id", "ts", "status", "duration_ms", "source"}


@pytest.fixture(autouse=True)
def _clean_broker():
    sse._brokers.pop(ACTIVITY_TOPIC, None)
    sse._topic_loops.pop(ACTIVITY_TOPIC, None)
    yield
    sse._brokers.pop(ACTIVITY_TOPIC, None)
    sse._topic_loops.pop(ACTIVITY_TOPIC, None)


def _drain(queue: asyncio.Queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(json.loads(queue.get_nowait()))
    return events


async def test_allowlisted_span_publishes_start_and_end_payloads():
    queue = await sse._subscribe(ACTIVITY_TOPIC)
    collector = SpanCollector("trace-activity-1", source="chat")

    async with collector.start_span("dispatch", agent_id="light-agent") as span:
        span["metadata"]["latency_ms"] = 12.0

    start, end = _drain(queue)
    assert set(start) == _EVENT_KEYS
    assert set(end) == _EVENT_KEYS
    stored = collector.get_spans()[0]

    assert start == {
        "type": "span_start",
        "span_id": stored["span_id"],
        "trace_id": "trace-activity-1",
        "span_name": "dispatch",
        "agent_id": "light-agent",
        "ts": stored["start_time"],
        "status": None,
        "duration_ms": None,
        "source": "chat",
    }
    assert end["type"] == "span_end"
    assert end["span_id"] == stored["span_id"]
    assert end["status"] == "ok"
    assert end["duration_ms"] == stored["duration_ms"]
    assert end["ts"] == stored["end_time"]
    assert end["source"] == "chat"


async def test_error_span_reports_error_status():
    queue = await sse._subscribe(ACTIVITY_TOPIC)
    collector = SpanCollector("trace-activity-err", source="ha")

    with pytest.raises(ValueError):
        async with collector.start_span("dispatch_fallback", agent_id="general-agent"):
            raise ValueError("boom detail that must not leak")

    _start, end = _drain(queue)
    assert end["status"] == "error"
    assert end["span_name"] == "dispatch_fallback"


@pytest.mark.parametrize("name", [*sorted(ACTIVITY_SPAN_NAMES), "classify.agents", "classify.cache_lookup"])
async def test_every_allowlisted_name_is_published(name):
    queue = await sse._subscribe(ACTIVITY_TOPIC)
    collector = SpanCollector("trace-allow")
    async with collector.start_span(name, agent_id="orchestrator"):
        pass
    events = _drain(queue)
    assert [e["type"] for e in events] == ["span_start", "span_end"]
    assert all(e["span_name"] == name for e in events)


@pytest.mark.parametrize("name", ["llm_call", "ha_action", "entity_match", "mediation", "classifier", "classifyx"])
async def test_non_allowlisted_span_publishes_nothing(name):
    queue = await sse._subscribe(ACTIVITY_TOPIC)
    collector = SpanCollector("trace-deny")
    async with collector.start_span(name, agent_id="light-agent"):
        pass
    assert queue.empty()


async def test_no_metadata_or_content_leaks_into_events():
    queue = await sse._subscribe(ACTIVITY_TOPIC)
    collector = SpanCollector("trace-privacy", source="api")

    async with collector.start_span("dispatch", agent_id="light-agent") as span:
        span["metadata"]["condensed_task"] = "turn on the secret bedroom lamp"
        span["metadata"]["agent_response"] = "Done, lamp is on"
        span["metadata"]["entity_id"] = "light.secret_lamp"

    events = _drain(queue)
    assert len(events) == 2
    raw = json.dumps(events)
    for secret in ("secret", "condensed_task", "agent_response", "light.secret_lamp", "Done, lamp"):
        assert secret not in raw
    assert all(set(e) == _EVENT_KEYS for e in events)


def test_build_activity_event_ignores_unknown_event_type_and_foreign_fields():
    span = {
        "span_id": "abc",
        "trace_id": "t",
        "span_name": "return",
        "agent_id": "orchestrator",
        "start_time": "2026-01-01T00:00:00+00:00",
        "metadata": {"user_text": "hello"},
        "user_text": "hello",
    }
    assert build_activity_event("span_progress", span, "chat") is None
    event = build_activity_event("span_start", span, None)
    assert event is not None
    assert event["source"] is None
    assert set(event) == _EVENT_KEYS


async def test_no_subscribers_builds_no_payload():
    assert not has_activity_subscribers()
    collector = SpanCollector("trace-nosub")
    with patch.object(sse, "build_activity_event") as build:
        async with collector.start_span("dispatch", agent_id="light-agent"):
            pass
    build.assert_not_called()
    assert len(collector.get_spans()) == 1


async def test_unsubscribe_stops_payload_building():
    queue = await sse._subscribe(ACTIVITY_TOPIC)
    assert has_activity_subscribers()
    sse._unsubscribe(ACTIVITY_TOPIC, queue)
    assert not has_activity_subscribers()


async def test_publish_failure_does_not_break_span():
    await sse._subscribe(ACTIVITY_TOPIC)
    collector = SpanCollector("trace-fail")
    with patch.object(sse, "publish_activity", side_effect=RuntimeError("queue exploded")):
        async with collector.start_span("dispatch", agent_id="light-agent") as span:
            span["metadata"]["ok"] = True
    spans = collector.get_spans()
    assert len(spans) == 1
    assert spans[0]["status"] == "ok"


async def test_import_failure_in_hook_does_not_break_span():
    collector = SpanCollector("trace-import-fail")
    with patch.dict(sys.modules, {"app.api.routes.sse": None}):
        async with collector.start_span("dispatch"):
            pass
    assert len(collector.get_spans()) == 1


def test_publish_span_activity_swallows_errors():
    with patch.object(sse, "has_activity_subscribers", side_effect=RuntimeError("x")):
        publish_span_activity("span_start", {"span_name": "dispatch"}, "chat")


async def test_discarded_collector_publishes_nothing():
    queue = await sse._subscribe(ACTIVITY_TOPIC)
    collector = SpanCollector("trace-discarded")
    collector.discard()
    async with collector.start_span("dispatch", agent_id="light-agent"):
        pass
    assert queue.empty()


async def test_full_queue_drops_oldest_event():
    queue = await sse._subscribe(ACTIVITY_TOPIC)
    for index in range(40):
        publish_activity({"type": "span_start", "n": index})
    messages = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
    assert len(messages) == 32
    assert messages[0]["n"] == 8
    assert messages[-1]["n"] == 39


async def test_publish_from_other_thread_is_delivered_on_loop():
    queue = await sse._subscribe(ACTIVITY_TOPIC)
    collector = SpanCollector("trace-thread", source="ai_task")

    def _run_in_thread() -> None:
        asyncio.run(_traced(collector))

    async def _traced(col: SpanCollector) -> None:
        async with col.start_span("rewrite", agent_id="rewrite-agent"):
            pass

    await asyncio.to_thread(_run_in_thread)
    await asyncio.sleep(0.05)
    events = _drain(queue)
    assert [e["type"] for e in events] == ["span_start", "span_end"]
    assert events[0]["span_name"] == "rewrite"


async def test_endpoint_streams_activity_frame_end_to_end():
    response = await sse.activity_stream(request=MagicMock(), _session={"username": "admin"})
    assert response.media_type == "text/event-stream"
    assert response.headers["x-accel-buffering"] == "no"
    assert "no-cache" in response.headers["cache-control"]
    assert has_activity_subscribers()

    collector = SpanCollector("trace-e2e", source="chat")
    async with collector.start_span("classify", agent_id="orchestrator"):
        pass

    iterator = response.body_iterator
    frame = await asyncio.wait_for(iterator.__anext__(), timeout=2)
    assert frame.startswith("event: activity\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["type"] == "span_start"
    assert payload["span_name"] == "classify"
    assert payload["trace_id"] == "trace-e2e"
    assert payload["source"] == "chat"
    await iterator.__anext__()  # span_end

    await iterator.aclose()
    assert not has_activity_subscribers()


def test_endpoint_requires_admin_session():
    app = FastAPI()
    app.include_router(sse.router)
    with TestClient(app) as client:
        response = client.get("/api/admin/activity/stream")
    assert response.status_code == 401
