"""Shared MCP tool-calling support for LLM-backed agents."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from app.agents.base import BaseAgent
from app.analytics.tracer import _optional_span, sanitize_trace_value

logger = logging.getLogger(__name__)


def mcp_tools_to_openai_format(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert MCP tool descriptors to OpenAI function-calling format."""
    openai_tools: list[dict[str, Any]] = []
    for tool in mcp_tools:
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
        )
    return openai_tools


def _payload_char_count(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False))
    except Exception:
        return len(str(value))


def _truncate_string(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


async def call_llm_with_mcp_tools(
    agent: BaseAgent,
    messages: list[dict[str, Any]],
    mcp_tools: list[dict[str, Any]],
    mcp_tool_manager: Any,
    *,
    span_collector=None,
    include_tool_payload_metadata: bool = True,
    **overrides: Any,
) -> str:
    """Call an agent LLM with assigned MCP tools and traced tool execution."""
    from app.llm.client import complete_with_tools

    agent_id = agent.agent_card.agent_id
    messages = agent._normalize_llm_messages(messages)
    tool_schemas = mcp_tools_to_openai_format(mcp_tools)
    tool_map = {tool["name"]: tool for tool in mcp_tools}

    async def execute_tool(name: str, arguments: dict) -> str:
        tool_info = tool_map.get(name)
        if not tool_info:
            return f"Error: unknown tool '{name}'"
        server_name = tool_info.get("_server_name", "")
        try:
            result = await mcp_tool_manager.call_tool(server_name, name, arguments)
            if hasattr(result, "content"):
                texts = [content.text for content in result.content if hasattr(content, "text")]
                return "\n".join(texts) if texts else str(result)
            return str(result)
        except Exception as exc:
            logger.warning("MCP tool '%s' failed for %s: %s", name, agent_id, exc)
            return f"Tool error: {exc}"

    async def traced_executor(name: str, arguments: dict) -> str:
        async with _optional_span(span_collector, "mcp_tool_call", agent_id=agent_id) as tool_span:
            tool_info = tool_map.get(name) or {}
            tool_span["metadata"]["tool_name"] = name
            tool_span["metadata"]["server_name"] = tool_info.get("_server_name", "")
            tool_span["metadata"]["argument_keys"] = sorted(str(key) for key in (arguments or {}))
            tool_span["metadata"]["argument_chars"] = _payload_char_count(arguments or {})
            if include_tool_payload_metadata:
                tool_span["metadata"]["arguments"] = sanitize_trace_value(arguments or {})
            result = await execute_tool(name, arguments)
            tool_span["metadata"]["result_chars"] = len(result or "")
            if include_tool_payload_metadata:
                tool_span["metadata"]["result"] = sanitize_trace_value(result or "")
            return result

    return await complete_with_tools(
        agent_id,
        messages,
        tools=tool_schemas,
        tool_executor=traced_executor,
        span_collector=span_collector,
        **overrides,
    )


async def call_llm_with_mcp_tools_stream(
    agent: BaseAgent,
    messages: list[dict[str, Any]],
    mcp_tools: list[dict[str, Any]],
    mcp_tool_manager: Any,
    *,
    span_collector=None,
    include_tool_payload_metadata: bool = True,
    **overrides: Any,
) -> AsyncGenerator[str, None]:
    """Streaming variant of :func:`call_llm_with_mcp_tools`.

    Yields LLM content tokens as they arrive (see
    :func:`app.llm.client.complete_with_tools_stream` for the round
    semantics); tool execution and tracing are identical to the
    non-streaming variant.
    """
    from app.llm.client import complete_with_tools_stream

    agent_id = agent.agent_card.agent_id
    messages = agent._normalize_llm_messages(messages)
    tool_schemas = mcp_tools_to_openai_format(mcp_tools)
    tool_map = {tool["name"]: tool for tool in mcp_tools}

    async def execute_tool(name: str, arguments: dict) -> str:
        tool_info = tool_map.get(name)
        if not tool_info:
            return f"Error: unknown tool '{name}'"
        server_name = tool_info.get("_server_name", "")
        try:
            result = await mcp_tool_manager.call_tool(server_name, name, arguments)
            if hasattr(result, "content"):
                texts = [content.text for content in result.content if hasattr(content, "text")]
                return "\n".join(texts) if texts else str(result)
            return str(result)
        except Exception as exc:
            logger.warning("MCP tool '%s' failed for %s: %s", name, agent_id, exc)
            return f"Tool error: {exc}"

    async def traced_executor(name: str, arguments: dict) -> str:
        async with _optional_span(span_collector, "mcp_tool_call", agent_id=agent_id) as tool_span:
            tool_info = tool_map.get(name) or {}
            tool_span["metadata"]["tool_name"] = name
            tool_span["metadata"]["server_name"] = tool_info.get("_server_name", "")
            tool_span["metadata"]["argument_keys"] = sorted(str(key) for key in (arguments or {}))
            tool_span["metadata"]["argument_chars"] = _payload_char_count(arguments or {})
            if include_tool_payload_metadata:
                tool_span["metadata"]["arguments"] = sanitize_trace_value(arguments or {})
            result = await execute_tool(name, arguments)
            tool_span["metadata"]["result_chars"] = len(result or "")
            if include_tool_payload_metadata:
                tool_span["metadata"]["result"] = sanitize_trace_value(result or "")
            return result

    async for token in complete_with_tools_stream(
        agent_id,
        messages,
        tools=tool_schemas,
        tool_executor=traced_executor,
        span_collector=span_collector,
        **overrides,
    ):
        yield token
