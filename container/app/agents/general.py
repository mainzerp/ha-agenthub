"""General fallback agent for unroutable requests."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.base import BaseAgent
from app.agents.decorator import agent
from app.agents.prompt_builder import PromptBuilder
from app.agents.tool_calling import call_llm_with_mcp_tools, mcp_tools_to_openai_format
from app.analytics.tracer import _optional_span
from app.models.agent import AgentCard, AgentErrorCode, DispatchTask, TaskResult

logger = logging.getLogger(__name__)


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

    async def handle_task(self, task: DispatchTask) -> TaskResult:
        span_collector = task.span_collector
        system_prompt = PromptBuilder.build(
            await self._load_prompt_async("general"),
            language=task.context.language if task.context else None,
            time_location=self._build_time_location_context(task.context),
            sequential_send=bool(task.context and task.context.sequential_send),
        )

        messages = [{"role": "system", "content": system_prompt}]

        if task.context and task.context.conversation_turns:
            self._append_conversation_turn_messages(messages, task.context.conversation_turns)

        # Prime Directive: the orchestrator owns intent classification and
        # condensation.  Agents MUST NOT see the raw user_text — they receive
        # only the distilled description.
        messages.append({"role": "user", "content": self._wrap_user_input(task.description)})

        # Check for available MCP tools
        llm_kwargs: dict[str, Any] = {}
        if task.context and task.context.sequential_send:
            llm_kwargs["max_tokens"] = 2048
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
