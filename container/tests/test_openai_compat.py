"""Tests for the OpenAI-compatible API (Open WebUI ingress) and external user mappings."""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import httpx
import pytest

from app.api.routes import openai_compat
from app.db.repository import ExternalUserMappingRepository
from tests.conftest import build_integration_test_app

pytestmark = pytest.mark.integration

_PERSON_STATES = [
    {
        "entity_id": "person.alice",
        "state": "home",
        "attributes": {"friendly_name": "Alice", "user_id": "ha-user-alice"},
    },
    {"entity_id": "person.guest", "state": "home", "attributes": {"friendly_name": "Guest"}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
]


def _make_dispatcher(response=None, frames=None) -> MagicMock:
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value=response if response is not None else {"speech": "Done."})
    dispatcher.stream_requests = []

    async def _stream(req):
        dispatcher.stream_requests.append(req)
        for frame in frames or [{"token": "", "done": True}]:
            yield frame

    dispatcher.dispatch_stream = _stream
    return dispatcher


class _Client:
    """Async context manager yielding an httpx client bound to a test app."""

    def __init__(self, *, dispatcher=None, override_api_key=True, ha_client=None) -> None:
        self.dispatcher = dispatcher if dispatcher is not None else _make_dispatcher()
        self.override_api_key = override_api_key
        self.ha_client = ha_client
        self._patch = None
        self._client = None
        self._prev_dispatcher = None

    async def __aenter__(self) -> httpx.AsyncClient:
        self._prev_dispatcher = openai_compat._dispatcher
        openai_compat.set_dispatcher(self.dispatcher)
        app = build_integration_test_app(
            setup_complete=True,
            override_api_key=self.override_api_key,
            override_admin_session=True,
            dispatcher=self.dispatcher,
            ha_client=self.ha_client,
        )
        self._patch = patch(
            "app.db.repository.SetupStateRepository.is_complete",
            new_callable=AsyncMock,
            return_value=True,
        )
        self._patch.start()
        self._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        return self._client

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()
        self._patch.stop()
        openai_compat.set_dispatcher(self._prev_dispatcher)


def _sent_task(dispatcher: MagicMock):
    """Return the IngressTask of the single non-streaming dispatch."""
    dispatcher.dispatch.assert_awaited_once()
    request = dispatcher.dispatch.await_args.args[0]
    return request.params["task"]


def _parse_sse(body: str) -> list:
    events = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


def _streamed_content(events: list) -> str:
    return "".join(e["choices"][0]["delta"].get("content", "") for e in events if isinstance(e, dict))


def _assert_valid_chunk_sequence(events: list) -> None:
    assert events[-1] == "[DONE]"
    chunks = events[:-1]
    assert chunks, "expected at least one chunk"
    ids = {c["id"] for c in chunks}
    assert len(ids) == 1 and next(iter(ids)).startswith("chatcmpl-")
    for chunk in chunks:
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["model"] == "ha-agenthub"
        assert isinstance(chunk["created"], int)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert all("role" not in c["choices"][0]["delta"] for c in chunks[1:])
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert all(c["choices"][0]["finish_reason"] is None for c in chunks[:-1])


def _chat_body(text, *, stream=False, history=None) -> dict:
    messages = [{"role": "system", "content": "You are helpful."}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": text})
    return {"model": "ha-agenthub", "messages": messages, "stream": stream}


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------


class TestModelsEndpoint:
    async def test_models_requires_api_key(self, db_repository):
        async with _Client(override_api_key=False) as client:
            resp = await client.get("/v1/models")
        assert resp.status_code == 401

    async def test_chat_completions_requires_api_key(self, db_repository):
        async with _Client(override_api_key=False) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("hi"))
        assert resp.status_code == 401

    async def test_models_shape(self, db_repository):
        async with _Client() as client:
            resp = await client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        assert len(data["data"]) == 1
        model = data["data"][0]
        assert model["id"] == "ha-agenthub"
        assert model["object"] == "model"
        assert model["owned_by"] == "ha-agenthub"
        assert isinstance(model["created"], int)


# ---------------------------------------------------------------------------
# Non-streaming completions
# ---------------------------------------------------------------------------


class TestNonStreamingCompletion:
    async def test_dispatches_last_user_message_to_orchestrator(self, db_repository):
        dispatcher = _make_dispatcher({"speech": "The kitchen light is on."})
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json=_chat_body("turn on the kitchen light", history=history),
                headers={"X-OpenWebUI-Chat-Id": "chat-123"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == "ha-agenthub"
        assert data["id"].startswith("chatcmpl-")
        assert data["choices"][0]["message"] == {"role": "assistant", "content": "The kitchen light is on."}
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        request = dispatcher.dispatch.await_args.args[0]
        assert request.method == "message/send"
        assert request.params["agent_id"] == "orchestrator"
        task = _sent_task(dispatcher)
        assert task.description == "turn on the kitchen light"
        assert task.conversation_id == "owui-chat-123"
        assert task.context.source == "openai"
        assert task.context.user_id is None
        # Seeded ``language`` setting (same resolution as the dashboard chat).
        assert task.context.language == "auto"

    @pytest.mark.parametrize("stream", [False, True])
    async def test_language_setting_reaches_a2a_request(self, db_repository, stream):
        from app.db.repository import SettingsRepository

        await SettingsRepository.set("language", "de")
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("mach das Licht an", stream=stream))
        assert resp.status_code == 200
        request = dispatcher.stream_requests[0] if stream else dispatcher.dispatch.await_args.args[0]
        assert request.params["task"].context.language == "de"

    async def test_list_content_parts_are_concatenated(self, db_repository):
        dispatcher = _make_dispatcher()
        body = {
            "model": "ha-agenthub",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "turn on"},
                        {"type": "image_url", "image_url": {"url": "data:,"}},
                        {"type": "text", "text": "the lamp"},
                    ],
                }
            ],
        }
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post("/v1/chat/completions", json=body)
        assert resp.status_code == 200
        assert _sent_task(dispatcher).description == "turn on\nthe lamp"

    async def test_conversation_id_chat_id_is_truncated(self, db_repository):
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            await client.post(
                "/v1/chat/completions",
                json=_chat_body("hi"),
                headers={"X-OpenWebUI-Chat-Id": "x" * 200},
            )
        conversation_id = _sent_task(dispatcher).conversation_id
        assert conversation_id.startswith("owui-")
        assert len(conversation_id) == 64

    async def test_conversation_id_fallback_hash(self, db_repository):
        dispatcher = _make_dispatcher()
        history = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "answer"},
        ]
        async with _Client(dispatcher=dispatcher) as client:
            await client.post(
                "/v1/chat/completions",
                json=_chat_body("second question", history=history),
                headers={"X-OpenWebUI-User-Id": "owui-user-1"},
            )
        expected = "owui-" + hashlib.sha256(b"owui-user-1\nfirst question").hexdigest()[:32]
        assert _sent_task(dispatcher).conversation_id == expected

    async def test_conversation_id_fallback_hash_anonymous(self, db_repository):
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            await client.post("/v1/chat/completions", json=_chat_body("only question"))
        expected = "owui-" + hashlib.sha256(b"anon\nonly question").hexdigest()[:32]
        assert _sent_task(dispatcher).conversation_id == expected

    async def test_unmapped_user_is_recorded_and_user_id_none(self, db_repository):
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            await client.post(
                "/v1/chat/completions",
                json=_chat_body("hi"),
                headers={
                    "X-OpenWebUI-User-Id": "u-1",
                    "X-OpenWebUI-User-Name": "J%C3%BCrgen",
                    "X-OpenWebUI-User-Email": "j@example.com",
                },
            )
        assert _sent_task(dispatcher).context.user_id is None
        row = await ExternalUserMappingRepository.get("openwebui", "u-1")
        assert row is not None
        assert row["display_name"] == "Jürgen"
        assert row["email"] == "j@example.com"
        assert row["ha_user_id"] is None

    async def test_mapped_user_passes_ha_user_id(self, db_repository):
        await ExternalUserMappingRepository.touch("openwebui", "u-2", "Alice", None)
        await ExternalUserMappingRepository.set_mapping("openwebui", "u-2", "ha-user-alice")
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            await client.post(
                "/v1/chat/completions",
                json=_chat_body("hi"),
                headers={"X-OpenWebUI-User-Id": "u-2"},
            )
        assert _sent_task(dispatcher).context.user_id == "ha-user-alice"

    async def test_repository_error_does_not_fail_request(self, db_repository):
        dispatcher = _make_dispatcher()
        with patch.object(ExternalUserMappingRepository, "touch", AsyncMock(side_effect=RuntimeError("db down"))):
            async with _Client(dispatcher=dispatcher) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json=_chat_body("hi"),
                    headers={"X-OpenWebUI-User-Id": "u-3"},
                )
        assert resp.status_code == 200
        assert _sent_task(dispatcher).context.user_id is None

    async def test_text_too_long_returns_400(self, db_repository):
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("x" * 501))
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert error["type"] == "invalid_request_error"
        assert error["code"] == "text_too_long"
        dispatcher.dispatch.assert_not_awaited()

    async def test_empty_message_returns_400(self, db_repository):
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "ha-agenthub", "messages": [{"role": "system", "content": "sys"}]},
            )
        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "invalid_request_error"
        dispatcher.dispatch.assert_not_awaited()

    async def test_surrounding_whitespace_is_stripped(self, db_repository):
        dispatcher = _make_dispatcher({"speech": "\n\nHello there.\n"})
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("hi"))
        assert resp.json()["choices"][0]["message"]["content"] == "Hello there."

    async def test_error_without_speech_is_reported(self, db_repository):
        dispatcher = _make_dispatcher({"speech": "", "error": "agent timeout"})
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("hi"))
        content = resp.json()["choices"][0]["message"]["content"]
        assert "agent timeout" in content


# ---------------------------------------------------------------------------
# Streaming completions
# ---------------------------------------------------------------------------


class TestStreamingCompletion:
    async def test_tokens_stream_as_chunks_and_filler_is_skipped(self, db_repository):
        frames = [
            {"token": "", "done": False, "filler_push": "One moment."},
            {"token": "", "done": False, "status": "dispatching", "agents": ["light-agent"]},
            {"token": "The light ", "done": False},
            {"token": "is on.", "done": False},
            {"token": "", "done": True, "conversation_id": "owui-c"},
        ]
        dispatcher = _make_dispatcher(frames=frames)
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("light on", stream=True))
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(resp.text)
        _assert_valid_chunk_sequence(events)
        assert _streamed_content(events) == "The light is on."
        assert "One moment." not in resp.text
        request = dispatcher.stream_requests[0]
        assert request.method == "message/stream"
        assert request.params["task"].context.source == "openai"

    async def test_mediated_speech_emitted_when_no_tokens(self, db_repository):
        frames = [{"token": "", "done": True, "mediated_speech": "Turned on the kitchen light."}]
        async with _Client(dispatcher=_make_dispatcher(frames=frames)) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("light on", stream=True))
        events = _parse_sse(resp.text)
        _assert_valid_chunk_sequence(events)
        assert _streamed_content(events) == "Turned on the kitchen light."

    async def test_mediated_speech_not_appended_after_streamed_tokens(self, db_repository):
        frames = [
            {"token": "Streamed answer.", "done": False},
            {"token": "", "done": True, "mediated_speech": "Different mediated answer."},
        ]
        async with _Client(dispatcher=_make_dispatcher(frames=frames)) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("q", stream=True))
        events = _parse_sse(resp.text)
        _assert_valid_chunk_sequence(events)
        assert _streamed_content(events) == "Streamed answer."

    async def test_error_streamed_when_nothing_else_was_sent(self, db_repository):
        frames = [{"token": "", "done": True, "error": "agent unavailable"}]
        async with _Client(dispatcher=_make_dispatcher(frames=frames)) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("q", stream=True))
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        _assert_valid_chunk_sequence(events)
        assert "agent unavailable" in _streamed_content(events)

    async def test_error_suppressed_after_streamed_tokens(self, db_repository):
        frames = [
            {"token": "Partial answer", "done": False},
            {"token": "", "done": True, "error": "late failure"},
        ]
        async with _Client(dispatcher=_make_dispatcher(frames=frames)) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("q", stream=True))
        events = _parse_sse(resp.text)
        assert _streamed_content(events) == "Partial answer"

    async def test_leading_whitespace_tokens_are_dropped(self, db_repository):
        frames = [
            {"token": "\n", "done": False},
            {"token": "\n", "done": False},
            {"token": "  Hello", "done": False},
            {"token": " there.\n\nBye.", "done": False},
            {"token": "", "done": True},
        ]
        async with _Client(dispatcher=_make_dispatcher(frames=frames)) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("hi", stream=True))
        events = _parse_sse(resp.text)
        _assert_valid_chunk_sequence(events)
        content_chunks = [e["choices"][0]["delta"].get("content", "") for e in events[:-1]]
        assert content_chunks[0] == "Hello"
        assert _streamed_content(events) == "Hello there.\n\nBye."

    async def test_leading_whitespace_of_mediated_speech_is_dropped(self, db_repository):
        frames = [
            {"token": "\n\n", "done": False},
            {"token": "", "done": True, "mediated_speech": "\n\nTurned on the light."},
        ]
        async with _Client(dispatcher=_make_dispatcher(frames=frames)) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("light on", stream=True))
        events = _parse_sse(resp.text)
        _assert_valid_chunk_sequence(events)
        assert _streamed_content(events) == "Turned on the light."

    async def test_empty_turn_still_emits_role_and_stop(self, db_repository):
        async with _Client(dispatcher=_make_dispatcher(frames=[{"token": "", "done": True}])) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("q", stream=True))
        events = _parse_sse(resp.text)
        _assert_valid_chunk_sequence(events)
        assert _streamed_content(events) == ""


# ---------------------------------------------------------------------------
# Open WebUI background tasks
# ---------------------------------------------------------------------------


_TITLE_PROMPT = (
    "### Task:\nGenerate a concise, 3-5 word title with an emoji summarizing the chat history.\n"
    "### Output:\nJSON format\n"
    "### Chat History:\n<chat_history>\nUSER: please turn on all the lights in the living room now\n"
    "ASSISTANT: Done.\n</chat_history>"
)


class TestBackgroundTasks:
    @pytest.mark.parametrize(
        ("task", "expected"),
        [
            ("title_generation", {"title": "please turn on all the lights"}),
            ("tags_generation", {"tags": []}),
            ("follow_up_generation", {"follow_ups": []}),
            ("query_generation", {"queries": []}),
        ],
    )
    async def test_task_header_returns_stub_without_dispatch(self, db_repository, task, expected):
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json=_chat_body(_TITLE_PROMPT),
                headers={"X-OpenWebUI-Task": task, "X-OpenWebUI-User-Id": "u-task"},
            )
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert json.loads(content) == expected
        dispatcher.dispatch.assert_not_awaited()
        assert dispatcher.stream_requests == []

    async def test_unknown_task_header_returns_empty_content(self, db_repository):
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json=_chat_body("turn on the lights"),
                headers={"X-OpenWebUI-Task": "emoji_generation"},
            )
        assert resp.json()["choices"][0]["message"]["content"] == ""
        dispatcher.dispatch.assert_not_awaited()

    @pytest.mark.parametrize("value", ["", "  ", "{{TASK}}", "None", "NULL", "false", "Undefined"])
    async def test_no_task_header_values_are_regular_turns(self, db_repository, value):
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json=_chat_body("turn on the lights"),
                headers={"X-OpenWebUI-Task": value},
            )
        assert resp.status_code == 200
        dispatcher.dispatch.assert_awaited_once()
        assert _sent_task(dispatcher).description == "turn on the lights"

    async def test_tasks_do_not_consume_conversation_rate_limit(self, db_repository):
        dispatcher = _make_dispatcher()
        task_headers = {"X-OpenWebUI-Task": "tags_generation"}
        async with _Client(dispatcher=dispatcher) as client:
            for _ in range(40):
                resp = await client.post("/v1/chat/completions", json=_chat_body("hi"), headers=task_headers)
                assert resp.status_code == 200
            for _ in range(30):
                resp = await client.post("/v1/chat/completions", json=_chat_body("hi"))
                assert resp.status_code == 200
            limited = await client.post("/v1/chat/completions", json=_chat_body("hi"))
            task_after = await client.post("/v1/chat/completions", json=_chat_body("hi"), headers=task_headers)
        assert limited.status_code == 429
        assert dispatcher.dispatch.await_count == 30
        assert task_after.status_code == 200
        assert json.loads(task_after.json()["choices"][0]["message"]["content"]) == {"tags": []}

    @pytest.mark.parametrize(
        ("prompt", "expected"),
        [
            (_TITLE_PROMPT, '{"title": "Chat"}'),
            ("### Task:\nGenerate 1-3 broad tags categorizing the main themes.\n### Output:\nJSON", '{"tags": []}'),
            ("### Task:\nSuggest 3-5 relevant follow-up questions.\n", '{"follow_ups": []}'),
            (
                "### Task:\nAnalyze the chat history to determine the necessity of generating search queries.",
                '{"queries": []}',
            ),
            ("### Task:\nDo something else entirely.", ""),
        ],
    )
    async def test_task_prompt_heuristic_returns_stub_without_dispatch(self, db_repository, prompt, expected):
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post("/v1/chat/completions", json=_chat_body("  " + prompt))
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == expected
        dispatcher.dispatch.assert_not_awaited()

    async def test_task_stub_streams_when_requested(self, db_repository):
        dispatcher = _make_dispatcher()
        async with _Client(dispatcher=dispatcher) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json=_chat_body("hi", stream=True),
                headers={"X-OpenWebUI-Task": "tags_generation"},
            )
        events = _parse_sse(resp.text)
        _assert_valid_chunk_sequence(events)
        assert _streamed_content(events) == '{"tags": []}'
        assert dispatcher.stream_requests == []

    async def test_task_does_not_record_user(self, db_repository):
        async with _Client() as client:
            await client.post(
                "/v1/chat/completions",
                json=_chat_body("hi"),
                headers={"X-OpenWebUI-Task": "tags_generation", "X-OpenWebUI-User-Id": "u-task-only"},
            )
        assert await ExternalUserMappingRepository.get("openwebui", "u-task-only") is None


# ---------------------------------------------------------------------------
# Tracing source
# ---------------------------------------------------------------------------


class TestTracingSource:
    async def test_v1_path_derives_openai_source(self, db_repository):
        from app.middleware.tracing import TracingMiddleware

        captured = {}

        async def _app(scope, receive, send):
            captured["source"] = scope["state"]["span_collector"].source
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = TracingMiddleware(_app)

        async def _receive():
            return {"type": "http.request", "body": b""}

        async def _send(_message):
            return None

        for path, expected in (("/v1/chat/completions", "openai"), ("/api/conversation", "ha"), ("/v10", "api")):
            await middleware({"type": "http", "path": path, "method": "POST", "headers": []}, _receive, _send)
            assert captured["source"] == expected

    async def test_request_user_id_lands_on_root_span(self, db_repository):
        from app.analytics.tracer import record_request_attribute
        from app.middleware.tracing import TracingMiddleware

        ha_user_id = "0123456789abcdef0123456789abcdef"

        async def _app(scope, receive, send):
            record_request_attribute(scope["state"]["span_collector"], "user_id", ha_user_id)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def _receive():
            return {"type": "http.request", "body": b""}

        async def _send(_message):
            return None

        spans: list = []

        async def _capture(batch):
            # flush() clears its span list afterwards; keep a copy.
            spans.extend(batch)

        with patch("app.analytics.tracer.TraceSpanRepository.insert_batch", side_effect=_capture):
            await TracingMiddleware(_app)(
                {"type": "http", "path": "/v1/chat/completions", "method": "POST", "headers": []}, _receive, _send
            )
        root = next(s for s in spans if s["parent_span"] is None)
        assert root["metadata"]["user_id"] == ha_user_id

    def test_trace_detail_reads_user_id_from_root_span(self):
        from app.api.routes.traces_api import _root_span_user_id

        spans = [
            {"span_name": "classify", "parent_span": "root", "metadata": {"user_id": "child"}},
            {"span_name": "POST /v1/chat/completions", "parent_span": None, "metadata": {"user_id": "ha-user-1"}},
        ]
        assert _root_span_user_id(spans) == "ha-user-1"
        assert _root_span_user_id([{"parent_span": None, "metadata": {"user_id": None}}]) is None


# ---------------------------------------------------------------------------
# Repository + migration
# ---------------------------------------------------------------------------


class TestExternalUserMappingRepository:
    async def test_touch_creates_and_updates(self, db_repository):
        assert await ExternalUserMappingRepository.touch("openwebui", "u-1", "Alice", "a@example.com") is None
        first = await ExternalUserMappingRepository.get("openwebui", "u-1")
        assert first["display_name"] == "Alice"
        assert first["first_seen_at"] == first["last_seen_at"]

        await ExternalUserMappingRepository.touch("openwebui", "u-1", "Alice B.", None)
        second = await ExternalUserMappingRepository.get("openwebui", "u-1")
        assert second["display_name"] == "Alice B."
        assert second["email"] == "a@example.com"
        assert second["first_seen_at"] == first["first_seen_at"]
        assert second["last_seen_at"] >= first["last_seen_at"]

    async def test_set_mapping_and_touch_returns_mapping(self, db_repository):
        await ExternalUserMappingRepository.touch("openwebui", "u-1")
        assert await ExternalUserMappingRepository.set_mapping("openwebui", "u-1", "ha-1") is True
        assert await ExternalUserMappingRepository.touch("openwebui", "u-1") == "ha-1"
        assert await ExternalUserMappingRepository.set_mapping("openwebui", "u-1", None) is True
        assert await ExternalUserMappingRepository.touch("openwebui", "u-1") is None
        assert await ExternalUserMappingRepository.set_mapping("openwebui", "missing", "ha-1") is False

    async def test_list_and_delete(self, db_repository):
        await ExternalUserMappingRepository.touch("openwebui", "u-1")
        await ExternalUserMappingRepository.touch("other", "u-2")
        assert {r["external_user_id"] for r in await ExternalUserMappingRepository.list_all()} == {"u-1", "u-2"}
        only = await ExternalUserMappingRepository.list_all("openwebui")
        assert [r["external_user_id"] for r in only] == ["u-1"]
        assert await ExternalUserMappingRepository.delete("openwebui", "u-1") is True
        assert await ExternalUserMappingRepository.delete("openwebui", "u-1") is False
        assert await ExternalUserMappingRepository.get("openwebui", "u-1") is None

    async def test_length_validation(self, db_repository):
        with pytest.raises(ValueError):
            await ExternalUserMappingRepository.touch("openwebui", "x" * 129)
        with pytest.raises(ValueError):
            await ExternalUserMappingRepository.touch("openwebui", "  ")
        await ExternalUserMappingRepository.touch("openwebui", "u-1")
        with pytest.raises(ValueError):
            await ExternalUserMappingRepository.set_mapping("openwebui", "u-1", "h" * 129)


class TestMigrationV45:
    async def test_migration_v45_creates_table_on_v44_database(self, db_repository):
        from app.db.schema import _run_migrations

        async with aiosqlite.connect(str(db_repository)) as db:
            await db.execute("DROP TABLE external_user_mappings")
            await db.executemany(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                [(v,) for v in range(2, 45)],
            )
            await db.commit()

            await _run_migrations(db)
            await _run_migrations(db)
            await db.commit()

            tables = await (
                await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='external_user_mappings'")
            ).fetchall()
            indexes = await (
                await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_external_user_mappings_ha_user'"
                )
            ).fetchall()
            versions = await (await db.execute("SELECT version FROM schema_version WHERE version = 45")).fetchall()

        assert len(tables) == 1
        assert len(indexes) == 1
        assert len(versions) == 1


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


def _ha_client(states=None, error=None) -> AsyncMock:
    client = AsyncMock()
    client.render_template = AsyncMock(return_value="")
    client.get_area_registry = AsyncMock(return_value={})
    client.get_config = AsyncMock(return_value={})
    client.get_states = AsyncMock(return_value=states or [], side_effect=error)
    return client


class TestExternalUsersAdminApi:
    async def test_list_returns_seen_users(self, db_repository):
        await ExternalUserMappingRepository.touch("openwebui", "u-1", "Alice", None)
        async with _Client(ha_client=_ha_client(_PERSON_STATES)) as client:
            resp = await client.get("/api/admin/external-users")
        assert resp.status_code == 200
        rows = resp.json()
        assert rows[0]["external_user_id"] == "u-1"
        assert rows[0]["display_name"] == "Alice"
        assert rows[0]["ha_user_id"] is None

    async def test_set_and_clear_mapping(self, db_repository):
        await ExternalUserMappingRepository.touch("openwebui", "u-1")
        async with _Client(ha_client=_ha_client(_PERSON_STATES)) as client:
            resp = await client.put("/api/admin/external-users/openwebui/u-1", json={"ha_user_id": "ha-user-alice"})
            assert resp.status_code == 200
            assert resp.json()["ha_user_id"] == "ha-user-alice"
            assert (await ExternalUserMappingRepository.get("openwebui", "u-1"))["ha_user_id"] == "ha-user-alice"

            resp = await client.put("/api/admin/external-users/openwebui/u-1", json={"ha_user_id": None})
            assert resp.status_code == 200
        assert (await ExternalUserMappingRepository.get("openwebui", "u-1"))["ha_user_id"] is None

    async def test_set_mapping_rejects_unknown_ha_user(self, db_repository):
        await ExternalUserMappingRepository.touch("openwebui", "u-1")
        async with _Client(ha_client=_ha_client(_PERSON_STATES)) as client:
            resp = await client.put("/api/admin/external-users/openwebui/u-1", json={"ha_user_id": "nobody"})
        assert resp.status_code == 400
        assert (await ExternalUserMappingRepository.get("openwebui", "u-1"))["ha_user_id"] is None

    async def test_set_mapping_unknown_user_returns_404(self, db_repository):
        async with _Client(ha_client=_ha_client(_PERSON_STATES)) as client:
            resp = await client.put("/api/admin/external-users/openwebui/missing", json={"ha_user_id": "ha-user-alice"})
        assert resp.status_code == 404

    async def test_delete(self, db_repository):
        await ExternalUserMappingRepository.touch("openwebui", "u-1")
        async with _Client(ha_client=_ha_client(_PERSON_STATES)) as client:
            resp = await client.delete("/api/admin/external-users/openwebui/u-1")
            assert resp.status_code == 200
            resp = await client.delete("/api/admin/external-users/openwebui/u-1")
            assert resp.status_code == 404

    async def test_requires_admin_session(self, db_repository):
        app = build_integration_test_app(setup_complete=True)
        with patch(
            "app.db.repository.SetupStateRepository.is_complete",
            new_callable=AsyncMock,
            return_value=True,
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                list_resp = await client.get("/api/admin/external-users")
                put_resp = await client.put("/api/admin/external-users/openwebui/u-1", json={"ha_user_id": None})
                delete_resp = await client.delete("/api/admin/external-users/openwebui/u-1")
        assert list_resp.status_code == 401
        assert put_resp.status_code == 401
        assert delete_resp.status_code == 401
