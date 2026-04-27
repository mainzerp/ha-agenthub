"""MCP server management API endpoints."""

from __future__ import annotations

import inspect
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.db.repository import McpServerRepository
from app.security.auth import require_admin_session

logger = logging.getLogger(__name__)

BUILTIN_MCP_SERVERS = {"duckduckgo-search"}
SUPPORTED_MCP_TRANSPORTS = frozenset({"stdio", "sse"})

router = APIRouter(
    prefix="/api/admin/mcp-servers",
    tags=["admin-mcp"],
    dependencies=[Depends(require_admin_session)],
)


class McpServerCreate(BaseModel):
    name: str
    transport: str
    command_or_url: str
    env_vars: dict[str, str] | None = None
    timeout: int = 30


@router.get("")
async def list_mcp_servers(request: Request) -> list[dict[str, Any]]:
    """List all MCP servers with connection status."""
    db_servers = await McpServerRepository.list_all()
    mcp_registry = request.app.state.mcp_registry
    live_status = {s["name"]: s["connected"] for s in mcp_registry.list_servers()}

    result = []
    for server in db_servers:
        server["connected"] = live_status.get(server["name"], False)
        server["is_builtin"] = server["name"] in BUILTIN_MCP_SERVERS
        result.append(server)
    return result


@router.post("", status_code=201)
async def add_mcp_server(request: Request, body: McpServerCreate) -> dict[str, Any]:
    """Add a new MCP server."""
    if body.transport not in SUPPORTED_MCP_TRANSPORTS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported transport. Supported transports: "
                + ", ".join(sorted(SUPPORTED_MCP_TRANSPORTS))
            ),
        )

    existing = await McpServerRepository.get(body.name)
    if existing:
        raise HTTPException(status_code=409, detail="Server with this name already exists")

    mcp_registry = request.app.state.mcp_registry
    mcp_tool_manager = request.app.state.mcp_tool_manager
    connected = await mcp_registry.add_server(
        name=body.name,
        transport=body.transport,
        command_or_url=body.command_or_url,
        env_vars=body.env_vars,
        timeout=body.timeout,
    )
    if connected:
        refresh_result = mcp_tool_manager.refresh_server(body.name)
        if inspect.isawaitable(refresh_result):
            await refresh_result
    else:
        mcp_tool_manager.invalidate_server(body.name)
    return {"name": body.name, "connected": connected}


@router.delete("/{name}")
async def remove_mcp_server(request: Request, name: str) -> dict[str, str]:
    """Remove an MCP server."""
    existing = await McpServerRepository.get(name)
    if not existing:
        raise HTTPException(status_code=404, detail="Server not found")
    if name in BUILTIN_MCP_SERVERS:
        raise HTTPException(status_code=403, detail="Cannot delete built-in MCP server")

    mcp_registry = request.app.state.mcp_registry
    mcp_tool_manager = request.app.state.mcp_tool_manager
    mcp_tool_manager.invalidate_server(name)
    await mcp_registry.remove_server(name)
    mcp_tool_manager.invalidate_server(name)
    return {"status": "deleted", "name": name}


@router.get("/agent-tools-summary")
async def get_all_agent_mcp_tools_summary() -> dict[str, list[dict]]:
    """Get MCP tool assignments grouped by agent_id for badge display."""
    from app.db.repository import AgentMcpToolsRepository

    all_assignments = await AgentMcpToolsRepository.get_all_assignments()
    summary: dict[str, list[dict]] = {}
    for row in all_assignments:
        aid = row["agent_id"]
        if aid not in summary:
            summary[aid] = []
        summary[aid].append({"server_name": row["server_name"], "tool_name": row["tool_name"]})
    return summary


@router.get("/{name}/tools")
async def list_server_tools(request: Request, name: str) -> list[dict[str, Any]]:
    """List discovered tools for a specific MCP server."""
    mcp_tool_manager = request.app.state.mcp_tool_manager
    all_tools = await mcp_tool_manager.discover_tools()
    server_tools = all_tools.get(name)
    if server_tools is None:
        existing = await McpServerRepository.get(name)
        if not existing:
            raise HTTPException(status_code=404, detail="Server not found")
        return []
    return server_tools


@router.get("/agent-tools/{agent_id}")
async def get_agent_mcp_tools(agent_id: str) -> list[dict]:
    """Get MCP tools assigned to an agent."""
    from app.db.repository import AgentMcpToolsRepository

    return await AgentMcpToolsRepository.get_tools(agent_id)


@router.post("/agent-tools/{agent_id}")
async def assign_mcp_tool(agent_id: str, body: dict) -> dict:
    """Assign an MCP tool to an agent."""
    from app.db.repository import AgentMcpToolsRepository

    server_name = body.get("server_name", "")
    tool_name = body.get("tool_name", "")
    if not server_name or not tool_name:
        raise HTTPException(status_code=400, detail="server_name and tool_name required")
    await AgentMcpToolsRepository.assign_tool(agent_id, server_name, tool_name)
    return {"status": "ok"}


@router.delete("/agent-tools/{agent_id}/{server_name}/{tool_name}")
async def unassign_mcp_tool(agent_id: str, server_name: str, tool_name: str) -> dict:
    """Remove an MCP tool assignment from an agent."""
    from app.db.repository import AgentMcpToolsRepository

    await AgentMcpToolsRepository.unassign_tool(agent_id, server_name, tool_name)
    return {"status": "ok"}
