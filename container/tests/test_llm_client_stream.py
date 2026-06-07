"""Tests for app.llm.client complete_stream function."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock litellm before importing any app.llm modules
_litellm_mock = MagicMock()


class _AuthenticationError(Exception):
    pass


class _APIError(Exception):
    pass


class _TimeoutError(Exception):
    pass


_litellm_mock.exceptions.AuthenticationError = _AuthenticationError
_litellm_mock.exceptions.APIError = _APIError
_litellm_mock.exceptions.Timeout = _TimeoutError
sys.modules.setdefault("litellm", _litellm_mock)

from app.llm.client import LLMError, complete_stream  # noqa: E402


class _FakeDelta:
    def __init__(self, content: str | None):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None, finish_reason: str | None = None):
        self.delta = _FakeDelta(content)
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, content: str | None, finish_reason: str | None = None):
        self.choices = [_FakeChoice(content, finish_reason)]


async def _async_iter(items):
    for item in items:
        yield item


class _AsyncIterWithUsage:
    def __init__(self, items, usage):
        self._items = items
        self.usage = usage

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self):
        if self._idx >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._idx]
        self._idx += 1
        return item


class TestCompleteStream:
    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_stream_yields_tokens(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "light-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.7,
                "max_tokens": 256,
                "description": "Light agent",
            }
        )
        mock_acompletion.return_value = _async_iter(
            [
                _FakeChunk("Hello "),
                _FakeChunk("world"),
                _FakeChunk(None),
            ]
        )

        tokens = []
        async for token in complete_stream("light-agent", [{"role": "user", "content": "hi"}]):
            tokens.append(token)

        assert tokens == ["Hello ", "world"]
        mock_acompletion.assert_awaited_once()
        call_kwargs = mock_acompletion.call_args.kwargs
        assert call_kwargs.get("stream") is True

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_stream_empty_choices_raises(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "light-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.7,
                "max_tokens": 256,
                "description": "Light agent",
            }
        )

        class _EmptyChunk:
            choices = ()

        mock_acompletion.return_value = _async_iter([_EmptyChunk()])

        with pytest.raises(LLMError, match="Empty choices"):
            async for _token in complete_stream("light-agent", [{"role": "user", "content": "hi"}]):
                pass

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_stream_auth_error_propagates(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "light-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.7,
                "max_tokens": 256,
                "description": "Light agent",
            }
        )
        mock_acompletion.side_effect = _AuthenticationError("bad key")

        with pytest.raises(_AuthenticationError):
            async for _token in complete_stream("light-agent", [{"role": "user", "content": "hi"}]):
                pass

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_stream_timeout_propagates(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "light-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.7,
                "max_tokens": 256,
                "description": "Light agent",
            }
        )
        mock_acompletion.side_effect = _TimeoutError("too slow")

        with pytest.raises(_TimeoutError):
            async for _token in complete_stream("light-agent", [{"role": "user", "content": "hi"}]):
                pass

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_stream_records_span_metadata(self, mock_repo, mock_params, mock_acompletion):
        from app.analytics.tracer import SpanCollector

        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "light-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.7,
                "max_tokens": 256,
                "description": "Light agent",
            }
        )
        mock_acompletion.return_value = _async_iter(
            [
                _FakeChunk("Hello "),
                _FakeChunk("world"),
            ]
        )

        collector = SpanCollector("trace-stream")
        async for _token in complete_stream(
            "light-agent", [{"role": "user", "content": "hi"}], span_collector=collector
        ):
            pass

        prov_spans = [s for s in collector._spans if s["span_name"] == "llm_provider_call"]
        assert len(prov_spans) == 1
        assert prov_spans[0]["metadata"]["model"] == "openrouter/openai/gpt-4o-mini"
        assert prov_spans[0]["metadata"]["streamed"] is True

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_stream_span_metadata_includes_ttft_and_tps(self, mock_repo, mock_params, mock_acompletion):
        from app.analytics.tracer import SpanCollector

        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "light-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.7,
                "max_tokens": 256,
                "description": "Light agent",
            }
        )
        usage = MagicMock()
        usage.prompt_tokens = 5
        usage.completion_tokens = 4
        mock_acompletion.return_value = _AsyncIterWithUsage(
            [
                _FakeChunk("Hello "),
                _FakeChunk("world"),
            ],
            usage=usage,
        )

        collector = SpanCollector("trace-stream-ttft-tps")
        async for _token in complete_stream(
            "light-agent", [{"role": "user", "content": "hi"}], span_collector=collector
        ):
            pass

        prov_spans = [s for s in collector._spans if s["span_name"] == "llm_provider_call"]
        assert len(prov_spans) == 1
        assert prov_spans[0]["metadata"]["ttft_ms"] >= 0
        assert prov_spans[0]["metadata"]["tps"] > 0
