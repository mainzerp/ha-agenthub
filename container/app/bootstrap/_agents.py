"""Bootstrap: RewriteAgent creation, agent installation, custom agent loader."""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING

from app.agents.custom_loader import CustomAgentLoader
from app.agents.decorator import install_all_agents
from app.agents.rewrite import RewriteAgent

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.entity.index import EntityIndex
    from app.ha_client.rest import HARestClient

logger = logging.getLogger(__name__)

BUILT_IN_AGENT_IDS: frozenset[str] = frozenset(
    {
        "orchestrator",
        "light-agent",
        "music-agent",
        "general-agent",
        "timer-agent",
        "climate-agent",
        "media-agent",
        "scene-agent",
        "automation-agent",
        "security-agent",
        "send-agent",
        "rewrite-agent",
        "filler-agent",
        "calendar-agent",
        "lists-agent",
        "cover-agent",
        "vacuum-agent",
        "cancel-interaction",
    }
)


async def setup_rewrite_agent(
    app: FastAPI,
    source: str,
    ha_client: HARestClient,
    entity_index: EntityIndex,
) -> RewriteAgent:
    """Create RewriteAgent and store on ``app.state.rewrite_agent``."""
    rewrite_agent = getattr(app.state, "rewrite_agent", None)
    if rewrite_agent is None:
        rewrite_agent = RewriteAgent(ha_client=ha_client, entity_index=entity_index)
        app.state.rewrite_agent = rewrite_agent
    return rewrite_agent


async def setup_agents(
    app: FastAPI,
    source: str,
    ha_client: HARestClient,
    entity_index: EntityIndex,
    mcp_tool_manager,
    registry,
) -> None:
    """Install all domain agents and load custom agents.

    Must be called after ``setup_rewrite_agent`` and MCP registrations.
    Requires ``app.state.mcp_tool_manager`` to be set.
    """
    orchestrator_instance = await install_all_agents(app)

    custom_loader = getattr(app.state, "custom_loader", None)
    if custom_loader is None:
        custom_loader = CustomAgentLoader(
            registry,
            ha_client=ha_client,
            entity_index=entity_index,
            mcp_tool_manager=mcp_tool_manager,
        )
        await custom_loader.load_all()
        app.state.custom_loader = custom_loader
    else:
        custom_loader._ha_client = ha_client
        custom_loader._entity_index = entity_index
        custom_loader._mcp_tool_manager = mcp_tool_manager
        reload_result = custom_loader.reload()
        if inspect.isawaitable(reload_result):
            await reload_result

    if orchestrator_instance is not None:
        orchestrator_instance._agent_registry.invalidate_caches()
