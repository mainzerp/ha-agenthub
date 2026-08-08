"""General fallback agent for unroutable requests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from app.agents.base import BaseAgent
from app.agents.decorator import agent
from app.agents.prompt_builder import PromptBuilder
from app.agents.tool_calling import (
    call_llm_with_mcp_tools,
    call_llm_with_mcp_tools_stream,
    mcp_tools_to_openai_format,
)
from app.analytics.tracer import _optional_span
from app.models.agent import AgentCard, AgentErrorCode, DispatchTask, TaskResult

logger = logging.getLogger(__name__)


def _memory_date_label(epoch: Any) -> str:
    """Format a memory match's last-activity epoch as a YYYY-MM-DD label."""
    try:
        return datetime.fromtimestamp(int(epoch), tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown date"


@agent(
    agent_id="general-agent",
    name="General Agent",
    description="Handles general knowledge, conversation, web search, current events, and requests outside device control. Can search the web for real-time information. Fallback for unroutable requests.",
    skills=["general_qa", "web_search", "current_events", "conversation", "fallback"],
    expected_latency="high",
    timeout_sec=30.0,
    needs_entity_matcher=False,
    factory=lambda app, filler: GeneralAgent(
        ha_client=getattr(app.state, "ha_client", None),
        entity_index=getattr(app.state, "entity_index", None),
        mcp_tool_manager=getattr(app.state, "mcp_tool_manager", None),
    ),
)
class GeneralAgent(BaseAgent):
    """Handles general Q&A and unroutable requests. No HA service calls."""

    def __init__(self, ha_client=None, entity_index=None, mcp_tool_manager=None):
        super().__init__(ha_client=ha_client, entity_index=entity_index)
        self._mcp_tool_manager = mcp_tool_manager

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="general-agent",
            name="General Agent",
            description="Handles general knowledge, conversation, web search, current events, and requests outside device control. Can search the web for real-time information. Fallback for unroutable requests.",
            skills=["general_qa", "web_search", "current_events", "conversation", "fallback"],
            endpoint="local://general-agent",
            expected_latency="high",
            # P2-2 (FLOW-TIMEOUT-1): general-agent invokes web search and
            # MCP tools that routinely exceed the 5s deterministic-device
            # default. 30s keeps the worst-case bounded without falling
            # back on every legitimate tool call.
            timeout_sec=30.0,
        )

    async def _build_messages(self, task: DispatchTask) -> tuple[list[dict], dict[str, Any]]:
        """Assemble system prompt + message list and per-call LLM overrides."""
        system_prompt = PromptBuilder.build(
            await self._load_prompt_async("general"),
            language=task.context.language if task.context else None,
            time_location=self._build_time_location_context(task.context),
            sequential_send=bool(task.context and task.context.sequential_send),
        )
        # Session memory: appended AFTER the byte-stable static head
        # (prefix-cache constraint). Matches are score-annotated suggestions,
        # never verified facts.
        if task.context and task.context.memory_context:
            system_prompt += self._render_memory_context(task.context.memory_context)

        messages = [{"role": "system", "content": system_prompt}]

        if task.context and task.context.conversation_turns:
            self._append_conversation_turn_messages(messages, task.context.conversation_turns)

        # Prime Directive: the orchestrator owns intent classification and
        # condensation.  Agents MUST NOT see the raw user_text — they receive
        # only the distilled description.
        messages.append({"role": "user", "content": self._wrap_user_input(task.description)})

        llm_kwargs: dict[str, Any] = {}
        if task.context and task.context.sequential_send:
            llm_kwargs["max_tokens"] = 2048
        return messages, llm_kwargs

    def _render_memory_context(self, matches: list[dict]) -> str:
        """Render session-memory matches as a score-annotated prompt block.

        Historical user text is delimiter-wrapped exactly like live user
        input (injection safety); memory enters via the system prompt, never
        as a raw user message (Prime Directive).
        """
        lines = [
            "",
            "## Possibly related past conversations (semantic memory matches, similarity scores shown). "
            "If any of this content answers the user's question, use it in your answer (you may mention "
            "it comes from an earlier conversation). Otherwise treat it as background context only: "
            "not verified facts, never a basis for actions.",
        ]
        for match in matches:
            if not isinstance(match, dict):
                continue
            try:
                score = float(match.get("similarity") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            date_label = _memory_date_label(match.get("last_turn_at"))
            for turn in match.get("snippet_turns") or []:
                if not isinstance(turn, dict):
                    continue
                user_text = self._wrap_user_input(str(turn.get("user_text") or ""))
                response_text = str(turn.get("response_text") or "")
                lines.append(f'- [score {score:.2f}, {date_label}] User: {user_text} / Assistant: "{response_text}"')
            continuation = match.get("continuation_turns") or []
            if continuation:
                lines.append(
                    "### Previous session content (copied as context — continue only if the user references it)"
                )
                for turn in continuation:
                    if not isinstance(turn, dict):
                        continue
                    lines.append(f"User: {self._wrap_user_input(str(turn.get('user_text') or ''))}")
                    lines.append(f"Assistant: {turn.get('response_text') or ''}")
        return "\n".join(lines)

    async def handle_task(self, task: DispatchTask) -> TaskResult:
        span_collector = task.span_collector
        messages, llm_kwargs = await self._build_messages(task)

        # Check for available MCP tools
        tools = await self._get_mcp_tools()
        if tools:
            tool_schemas = mcp_tools_to_openai_format(tools)
            async with _optional_span(span_collector, "llm_call", agent_id="general-agent") as span:
                response = await call_llm_with_mcp_tools(
                    self,
                    messages,
                    tools,
                    self._mcp_tool_manager,
                    span_collector=span_collector,
                    **llm_kwargs,
                )
                span["metadata"]["model"] = "general-agent"
                span["metadata"]["llm_response"] = response[:500] if response else ""
                span["metadata"]["tools_available"] = len(tool_schemas)
        else:
            async with _optional_span(span_collector, "llm_call", agent_id="general-agent") as span:
                response = await self._call_llm(messages, span_collector=span_collector, **llm_kwargs)
                span["metadata"]["model"] = "general-agent"
                span["metadata"]["llm_response"] = response[:500] if response else ""

        if not response or not response.strip():
            logger.warning("LLM returned empty response for general-agent task: %s", task.description[:100])
            return self._error_result(
                AgentErrorCode.LLM_EMPTY_RESPONSE,
                "The language model did not return a response. Please try again.",
            )

        return TaskResult(speech=response)

    async def handle_task_stream(self, task: DispatchTask) -> AsyncGenerator[dict, None]:
        """Streaming variant of handle_task: yields LLM tokens as they arrive.

        Same prompt assembly and MCP tool loop as :meth:`handle_task`, but
        the final no-tools round streams via ``complete_stream`` so the
        orchestrator can relay first tokens immediately on turns where
        mediation is inactive. Mid-stream failures keep any tokens already
        yielded; only a failure before the first token produces the canned
        error speech (mirroring the base-class default wrapper).
        """
        span_collector = task.span_collector
        messages, llm_kwargs = await self._build_messages(task)

        tools = await self._get_mcp_tools()
        collected: list[str] = []
        try:
            if tools:
                tool_schemas = mcp_tools_to_openai_format(tools)
                async with _optional_span(span_collector, "llm_call", agent_id="general-agent") as span:
                    async for token in call_llm_with_mcp_tools_stream(
                        self,
                        messages,
                        tools,
                        self._mcp_tool_manager,
                        span_collector=span_collector,
                        **llm_kwargs,
                    ):
                        collected.append(token)
                        yield {"token": token, "done": False, "conversation_id": task.conversation_id}
                    span["metadata"]["model"] = "general-agent"
                    span["metadata"]["llm_response"] = "".join(collected)[:500]
                    span["metadata"]["tools_available"] = len(tool_schemas)
            else:
                async with _optional_span(span_collector, "llm_call", agent_id="general-agent") as span:
                    async for token in self._call_llm_stream(messages, span_collector=span_collector, **llm_kwargs):
                        collected.append(token)
                        yield {"token": token, "done": False, "conversation_id": task.conversation_id}
                    span["metadata"]["model"] = "general-agent"
                    span["metadata"]["llm_response"] = "".join(collected)[:500]
        except asyncio.CancelledError:
            raise
        except Exception:
            # Mid-stream failure: tokens already yielded stay spoken; the
            # turn terminates below with the partial answer. A failure
            # before the first token falls through to the canned error.
            logger.exception("LLM stream failed for general-agent")

        speech = "".join(collected).strip()
        if not speech:
            logger.warning("LLM returned empty response for general-agent task: %s", task.description[:100])
            err = self._error_result(
                AgentErrorCode.LLM_EMPTY_RESPONSE,
                "The language model did not return a response. Please try again.",
            )
            yield {"token": err.speech, "done": True, "conversation_id": task.conversation_id}
            return
        yield {"token": "", "done": True, "conversation_id": task.conversation_id}

    async def _get_mcp_tools(self) -> list[dict]:
        """Get MCP tools assigned to this agent."""
        if not self._mcp_tool_manager:
            return []
        try:
            return await self._mcp_tool_manager.get_tools_for_agent(self.agent_card.agent_id)
        except Exception:
            logger.warning("Failed to get MCP tools for general-agent", exc_info=True)
            return []

    @staticmethod
    def _mcp_tools_to_openai_format(mcp_tools: list[dict]) -> list[dict]:
        """Convert MCP tool descriptors to OpenAI function-calling format."""
        return mcp_tools_to_openai_format(mcp_tools)

    async def _call_llm_with_tools(self, messages, tool_schemas, mcp_tools, span_collector=None, **overrides):
        """Call LLM with tool calling support."""
        return await call_llm_with_mcp_tools(
            self,
            messages,
            mcp_tools,
            self._mcp_tool_manager,
            span_collector=span_collector,
            **overrides,
        )
