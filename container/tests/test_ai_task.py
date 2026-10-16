"""Tests for the AI-task feature (``POST /api/ai_task``).

Covers the route contract (validation, vision pre-step, structured
formatter), the stateless ``ai_task`` request source in the orchestrator
(no cache, mediation, filler, follow-up, pending question or history) and
migration 52 (helper agent config rows).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from app.agents.orchestrator import OrchestratorAgent
from app.models.agent import (
    INTERNAL_ONLY_AGENTS,
    TASK_HELPER_AGENTS,
    AgentCard,
    TaskContext,
    source_allows_filler,
    source_allows_voice_followup,
)
from app.security.auth import require_api_key
from tests.conftest import build_integration_test_app
from tests.helpers import make_ingress_task, shutdown_aiosqlite

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")
_SCHEMA = {
    "type": "object",
    "properties": {
        "package_present": {"type": "boolean"},
        "summary": {"type": ["string", "null"]},
    },
    "required": ["package_present"],
}


# ---------------------------------------------------------------------------
# Route fixtures
# ---------------------------------------------------------------------------


class _Route:
    def __init__(self, client: httpx.AsyncClient, dispatcher: MagicMock) -> None:
        self.client = client
        self.dispatcher = dispatcher

    async def post(self, body: dict[str, Any]) -> httpx.Response:
        return await self.client.post("/api/ai_task", json=body)

    def sent_task(self):
        return self.dispatcher.dispatch.await_args.args[0].params["task"]

    def sent_request(self):
        return self.dispatcher.dispatch.await_args.args[0]


@pytest_asyncio.fixture()
async def route(db_repository):
    from app.api.routes import ai_task as ai_task_routes
    from app.middleware.rate_limit import rate_limit_conversation

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(
        return_value={
            "speech": "A package is at the door.",
            "task_text": "A **package** is at the door.",
            "conversation_id": "conv-task",
            "routed_to": "general-agent",
            "action_executed": None,
        }
    )
    old_dispatcher = ai_task_routes._dispatcher
    ai_task_routes.set_dispatcher(dispatcher)
    app = build_integration_test_app(setup_complete=True, override_api_key=True, dispatcher=dispatcher)
    app.dependency_overrides[rate_limit_conversation] = lambda: None
    try:
        with patch(
            "app.db.repository.SetupStateRepository.is_complete",
            new_callable=AsyncMock,
            return_value=True,
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                yield _Route(client, dispatcher)
    finally:
        ai_task_routes._dispatcher = old_dispatcher


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"task_name": "Door check", "instructions": "Is there a package at the front door?"}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Route validation
# ---------------------------------------------------------------------------


class TestAiTaskValidation:
    async def test_missing_instructions_returns_400(self, route: _Route) -> None:
        resp = await route.post({"task_name": "Door check"})
        assert resp.status_code == 400
        assert "instructions" in resp.json()["detail"]
        route.dispatcher.dispatch.assert_not_awaited()

    async def test_invalid_json_body_returns_400(self, route: _Route) -> None:
        resp = await route.client.post(
            "/api/ai_task", content=b"{not json", headers={"content-type": "application/json"}
        )
        assert resp.status_code == 400

    async def test_invalid_schema_returns_400(self, route: _Route) -> None:
        resp = await route.post(_body(structure={"type": "object", "properties": {"x": {"type": 12}}}))
        assert resp.status_code == 400
        assert "JSON schema" in resp.json()["detail"]
        route.dispatcher.dispatch.assert_not_awaited()

    async def test_non_object_schema_returns_400(self, route: _Route) -> None:
        resp = await route.post(_body(structure={"type": "string"}))
        assert resp.status_code == 400
        assert "object" in resp.json()["detail"]

    async def test_too_many_attachments_returns_400(self, route: _Route) -> None:
        attachments = [{"mime_type": "image/png", "data": _PNG_B64}] * 5
        resp = await route.post(_body(attachments=attachments))
        assert resp.status_code == 400
        assert "attachments" in resp.json()["detail"]

    async def test_non_image_mime_returns_400(self, route: _Route) -> None:
        resp = await route.post(_body(attachments=[{"mime_type": "application/pdf", "data": _PNG_B64}]))
        assert resp.status_code == 400
        assert "image/*" in resp.json()["detail"]

    async def test_bad_base64_returns_400(self, route: _Route) -> None:
        resp = await route.post(_body(attachments=[{"mime_type": "image/png", "data": "not*base64!"}]))
        assert resp.status_code == 400
        assert "base64" in resp.json()["detail"]
        assert "not*base64" not in resp.text

    async def test_oversize_image_returns_400(self, route: _Route) -> None:
        big = base64.b64encode(b"\x00" * (8 * 1024 * 1024 + 1)).decode("ascii")
        resp = await route.post(_body(attachments=[{"mime_type": "image/jpeg", "data": big}]))
        assert resp.status_code == 400
        assert "8 MB" in resp.json()["detail"]

    async def test_missing_api_key_returns_401(self, route: _Route, db_repository) -> None:
        app = build_integration_test_app(setup_complete=True, override_api_key=False)
        with patch(
            "app.db.repository.SetupStateRepository.is_complete",
            new_callable=AsyncMock,
            return_value=True,
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post("/api/ai_task", json=_body())
        assert resp.status_code == 401

    def test_body_limit_fits_four_max_size_images(self) -> None:
        import math

        from app.models.ai_task import AI_TASK_MAX_ATTACHMENTS, AI_TASK_MAX_BODY_BYTES, AI_TASK_MAX_IMAGE_BYTES

        per_image = 4 * math.ceil(AI_TASK_MAX_IMAGE_BYTES / 3)
        assert AI_TASK_MAX_ATTACHMENTS * per_image + 64 * 1024 <= AI_TASK_MAX_BODY_BYTES

    def test_four_near_max_images_pass_size_gate_and_parse(self) -> None:
        import json

        from app.api.routes.ai_task import parse_ai_task_request
        from app.models.ai_task import AI_TASK_MAX_BODY_BYTES, AI_TASK_MAX_IMAGE_BYTES

        image_b64 = base64.b64encode(b"\x00" * AI_TASK_MAX_IMAGE_BYTES).decode("ascii")
        raw = json.dumps(
            {
                "task_name": "t",
                "instructions": "i" * 4000,
                "structure": _SCHEMA,
                "attachments": [{"mime_type": "image/jpeg", "data": image_b64}] * 4,
            }
        ).encode()
        assert len(raw) <= AI_TASK_MAX_BODY_BYTES
        task_request, images = parse_ai_task_request(raw)
        assert len(images) == 4
        assert all(image.size_bytes == AI_TASK_MAX_IMAGE_BYTES for image in images)
        # The request model no longer holds the base64 payloads.
        assert task_request.attachments == []

    async def test_body_reader_stops_at_limit(self) -> None:
        from fastapi import HTTPException

        from app.api.routes.ai_task import _read_body_limited

        class _Request:
            def __init__(self, chunks: list[bytes]) -> None:
                self.chunks = chunks
                self.read = 0

            async def stream(self):
                for chunk in self.chunks:
                    self.read += 1
                    yield chunk

        ok = _Request([b"a" * 10, b"b" * 10])
        assert bytes(await _read_body_limited(ok, 20)) == b"a" * 10 + b"b" * 10
        over = _Request([b"a" * 10, b"b" * 11, b"c" * 10])
        with pytest.raises(HTTPException) as exc_info:
            await _read_body_limited(over, 20)
        assert exc_info.value.status_code == 413
        assert over.read == 2

    def test_jpg_alias_is_normalized(self) -> None:
        from app.api.routes.ai_task import parse_ai_task_request

        raw = (
            b'{"task_name": "t", "instructions": "i", "attachments": [{"mime_type": "image/JPG", "data": "'
            + _PNG_B64.encode()
            + b'"}]}'
        )
        _request, images = parse_ai_task_request(raw)
        assert images[0].mime_type == "image/jpeg"
        assert images[0].size_bytes == len(_PNG_BYTES)
        assert _PNG_B64 not in repr(images[0])


# ---------------------------------------------------------------------------
# Route happy paths, formatter, vision
# ---------------------------------------------------------------------------


class TestAiTaskRoute:
    async def test_without_structure_returns_unmediated_text(self, route: _Route) -> None:
        resp = await route.post(_body(user_id="user-1", language="de"))
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "conversation_id": "conv-task",
            "data": "A **package** is at the door.",
            "text": "A **package** is at the door.",
            "routed_agent": "general-agent",
            "action_executed": False,
        }
        task = route.sent_task()
        assert task.description == "Is there a package at the front door?"
        assert task.context.source == "ai_task"
        assert task.context.user_id == "user-1"
        assert task.context.language == "de"
        assert task.context.attachment_description is None

    async def test_trace_source_is_ai_task(self, route: _Route) -> None:
        resp = await route.post(_body())
        assert resp.status_code == 200
        assert route.sent_request().params["_span_collector"].source == "ai_task"

    async def test_long_instructions_are_not_truncated_to_conversation_limit(self, route: _Route) -> None:
        instructions = "Check the door. " * 200  # 3200 chars, above the 500-char turn limit
        resp = await route.post(_body(instructions=instructions))
        assert resp.status_code == 200
        assert route.sent_task().description == instructions.strip()

    async def test_action_executed_is_reported(self, route: _Route) -> None:
        route.dispatcher.dispatch.return_value = {
            "speech": "Porch light is on.",
            "routed_to": "light-agent",
            "action_executed": {"action": "turn_on", "entity_id": "light.porch", "success": True},
        }
        resp = await route.post(_body(instructions="Turn on the porch light"))
        assert resp.status_code == 200
        assert resp.json()["action_executed"] is True
        assert resp.json()["text"] == "Porch light is on."

    async def test_timer_directive_returns_explanatory_text(self, route: _Route) -> None:
        from app.api.routes.ai_task import DIRECTIVE_NOT_SUPPORTED_TEXT

        route.dispatcher.dispatch.return_value = {
            "speech": "",
            "routed_to": "timer-agent",
            "directive": "delegate_native_plain_timer",
            "reason": "plain_timer",
        }
        resp = await route.post(_body(instructions="Set a timer for 5 minutes"))
        assert resp.status_code == 200
        assert resp.json()["text"] == DIRECTIVE_NOT_SUPPORTED_TEXT
        assert resp.json()["action_executed"] is False

    async def test_error_turn_returns_502_without_formatter(self, route: _Route) -> None:
        route.dispatcher.dispatch.return_value = {
            "speech": "Sorry, none of the agents could be reached.",
            "task_text": "Sorry, none of the agents could be reached.",
            "routed_to": "light-agent, music-agent",
            "error": "unknown",
        }
        formatter = AsyncMock(return_value='{"package_present": false}')
        with patch("app.agents.task_formatter.complete", new=formatter):
            resp = await route.post(_body(structure=_SCHEMA))
        assert resp.status_code == 502
        assert resp.json()["detail"] == "Sorry, none of the agents could be reached."
        formatter.assert_not_awaited()

    async def test_request_source_is_ai_task_regardless_of_trace_source(self) -> None:
        from app.api.routes.ai_task import _build_task_request
        from app.models.ai_task import AiTaskRequest

        request = _build_task_request(
            AiTaskRequest(task_name="t", instructions="i"),
            "i",
            None,
            MagicMock(source="api"),
        )
        assert request.params["task"].context.source == "ai_task"

    async def test_chunked_body_over_limit_returns_413(self, route: _Route) -> None:
        from app.models.ai_task import AI_TASK_MAX_BODY_BYTES

        chunk = b" " * (1024 * 1024)
        produced = {"bytes": 0}

        async def _chunks():
            yield b'{"task_name": "t", "instructions": "i", "pad": "'
            while produced["bytes"] <= AI_TASK_MAX_BODY_BYTES + 2 * len(chunk):
                produced["bytes"] += len(chunk)
                yield chunk
            yield b'"}'

        resp = await route.client.post("/api/ai_task", content=_chunks(), headers={"content-type": "application/json"})
        assert resp.status_code == 413
        route.dispatcher.dispatch.assert_not_awaited()

    async def test_instructions_are_sanitized_for_every_step(self, route: _Route) -> None:
        vision = AsyncMock(return_value="A box.")
        formatter = AsyncMock(return_value='{"package_present": true}')
        with (
            patch("app.agents.task_vision.complete", new=vision),
            patch("app.agents.task_vision.model_supports_vision", return_value=True),
            patch("app.agents.task_formatter.complete", new=formatter),
        ):
            resp = await route.post(
                _body(
                    task_name="Door\x00 check",
                    instructions="Is there\x00 a package\u202e at the door?",
                    structure=_SCHEMA,
                    attachments=[{"mime_type": "image/png", "data": _PNG_B64}],
                )
            )
        assert resp.status_code == 200
        vision_text = vision.await_args.args[1][1]["content"][0]["text"]
        formatter_text = formatter.await_args.args[1][1]["content"]
        dispatched = route.sent_task().description
        for text in (vision_text, formatter_text, dispatched):
            assert "\x00" not in text
            assert "\u202e" not in text
            assert "Is there a package at the door?" in text
        assert "Door check" in vision_text
        assert "Door check" in formatter_text

    async def test_dispatch_failure_returns_502(self, route: _Route) -> None:
        route.dispatcher.dispatch.side_effect = RuntimeError("Agent error: orchestrator")
        resp = await route.post(_body())
        assert resp.status_code == 502

    async def test_with_structure_returns_formatted_object(self, route: _Route) -> None:
        with patch(
            "app.agents.task_formatter.complete",
            new=AsyncMock(return_value='{"package_present": true, "summary": "A package is at the door."}'),
        ) as mock_complete:
            resp = await route.post(_body(structure=_SCHEMA))
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == {"package_present": True, "summary": "A package is at the door."}
        assert body["text"] == "A **package** is at the door."
        assert mock_complete.await_count == 1
        call = mock_complete.await_args
        assert call.args[0] == "task-formatter"
        assert call.kwargs["response_format"] == {"type": "json_object"}
        user_content = call.args[1][1]["content"]
        assert "A **package** is at the door." in user_content
        assert '"package_present"' in user_content
        assert "Home Assistant action executed: no" in user_content

    async def test_formatter_repairs_once_then_succeeds(self, route: _Route) -> None:
        answers = AsyncMock(
            side_effect=[
                '```json\n{"summary": "no flag"}\n```',
                '{"package_present": false, "summary": null}',
            ]
        )
        with patch("app.agents.task_formatter.complete", new=answers):
            resp = await route.post(_body(structure=_SCHEMA))
        assert resp.status_code == 200
        assert resp.json()["data"] == {"package_present": False, "summary": None}
        assert answers.await_count == 2
        repair_messages = answers.await_args_list[1].args[1]
        assert repair_messages[-1]["role"] == "user"
        assert "package_present" in repair_messages[-1]["content"]
        assert "required" in repair_messages[-1]["content"]

    async def test_formatter_repair_failure_returns_502(self, route: _Route) -> None:
        answers = AsyncMock(side_effect=['{"summary": 1}', "not json at all"])
        with patch("app.agents.task_formatter.complete", new=answers):
            resp = await route.post(_body(structure=_SCHEMA))
        assert resp.status_code == 502
        assert "schema" in resp.json()["detail"]
        assert answers.await_count == 2

    async def test_formatter_falls_back_without_json_mode(self, route: _Route) -> None:
        import litellm

        rejection = litellm.exceptions.BadRequestError(
            message="response_format not supported", model="m", llm_provider="p"
        )
        answers = AsyncMock(side_effect=[rejection, '{"package_present": true}'])
        with patch("app.agents.task_formatter.complete", new=answers):
            resp = await route.post(_body(structure=_SCHEMA))
        assert resp.status_code == 200
        assert resp.json()["data"] == {"package_present": True}
        assert "response_format" not in answers.await_args_list[1].kwargs

    async def test_vision_step_sends_image_parts_and_passes_description(self, route: _Route) -> None:
        vision = AsyncMock(return_value="Image 1: a brown cardboard box lies on the doormat.")
        with (
            patch("app.agents.task_vision.complete", new=vision),
            patch("app.agents.task_vision.model_supports_vision", return_value=True),
        ):
            resp = await route.post(
                _body(
                    attachments=[
                        {"mime_type": "image/png", "data": _PNG_B64},
                        {"mime_type": "image/jpg", "data": _PNG_B64},
                    ]
                )
            )
        assert resp.status_code == 200
        assert vision.await_args.args[0] == "vision-agent"
        user_content = vision.await_args.args[1][1]["content"]
        image_parts = [part for part in user_content if part["type"] == "image_url"]
        assert [part["image_url"]["url"] for part in image_parts] == [
            f"data:image/png;base64,{_PNG_B64}",
            f"data:image/jpeg;base64,{_PNG_B64}",
        ]
        assert user_content[0]["type"] == "text"

        task = route.sent_task()
        assert task.description.startswith("Is there a package at the front door?")
        assert "[UNTRUSTED_DATA_START]" in task.description
        assert "brown cardboard box" in task.description
        assert task.context.attachment_description == "Image 1: a brown cardboard box lies on the doormat."
        # Image bytes never enter the A2A envelope.
        assert _PNG_B64 not in task.model_dump_json()

    async def test_vision_unsupported_model_returns_502(self, route: _Route) -> None:
        vision = AsyncMock()
        with (
            patch("app.agents.task_vision.complete", new=vision),
            patch("app.agents.task_vision.litellm.supports_vision", return_value=False),
            patch("app.agents.task_vision.litellm.get_model_info", return_value={"key": "known"}),
        ):
            resp = await route.post(_body(attachments=[{"mime_type": "image/png", "data": _PNG_B64}]))
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert "openrouter/openai/gpt-4o-mini" in detail
        assert "vision-agent" in detail
        vision.assert_not_awaited()
        route.dispatcher.dispatch.assert_not_awaited()

    async def test_vision_unknown_model_lets_provider_decide(self, route: _Route) -> None:
        vision = AsyncMock(return_value="A street.")
        with (
            patch("app.agents.task_vision.complete", new=vision),
            patch("app.agents.task_vision.litellm.supports_vision", return_value=False),
            patch("app.agents.task_vision.litellm.get_model_info", side_effect=Exception("unknown model")),
        ):
            resp = await route.post(_body(attachments=[{"mime_type": "image/png", "data": _PNG_B64}]))
        assert resp.status_code == 200
        vision.assert_awaited_once()

    async def test_vision_llm_failure_returns_502(self, route: _Route) -> None:
        with (
            patch("app.agents.task_vision.complete", new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch("app.agents.task_vision.model_supports_vision", return_value=True),
        ):
            resp = await route.post(_body(attachments=[{"mime_type": "image/png", "data": _PNG_B64}]))
        assert resp.status_code == 502
        route.dispatcher.dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Spans and formatter / vision units
# ---------------------------------------------------------------------------


class TestTaskSpans:
    async def test_vision_span_records_metadata_without_content(self) -> None:
        from app.agents.task_vision import TaskImage, describe_images

        collector = MagicMock()
        span: dict[str, Any] = {"metadata": {}}

        class _Ctx:
            async def __aenter__(self):
                return span

            async def __aexit__(self, *exc):
                return False

        collector.start_span = MagicMock(return_value=_Ctx())
        row = {"agent_id": "vision-agent", "enabled": 1, "model": "openai/gpt-4o-mini"}
        with (
            patch("app.agents.task_vision.AgentConfigRepository.get", new=AsyncMock(return_value=row)),
            patch("app.agents.task_vision.model_supports_vision", return_value=True),
            patch("app.agents.task_vision.complete", new=AsyncMock(return_value="A box.")),
        ):
            description = await describe_images(
                "Door", "Check", [TaskImage("image/png", _PNG_B64, len(_PNG_BYTES))], span_collector=collector
            )
        assert description == "A box."
        assert collector.start_span.call_args.args[0] == "task_vision"
        metadata = span["metadata"]
        assert metadata["model"] == "openai/gpt-4o-mini"
        assert metadata["attachment_count"] == 1
        assert "latency_ms" in metadata
        assert _PNG_B64 not in repr(metadata)
        assert "A box." not in repr(metadata)

    async def test_format_span_records_attempts_and_validity(self) -> None:
        from app.agents.task_formatter import format_structured

        collector = MagicMock()
        span: dict[str, Any] = {"metadata": {}}

        class _Ctx:
            async def __aenter__(self):
                return span

            async def __aexit__(self, *exc):
                return False

        collector.start_span = MagicMock(return_value=_Ctx())
        row = {"agent_id": "task-formatter", "enabled": 1, "model": "openai/gpt-4o-mini"}
        answers = AsyncMock(side_effect=["{}", '{"package_present": true}'])
        with (
            patch("app.agents.task_formatter.AgentConfigRepository.get", new=AsyncMock(return_value=row)),
            patch("app.agents.task_formatter.complete", new=answers),
        ):
            data = await format_structured(
                task_name="Door",
                instructions="Check",
                result_text="A package.",
                action_executed=False,
                schema=_SCHEMA,
                span_collector=collector,
            )
        assert data == {"package_present": True}
        assert collector.start_span.call_args.args[0] == "task_format"
        assert span["metadata"]["attempts"] == 2
        assert span["metadata"]["valid"] is True
        assert span["metadata"]["model"] == "openai/gpt-4o-mini"

    def test_parse_json_object_strips_code_fences(self) -> None:
        from app.agents.task_formatter import parse_json_object

        assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
        assert parse_json_object('Here you go: {"a": 2}') == {"a": 2}
        with pytest.raises(ValueError):
            parse_json_object("[1, 2]")

    def test_model_supports_vision_is_tri_state(self) -> None:
        from app.agents.task_vision import model_supports_vision

        with patch("app.agents.task_vision.litellm.supports_vision", return_value=True):
            assert model_supports_vision("m") is True
        with (
            patch("app.agents.task_vision.litellm.supports_vision", return_value=False),
            patch("app.agents.task_vision.litellm.get_model_info", return_value={}),
        ):
            assert model_supports_vision("m") is False
        with patch("app.agents.task_vision.litellm.supports_vision", side_effect=Exception("broken")):
            assert model_supports_vision("m") is None


# ---------------------------------------------------------------------------
# Stateless ``ai_task`` source in the pipeline
# ---------------------------------------------------------------------------


def test_ai_task_source_is_filler_and_followup_exempt() -> None:
    assert source_allows_filler("ai_task") is False
    assert source_allows_voice_followup("ai_task") is False
    assert source_allows_filler("ha") is True


def test_task_helpers_are_internal_only() -> None:
    assert {"vision-agent", "task-formatter"} == set(TASK_HELPER_AGENTS)
    assert TASK_HELPER_AGENTS <= INTERNAL_ONLY_AGENTS


class TestAiTaskOrchestrator:
    @pytest.fixture(autouse=True)
    def _mock_conversation_repo(self):
        with (
            patch("app.agents.conversation_manager.ConversationRepository") as mock_repo,
            patch("app.agents.conversation_manager.get_memory_service", return_value=None),
        ):
            mock_repo.insert = AsyncMock(return_value=1)
            self.conversation_repo = mock_repo
            yield mock_repo

    def _make_orchestrator(self, dispatch_result: dict[str, Any]):
        dispatcher = AsyncMock()
        registry = AsyncMock()
        cache_manager = MagicMock()
        cache_manager.process = AsyncMock(return_value=MagicMock(hit_type="miss", agent_id=None, similarity=0.5))
        cache_manager.try_replay_action = AsyncMock(return_value=None)
        cache_manager.try_routing_skip = AsyncMock(return_value=None)
        cache_manager.store_routing_async = AsyncMock()
        cache_manager.store_action_async = AsyncMock()
        dispatcher.dispatch = AsyncMock(return_value=dispatch_result)
        registry.list_agents = AsyncMock(
            return_value=[
                AgentCard(agent_id="light-agent", name="Light Agent", description="", skills=["light"]),
                AgentCard(agent_id="music-agent", name="Music Agent", description="", skills=["music"]),
                AgentCard(agent_id="general-agent", name="General Agent", description="", skills=["general"]),
            ]
        )
        orchestrator = OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=cache_manager)
        calendar = MagicMock()
        calendar.inject_reminders = AsyncMock(return_value="Dentist at 3 pm.")
        orchestrator._calendar_injector = calendar
        return orchestrator, dispatcher, cache_manager, calendar

    async def _run(
        self,
        source: str,
        dispatch_result: dict[str, Any],
        classification: str,
        text: str,
        *,
        pending_question: tuple[str, str] | None = None,
    ):
        orch, _dispatcher, cache_manager, calendar = self._make_orchestrator(dispatch_result)
        if pending_question is not None:
            orch._conversation_manager.set_pending_question("conv-1", *pending_question)
        context = TaskContext(source=source, language="en", user_id="user-1")
        task = make_ingress_task(description=text, conversation_id="conv-1", context=context)
        with (
            patch("app.agents.orchestrator.SettingsRepository") as mock_settings,
            patch("app.agents.orchestrator.track_request", new_callable=AsyncMock),
            patch("app.llm.client.complete", new_callable=AsyncMock) as mock_complete,
            patch.object(orch, "_get_personality_cached", new=AsyncMock(return_value="You are a cheerful pirate.")),
        ):
            mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: "auto" if k == "language" else d)
            mock_complete.side_effect = [classification, "Arr, mediated answer!", "Arr, mediated answer!"]
            result = await orch.handle_task(task)
        return result, orch, cache_manager, calendar, mock_complete

    async def test_ai_task_skips_cache_mediation_followup_and_history(self) -> None:
        dispatch_result = {
            "speech": "Which **light** do you mean?",
            "voice_followup": True,
            "action_executed": None,
        }
        result, orch, cache_manager, calendar, mock_complete = await self._run(
            "ai_task", dispatch_result, "light-agent (95%): turn on the light", "turn on the light"
        )
        # Only the classification LLM call: no mediation, no localization.
        assert mock_complete.await_count == 1
        cache_manager.try_replay_action.assert_not_awaited()
        cache_manager.try_routing_skip.assert_not_awaited()
        cache_manager.store_action_async.assert_not_awaited()
        cache_manager.store_routing_async.assert_not_awaited()
        calendar.inject_reminders.assert_not_awaited()
        assert result["voice_followup"] is False
        assert result["task_text"] == "Which **light** do you mean?"
        assert result["speech"] == "Which light do you mean?"
        assert orch._conversation_manager.has_pending_question("conv-1") is False
        self.conversation_repo.insert.assert_not_awaited()
        assert await orch._conversation_manager.get_turns("conv-1") == []

    async def test_ai_task_keeps_existing_pending_question(self) -> None:
        dispatch_result = {"speech": "Which **lamp**?", "voice_followup": True, "action_executed": None}
        with patch(
            "app.agents.classification_engine.ClassificationEngine.classify",
            new=AsyncMock(return_value=([("light-agent", "turn on the light", 0.95)], False)),
        ) as classify:
            result, orch, *_ = await self._run(
                "ai_task",
                dispatch_result,
                "unused",
                "turn on the light",
                pending_question=("Which room did you mean?", "light-agent"),
            )
        assert result["task_text"] == "Which **lamp**?"
        # The interactive conversation's question was neither consumed nor overwritten.
        assert classify.await_args.kwargs["pending_question"] is None
        pending = orch._conversation_manager.pop_pending_question("conv-1")
        assert pending == {"question": "Which room did you mean?", "agent_id": "light-agent"}

    async def test_ai_task_multi_agent_lists_unreachable_and_skipped_agents(self) -> None:
        from app.agents.pipeline_strategies import INTENT_LIMIT_REASON, DefaultFinalizationStrategy
        from app.agents.task_pipeline import DispatchResult

        conversation_manager = MagicMock()
        conversation_manager.store_turn = AsyncMock()
        strategy = DefaultFinalizationStrategy(
            conversation_manager=conversation_manager,
            merge_responses=AsyncMock(side_effect=AssertionError("merge LLM must not run")),
            create_trace=AsyncMock(),
        )
        dispatch_result = DispatchResult(
            classifications=[
                ("light-agent", "turn on the light", 0.95),
                ("music-agent", "play jazz", 0.9),
                ("scene-agent", "start movie night", 0.5),
            ],
            target_agent="light-agent",
            routed_to="light-agent",
            speech="Light is on.",
            failed_agents=[("music-agent", "timeout"), ("scene-agent", INTENT_LIMIT_REASON)],
            agent_responses=[("light-agent", "Light is on.", True)],
        )
        task = make_ingress_task("do three things", context=TaskContext(source="ai_task"))
        response = await strategy.execute(
            task,
            dispatch_result,
            "do three things",
            "en",
            "conv-1",
            [],
            None,
            dispatch_result.classifications,
            False,
            False,
        )
        assert response["task_text"] == "Light is on.\nNot completed by: music-agent, scene-agent."

    async def test_control_api_source_mediates_and_stores(self) -> None:
        dispatch_result = {"speech": "Which light do you mean?", "voice_followup": True, "action_executed": None}
        result, _orch, cache_manager, calendar, mock_complete = await self._run(
            "api", dispatch_result, "light-agent (95%): turn on the light", "turn on the light"
        )
        assert mock_complete.await_count >= 2
        cache_manager.try_replay_action.assert_awaited()
        calendar.inject_reminders.assert_awaited()
        assert "task_text" not in result
        self.conversation_repo.insert.assert_awaited()

    async def test_ai_task_multi_agent_joins_results_without_merge_llm(self) -> None:
        dispatch_result = {"speech": "Done.", "action_executed": None}
        result, _orch, _cache, calendar, mock_complete = await self._run(
            "ai_task",
            dispatch_result,
            "light-agent (95%): turn on the light\nmusic-agent (90%): play jazz",
            "turn on the light and play jazz",
        )
        assert mock_complete.await_count == 1
        calendar.inject_reminders.assert_not_awaited()
        assert result["task_text"] == "Done.\nDone."
        self.conversation_repo.insert.assert_not_awaited()

    async def test_store_after_dispatch_skips_ai_task(self) -> None:
        from app.agents.cache_orchestrator import CacheOrchestrator

        cache_manager = MagicMock()
        cache_manager.store_action_async = AsyncMock()
        cache_manager.store_routing_async = AsyncMock()
        cache_orch = CacheOrchestrator(cache_manager=cache_manager)
        task = make_ingress_task("turn on the light", context=TaskContext(source="ai_task"))
        stored = await cache_orch.store_after_dispatch(
            user_text="turn on the light",
            language="en",
            target_agent="light-agent",
            condensed_task="turn on the light",
            confidence=0.95,
            speech="Light is on.",
            action_executed={"action": "turn_on", "entity_id": "light.kitchen", "success": True},
            has_error=False,
            task=task,
        )
        assert stored == (False, False)
        cache_manager.store_action_async.assert_not_awaited()
        cache_manager.store_routing_async.assert_not_awaited()

    async def test_conversation_manager_drops_ai_task_turns(self) -> None:
        from app.agents.conversation_manager import ConversationManager

        manager = ConversationManager()
        await manager.store_turn("conv-task", "describe the image", "A box.", source="ai_task")
        assert await manager.get_turns("conv-task") == []
        self.conversation_repo.insert.assert_not_awaited()


# ---------------------------------------------------------------------------
# Migration 52
# ---------------------------------------------------------------------------


async def _agent_rows(path: Path) -> dict[str, tuple]:
    db = await aiosqlite.connect(str(path))
    try:
        cursor = await db.execute(
            "SELECT agent_id, enabled, model, description FROM agent_configs "
            "WHERE agent_id IN ('vision-agent', 'task-formatter')"
        )
        return {row[0]: tuple(row) for row in await cursor.fetchall()}
    finally:
        await shutdown_aiosqlite(db)


async def test_migration_52_adds_helper_rows_idempotently(tmp_path: Path) -> None:
    from app.db.schema import _create_indexes, _create_tables, _seed_defaults
    from app.db.schema._migrations import _migrate_to_52

    path = tmp_path / "m52.db"
    db = await aiosqlite.connect(str(path))
    try:
        await _create_tables(db)
        await _create_indexes(db)
        await _seed_defaults(db)
        # Simulate a pre-52 database: the helper rows do not exist yet.
        await db.execute("DELETE FROM agent_configs WHERE agent_id IN ('vision-agent', 'task-formatter')")
        await db.commit()
        await _migrate_to_52(db)
        await db.execute("UPDATE agent_configs SET model = 'openai/gpt-4o' WHERE agent_id = 'vision-agent'")
        await _migrate_to_52(db)
        await db.commit()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM agent_configs WHERE agent_id IN ('vision-agent', 'task-formatter')"
        )
        assert (await cursor.fetchone())[0] == 2
        cursor = await db.execute("SELECT COUNT(*) FROM schema_version WHERE version = 52")
        assert (await cursor.fetchone())[0] == 1
    finally:
        await shutdown_aiosqlite(db)

    rows = await _agent_rows(path)
    assert rows["vision-agent"][2] == "openai/gpt-4o"  # admin edit survives a re-run
    assert rows["task-formatter"][1] == 1
    assert rows["task-formatter"][2] == "openrouter/openai/gpt-4o-mini"


async def test_fresh_install_seeds_helper_rows(db_repository) -> None:
    from app.db.repository import AgentConfigRepository

    vision = await AgentConfigRepository.get("vision-agent")
    formatter = await AgentConfigRepository.get("task-formatter")
    general = await AgentConfigRepository.get("general-agent")
    assert vision is not None and formatter is not None
    assert vision["model"] == general["model"]
    assert formatter["model"] == general["model"]


async def test_admin_agent_list_includes_helpers_without_registering_them(db_repository) -> None:
    from app.api.routes.admin import _agents, _shared

    registry = MagicMock()
    registry.list_agents = AsyncMock(return_value=[])
    old_registry = _shared._registry
    _shared._registry = registry
    try:
        listing = await _agents.list_agents()
    finally:
        _shared._registry = old_registry
    ids = {agent["agent_id"] for agent in listing["agents"]}
    assert {"vision-agent", "task-formatter"} <= ids
    registry.register.assert_not_called()


def test_api_key_dependency_is_required_on_route() -> None:
    from app.api.routes.ai_task import router

    route = next(r for r in router.routes if getattr(r, "path", "") == "/api/ai_task")
    dependency_calls = {dep.call for dep in route.dependant.dependencies}
    assert require_api_key in dependency_calls
