"""Tests for app.llm.client complete_with_tools_stream (P0 first-frame latency)."""

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

from app.llm.client import complete_with_tools_stream  # noqa: E402

_AGENT_CONFIG = {
    "agent_id": "general-agent",
    "enabled": True,
    "model": "openrouter/openai/gpt-4o-mini",
    "timeout": 5,
    "max_iterations": 3,
    "temperature": 0.7,
    "max_tokens": 256,
    "description": "General agent",
}

_TOOLS = [
    {
        "type": "function",
        "function": {"name": "web_search", "description": "search the web", "parameters": {}},
    }
]


class _FakeFunctionDelta:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _FakeToolCallDelta:
    def __init__(self, index=0, tc_id=None, name=None, arguments=None):
        self.index = index
        self.id = tc_id
        self.function = _FakeFunctionDelta(name, arguments)


class _FakeDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, delta, finish_reason=None):
        self.choices = [_FakeChoice(delta, finish_reason)]


def _content_chunk(content, finish_reason=None):
    return _FakeChunk(_FakeDelta(content=content), finish_reason)


def _tool_call_chunk(tc_id=None, name=None, arguments=None, finish_reason=None):
    return _FakeChunk(
        _FakeDelta(tool_calls=[_FakeToolCallDelta(index=0, tc_id=tc_id, name=name, arguments=arguments)]),
        finish_reason,
    )


async def _async_iter(items):
    for item in items:
        yield item


def _patch_config(mock_repo):
    mock_repo.get = AsyncMock(return_value=dict(_AGENT_CONFIG))


@pytest.mark.asyncio
@patch("app.llm.client.track_token_usage", new_callable=AsyncMock)
@patch("litellm.acompletion", new_callable=AsyncMock)
@patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
@patch("app.llm.client.AgentConfigRepository")
async def test_final_round_without_tool_calls_streams_tokens(mock_repo, mock_params, mock_acompletion, mock_track):
    """A first round without tool calls is the final answer; its tokens are
    streamed and the tool executor never runs."""
    _patch_config(mock_repo)
    mock_acompletion.return_value = _async_iter(
        [
            _content_chunk("Hello "),
            _content_chunk("there.", finish_reason="stop"),
        ]
    )
    executor = AsyncMock(return_value="tool result")

    tokens = [
        t
        async for t in complete_with_tools_stream(
            "general-agent",
            [{"role": "user", "content": "hi"}],
            tools=_TOOLS,
            tool_executor=executor,
        )
    ]

    assert tokens == ["Hello ", "there."]
    executor.assert_not_called()
    assert mock_acompletion.await_count == 1
    call_kwargs = mock_acompletion.await_args.kwargs
    assert call_kwargs["stream"] is True
    assert call_kwargs["tools"] == _TOOLS


@pytest.mark.asyncio
@patch("app.llm.client.track_token_usage", new_callable=AsyncMock)
@patch("litellm.acompletion", new_callable=AsyncMock)
@patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
@patch("app.llm.client.AgentConfigRepository")
async def test_tool_round_executes_then_streams_final_answer(mock_repo, mock_params, mock_acompletion, mock_track):
    """Round 1 returns tool calls (reconstructed from stream deltas), the
    tool executes, and round 2 streams the final answer. The assistant
    history keeps the reconstructed tool_calls and the tool result."""
    _patch_config(mock_repo)
    mock_acompletion.side_effect = [
        _async_iter(
            [
                _tool_call_chunk(tc_id="call_1", name="web_search"),
                _tool_call_chunk(arguments='{"query":'),
                _tool_call_chunk(arguments=' "weather"}', finish_reason="tool_calls"),
            ]
        ),
        _async_iter(
            [
                _content_chunk("It is "),
                _content_chunk("sunny.", finish_reason="stop"),
            ]
        ),
    ]
    executor = AsyncMock(return_value="sunny, 22C")

    tokens = [
        t
        async for t in complete_with_tools_stream(
            "general-agent",
            [{"role": "user", "content": "weather?"}],
            tools=_TOOLS,
            tool_executor=executor,
        )
    ]

    assert tokens == ["It is ", "sunny."]
    executor.assert_awaited_once_with("web_search", {"query": "weather"})

    assert mock_acompletion.await_count == 2
    second_messages = mock_acompletion.await_args_list[1].kwargs["messages"]
    assistant_msg = second_messages[-2]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["tool_calls"][0]["id"] == "call_1"
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "web_search"
    assert assistant_msg["tool_calls"][0]["function"]["arguments"] == '{"query": "weather"}'
    tool_msg = second_messages[-1]
    assert tool_msg == {"role": "tool", "tool_call_id": "call_1", "content": "sunny, 22C"}


@pytest.mark.asyncio
@patch("app.llm.client.track_token_usage", new_callable=AsyncMock)
@patch("litellm.acompletion", new_callable=AsyncMock)
@patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={})
@patch("app.llm.client.AgentConfigRepository")
async def test_max_tool_rounds_forces_streamed_final_without_tools(
    mock_repo, mock_params, mock_acompletion, mock_track
):
    """Exhausting the tool rounds forces a final streamed round without tools."""
    _patch_config(mock_repo)

    def _tool_round():
        return _async_iter(
            [
                _tool_call_chunk(tc_id="call_1", name="web_search", arguments="{}", finish_reason="tool_calls"),
            ]
        )

    mock_acompletion.side_effect = [
        _tool_round(),
        _tool_round(),
        _async_iter([_content_chunk("Forced final.", finish_reason="stop")]),
    ]
    executor = AsyncMock(return_value="ok")

    tokens = [
        t
        async for t in complete_with_tools_stream(
            "general-agent",
            [{"role": "user", "content": "hi"}],
            tools=_TOOLS,
            tool_executor=executor,
            max_tool_rounds=2,
        )
    ]

    assert tokens == ["Forced final."]
    assert mock_acompletion.await_count == 3
    final_kwargs = mock_acompletion.await_args_list[2].kwargs
    assert "tools" not in final_kwargs
