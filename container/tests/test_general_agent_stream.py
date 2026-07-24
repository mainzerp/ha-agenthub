"""Tests for GeneralAgent.handle_task_stream (P0 first-frame latency)."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock litellm before importing any app modules that depend on it.
_litellm_mock = MagicMock()


class _AuthenticationError(Exception):
    pass


class _APIError(Exception):
    pass


_litellm_mock.exceptions.AuthenticationError = _AuthenticationError
_litellm_mock.exceptions.APIError = _APIError
sys.modules.setdefault("litellm", _litellm_mock)

import app.llm.client  # noqa: E402,F401
from app.agents.general import GeneralAgent  # noqa: E402
from app.models.agent import DispatchTask, TaskContext  # noqa: E402


def _make_task(text: str = "what is the weather") -> DispatchTask:
    return DispatchTask(
        description=text,
        conversation_id="conv-general-stream",
        context=TaskContext(language="en"),
    )


def _agent() -> GeneralAgent:
    return GeneralAgent()


class TestGeneralAgentStreamNoTools:
    @pytest.mark.asyncio
    async def test_tokens_relayed_incrementally_then_done(self):
        """No-tools path: complete_stream tokens are yielded as non-done
        chunks, followed by a single done chunk with an empty token."""
        agent = _agent()
        agent._get_mcp_tools = AsyncMock(return_value=[])

        async def _fake_stream(agent_id, messages, **overrides):
            yield "The "
            yield "weather "
            yield "is fine."

        with patch("app.llm.client.complete_stream", side_effect=_fake_stream):
            chunks = [c async for c in agent.handle_task_stream(_make_task())]

        token_chunks = [c for c in chunks if not c["done"]]
        assert [c["token"] for c in token_chunks] == ["The ", "weather ", "is fine."]
        done_chunks = [c for c in chunks if c["done"]]
        assert len(done_chunks) == 1
        assert done_chunks[0]["token"] == ""
        assert done_chunks[0]["conversation_id"] == "conv-general-stream"

    @pytest.mark.asyncio
    async def test_empty_stream_yields_canned_error_speech(self):
        """An LLM stream with zero tokens produces the canned error speech in
        a single done chunk (mirroring the base-class default wrapper)."""
        agent = _agent()
        agent._get_mcp_tools = AsyncMock(return_value=[])

        async def _empty_stream(agent_id, messages, **overrides):
            return
            yield  # pragma: no cover -- async generator shape

        with patch("app.llm.client.complete_stream", side_effect=_empty_stream):
            chunks = [c async for c in agent.handle_task_stream(_make_task())]

        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert "did not return a response" in chunks[0]["token"]

    @pytest.mark.asyncio
    async def test_failure_before_first_token_yields_canned_error(self):
        """An LLM failure before any token produces the canned error speech."""
        agent = _agent()
        agent._get_mcp_tools = AsyncMock(return_value=[])

        async def _failing_stream(agent_id, messages, **overrides):
            raise RuntimeError("provider down")
            yield  # pragma: no cover -- async generator shape

        with patch("app.llm.client.complete_stream", side_effect=_failing_stream):
            chunks = [c async for c in agent.handle_task_stream(_make_task())]

        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert "did not return a response" in chunks[0]["token"]

    @pytest.mark.asyncio
    async def test_mid_stream_failure_keeps_partial_output(self):
        """A failure after tokens were yielded keeps the partial answer and
        terminates with a plain done chunk (no canned error appended)."""
        agent = _agent()
        agent._get_mcp_tools = AsyncMock(return_value=[])

        async def _partial_stream(agent_id, messages, **overrides):
            yield "Partial answer."
            raise RuntimeError("stream broke")

        with patch("app.llm.client.complete_stream", side_effect=_partial_stream):
            chunks = [c async for c in agent.handle_task_stream(_make_task())]

        token_chunks = [c for c in chunks if not c["done"]]
        assert [c["token"] for c in token_chunks] == ["Partial answer."]
        done_chunks = [c for c in chunks if c["done"]]
        assert len(done_chunks) == 1
        assert done_chunks[0]["token"] == ""


class TestGeneralAgentStreamWithTools:
    @pytest.mark.asyncio
    async def test_tool_loop_streams_final_round_tokens(self):
        """With MCP tools assigned, the streaming tool loop relays the final
        no-tools round tokens incrementally."""
        agent = _agent()
        tools = [{"name": "web_search", "description": "search", "input_schema": {}, "_server_name": "srv"}]
        agent._get_mcp_tools = AsyncMock(return_value=tools)

        async def _fake_tools_stream(self_agent, messages, mcp_tools, manager, **kwargs):
            yield "Found "
            yield "it."

        with patch(
            "app.agents.general.call_llm_with_mcp_tools_stream",
            side_effect=_fake_tools_stream,
        ):
            chunks = [c async for c in agent.handle_task_stream(_make_task())]

        token_chunks = [c for c in chunks if not c["done"]]
        assert [c["token"] for c in token_chunks] == ["Found ", "it."]
        done_chunks = [c for c in chunks if c["done"]]
        assert len(done_chunks) == 1
        assert done_chunks[0]["token"] == ""

    @pytest.mark.asyncio
    async def test_tool_loop_failure_yields_canned_error(self):
        """A failing streaming tool loop before any token produces the canned
        error speech."""
        agent = _agent()
        tools = [{"name": "web_search", "description": "search", "input_schema": {}, "_server_name": "srv"}]
        agent._get_mcp_tools = AsyncMock(return_value=tools)

        async def _failing_tools_stream(self_agent, messages, mcp_tools, manager, **kwargs):
            raise RuntimeError("tool loop broke")
            yield  # pragma: no cover -- async generator shape

        with patch(
            "app.agents.general.call_llm_with_mcp_tools_stream",
            side_effect=_failing_tools_stream,
        ):
            chunks = [c async for c in agent.handle_task_stream(_make_task())]

        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert "did not return a response" in chunks[0]["token"]
