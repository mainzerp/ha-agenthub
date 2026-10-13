"""Error detail recorded on spans that exit with an exception."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.actionable import LightAgent
from app.analytics.tracer import SpanCollector, record_span_error
from app.models.agent import AgentErrorCode, DispatchTask, TaskContext


class RateLimitError(Exception):
    """Stand-in for a provider exception that carries an HTTP status."""

    def __init__(self, message: str, status_code: int = 429) -> None:
        super().__init__(message)
        self.status_code = status_code


class _ResponseError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status_code)


async def _raise_in_span(collector: SpanCollector, exc: Exception, preset: dict | None = None) -> dict:
    with pytest.raises(type(exc)) as excinfo:
        async with collector.start_span("llm_call", agent_id="light-agent") as span:
            span["metadata"].update(preset or {})
            raise exc
    assert excinfo.value is exc
    return collector.get_spans()[-1]


async def test_exception_in_span_records_error_type_message_and_status():
    collector = SpanCollector("trace-error-detail")
    exc = RateLimitError("We're experiencing high traffic right now! Please try again soon.")

    span = await _raise_in_span(collector, exc)

    assert span["status"] == "error"
    metadata = span["metadata"]
    assert metadata["error_type"] == f"{__name__.split('.', 1)[0]}.RateLimitError"
    assert metadata["error_message"] == "We're experiencing high traffic right now! Please try again soon."
    assert metadata["error_status"] == 429


async def test_error_status_falls_back_to_response_status_code():
    collector = SpanCollector("trace-error-response")
    span = await _raise_in_span(collector, _ResponseError("upstream unavailable", 503))
    assert span["metadata"]["error_status"] == 503


async def test_builtin_exception_type_is_bare_class_name_without_status():
    collector = SpanCollector("trace-error-builtin")
    span = await _raise_in_span(collector, ValueError("bad value"))
    assert span["metadata"]["error_type"] == "ValueError"
    assert span["metadata"]["error_message"] == "bad value"
    assert "error_status" not in span["metadata"]


async def test_error_message_is_single_line_and_truncated():
    collector = SpanCollector("trace-error-truncate")
    message = "first line\nsecond\tline " + ("word " * 200)
    span = await _raise_in_span(collector, RuntimeError(message))

    stored = span["metadata"]["error_message"]
    assert "\n" not in stored and "\t" not in stored
    assert stored.startswith("first line second line word")
    assert len(stored) <= 300
    assert stored.endswith("...")


async def test_error_message_redacts_api_keys_and_bearer_tokens():
    collector = SpanCollector("trace-error-redact")
    message = (
        "AuthenticationError: invalid key sk-abcdefghijklmnopqrstuvwx "
        "header Authorization: Bearer abcdefghijklmnop1234 api_key=supersecretvalue"
    )
    span = await _raise_in_span(collector, RuntimeError(message))

    stored = span["metadata"]["error_message"]
    assert "sk-abcdefghijklmnopqrstuvwx" not in stored
    assert "abcdefghijklmnop1234" not in stored
    assert "supersecretvalue" not in stored
    assert "REDACTED" in stored


async def test_caller_set_error_keys_are_not_overwritten():
    collector = SpanCollector("trace-error-preset")
    preset = {"error_type": "caller_type", "error_message": "caller message", "error_status": 418}
    span = await _raise_in_span(collector, RateLimitError("provider text", status_code=429), preset)

    assert span["metadata"]["error_type"] == "caller_type"
    assert span["metadata"]["error_message"] == "caller message"
    assert span["metadata"]["error_status"] == 418


async def test_successful_span_has_no_error_keys():
    collector = SpanCollector("trace-ok")
    async with collector.start_span("llm_call") as span:
        span["metadata"]["model"] = "light-agent"
    stored = collector.get_spans()[-1]
    assert stored["status"] == "ok"
    assert not any(key.startswith("error_") for key in stored["metadata"])


async def test_error_detail_survives_flush_sanitization():
    collector = SpanCollector("trace-error-flush")
    await _raise_in_span(collector, RateLimitError("rate limited, retry later"))
    captured: dict[str, list] = {}

    async def fake_insert_batch(spans):
        captured["spans"] = [dict(span) for span in spans]

    with (
        patch("app.analytics.tracer.TraceSpanRepository.insert_batch", new=fake_insert_batch),
        patch("app.analytics.tracer.TraceSummaryRepository.update_duration", new=AsyncMock()),
    ):
        await collector.flush()

    metadata = captured["spans"][0]["metadata"]
    assert metadata["error_message"] == "rate limited, retry later"
    assert metadata["error_status"] == 429
    assert metadata["error_type"].endswith("RateLimitError")


def test_record_span_error_tolerates_missing_metadata_and_broken_str():
    class _BrokenStrError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("cannot render")

    span: dict = {"metadata": None}
    record_span_error(span, _BrokenStrError())
    assert span["metadata"]["error_type"].endswith("_BrokenStrError")
    assert "error_message" not in span["metadata"]


async def test_actionable_llm_failure_records_error_detail_on_llm_call_span():
    agent = LightAgent(ha_client=None)
    agent._entity_matcher = MagicMock()
    agent._call_llm = AsyncMock(side_effect=RateLimitError("We're experiencing high traffic right now!"))
    collector = SpanCollector("trace-actionable-llm-error")
    task = DispatchTask(description="turn on the kitchen light", context=TaskContext())
    task.span_collector = collector

    result = await agent.handle_task(task)

    assert result.error is not None
    assert result.error.code == AgentErrorCode.LLM_ERROR
    llm_spans = [span for span in collector.get_spans() if span["span_name"] == "llm_call"]
    assert len(llm_spans) == 1
    llm_span = llm_spans[0]
    assert llm_span["status"] == "error"
    assert llm_span["metadata"]["error_type"].endswith("RateLimitError")
    assert llm_span["metadata"]["error_message"] == "We're experiencing high traffic right now!"
    assert llm_span["metadata"]["error_status"] == 429
