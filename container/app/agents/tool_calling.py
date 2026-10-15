"""Shared MCP tool-calling support for LLM-backed agents."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass
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


# Span-metadata ``server_name`` for in-process tools (they have no MCP server).
LOCAL_TOOL_SERVER_NAME = "local"

LocalToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class LocalTool:
    """An in-process tool offered to the LLM alongside an agent's MCP tools.

    ``parameters`` is a JSON-schema object; ``handler`` receives the parsed
    tool arguments and returns the text fed back to the LLM.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: LocalToolHandler

    def to_openai_schema(self) -> dict[str, Any]:
        """Return the tool definition in OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# (agent_id, tool name) pairs already warned about, so a colliding MCP tool
# logs once per process instead of on every turn.
_warned_tool_collisions: set[tuple[str, str]] = set()


def merge_local_tools(
    agent_id: str,
    mcp_tools: list[dict[str, Any]],
    local_tools: Sequence[LocalTool] | None,
) -> tuple[list[dict[str, Any]], dict[str, LocalTool]]:
    """Combine MCP tools with local tools; local names win on collision.

    Returns the MCP tools that stay available (colliding ones dropped, with a
    one-time warning) and the local tools keyed by name.
    """
    local_map = {tool.name: tool for tool in local_tools or ()}
    if not local_map:
        return list(mcp_tools), {}
    kept: list[dict[str, Any]] = []
    for tool in mcp_tools:
        name = tool.get("name")
        if name in local_map:
            key = (agent_id, name)
            if key not in _warned_tool_collisions:
                _warned_tool_collisions.add(key)
                logger.warning(
                    "MCP tool '%s' (server '%s') collides with a built-in tool of %s; the MCP tool is ignored",
                    name,
                    tool.get("_server_name", ""),
                    agent_id,
                )
            continue
        kept.append(tool)
    return kept, local_map


def build_tool_schemas(mcp_tools: list[dict[str, Any]], local_tools: dict[str, LocalTool]) -> list[dict[str, Any]]:
    """OpenAI tool schemas: local tools first (stable prefix), then MCP tools."""
    return [tool.to_openai_schema() for tool in local_tools.values()] + mcp_tools_to_openai_format(mcp_tools)


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
    local_tools: dict[str, LocalTool] | None = None,
) -> Callable[[str, dict], Awaitable[str]]:
    """Return the traced, size-bounded tool executor.

    Names in ``local_tools`` run in-process (they never touch Home Assistant
    entities); every other name dispatches to MCP behind the entity
    visibility guard.
    """
    agent_id = agent.agent_card.agent_id
    tool_map = {tool["name"]: tool for tool in mcp_tools}
    local_map = local_tools or {}

    async def execute_local_tool(tool: LocalTool, arguments: dict) -> str:
        try:
            text = await tool.handler(arguments or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Local tool '%s' failed for %s: %s", tool.name, agent_id, exc)
            text = f"Tool error: {exc}"
        return _truncate_string(str(text or ""), MCP_TOOL_RESULT_MAX_CHARS)

    async def execute_tool(name: str, arguments: dict) -> str:
        local_tool = local_map.get(name)
        if local_tool is not None:
            return await execute_local_tool(local_tool, arguments)
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
            is_local = name in local_map
            tool_info = tool_map.get(name) or {}
            tool_span["metadata"]["tool_name"] = name
            tool_span["metadata"]["server_name"] = (
                LOCAL_TOOL_SERVER_NAME if is_local else tool_info.get("_server_name", "")
            )
            tool_span["metadata"]["local"] = is_local
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
    local_tools: Sequence[LocalTool] | None = None,
    **overrides: Any,
) -> str:
    """Call an agent LLM with assigned MCP tools and traced tool execution.

    ``local_tools`` adds in-process tools (see :class:`LocalTool`); a local
    name shadows an MCP tool of the same name. The whole tool loop is
    bounded by the agent's dispatch budget (``deadline`` override, see
    :func:`app.llm.client.complete_with_tools`) unless the caller passes its
    own ``deadline``.
    """
    from app.llm.client import complete_with_tools

    messages = agent._normalize_llm_messages(messages)
    mcp_tools, local_map = merge_local_tools(agent.agent_card.agent_id, mcp_tools, local_tools)
    tool_schemas = build_tool_schemas(mcp_tools, local_map)
    executor = _build_tool_executor(
        agent,
        mcp_tools,
        mcp_tool_manager,
        span_collector=span_collector,
        include_tool_payload_metadata=include_tool_payload_metadata,
        local_tools=local_map,
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
    local_tools: Sequence[LocalTool] | None = None,
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
    mcp_tools, local_map = merge_local_tools(agent.agent_card.agent_id, mcp_tools, local_tools)
    tool_schemas = build_tool_schemas(mcp_tools, local_map)
    executor = _build_tool_executor(
        agent,
        mcp_tools,
        mcp_tool_manager,
        span_collector=span_collector,
        include_tool_payload_metadata=include_tool_payload_metadata,
        local_tools=local_map,
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
