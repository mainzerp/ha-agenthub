"""#132: LLM client deadlines, bounded retries, max_iterations and strict tool names."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_CONFIG = {
    "agent_id": "general-agent",
    "model": "groq/test",
    "timeout": 5,
    "max_tokens": 256,
    "temperature": 0.2,
}


class _ProviderTimeoutError(Exception):
    """Stand-in for litellm.exceptions.Timeout."""


def _text_response(text: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = text
    response.choices[0].message.tool_calls = None
    response.choices[0].finish_reason = "stop"
    response.usage = None
    return response


def _tool_call_response(name: str, call_id: str = "call_1", arguments: str = "{}") -> MagicMock:
    tool_call = MagicMock()
    tool_call.id = call_id
    tool_call.function.name = name
    tool_call.function.arguments = arguments
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = None
    response.choices[0].message.tool_calls = [tool_call]
    response.choices[0].message.refusal = None
    response.usage = None
    return response


def _tools(*names: str) -> list[dict]:
    return [{"type": "function", "function": {"name": name, "parameters": {}}} for name in names]


@pytest.fixture
def llm_env():
    with (
        patch("litellm.acompletion", new_callable=AsyncMock) as acompletion,
        patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={}),
        patch("app.llm.client.AgentConfigRepository") as repo,
        patch("app.llm.client.LiteLLMTimeout", _ProviderTimeoutError),
    ):
        repo.get = AsyncMock(return_value=dict(_CONFIG))
        yield acompletion, repo


class TestStrictToolNames:
    async def test_near_miss_tool_name_is_not_executed(self, llm_env):
        """A misspelled tool name must not be remapped to (and run as) another tool."""
        acompletion, _repo = llm_env
        acompletion.side_effect = [_tool_call_response("web_seach"), _text_response("ok")]
        tool_executor = AsyncMock(return_value="result")

        from app.llm.client import complete_with_tools

        result = await complete_with_tools(
            "general-agent",
            [{"role": "user", "content": "q"}],
            tools=_tools("web_search", "wikipedia_search"),
            tool_executor=tool_executor,
        )
        assert result == "ok"
        tool_executor.assert_not_awaited()
        tool_messages = [m for m in acompletion.call_args_list[1].kwargs["messages"] if m.get("role") == "tool"]
        assert "invalid tool name 'web_seach'" in tool_messages[0]["content"]


class TestMaxIterations:
    async def test_agent_max_iterations_bounds_tool_rounds(self, llm_env):
        """max_tool_rounds defaults to AgentConfig.max_iterations (was hard-coded to 5)."""
        acompletion, repo = llm_env
        repo.get = AsyncMock(return_value={**_CONFIG, "max_iterations": 2})
        acompletion.side_effect = [
            _tool_call_response("web_search", "c1"),
            _tool_call_response("web_search", "c2"),
            _text_response("forced"),
        ]

        from app.llm.client import complete_with_tools

        result = await complete_with_tools(
            "general-agent",
            [{"role": "user", "content": "q"}],
            tools=_tools("web_search"),
            tool_executor=AsyncMock(return_value="r"),
        )
        assert result == "forced"
        assert acompletion.await_count == 3
        assert "tools" not in acompletion.call_args_list[2].kwargs


class TestToolLoopDeadline:
    async def test_deadline_stops_tool_rounds_and_forces_final(self, llm_env):
        acompletion, repo = llm_env
        repo.get = AsyncMock(return_value={**_CONFIG, "max_iterations": 5})
        acompletion.side_effect = [_tool_call_response("web_search"), _text_response("final")]

        from app.llm.client import complete_with_tools

        # 1.5s budget: round 1 runs, then less than the 1s minimum remains
        # once the tool has taken 0.6s -> forced final answer.
        async def slow_tool(name, args):
            await asyncio.sleep(0.6)
            return "r"

        result = await complete_with_tools(
            "general-agent",
            [{"role": "user", "content": "q"}],
            tools=_tools("web_search"),
            tool_executor=slow_tool,
            deadline=time.monotonic() + 1.5,
        )
        assert result == "final"
        assert acompletion.await_count == 2
        # Every provider timeout is clamped to the remaining budget.
        for call in acompletion.call_args_list:
            assert call.kwargs["timeout"] <= 1.5

    async def test_tool_call_bounded_by_deadline(self, llm_env):
        acompletion, _repo = llm_env
        acompletion.side_effect = [_tool_call_response("web_search"), _text_response("final")]

        async def hanging_tool(name, args):
            await asyncio.sleep(30)
            return "never"

        from app.llm.client import complete_with_tools

        started = time.monotonic()
        result = await complete_with_tools(
            "general-agent",
            [{"role": "user", "content": "q"}],
            tools=_tools("web_search"),
            tool_executor=hanging_tool,
            deadline=time.monotonic() + 1.2,
        )
        assert result == "final"
        assert time.monotonic() - started < 5
        tool_messages = [m for m in acompletion.call_args_list[1].kwargs["messages"] if m.get("role") == "tool"]
        assert "did not finish" in tool_messages[0]["content"]

    async def test_exhausted_deadline_raises_instead_of_calling_provider(self, llm_env):
        acompletion, _repo = llm_env

        from app.llm.client import LLMDeadlineExceededError, complete_with_tools

        with pytest.raises(LLMDeadlineExceededError):
            await complete_with_tools(
                "general-agent",
                [{"role": "user", "content": "q"}],
                tools=_tools("web_search"),
                tool_executor=AsyncMock(),
                deadline=time.monotonic() - 1,
            )
        acompletion.assert_not_awaited()


class TestCompleteTimeoutRetry:
    async def test_timeout_retry_still_happens_without_deadline(self, llm_env):
        acompletion, _repo = llm_env
        acompletion.side_effect = [_ProviderTimeoutError("slow"), _text_response("ok")]

        from app.llm.client import complete

        with patch("app.llm.client._LLM_TIMEOUT_RETRY_DELAY_SEC", 0.0):
            result = await complete("general-agent", [{"role": "user", "content": "q"}])
        assert result == "ok"
        assert acompletion.await_count == 2

    async def test_retry_on_timeout_false_raises_after_first_timeout(self, llm_env):
        acompletion, _repo = llm_env
        acompletion.side_effect = [_ProviderTimeoutError("slow"), _text_response("late")]

        from app.llm.client import complete

        with pytest.raises(_ProviderTimeoutError):
            await complete("light-agent", [{"role": "user", "content": "q"}], retry_on_timeout=False)
        assert acompletion.await_count == 1

    async def test_timeout_retry_skipped_when_deadline_has_no_budget(self, llm_env):
        """First timeout + 2s backoff + a full retry would cross the dispatch budget."""
        acompletion, _repo = llm_env
        acompletion.side_effect = [_ProviderTimeoutError("slow"), _text_response("late")]

        from app.llm.client import complete

        with pytest.raises(_ProviderTimeoutError):
            await complete(
                "light-agent",
                [{"role": "user", "content": "q"}],
                deadline=time.monotonic() + 2.5,
            )
        assert acompletion.await_count == 1
        assert acompletion.call_args.kwargs["timeout"] <= 2.5

    async def test_empty_response_retry_skipped_when_deadline_has_no_budget(self, llm_env):
        acompletion, _repo = llm_env
        acompletion.side_effect = [_text_response(""), _text_response("late")]

        from app.llm.client import complete

        with pytest.raises(ValueError, match="without retry budget"):
            await complete(
                "light-agent",
                [{"role": "user", "content": "q"}],
                deadline=time.monotonic() + 1.5,
            )
        assert acompletion.await_count == 1
