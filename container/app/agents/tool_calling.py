"""Shared MCP tool-calling support for LLM-backed agents."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from app.agents.base import BaseAgent
from app.analytics.tracer import _optional_span, sanitize_trace_value
from app.entity.visibility import _index_has_async_get_by_id, entity_is_visible

logger = logging.getLogger(__name__)

# Upper bound for one MCP tool result fed back into the LLM conversation.
# MCP servers can return arbitrarily large payloads (full state dumps, web
# pages); an unbounded result blows the context window and the token bill.
MCP_TOOL_RESULT_MAX_CHARS = 8000
_TRUNCATION_MARKER = "\n[tool result truncated]"

# Entity-id-shaped tokens inside tool arguments (``light.kitchen``). Only
# tokens that exist in the entity index are treated as entity references.
_ENTITY_ID_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9_]*\.[a-z0-9_]+\b")
_MAX_GUARDED_ENTITY_REFS = 50

_HIDDEN_ENTITY_TOOL_ERROR = (
    "Error: tool call rejected. It references a Home Assistant entity this agent is not allowed to access."
)


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
    """Cap a string at ``limit`` chars (marker included); non-strings pass through."""
    if isinstance(value, str) and len(value) > limit:
        keep = max(0, limit - len(_TRUNCATION_MARKER))
        return value[:keep] + _TRUNCATION_MARKER
    return value


def _collect_strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                out.append(key)
            _collect_strings(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_strings(item, out)


async def _index_has_entity(entity_index: Any, entity_id: str) -> bool:
    try:
        if _index_has_async_get_by_id(entity_index):
            return await entity_index.get_by_id_async(entity_id) is not None
        return entity_index.get_by_id(entity_id) is not None
    except Exception:
        logger.debug("Entity index lookup failed for %s", entity_id, exc_info=True)
        return False


async def find_hidden_entity_references(agent_id: str, arguments: Any, entity_index: Any) -> list[str]:
    """Return entity ids referenced in ``arguments`` that ``agent_id`` may not see.

    Best-effort guard for MCP tools that act on Home Assistant directly
    (Prime Directive 5): every entity-id-shaped token in the (nested) tool
    arguments that exists in the entity index is checked against the
    agent's visibility rules. Name-based arguments ("kitchen light") cannot
    be mapped reliably and are NOT covered -- see docs/plugin-development.md.
    Without an entity index the guard cannot tell entity ids from other
    dotted tokens and returns no findings.
    """
    if entity_index is None or not arguments:
        return []
    strings: list[str] = []
    _collect_strings(arguments, strings)
    candidates: list[str] = []
    for text in strings:
        for token in _ENTITY_ID_TOKEN_RE.findall(text.lower()):
            if token not in candidates:
                candidates.append(token)
            if len(candidates) >= _MAX_GUARDED_ENTITY_REFS:
                break
    hidden: list[str] = []
    for entity_id in candidates:
        if not await _index_has_entity(entity_index, entity_id):
            continue
        try:
            visible = await entity_is_visible(agent_id, entity_id, entity_index)
        except Exception:
            logger.warning("Visibility check failed for %s; rejecting tool call (fail-closed)", entity_id)
            visible = False
        if not visible:
            hidden.append(entity_id)
    return hidden


async def _resolve_tool_loop_deadline(agent: BaseAgent, overrides: dict[str, Any]) -> None:
    """Default the whole-loop ``deadline`` override to the agent's dispatch budget."""
    if overrides.get("deadline") is not None:
        return
    resolver = getattr(agent, "_resolve_dispatch_budget_sec", None)
    if not callable(resolver):
        return
    budget = await resolver()
    if budget is not None:
        overrides["deadline"] = time.monotonic() + budget


def _build_tool_executor(
    agent: BaseAgent,
    mcp_tools: list[dict[str, Any]],
    mcp_tool_manager: Any,
    *,
    span_collector,
    include_tool_payload_metadata: bool,
) -> Callable[[str, dict], Awaitable[str]]:
    """Return the traced, visibility-guarded, size-bounded MCP tool executor."""
    agent_id = agent.agent_card.agent_id
    tool_map = {tool["name"]: tool for tool in mcp_tools}

    async def execute_tool(name: str, arguments: dict) -> str:
        tool_info = tool_map.get(name)
        if not tool_info:
            return f"Error: unknown tool '{name}'"
        server_name = tool_info.get("_server_name", "")
        hidden = await find_hidden_entity_references(agent_id, arguments, getattr(agent, "_entity_index", None))
        if hidden:
            logger.warning(
                "Rejected MCP tool '%s' for %s: references %d hidden entit%s",
                name,
                agent_id,
                len(hidden),
                "y" if len(hidden) == 1 else "ies",
            )
            return _HIDDEN_ENTITY_TOOL_ERROR
        try:
            result = await mcp_tool_manager.call_tool(server_name, name, arguments)
            if hasattr(result, "content"):
                texts = [content.text for content in result.content if hasattr(content, "text")]
                text = "\n".join(texts) if texts else str(result)
            else:
                text = str(result)
        except Exception as exc:
            logger.warning("MCP tool '%s' failed for %s: %s", name, agent_id, exc)
            text = f"Tool error: {exc}"
        return _truncate_string(text, MCP_TOOL_RESULT_MAX_CHARS)

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

    return traced_executor


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
    """Call an agent LLM with assigned MCP tools and traced tool execution.

    The whole tool loop is bounded by the agent's dispatch budget (``deadline``
    override, see :func:`app.llm.client.complete_with_tools`) unless the
    caller passes its own ``deadline``.
    """
    from app.llm.client import complete_with_tools

    messages = agent._normalize_llm_messages(messages)
    tool_schemas = mcp_tools_to_openai_format(mcp_tools)
    executor = _build_tool_executor(
        agent,
        mcp_tools,
        mcp_tool_manager,
        span_collector=span_collector,
        include_tool_payload_metadata=include_tool_payload_metadata,
    )
    await _resolve_tool_loop_deadline(agent, overrides)
    return await complete_with_tools(
        agent.agent_card.agent_id,
        messages,
        tools=tool_schemas,
        tool_executor=executor,
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
    semantics); tool execution, guarding and tracing are identical to the
    non-streaming variant.
    """
    from app.llm.client import complete_with_tools_stream

    messages = agent._normalize_llm_messages(messages)
    tool_schemas = mcp_tools_to_openai_format(mcp_tools)
    executor = _build_tool_executor(
        agent,
        mcp_tools,
        mcp_tool_manager,
        span_collector=span_collector,
        include_tool_payload_metadata=include_tool_payload_metadata,
    )
    await _resolve_tool_loop_deadline(agent, overrides)
    async for token in complete_with_tools_stream(
        agent.agent_card.agent_id,
        messages,
        tools=tool_schemas,
        tool_executor=executor,
        span_collector=span_collector,
        **overrides,
    ):
        yield token
