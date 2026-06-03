"""Dynamic loader for runtime-created custom agents."""

from __future__ import annotations

import inspect
import logging
from typing import Any

from app.a2a.registry import AgentRegistry
from app.agents.base import BaseAgent
from app.agents.prompt_builder import PromptBuilder
from app.agents.tool_calling import call_llm_with_mcp_tools, mcp_tools_to_openai_format
from app.analytics.tracer import _optional_span
from app.db.repository import CustomAgentRepository
from app.models.agent import AgentCard, AgentErrorCode, AgentTask, TaskResult

logger = logging.getLogger(__name__)


class DynamicAgent(BaseAgent):
    """A dynamically-created agent from the custom_agents DB table."""

    def __init__(
        self,
        name: str,
        description: str,
        system_prompt: str,
        skills: list[str],
        normalized_name: str | None = None,
        ha_client=None,
        entity_index=None,
        mcp_tool_manager=None,
        model_override: str | None = None,
        timeout_sec: float | None = None,
        mcp_tools: list[dict[str, str]] | None = None,
        entity_visibility: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(ha_client=ha_client, entity_index=entity_index)
        self._name = name
        self._normalized_name = CustomAgentRepository.normalize_name(normalized_name or name)
        self._agent_id = f"custom-{self._normalized_name}"
        self._description = description
        self._system_prompt = system_prompt
        self._skills = skills
        self._mcp_tool_manager = mcp_tool_manager
        self._model_override = model_override
        self._timeout_sec = timeout_sec
        self._mcp_tool_assignments = mcp_tools or []
        self._entity_visibility = entity_visibility or []

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id=self._agent_id,
            name=self._name,
            description=self._description,
            skills=self._skills,
            endpoint=f"local://{self._agent_id}",
            # P2-2 (FLOW-TIMEOUT-1): custom plugin agents typically wrap
            # MCP / reasoning calls. 30s default mirrors general-agent;
            # operators can still narrow this per agent_id via the
            # ``agent.dispatch_timeout.custom-<name>`` setting.
            timeout_sec=self._timeout_sec if self._timeout_sec is not None else 30.0,
        )

    async def handle_task(self, task: AgentTask) -> TaskResult:
        agent_id = self.agent_card.agent_id
        span_collector = task.span_collector
        prompt = PromptBuilder.build(
            self._system_prompt + "\nNEVER translate or normalize entity/room names.",
            time_location=self._build_time_location_context(task.context),
        )

        messages = [{"role": "system", "content": prompt}]

        if task.context and task.context.conversation_turns:
            self._append_conversation_turn_messages(messages, task.context.conversation_turns)

        messages.append({"role": "user", "content": self._wrap_user_input(task.description)})
        tools = await self._get_mcp_tools()
        if tools:
            tool_schemas = mcp_tools_to_openai_format(tools)
            async with _optional_span(span_collector, "llm_call", agent_id=agent_id) as span:
                response = await call_llm_with_mcp_tools(
                    self,
                    messages,
                    tools,
                    self._mcp_tool_manager,
                    span_collector=span_collector,
                )
                span["metadata"]["model"] = self._model_override or "agent_config"
                span["metadata"]["response_chars"] = len(response or "")
                span["metadata"]["tools_available"] = len(tool_schemas)
        else:
            async with _optional_span(span_collector, "llm_call", agent_id=agent_id) as span:
                response = await self._call_llm(messages, span_collector=span_collector)
                span["metadata"]["model"] = self._model_override or "agent_config"
                span["metadata"]["response_chars"] = len(response or "")
                span["metadata"]["tools_available"] = 0
        if not response or not response.strip():
            logger.warning("LLM returned empty response for custom agent %s", agent_id)
            return self._error_result(
                AgentErrorCode.LLM_EMPTY_RESPONSE,
                "The language model did not return a response. Please try again.",
            )
        return TaskResult(speech=response)

    async def _get_mcp_tools(self) -> list[dict[str, Any]]:
        if not self._mcp_tool_manager:
            return []
        try:
            return await self._mcp_tool_manager.get_tools_for_agent(self.agent_card.agent_id)
        except Exception:
            logger.warning("Failed to get MCP tools for %s", self.agent_card.agent_id, exc_info=True)
            return []


class CustomAgentLoader:
    """Loads custom agent definitions from DB and registers with A2A."""

    def __init__(self, registry: AgentRegistry, ha_client=None, entity_index=None, mcp_tool_manager=None) -> None:
        self._registry = registry
        self._ha_client = ha_client
        self._entity_index = entity_index
        self._mcp_tool_manager = mcp_tool_manager
        self._loaded: dict[str, DynamicAgent] = {}

    async def load_all(self) -> int:
        """Load all enabled custom agents from DB and register."""
        agents = await CustomAgentRepository.list_enabled()
        count = 0
        for row in agents:
            try:
                sync_result = CustomAgentRepository.ensure_runtime_state(row)
                if inspect.isawaitable(sync_result):
                    await sync_result
                await self._load_one(row)
                count += 1
            except Exception:
                logger.error("Failed to load custom agent '%s'", row.get("name"), exc_info=True)
        logger.info("Loaded %d custom agents", count)
        return count

    async def reload(self) -> int:
        """Hot reload: unregister all custom agents, re-load from DB."""
        for agent_id in list(self._loaded.keys()):
            await self._registry.unregister(agent_id)
        self._loaded.clear()
        return await self.load_all()

    async def _load_one(self, row: dict[str, Any]) -> None:
        name = row["name"]
        normalized_name = CustomAgentRepository.normalize_name(name)
        agent_id = CustomAgentRepository.agent_id_for_name(name)
        if agent_id in self._loaded:
            raise ValueError(f"Custom agent name conflict: '{name}' maps to already loaded agent ID '{agent_id}'")
        existing_card = await self._registry.discover(agent_id)
        if isinstance(existing_card, AgentCard):
            raise ValueError(f"Custom agent name conflict: '{name}' maps to already registered agent ID '{agent_id}'")
        intent_patterns = row.get("intent_patterns") or []
        skills = intent_patterns if intent_patterns else [name]
        agent = DynamicAgent(
            name=name,
            description=row.get("description", ""),
            system_prompt=row["system_prompt"],
            skills=skills,
            normalized_name=normalized_name,
            ha_client=self._ha_client,
            entity_index=self._entity_index,
            mcp_tool_manager=self._mcp_tool_manager,
            model_override=row.get("model_override"),
            timeout_sec=row.get("timeout_sec"),
            mcp_tools=row.get("mcp_tools") or [],
            entity_visibility=row.get("entity_visibility") or [],
        )
        await self._registry.register(agent)
        self._loaded[agent.agent_card.agent_id] = agent
