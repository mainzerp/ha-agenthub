"""Tests for app.llm -- client and providers."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock litellm before importing any app.llm modules
_litellm_mock = MagicMock()


class _AuthenticationError(Exception):
    pass


class _APIError(Exception):
    pass


_litellm_mock.exceptions.AuthenticationError = _AuthenticationError
_litellm_mock.exceptions.APIError = _APIError
sys.modules.setdefault("litellm", _litellm_mock)

from app.llm.providers import (  # noqa: E402
    extract_provider,
    get_api_key,
    get_base_url,
    resolve_provider_params,
)

# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------


class TestExtractProvider:
    def test_extract_provider_from_slashed_model(self):
        assert extract_provider("openrouter/openai/gpt-4o-mini") == "openrouter"

    def test_extract_provider_from_groq(self):
        assert extract_provider("groq/llama3-70b") == "groq"

    def test_extract_provider_defaults_to_openai(self):
        assert extract_provider("gpt-4") == "openai"

    def test_extract_provider_ollama(self):
        assert extract_provider("ollama/llama3") == "ollama"


class TestGetApiKey:
    @patch("app.llm.providers.retrieve_secret", new_callable=AsyncMock, return_value="sk-test-key")
    async def test_get_api_key_returns_key(self, mock_retrieve):
        key = await get_api_key("openrouter")
        assert key == "sk-test-key"
        mock_retrieve.assert_awaited_once_with("openrouter_api_key")

    @patch("app.llm.providers.retrieve_secret", new_callable=AsyncMock, return_value=None)
    async def test_get_api_key_returns_none_for_missing_key(self, mock_retrieve):
        key = await get_api_key("openrouter")
        assert key is None

    async def test_get_api_key_returns_none_for_local_provider(self):
        key = await get_api_key("ollama")
        assert key is None

    async def test_get_api_key_returns_none_for_unknown_provider(self):
        key = await get_api_key("unknown-provider")
        assert key is None


class TestGetBaseUrl:
    @patch("app.llm.providers.SettingsRepository")
    async def test_get_base_url_ollama(self, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://ollama:11434")
        url = await get_base_url("ollama")
        assert url == "http://ollama:11434"

    async def test_get_base_url_returns_none_for_non_ollama(self):
        url = await get_base_url("openrouter")
        assert url is None


class TestResolveProviderParams:
    @patch("app.llm.providers.retrieve_secret", new_callable=AsyncMock, return_value="sk-key")
    async def test_resolve_openrouter_includes_api_key(self, mock_retrieve):
        params = await resolve_provider_params("openrouter/openai/gpt-4o")
        assert params["api_key"] == "sk-key"

    @patch("app.llm.providers.SettingsRepository")
    async def test_resolve_ollama_includes_base_url(self, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="http://localhost:11434")
        params = await resolve_provider_params("ollama/llama3")
        assert params["api_base"] == "http://localhost:11434"
        assert "api_key" not in params

    @patch("app.llm.providers.retrieve_secret", new_callable=AsyncMock, return_value="custom-key")
    @patch("app.llm.providers.SettingsRepository")
    async def test_resolve_custom_openai_includes_all_params(self, mock_settings, mock_retrieve):
        mock_settings.get_value = AsyncMock(side_effect=["http://custom.local:8000/v1", '{"X-Custom": "val"}'])
        params = await resolve_provider_params("custom_openai/my-model")
        assert params["api_key"] == "custom-key"
        assert params["api_base"] == "http://custom.local:8000/v1"
        assert params["extra_headers"] == {"X-Custom": "val"}

    @patch("app.llm.providers.retrieve_secret", new_callable=AsyncMock, return_value="custom-key")
    @patch("app.llm.providers.SettingsRepository")
    async def test_resolve_custom_openai_ignores_empty_headers(self, mock_settings, mock_retrieve):
        mock_settings.get_value = AsyncMock(side_effect=["http://custom.local:8000/v1", "{}"])
        params = await resolve_provider_params("custom_openai/my-model")
        assert params["api_key"] == "custom-key"
        assert params["api_base"] == "http://custom.local:8000/v1"
        assert "extra_headers" not in params

    @patch("app.llm.providers.retrieve_secret", new_callable=AsyncMock, return_value="custom-key")
    @patch("app.llm.providers.SettingsRepository")
    async def test_resolve_custom_openai_tolerates_bad_json_headers(self, mock_settings, mock_retrieve):
        mock_settings.get_value = AsyncMock(side_effect=["http://custom.local:8000/v1", "not-json"])
        params = await resolve_provider_params("custom_openai/my-model")
        assert params["api_key"] == "custom-key"
        assert "extra_headers" not in params


# ---------------------------------------------------------------------------
# LLM complete function
# ---------------------------------------------------------------------------


class TestLLMComplete:
    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_calls_litellm(self, mock_repo, mock_params, mock_acompletion):
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
        choice = MagicMock()
        choice.message.content = "Done!"
        mock_acompletion.return_value = MagicMock(choices=[choice])

        from app.llm.client import complete

        result = await complete("light-agent", [{"role": "user", "content": "turn on light"}])
        assert result == "Done!"
        mock_acompletion.assert_awaited_once()

    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_raises_on_missing_config(self, mock_repo, mock_params):
        mock_repo.get = AsyncMock(return_value=None)
        from app.llm.client import complete

        with pytest.raises(ValueError, match="No config found"):
            await complete("nonexistent-agent", [{"role": "user", "content": "hi"}])

    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_raises_on_no_model(self, mock_repo, mock_params):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "enabled": True,
                "model": None,
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.7,
                "max_tokens": 256,
                "description": "No model",
            }
        )
        from app.llm.client import complete

        with pytest.raises(ValueError, match="No model configured"):
            await complete("test-agent", [{"role": "user", "content": "hi"}])

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_passes_overrides(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.5,
                "max_tokens": 100,
                "description": "test",
            }
        )
        choice = MagicMock()
        choice.message.content = "result"
        mock_acompletion.return_value = MagicMock(choices=[choice])

        from app.llm.client import complete

        await complete("test-agent", [{"role": "user", "content": "test"}], temperature=0.1)
        call_kwargs = mock_acompletion.call_args
        assert call_kwargs.kwargs.get("temperature") == 0.1 or call_kwargs[1].get("temperature") == 0.1

    @patch("litellm.acompletion", new_callable=AsyncMock, side_effect=Exception("API Error"))
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_propagates_llm_error(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.7,
                "max_tokens": 256,
                "description": "test",
            }
        )
        from app.llm.client import complete

        with pytest.raises(Exception, match="API Error"):
            await complete("test-agent", [{"role": "user", "content": "test"}])

    @patch("app.llm.client._LLM_EMPTY_RESPONSE_RETRY_DELAY_SEC", 0.05)
    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_retries_once_on_empty_response(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "light-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.2,
                "max_tokens": 256,
                "description": "Light agent",
            }
        )
        empty_choice = MagicMock()
        empty_choice.message.content = ""
        empty_choice.finish_reason = "length"
        empty_response = MagicMock(choices=[empty_choice])

        valid_choice = MagicMock()
        valid_choice.message.content = "Light is on!"
        valid_response = MagicMock(choices=[valid_choice])

        mock_acompletion.side_effect = [empty_response, valid_response]

        from app.llm.client import complete

        result = await complete("light-agent", [{"role": "user", "content": "turn on light"}])
        assert result == "Light is on!"
        assert mock_acompletion.await_count == 2

    @patch("app.llm.client._LLM_EMPTY_RESPONSE_RETRY_DELAY_SEC", 0.05)
    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_retries_once_on_whitespace_response(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "light-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.2,
                "max_tokens": 256,
                "description": "Light agent",
            }
        )
        whitespace_choice = MagicMock()
        whitespace_choice.message.content = "   \n\t  "
        whitespace_choice.finish_reason = "stop"
        whitespace_response = MagicMock(choices=[whitespace_choice])

        valid_choice = MagicMock()
        valid_choice.message.content = "Light is on!"
        valid_response = MagicMock(choices=[valid_choice])

        mock_acompletion.side_effect = [whitespace_response, valid_response]

        from app.llm.client import complete

        result = await complete("light-agent", [{"role": "user", "content": "turn on light"}])
        assert result == "Light is on!"
        assert mock_acompletion.await_count == 2

    @patch("app.llm.client._LLM_EMPTY_RESPONSE_RETRY_DELAY_SEC", 0.05)
    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_returns_empty_after_retry_exhausted(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "light-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.2,
                "max_tokens": 256,
                "description": "Light agent",
            }
        )
        empty_choice = MagicMock()
        empty_choice.message.content = ""
        empty_choice.finish_reason = "length"
        empty_response = MagicMock(choices=[empty_choice])

        mock_acompletion.side_effect = [empty_response, empty_response]

        from app.llm.client import complete

        with pytest.raises(ValueError, match="Empty LLM response"):
            await complete("light-agent", [{"role": "user", "content": "turn on light"}])
        assert mock_acompletion.await_count == 2


# ---------------------------------------------------------------------------
# LLM complete_with_tools function
# ---------------------------------------------------------------------------


class TestLLMReasoningEffort:
    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_passes_reasoning_effort(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "enabled": True,
                "model": "anthropic/claude-3-7-sonnet",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.2,
                "max_tokens": 1024,
                "description": "Test",
                "reasoning_effort": "low",
            }
        )
        choice = MagicMock()
        choice.message.content = "Done!"
        choice.finish_reason = "stop"
        mock_acompletion.return_value = MagicMock(choices=[choice], usage=None)

        from app.llm.client import complete

        await complete("test-agent", [{"role": "user", "content": "test"}])

        call_kwargs = mock_acompletion.call_args
        all_kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        assert all_kwargs.get("reasoning_effort") == "low"
        assert all_kwargs.get("drop_params") is True

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_omits_reasoning_effort_when_none(self, mock_repo, mock_params, mock_acompletion):
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "test-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.2,
                "max_tokens": 1024,
                "description": "Test",
                "reasoning_effort": None,
            }
        )
        choice = MagicMock()
        choice.message.content = "Done!"
        choice.finish_reason = "stop"
        mock_acompletion.return_value = MagicMock(choices=[choice], usage=None)

        from app.llm.client import complete

        await complete("test-agent", [{"role": "user", "content": "test"}])

        call_kwargs = mock_acompletion.call_args
        all_kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        assert "reasoning_effort" not in all_kwargs
        assert "drop_params" not in all_kwargs


class TestCompleteWithTools:
    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_returns_direct_answer_when_no_tool_calls(self, mock_repo, mock_params, mock_acompletion):
        """LLM responds without tool calls -- returns content directly."""
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "general-agent",
                "model": "groq/test",
                "max_tokens": 256,
                "temperature": 0.2,
            }
        )
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "The answer is 42"
        response.choices[0].message.tool_calls = None
        mock_acompletion.return_value = response

        from app.llm.client import complete_with_tools

        result = await complete_with_tools(
            "general-agent",
            [{"role": "user", "content": "test"}],
            tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}],
            tool_executor=AsyncMock(),
        )
        assert result == "The answer is 42"

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_executes_tool_and_returns_final_answer(self, mock_repo, mock_params, mock_acompletion):
        """LLM calls a tool, gets result, then gives final answer."""
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "general-agent",
                "model": "groq/test",
                "max_tokens": 256,
                "temperature": 0.2,
            }
        )
        # First call: LLM requests a tool call
        tool_call = MagicMock()
        tool_call.id = "call_123"
        tool_call.function.name = "web_search"
        tool_call.function.arguments = '{"query": "latest news"}'

        first_response = MagicMock()
        first_response.choices = [MagicMock()]
        first_response.choices[0].message.content = None
        first_response.choices[0].message.tool_calls = [tool_call]

        # Second call: LLM gives final answer
        second_response = MagicMock()
        second_response.choices = [MagicMock()]
        second_response.choices[0].message.content = "Here are the latest news..."
        second_response.choices[0].message.tool_calls = None

        mock_acompletion.side_effect = [first_response, second_response]

        tool_executor = AsyncMock(return_value='[{"title": "News", "url": "http://example.com"}]')

        from app.llm.client import complete_with_tools

        result = await complete_with_tools(
            "general-agent",
            [{"role": "user", "content": "what's the news?"}],
            tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
            tool_executor=tool_executor,
        )
        assert result == "Here are the latest news..."
        tool_executor.assert_awaited_once_with("web_search", {"query": "latest news"})

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_parallel_tool_calls_same_round(self, mock_repo, mock_params, mock_acompletion):
        """Several tool_calls in one assistant message execute concurrently."""
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "general-agent",
                "model": "groq/test",
                "max_tokens": 256,
                "temperature": 0.2,
            }
        )
        tc1 = MagicMock()
        tc1.id = "call_a"
        tc1.function.name = "tool_a"
        tc1.function.arguments = "{}"
        tc2 = MagicMock()
        tc2.id = "call_b"
        tc2.function.name = "tool_b"
        tc2.function.arguments = "{}"

        first_response = MagicMock()
        first_response.choices = [MagicMock()]
        first_response.choices[0].message.content = None
        first_response.choices[0].message.tool_calls = [tc1, tc2]

        second_response = MagicMock()
        second_response.choices = [MagicMock()]
        second_response.choices[0].message.content = "done"
        second_response.choices[0].message.tool_calls = None

        mock_acompletion.side_effect = [first_response, second_response]

        concurrent = 0
        max_conc: list[int] = [0]

        async def tool_executor(name, args):
            nonlocal concurrent
            concurrent += 1
            max_conc[0] = max(max_conc[0], concurrent)
            await asyncio.sleep(0.02)
            concurrent -= 1
            return f"result-{name}"

        from app.llm.client import complete_with_tools

        result = await complete_with_tools(
            "general-agent",
            [{"role": "user", "content": "test"}],
            tools=[{"type": "function", "function": {"name": "tool_a", "parameters": {}}}],
            tool_executor=tool_executor,
        )
        assert result == "done"
        assert max_conc[0] == 2

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_max_tool_rounds_prevents_infinite_loop(self, mock_repo, mock_params, mock_acompletion):
        """Exceeding max_tool_rounds forces a final answer."""
        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "general-agent",
                "model": "groq/test",
                "max_tokens": 256,
                "temperature": 0.2,
            }
        )
        # Every call returns a tool call (infinite loop scenario)
        tool_call = MagicMock()
        tool_call.id = "call_loop"
        tool_call.function.name = "web_search"
        tool_call.function.arguments = '{"query": "loop"}'

        loop_response = MagicMock()
        loop_response.choices = [MagicMock()]
        loop_response.choices[0].message.content = None
        loop_response.choices[0].message.tool_calls = [tool_call]

        final_response = MagicMock()
        final_response.choices = [MagicMock()]
        final_response.choices[0].message.content = "Forced answer"
        final_response.choices[0].message.tool_calls = None

        # max_tool_rounds=2: 2 tool calls + 1 forced final = 3 LLM calls
        mock_acompletion.side_effect = [loop_response, loop_response, final_response]

        from app.llm.client import complete_with_tools

        result = await complete_with_tools(
            "general-agent",
            [{"role": "user", "content": "test"}],
            tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
            tool_executor=AsyncMock(return_value="result"),
            max_tool_rounds=2,
        )
        assert result == "Forced answer"


# ---------------------------------------------------------------------------
# LLM provider span instrumentation
# ---------------------------------------------------------------------------


class TestLLMProviderSpans:
    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_creates_provider_span(self, mock_repo, mock_params, mock_acompletion):
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
        choice = MagicMock()
        choice.message.content = "Done!"
        mock_acompletion.return_value = MagicMock(choices=[choice], usage=None)

        collector = SpanCollector("trace-provider")
        from app.llm.client import complete

        result = await complete("light-agent", [{"role": "user", "content": "test"}], span_collector=collector)
        assert result == "Done!"
        prov_spans = [s for s in collector._spans if s["span_name"] == "llm_provider_call"]
        assert len(prov_spans) == 1
        assert prov_spans[0]["agent_id"] == "light-agent"
        assert prov_spans[0]["metadata"]["model"] == "openrouter/openai/gpt-4o-mini"

    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_works_without_span_collector(self, mock_repo, mock_params, mock_acompletion):
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
        choice = MagicMock()
        choice.message.content = "Done!"
        mock_acompletion.return_value = MagicMock(choices=[choice], usage=None)

        from app.llm.client import complete

        result = await complete("light-agent", [{"role": "user", "content": "test"}])
        assert result == "Done!"

    @patch("app.llm.client._LLM_EMPTY_RESPONSE_RETRY_DELAY_SEC", 0.05)
    @patch("litellm.acompletion", new_callable=AsyncMock)
    @patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
    @patch("app.llm.client.AgentConfigRepository")
    async def test_complete_creates_two_provider_spans_on_retry(self, mock_repo, mock_params, mock_acompletion):
        from app.analytics.tracer import SpanCollector

        mock_repo.get = AsyncMock(
            return_value={
                "agent_id": "light-agent",
                "enabled": True,
                "model": "openrouter/openai/gpt-4o-mini",
                "timeout": 5,
                "max_iterations": 3,
                "temperature": 0.2,
                "max_tokens": 256,
                "description": "Light agent",
            }
        )
        empty_choice = MagicMock()
        empty_choice.message.content = ""
        empty_choice.finish_reason = "length"
        empty_response = MagicMock(choices=[empty_choice], usage=None)

        valid_choice = MagicMock()
        valid_choice.message.content = "Light is on!"
        valid_response = MagicMock(choices=[valid_choice], usage=None)

        mock_acompletion.side_effect = [empty_response, valid_response]

        collector = SpanCollector("trace-provider-retry")
        from app.llm.client import complete

        result = await complete("light-agent", [{"role": "user", "content": "test"}], span_collector=collector)
        assert result == "Light is on!"
        prov_spans = [s for s in collector._spans if s["span_name"] == "llm_provider_call"]
        assert len(prov_spans) == 2
        assert prov_spans[1]["metadata"].get("retry") is True
