"""Focused tests for trace redaction and sanitized tool metadata."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agents.tool_calling import call_llm_with_mcp_tools
from app.analytics.tracer import SpanCollector, create_trace_summary, record_span, sanitize_trace_value


def test_sanitize_trace_value_redacts_nested_structures_and_preserves_safe_keys():
    value = {
        "entity_id": "light.kitchen_ceiling",
        "authorization": "Bearer super-secret-token-value",
        "payload": {
            "token": "abc123456789",
            "items": [
                {
                    "service": "notify",
                    "password": "hunter2",
                    "count": 2,
                    "area": "kitchen",
                }
            ],
        },
        "tuple_data": (
            {"api_key": "sk-test-secret"},
            "verification code 123456",
        ),
    }

    sanitized = sanitize_trace_value(value)

    assert sanitized["entity_id"] == "light.kitchen_ceiling"
    assert sanitized["authorization"] == "[REDACTED]"
    assert sanitized["payload"]["token"] == "[REDACTED]"
    assert sanitized["payload"]["items"][0]["service"] == "notify"
    assert sanitized["payload"]["items"][0]["password"] == "[REDACTED]"
    assert sanitized["payload"]["items"][0]["count"] == 2
    assert sanitized["payload"]["items"][0]["area"] == "kitchen"
    assert sanitized["tuple_data"][0]["api_key"] == "[REDACTED]"
    assert sanitized["tuple_data"][1] == "verification code [REDACTED_CODE]"


def test_sanitize_trace_value_redacts_urls_tokens_and_codes():
    value = (
        "Visit https://user:pass@example.com/api/run?token=abcdef123456&page=1 "
        "with Bearer abcdefghijklmnop and use code 654321"
    )

    sanitized = sanitize_trace_value(value)

    assert "user:pass@" not in sanitized
    assert "token=abcdef123456" not in sanitized
    assert "token=[REDACTED]" in sanitized
    assert "page=1" in sanitized
    assert "Bearer [REDACTED_TOKEN]" in sanitized
    assert "code [REDACTED_CODE]" in sanitized


async def test_span_collector_flush_sanitizes_nested_metadata():
    collector = SpanCollector("trace-nested")
    async with collector.start_span("llm_call", agent_id="general-agent") as span:
        span["metadata"]["entity_id"] = "light.kitchen"
        span["metadata"]["authorization"] = "Bearer topsecretvalue"
        span["metadata"]["payload"] = {
            "cookie": "session=abc",
            "items": [{"service": "notify", "password": "pw", "count": 1}],
        }

    captured: dict[str, object] = {}

    async def fake_insert_batch(spans):
        captured["spans"] = [
            {
                **span,
                "metadata": dict(span.get("metadata") or {}),
            }
            for span in spans
        ]

    with (
        patch("app.analytics.tracer.TraceSpanRepository.insert_batch", new=fake_insert_batch),
        patch("app.analytics.tracer.TraceSummaryRepository.update_duration", new=AsyncMock()),
    ):
        await collector.flush()

    inserted_span = captured["spans"][0]
    metadata = inserted_span["metadata"]
    assert metadata["entity_id"] == "light.kitchen"
    assert metadata["authorization"] == "[REDACTED]"
    assert metadata["payload"]["cookie"] == "[REDACTED]"
    assert metadata["payload"]["items"][0]["service"] == "notify"
    assert metadata["payload"]["items"][0]["password"] == "[REDACTED]"
    assert metadata["payload"]["items"][0]["count"] == 1


async def test_record_span_sanitizes_llm_preview_before_insert():
    with patch("app.analytics.tracer.TraceSpanRepository.insert", new=AsyncMock(return_value=1)) as mock_insert:
        await record_span(
            trace_id="trace-record",
            span_name="llm_call",
            start_time="2026-04-25T00:00:00+00:00",
            duration_ms=1.0,
            metadata={
                "llm_response": (
                    "Bearer abcdefghijklmnop https://user:pass@example.com/path?api_key=sk-test-secret "
                    "verification code 123456"
                )
            },
        )

    metadata = mock_insert.await_args.kwargs["metadata"]
    assert "Bearer [REDACTED_TOKEN]" in metadata["llm_response"]
    assert "user:pass@" not in metadata["llm_response"]
    assert "api_key=sk-test-secret" not in metadata["llm_response"]
    assert "verification code [REDACTED_CODE]" in metadata["llm_response"]


async def test_create_trace_summary_sanitizes_sensitive_fields():
    with patch("app.analytics.tracer.TraceSummaryRepository.create", new=AsyncMock()) as mock_create:
        await create_trace_summary(
            trace_id="trace-summary",
            conversation_id="conv-1",
            user_input="Bearer abcdefghijklmnop",
            final_response='{"code":"123456","entity_id":"light.kitchen"}',
            routing_agent="general-agent",
            routing_confidence=0.9,
            routing_duration_ms=12.5,
            condensed_task="call tool",
            agents=["general-agent"],
            source="api",
            agent_instructions={
                "general-agent": "Use token abcdefghijklmnop",
                "tool": "https://user:pass@example.com/run?token=secret",
            },
            conversation_turns=[
                {"role": "user", "content": "api_key=sk-test-secret"},
                {"role": "assistant", "content": "Open https://svc.example/path?code=654321"},
            ],
        )

    payload = mock_create.await_args.args[0]
    assert payload["user_input"] == "Bearer [REDACTED_TOKEN]"
    final_response = json.loads(payload["final_response"])
    assert final_response["code"] == "[REDACTED_CODE]"
    assert final_response["entity_id"] == "light.kitchen"
    assert "[REDACTED_TOKEN]" in payload["agent_instructions"]["general-agent"]
    assert "user:pass@" not in payload["agent_instructions"]["tool"]
    assert "token=[REDACTED]" in payload["agent_instructions"]["tool"]
    assert payload["conversation_turns"][0]["content"] == "api_key=[REDACTED_TOKEN]"
    assert "code=[REDACTED]" in payload["conversation_turns"][1]["content"]


async def test_call_llm_with_mcp_tools_records_sanitized_arguments_and_result():
    fake_agent = SimpleNamespace(
        agent_card=SimpleNamespace(agent_id="general-agent"),
        _normalize_llm_messages=lambda messages: messages,
    )
    tool_manager = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=(
                '{"status":"ok","access_token":"abcdef1234567890abcdef1234567890",'
                '"entity_id":"light.kitchen","url":"https://svc.example/run?apikey=secret"}'
            )
        )
    )
    collector = SpanCollector("trace-tool")

    async def fake_complete_with_tools(agent_id, messages, tools, tool_executor, span_collector=None, **kwargs):
        await tool_executor(
            "lookup_status",
            {
                "entity_id": "light.kitchen",
                "api_key": "sk-test-secret",
                "url": "https://user:pass@example.com/run?token=secret&page=1",
            },
        )
        return "done"

    with patch("app.llm.client.complete_with_tools", new=fake_complete_with_tools):
        await call_llm_with_mcp_tools(
            fake_agent,
            [{"role": "user", "content": "hello"}],
            [
                {
                    "name": "lookup_status",
                    "description": "Lookup device status",
                    "input_schema": {},
                    "_server_name": "ops-server",
                }
            ],
            tool_manager,
            span_collector=collector,
        )

    metadata = collector._spans[0]["metadata"]
    assert metadata["tool_name"] == "lookup_status"
    assert metadata["server_name"] == "ops-server"
    assert metadata["argument_keys"] == ["api_key", "entity_id", "url"]
    assert metadata["arguments"]["entity_id"] == "light.kitchen"
    assert metadata["arguments"]["api_key"] == "[REDACTED]"
    assert "user:pass@" not in metadata["arguments"]["url"]
    assert "page=1" in metadata["arguments"]["url"]
    result = json.loads(metadata["result"])
    assert result["status"] == "ok"
    assert result["access_token"] == "[REDACTED]"
    assert result["entity_id"] == "light.kitchen"
    assert "apikey=[REDACTED]" in result["url"]
    assert metadata["result_chars"] > 0
