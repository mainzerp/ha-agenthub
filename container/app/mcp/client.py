"""MCP client for connecting to MCP servers.

Uses an owner-task pattern: a single asyncio task holds the
``async with stdio_client(...) as (r, w): async with ClientSession(r, w):``
context open for the lifetime of the client. All callers (``list_tools``,
``call_tool``, ``disconnect``) submit requests to the owner via an
``asyncio.Queue`` and await a per-request future. This guarantees that
``__aenter__`` and ``__aexit__`` for the underlying transport contexts
run in the same task, which is required by the MCP SDK's anyio task
groups.

Regression: see CRIT-4 in docs/SubAgent/DEEP_CODE_REVIEW_ANALYSIS.md.
The legacy direct-context implementation is preserved as
``app/mcp/client_legacy.py``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import shlex
from typing import Any
from urllib.parse import urlparse

try:  # pragma: no cover - executed only when the optional SDK is missing
    from mcp.client.sse import sse_client as _sdk_sse_client
    from mcp.client.stdio import StdioServerParameters as _SDKStdioServerParameters
    from mcp.client.stdio import stdio_client as _sdk_stdio_client

    from mcp import ClientSession as _SDKClientSession
except Exception:  # pragma: no cover
    _SDKClientSession = None  # type: ignore[misc, assignment]
    _sdk_stdio_client = None  # type: ignore[misc, assignment]
    _SDKStdioServerParameters = None  # type: ignore[misc, assignment]
    _sdk_sse_client = None  # type: ignore[misc, assignment]

# Re-exported as module attributes so tests can monkey-patch them.
ClientSession = _SDKClientSession
stdio_client = _sdk_stdio_client
StdioServerParameters = _SDKStdioServerParameters
sse_client = _sdk_sse_client

logger = logging.getLogger(__name__)

# Shell metacharacters that must not appear in an MCP stdio command.
_SHELL_META_RE = re.compile(r"[;|&`$(){}\[\]<>]")

_STOP = "STOP"
_LIST_TOOLS = "list_tools"
_CALL_TOOL = "call_tool"

# P3-11: how long ``disconnect`` waits for the owner task to drain its
# request queue and exit cleanly before forcing a cancel.
_OWNER_TASK_DISCONNECT_TIMEOUT_SEC = 5.0

# Private/reserved IP ranges blocked for SSE transport (M-SEC-03).
_PRIVATE_IPV4_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
]
_PRIVATE_IPV6_RANGES = [
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
]


def _validate_sse_url(url: str) -> None:
    """Validate an MCP SSE URL: scheme, no private/reserved IPs.

    Raises ValueError on unsafe or invalid URLs.
    """
    if not url or not url.strip():
        raise ValueError("Invalid MCP SSE URL: empty URL")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid MCP SSE URL scheme: {parsed.scheme} (must be http or https)")
    host = parsed.hostname
    if not host:
        raise ValueError("Invalid MCP SSE URL: no host")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Host is a domain name -- resolve it and check all addresses.
        import socket

        try:
            addrs = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            raise ValueError(f"Invalid MCP SSE URL: cannot resolve host '{host}'") from None
        for _family, _, _, _, sockaddr in addrs:
            addr_str = sockaddr[0]
            try:
                addr = ipaddress.ip_address(addr_str)
            except ValueError:
                continue
            if _is_private_ip(addr):
                raise ValueError(
                    f"Invalid MCP SSE URL: host '{host}' resolves to private/reserved IP {addr_str}"
                ) from None
        return
    if _is_private_ip(addr):
        raise ValueError(f"Invalid MCP SSE URL: host is private/reserved IP '{host}'")


def _is_private_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(addr, ipaddress.IPv4Address):
        return any(addr in r for r in _PRIVATE_IPV4_RANGES)
    return any(addr in r for r in _PRIVATE_IPV6_RANGES)


def _validate_mcp_command(command_or_url: str) -> None:
    """Validate an MCP stdio command string before shlex.split().

    Raises ValueError if the command contains unsafe characters, path
    traversal, or is not an absolute path.
    """
    if not command_or_url:
        raise ValueError("Invalid MCP command: empty command")
    parts = command_or_url.split()
    _command = parts[0]
    # Allow simple PATH-resolved commands (e.g. "python", "python3")
    # as well as absolute paths. The shlex.split + shell-meta check
    # below is the real safety boundary.
    if _SHELL_META_RE.search(command_or_url):
        raise ValueError("Invalid MCP command: contains unsafe characters or path traversal")
    if ".." in command_or_url:
        raise ValueError("Invalid MCP command: contains path traversal")


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
        # Legacy attributes kept so existing tests that poke them still work.
        self._session_cm: Any = None
        self._transport_cm: Any = None
        self._connected: bool = False

        self._owner_task: asyncio.Task | None = None
        self._req_q: asyncio.Queue | None = None
        self._ready: asyncio.Event | None = None

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
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.error(
                "Connection to MCP server '%s' timed out after %ds",
                self._name,
                self._timeout,
            )
            await self._abort_owner()
            self._connected = False
            return False
        except Exception:
            logger.error("Failed to connect to MCP server '%s'", self._name, exc_info=True)
            await self._abort_owner()
            self._connected = False
            return False

    # ------------------------------------------------------------------
    # owner-task setup
    # ------------------------------------------------------------------

    async def _start_owner(self, transport_factory) -> bool:
        """Spawn the owner task and wait until it is ready (or fails)."""
        self._req_q = asyncio.Queue()
        self._ready = asyncio.Event()
        self._owner_task = asyncio.create_task(
            self._owner_loop(transport_factory),
            name=f"mcp-owner-{self._name}",
        )
        await self._ready.wait()
        return self._connected

    async def _owner_loop(self, transport_factory) -> None:
        """Hold the transport + session contexts open and serve requests."""
        # Resolve module-level binding fresh on every call so tests can
        # monkey-patch ``app.mcp.client.ClientSession``.
        import app.mcp.client as _mod

        client_session_cls = _mod.ClientSession

        try:
            async with transport_factory() as (read, write), client_session_cls(read, write) as session:
                await session.initialize()
                self._session = session
                self._connected = True
                if self._ready is None:
                    raise RuntimeError("MCP client not initialized")
                self._ready.set()

                while True:
                    if self._req_q is None:
                        raise RuntimeError("MCP client not initialized")
                    fut, op, args = await self._req_q.get()
                    if op == _STOP:
                        if not fut.done():
                            fut.set_result(None)
                        return
                    try:
                        result: Any = None
                        if op == _LIST_TOOLS:
                            result = await session.list_tools()
                        elif op == _CALL_TOOL:
                            tool_name, arguments = args
                            result = await session.call_tool(tool_name, arguments=arguments or {})
                        else:
                            raise ValueError(f"unknown op: {op}")
                        if not fut.done():
                            fut.set_result(result)
                    except asyncio.CancelledError:
                        if not fut.done():
                            fut.cancel()
                        raise
                    except Exception as exc:
                        if not fut.done():
                            fut.set_exception(exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("MCP owner loop for '%s' crashed", self._name, exc_info=True)
        finally:
            self._connected = False
            self._session = None
            if self._ready is not None and not self._ready.is_set():
                self._ready.set()

    async def _connect_stdio(self) -> bool:
        import app.mcp.client as _mod

        stdio_client = _mod.stdio_client
        stdio_server_parameters_cls = _mod.StdioServerParameters

        _validate_mcp_command(self._command_or_url)
        parts = shlex.split(self._command_or_url)
        command = parts[0]
        args = parts[1:] if len(parts) > 1 else []
        env = dict(self._env_vars) if self._env_vars else None

        server_params = stdio_server_parameters_cls(command=command, args=args, env=env)

        def _factory():
            return stdio_client(server_params)

        ok = await self._start_owner(_factory)
        if ok:
            logger.info("Connected to MCP server '%s' via stdio", self._name)
        return ok

    async def _connect_sse(self) -> bool:
        import app.mcp.client as _mod

        sse_client = _mod.sse_client

        _validate_sse_url(self._command_or_url)

        def _factory():
            return sse_client(self._command_or_url)

        ok = await self._start_owner(_factory)
        if ok:
            logger.info("Connected to MCP server '%s' via SSE", self._name)
        return ok

    # ------------------------------------------------------------------
    # request dispatch helpers
    # ------------------------------------------------------------------

    def _has_owner(self) -> bool:
        return self._owner_task is not None and not self._owner_task.done() and self._req_q is not None

    async def _submit(self, op: str, args: tuple) -> Any:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        if self._req_q is None:
            raise RuntimeError("MCP client not initialized")
        await self._req_q.put((fut, op, args))
        return await fut

    async def _abort_owner(self) -> None:
        if self._owner_task is not None and not self._owner_task.done():
            self._owner_task.cancel()
            try:
                await self._owner_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("MCP owner task cleanup raised", exc_info=True)
        self._owner_task = None
        self._req_q = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        try:
            if self._has_owner():
                loop = asyncio.get_running_loop()
                fut: asyncio.Future = loop.create_future()
                if self._req_q is None:
                    raise RuntimeError("MCP client not initialized")
                await self._req_q.put((fut, _STOP, ()))
                try:
                    assert self._owner_task is not None
                    await asyncio.wait_for(self._owner_task, timeout=_OWNER_TASK_DISCONNECT_TIMEOUT_SEC)
                except TimeoutError:
                    logger.warning(
                        "MCP owner for '%s' did not stop within %.0fs; cancelling",
                        self._name,
                        _OWNER_TASK_DISCONNECT_TIMEOUT_SEC,
                    )
                    if self._owner_task is None:
                        raise RuntimeError("MCP client not initialized") from None
                    self._owner_task.cancel()
                    try:
                        await self._owner_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.debug("MCP disconnect owner task cleanup raised", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Error disconnecting from MCP server '%s'", self._name, exc_info=True)
        finally:
            self._owner_task = None
            self._req_q = None
            self._ready = None
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
            if self._has_owner():
                result = await self._submit(_LIST_TOOLS, ())
            else:
                # Fallback for tests that wire up _session directly without an owner.
                result = await self._session.list_tools()
            return [
                {
                    "name": tool.name,
                    "description": getattr(tool, "description", "") or "",
                    "input_schema": getattr(tool, "inputSchema", {}) or {},
                }
                for tool in result.tools
            ]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Failed to list tools from MCP server '%s'", self._name, exc_info=True)
            return []

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> Any:
        """Invoke a tool on the MCP server and return the result."""
        if not self._connected or not self._session:
            raise ConnectionError(f"Not connected to MCP server '{self._name}'")
        try:
            if self._has_owner():
                return await self._submit(_CALL_TOOL, (tool_name, arguments))
            return await self._session.call_tool(tool_name, arguments=arguments or {})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Failed to call tool '%s' on MCP server '%s'",
                tool_name,
                self._name,
                exc_info=True,
            )
            raise
