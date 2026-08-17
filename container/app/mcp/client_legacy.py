"""MCP client for connecting to MCP servers."""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any

from app.mcp.client import _validate_mcp_command

logger = logging.getLogger(__name__)


class MCPClient:
    """Client for connecting to a single MCP server."""

    def __init__(
        self,
        name: str,
        transport: str,
        command_or_url: str,
        env_vars: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> None:
        self._name = name
        self._transport = transport
        self._command_or_url = command_or_url
        self._env_vars = env_vars or {}
        self._timeout = timeout
        self._session: Any = None
        self._session_cm: Any = None
        self._transport_cm: Any = None
        self._connected: bool = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def connected(self) -> bool:
        return self._connected and self._session is not None

    @property
    def timeout(self) -> int:
        """Per-server timeout in seconds."""
        return self._timeout

    async def connect(self) -> bool:
        """Connect to the MCP server. Returns True on success."""
        try:
            if self._transport == "stdio":
                return await asyncio.wait_for(self._connect_stdio(), timeout=float(self._timeout))
            elif self._transport == "sse":
                return await asyncio.wait_for(self._connect_sse(), timeout=float(self._timeout))
            else:
                logger.error(
                    "Unsupported transport type '%s' for MCP server '%s'. Supported transports: stdio, sse.",
                    self._transport,
                    self._name,
                )
                return False
        except TimeoutError:
            logger.error(
                "Connection to MCP server '%s' timed out after %ds",
                self._name,
                self._timeout,
            )
            self._connected = False
            return False
        except Exception:
            logger.error("Failed to connect to MCP server '%s'", self._name, exc_info=True)
            self._connected = False
            return False

    async def _connect_stdio(self) -> bool:
        from mcp.client.stdio import StdioServerParameters, stdio_client

        from mcp import ClientSession

        _validate_mcp_command(self._command_or_url)
        parts = shlex.split(self._command_or_url)
        command = parts[0]
        args = parts[1:] if len(parts) > 1 else []
        env = dict(self._env_vars) if self._env_vars else None

        server_params = StdioServerParameters(command=command, args=args, env=env)
        self._transport_cm = stdio_client(server_params)
        read, write = await self._transport_cm.__aenter__()

        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

        self._connected = True
        logger.info("Connected to MCP server '%s' via stdio", self._name)
        return True

    async def _connect_sse(self) -> bool:
        from mcp.client.sse import sse_client

        from mcp import ClientSession

        self._transport_cm = sse_client(self._command_or_url)
        read, write = await self._transport_cm.__aenter__()

        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

        self._connected = True
        logger.info("Connected to MCP server '%s' via SSE", self._name)
        return True

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        try:
            if self._session_cm is not None:
                await self._session_cm.__aexit__(None, None, None)
            if self._transport_cm is not None:
                await self._transport_cm.__aexit__(None, None, None)
        except Exception:
            logger.warning("Error disconnecting from MCP server '%s'", self._name, exc_info=True)
        finally:
            self._session = None
            self._session_cm = None
            self._transport_cm = None
            self._connected = False
            logger.info("Disconnected from MCP server '%s'", self._name)

    async def list_tools(self) -> list[dict[str, Any]]:
        """List all tools exposed by this MCP server."""
        if not self._connected or not self._session:
            return []
        try:
            result = await self._session.list_tools()
            return [
                {
                    "name": tool.name,
                    "description": getattr(tool, "description", "") or "",
                    "input_schema": tool.input_schema or {},
                }
                for tool in result.tools
            ]
        except Exception:
            logger.error("Failed to list tools from MCP server '%s'", self._name, exc_info=True)
            return []

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> Any:
        """Invoke a tool on the MCP server and return the result."""
        if not self._connected or not self._session:
            raise ConnectionError(f"Not connected to MCP server '{self._name}'")
        try:
            result = await self._session.call_tool(tool_name, arguments=arguments or {})
            return result
        except Exception:
            logger.error(
                "Failed to call tool '%s' on MCP server '%s'",
                tool_name,
                self._name,
                exc_info=True,
            )
            raise
