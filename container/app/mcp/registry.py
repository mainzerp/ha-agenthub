"""MCP server registry."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.db.repository import McpServerRepository
from app.mcp.client import MCPClient

logger = logging.getLogger(__name__)


class MCPServerRegistry:
    """Manages MCP server connections backed by the DB."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._change_listeners: list[Callable[[str | None], None]] = []

    def add_change_listener(self, listener: Callable[[str | None], None]) -> None:
        """Register a synchronous callback for MCP server connection-set changes."""
        self._change_listeners.append(listener)

    def _notify_changed(self, server_name: str | None = None) -> None:
        for listener in list(self._change_listeners):
            try:
                listener(server_name)
            except Exception:
                logger.debug("MCP registry change listener failed", exc_info=True)

    async def load_from_db(self) -> None:
        """Read enabled MCP servers from DB, create clients, and connect."""
        servers = await McpServerRepository.list_enabled()
        for row in servers:
            name = row["name"]
            try:
                client = MCPClient(
                    name=name,
                    transport=row["transport"],
                    command_or_url=row["command_or_url"],
                    env_vars=row.get("env_vars"),
                    timeout=row.get("timeout", 30),
                )
                connected = await client.connect()
                self._clients[name] = client
                if not connected:
                    logger.warning("MCP server '%s' registered but not connected", name)
            except Exception:
                logger.error("Failed to load MCP server '%s'", name, exc_info=True)
        logger.info(
            "MCP registry loaded %d servers (%d connected)",
            len(self._clients),
            sum(1 for c in self._clients.values() if c.connected),
        )
        self._notify_changed(None)

    async def add_server(
        self,
        name: str,
        transport: str,
        command_or_url: str,
        env_vars: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> bool:
        """Add a new MCP server to DB and connect."""
        await McpServerRepository.upsert(
            name=name,
            transport=transport,
            command_or_url=command_or_url,
            env_vars=env_vars,
            timeout=timeout,
        )
        client = MCPClient(
            name=name,
            transport=transport,
            command_or_url=command_or_url,
            env_vars=env_vars,
            timeout=timeout,
        )
        connected = await client.connect()
        existing = self._clients.pop(name, None)
        if existing is not None:
            try:
                await existing.disconnect()
            except Exception:
                logger.warning("Error disconnecting replaced MCP server '%s'", name, exc_info=True)
        self._clients[name] = client
        self._notify_changed(name)
        return connected

    async def remove_server(self, name: str) -> None:
        """Disconnect and remove an MCP server."""
        client = self._clients.pop(name, None)
        if client:
            await client.disconnect()
        await McpServerRepository.delete(name)
        self._notify_changed(name)

    def list_servers(self) -> list[dict[str, Any]]:
        """Return all server info with connection status."""
        return [{"name": name, "connected": client.connected} for name, client in self._clients.items()]

    def get_client(self, name: str) -> MCPClient | None:
        """Return an MCP client by server name."""
        return self._clients.get(name)

    async def disconnect_all(self) -> None:
        """Disconnect all MCP clients. Called on shutdown."""
        for name, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception:
                logger.warning("Error disconnecting MCP server '%s'", name, exc_info=True)
        self._clients.clear()
        self._notify_changed(None)
        logger.info("All MCP servers disconnected")
