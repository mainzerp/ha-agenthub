"""Bootstrap: MCP server registry DB load, built-in MCP server registrations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.db.repository import AgentMcpToolsRepository, McpServerRepository

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def setup_mcp(app: FastAPI, source: str) -> None:
    """Load MCP servers from DB, register built-in DuckDuckGo, Wikipedia servers.

    Assumes ``app.state.mcp_registry`` and ``app.state.mcp_tool_manager`` are set.
    """
    mcp_registry = app.state.mcp_registry
    mcp_tool_manager = app.state.mcp_tool_manager

    try:
        await mcp_registry.load_from_db()
    except Exception:
        logger.warning("Setup init (%s): failed to load MCP servers from DB", source, exc_info=True)

    ddg_server = await McpServerRepository.get("duckduckgo-search")
    if ddg_server is None:
        logger.info("Setup init (%s): registering built-in DuckDuckGo MCP server", source)
        connected = await mcp_registry.add_server(
            name="duckduckgo-search",
            transport="stdio",
            command_or_url="python -m app.mcp.servers.duckduckgo_server",
        )
        if connected:
            try:
                tools = await mcp_tool_manager.refresh_server("duckduckgo-search")
                for tool in tools:
                    await AgentMcpToolsRepository.assign_tool(
                        "general-agent",
                        "duckduckgo-search",
                        tool["name"],
                    )
                logger.info("Assigned %d DuckDuckGo tools to general-agent", len(tools))
            except Exception:
                logger.warning(
                    "Setup init (%s): failed to auto-assign DuckDuckGo tools",
                    source,
                    exc_info=True,
                )
        else:
            logger.warning(
                "Setup init (%s): DuckDuckGo MCP server registered but failed to connect",
                source,
            )

    wiki_server = await McpServerRepository.get("wikipedia-search")
    if wiki_server is None:
        logger.info("Setup init (%s): registering built-in Wikipedia MCP server", source)
        connected = await mcp_registry.add_server(
            name="wikipedia-search",
            transport="stdio",
            command_or_url="python -m app.mcp.servers.wikipedia_server",
        )
        if connected:
            try:
                tools = await mcp_tool_manager.refresh_server("wikipedia-search")
                for tool in tools:
                    await AgentMcpToolsRepository.assign_tool(
                        "general-agent",
                        "wikipedia-search",
                        tool["name"],
                    )
                logger.info("Assigned %d Wikipedia tools to general-agent", len(tools))
            except Exception:
                logger.warning(
                    "Setup init (%s): failed to auto-assign Wikipedia tools",
                    source,
                    exc_info=True,
                )
        else:
            logger.warning(
                "Setup init (%s): Wikipedia MCP server registered but failed to connect",
                source,
            )
