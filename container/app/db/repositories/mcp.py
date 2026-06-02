"""MCP server and MCP tool assignment CRUD."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.db.repositories._utils import _now
from app.db.schema import get_db_read, get_db_write

logger = logging.getLogger(__name__)


class McpServerRepository:
    """CRUD for MCP server configurations."""

    @staticmethod
    async def get(name: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM mcp_servers WHERE name = ?", (name,))
            row = await cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            raw_env = result.get("env_vars")
            if raw_env:
                try:
                    result["env_vars"] = json.loads(raw_env)
                except json.JSONDecodeError:
                    logger.warning("Malformed JSON in env_vars for MCP server %s", name)
                    result["env_vars"] = {}
            return result

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM mcp_servers")
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                raw_env = row.get("env_vars")
                if raw_env:
                    try:
                        row["env_vars"] = json.loads(raw_env)
                    except json.JSONDecodeError:
                        logger.warning("Malformed JSON in env_vars for MCP server %s", row.get("name"))
                        row["env_vars"] = {}
            return rows

    @staticmethod
    async def create(
        name: str, transport: str, command_or_url: str, env_vars: dict | None = None, timeout: int = 30
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO mcp_servers (name, transport, command_or_url, env_vars, timeout) VALUES (?, ?, ?, ?, ?)",
                (name, transport, command_or_url, json.dumps(env_vars) if env_vars else None, timeout),
            )

    @staticmethod
    async def delete(name: str) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))

    @staticmethod
    async def list_enabled() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM mcp_servers WHERE enabled = 1")
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                if row.get("env_vars"):
                    row["env_vars"] = json.loads(row["env_vars"])
            return rows

    @staticmethod
    async def upsert(
        name: str, transport: str, command_or_url: str, env_vars: dict | None = None, timeout: int = 30
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO mcp_servers (name, transport, command_or_url, env_vars, timeout, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET transport=?, command_or_url=?, env_vars=?, timeout=?, updated_at=?",
                (
                    name,
                    transport,
                    command_or_url,
                    json.dumps(env_vars) if env_vars else None,
                    timeout,
                    _now(),
                    transport,
                    command_or_url,
                    json.dumps(env_vars) if env_vars else None,
                    timeout,
                    _now(),
                ),
            )

    @staticmethod
    async def set_enabled(name: str, enabled: bool) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE mcp_servers SET enabled = ?, updated_at = ? WHERE name = ?",
                (1 if enabled else 0, _now(), name),
            )


class AgentMcpToolsRepository:
    """CRUD for MCP tool assignments to agents (built-in and custom)."""

    @staticmethod
    async def get_tools(agent_id: str) -> list[dict[str, str]]:
        """Return list of {server_name, tool_name} for an agent."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT server_name, tool_name FROM agent_mcp_tools WHERE agent_id = ?",
                (agent_id,),
            )
            return [
                {"server_name": row["server_name"], "tool_name": row["tool_name"]} for row in await cursor.fetchall()
            ]

    @staticmethod
    async def assign_tool(agent_id: str, server_name: str, tool_name: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT OR IGNORE INTO agent_mcp_tools (agent_id, server_name, tool_name) VALUES (?, ?, ?)",
                (agent_id, server_name, tool_name),
            )

    @staticmethod
    async def unassign_tool(agent_id: str, server_name: str, tool_name: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "DELETE FROM agent_mcp_tools WHERE agent_id = ? AND server_name = ? AND tool_name = ?",
                (agent_id, server_name, tool_name),
            )

    @staticmethod
    async def replace_tools(agent_id: str, tools: list[dict[str, str]] | None) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM agent_mcp_tools WHERE agent_id = ?", (agent_id,))
            for tool in tools or []:
                server_name = tool.get("server_name") or tool.get("server") or ""
                tool_name = tool.get("tool_name") or tool.get("tool") or ""
                if not server_name or not tool_name:
                    continue
                await db.execute(
                    "INSERT OR IGNORE INTO agent_mcp_tools (agent_id, server_name, tool_name) VALUES (?, ?, ?)",
                    (agent_id, server_name, tool_name),
                )

    @staticmethod
    async def clear_agent(agent_id: str) -> None:
        await AgentMcpToolsRepository.replace_tools(agent_id, [])

    @staticmethod
    async def get_all_assignments() -> list[dict[str, str]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT agent_id, server_name, tool_name FROM agent_mcp_tools")
            return [dict(row) for row in await cursor.fetchall()]
