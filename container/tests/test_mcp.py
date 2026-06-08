"""Tests for app.mcp -- client, registry, and tool manager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.client import MCPClient
from app.mcp.registry import MCPServerRegistry
from app.mcp.tools import MCPToolManager
from tests.conftest import build_integration_test_app

# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------


class TestMCPClient:
    def test_initial_state_not_connected(self):
        client = MCPClient(name="test", transport="stdio", command_or_url="echo hello")
        assert client.connected is False
        assert client.name == "test"

    async def test_connect_unknown_transport_returns_false(self):
        client = MCPClient(name="test", transport="grpc", command_or_url="localhost:50051")
        result = await client.connect()
        assert result is False
        assert client.connected is False

    async def test_list_tools_returns_empty_when_not_connected(self):
        client = MCPClient(name="test", transport="stdio", command_or_url="echo")
        tools = await client.list_tools()
        assert tools == []

    async def test_call_tool_raises_when_not_connected(self):
        client = MCPClient(name="test", transport="stdio", command_or_url="echo")
        with pytest.raises(ConnectionError, match="Not connected"):
            await client.call_tool("some_tool", {"arg": 1})

    async def test_disconnect_clears_state(self):
        client = MCPClient(name="test", transport="stdio", command_or_url="echo")
        client._connected = True
        client._session = MagicMock()
        client._session_cm = None
        client._transport_cm = None
        await client.disconnect()
        assert client.connected is False
        assert client._session is None

    @patch("app.mcp.client.MCPClient._connect_stdio", new_callable=AsyncMock, return_value=True)
    async def test_connect_stdio_transport(self, mock_connect):
        client = MCPClient(name="test", transport="stdio", command_or_url="echo hello")
        result = await client.connect()
        assert result is True

    @patch("app.mcp.client.MCPClient._connect_sse", new_callable=AsyncMock, return_value=True)
    async def test_connect_sse_transport(self, mock_connect):
        client = MCPClient(name="test", transport="sse", command_or_url="http://localhost:3000")
        result = await client.connect()
        assert result is True

    async def test_list_tools_parses_result(self):
        client = MCPClient(name="test", transport="stdio", command_or_url="echo")
        client._connected = True
        tool_mock = MagicMock()
        tool_mock.name = "get_weather"
        tool_mock.description = "Get weather info"
        tool_mock.inputSchema = {"type": "object"}
        session = AsyncMock()
        session.list_tools = AsyncMock(return_value=MagicMock(tools=[tool_mock]))
        client._session = session

        tools = await client.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "get_weather"

    async def test_call_tool_invokes_session(self):
        client = MCPClient(name="test", transport="stdio", command_or_url="echo")
        client._connected = True
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value={"result": "ok"})
        client._session = session

        result = await client.call_tool("my_tool", {"arg": "val"})
        assert result == {"result": "ok"}
        session.call_tool.assert_awaited_once_with("my_tool", arguments={"arg": "val"})

    def test_timeout_property_returns_configured_value(self):
        client = MCPClient(name="test", transport="stdio", command_or_url="echo", timeout=60)
        assert client.timeout == 60

    async def test_connect_http_transport_returns_false(self):
        """http transport is no longer supported; only stdio and sse are valid."""
        client = MCPClient(name="test", transport="http", command_or_url="http://localhost")
        result = await client.connect()
        assert result is False
        assert client.connected is False

    @patch("app.mcp.client.MCPClient._connect_stdio", new_callable=AsyncMock)
    async def test_connect_timeout_returns_false(self, mock_stdio):
        """If connection takes longer than timeout, connect() returns False."""

        async def slow_connect():
            await asyncio.sleep(0.1)
            return True

        mock_stdio.side_effect = slow_connect
        client = MCPClient(name="test", transport="stdio", command_or_url="echo", timeout=0.05)
        result = await client.connect()
        assert result is False
        assert client.connected is False


# ---------------------------------------------------------------------------
# MCPServerRegistry
# ---------------------------------------------------------------------------


class TestMCPServerRegistry:
    def test_list_servers_empty_on_init(self):
        registry = MCPServerRegistry()
        assert registry.list_servers() == []

    def test_get_client_returns_none_for_unknown(self):
        registry = MCPServerRegistry()
        assert registry.get_client("nonexistent") is None

    @patch("app.mcp.registry.McpServerRepository")
    @patch("app.mcp.registry.MCPClient")
    async def test_add_server_registers_and_connects(self, mock_mcp_client, mock_repo):
        mock_repo.upsert = AsyncMock()
        client_instance = AsyncMock()
        client_instance.connect = AsyncMock(return_value=True)
        client_instance.connected = True
        mock_mcp_client.return_value = client_instance

        registry = MCPServerRegistry()
        result = await registry.add_server("test-server", "stdio", "echo hello")
        assert result is True
        assert len(registry.list_servers()) == 1

    @patch("app.mcp.registry.McpServerRepository")
    async def test_remove_server_disconnects_and_removes(self, mock_repo):
        mock_repo.delete = AsyncMock()
        registry = MCPServerRegistry()
        client = AsyncMock()
        client.connected = True
        registry._clients["test-server"] = client

        await registry.remove_server("test-server")
        client.disconnect.assert_awaited_once()
        assert registry.get_client("test-server") is None

    async def test_disconnect_all_clears_clients(self):
        registry = MCPServerRegistry()
        client1 = AsyncMock()
        client2 = AsyncMock()
        registry._clients = {"a": client1, "b": client2}
        await registry.disconnect_all()
        assert len(registry._clients) == 0
        client1.disconnect.assert_awaited_once()
        client2.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# MCPToolManager
# ---------------------------------------------------------------------------


class TestMCPToolManager:
    async def test_discover_tools_returns_tools_for_connected_servers(self):
        registry = MagicMock(spec=MCPServerRegistry)
        registry.list_servers.return_value = [
            {"name": "server1", "connected": True},
            {"name": "server2", "connected": False},
        ]
        client1 = AsyncMock()
        client1.list_tools = AsyncMock(return_value=[{"name": "tool1", "description": "d", "input_schema": {}}])
        registry.get_client.side_effect = lambda n: client1 if n == "server1" else None

        manager = MCPToolManager(registry)
        tools = await manager.discover_tools()
        assert "server1" in tools
        assert "server2" not in tools
        assert len(tools["server1"]) == 1

    async def test_get_tools_for_agent_caches_descriptors_once_per_server(self):
        with patch(
            "app.db.repository.AgentMcpToolsRepository.get_tools",
            new_callable=AsyncMock,
            return_value=[
                {"server_name": "duckduckgo-search", "tool_name": "web_search"},
                {"server_name": "duckduckgo-search", "tool_name": "web_search_news"},
            ],
        ):
            registry = MCPServerRegistry()
            mock_client = MagicMock()
            mock_client.connected = True
            mock_client.list_tools = AsyncMock(
                return_value=[
                    {"name": "web_search", "description": "Search", "input_schema": {}},
                    {"name": "web_search_news", "description": "News", "input_schema": {}},
                ]
            )
            registry._clients["duckduckgo-search"] = mock_client
            manager = MCPToolManager(registry)

            first = await manager.get_tools_for_agent("general-agent")
            second = await manager.get_tools_for_agent("general-agent")

        assert [tool["name"] for tool in first] == ["web_search", "web_search_news"]
        assert [tool["name"] for tool in second] == ["web_search", "web_search_news"]
        assert mock_client.list_tools.await_count == 1

    async def test_get_tools_for_agent_lock_prevents_concurrent_discovery_stampede(self):
        with patch(
            "app.db.repository.AgentMcpToolsRepository.get_tools",
            new_callable=AsyncMock,
            return_value=[{"server_name": "duckduckgo-search", "tool_name": "web_search"}],
        ):
            registry = MCPServerRegistry()
            mock_client = MagicMock()
            mock_client.connected = True

            async def slow_list_tools():
                await asyncio.sleep(0.01)
                return [{"name": "web_search", "description": "Search", "input_schema": {}}]

            mock_client.list_tools = AsyncMock(side_effect=slow_list_tools)
            registry._clients["duckduckgo-search"] = mock_client
            manager = MCPToolManager(registry)

            results = await asyncio.gather(
                manager.get_tools_for_agent("general-agent"),
                manager.get_tools_for_agent("general-agent"),
                manager.get_tools_for_agent("general-agent"),
            )

        assert all([tool["name"] for tool in result] == ["web_search"] for result in results)
        assert mock_client.list_tools.await_count == 1

    async def test_admin_discover_refreshes_cached_descriptors(self):
        with patch(
            "app.db.repository.AgentMcpToolsRepository.get_tools",
            new_callable=AsyncMock,
            return_value=[{"server_name": "duckduckgo-search", "tool_name": "web_search"}],
        ):
            registry = MCPServerRegistry()
            mock_client = MagicMock()
            mock_client.connected = True
            mock_client.list_tools = AsyncMock(
                side_effect=[
                    [{"name": "web_search", "description": "Old", "input_schema": {}}],
                    [{"name": "web_search", "description": "New", "input_schema": {}}],
                ]
            )
            registry._clients["duckduckgo-search"] = mock_client
            manager = MCPToolManager(registry)

            first = await manager.get_tools_for_agent("general-agent")
            refreshed = await manager.discover_tools()
            second = await manager.get_tools_for_agent("general-agent")

        assert first[0]["description"] == "Old"
        assert refreshed["duckduckgo-search"][0]["description"] == "New"
        assert second[0]["description"] == "New"
        assert mock_client.list_tools.await_count == 2

    async def test_disconnected_server_does_not_serve_stale_cached_tools(self):
        with patch(
            "app.db.repository.AgentMcpToolsRepository.get_tools",
            new_callable=AsyncMock,
            return_value=[{"server_name": "duckduckgo-search", "tool_name": "web_search"}],
        ):
            registry = MCPServerRegistry()
            mock_client = MagicMock()
            mock_client.connected = True
            mock_client.list_tools = AsyncMock(
                side_effect=[
                    [{"name": "web_search", "description": "Before disconnect", "input_schema": {}}],
                    [{"name": "web_search", "description": "After reconnect", "input_schema": {}}],
                ]
            )
            registry._clients["duckduckgo-search"] = mock_client
            manager = MCPToolManager(registry)

            cached = await manager.get_tools_for_agent("general-agent")
            mock_client.connected = False
            disconnected = await manager.get_tools_for_agent("general-agent")
            mock_client.connected = True
            reconnected = await manager.get_tools_for_agent("general-agent")

        assert cached[0]["description"] == "Before disconnect"
        assert disconnected == []
        assert reconnected[0]["description"] == "After reconnect"
        assert mock_client.list_tools.await_count == 2

    async def test_call_tool_raises_on_unknown_server(self):
        registry = MagicMock(spec=MCPServerRegistry)
        registry.get_client.return_value = None
        manager = MCPToolManager(registry)
        with pytest.raises(ValueError, match="not found"):
            await manager.call_tool("unknown", "tool1")

    async def test_call_tool_raises_on_disconnected_server(self):
        registry = MagicMock(spec=MCPServerRegistry)
        client = MagicMock()
        client.connected = False
        registry.get_client.return_value = client
        manager = MCPToolManager(registry)
        with pytest.raises(ConnectionError, match="not connected"):
            await manager.call_tool("server1", "tool1")

    async def test_call_tool_uses_per_server_timeout(self):
        """call_tool should use the client's configured timeout, not a hardcoded value."""
        registry = MagicMock(spec=MCPServerRegistry)
        client = MagicMock()
        client.connected = True
        client.timeout = 10
        client.call_tool = AsyncMock(return_value={"result": "ok"})
        registry.get_client.return_value = client

        manager = MCPToolManager(registry)
        result = await manager.call_tool("server1", "tool1", {"arg": "val"})
        assert result == {"result": "ok"}

    async def test_call_tool_raises_timeout_error(self):
        """call_tool should raise TimeoutError when tool execution exceeds server timeout."""
        registry = MagicMock(spec=MCPServerRegistry)
        client = MagicMock()
        client.connected = True
        client.timeout = 0.05

        async def slow_tool(*args, **kwargs):
            await asyncio.sleep(0.1)
            return {}

        client.call_tool = AsyncMock(side_effect=slow_tool)
        registry.get_client.return_value = client

        manager = MCPToolManager(registry)
        with pytest.raises(asyncio.TimeoutError):
            await manager.call_tool("server1", "tool1")


# ---------------------------------------------------------------------------
# DuckDuckGo MCP Server
# ---------------------------------------------------------------------------


class TestDuckDuckGoServerTools:
    def test_server_module_importable(self):
        """The DuckDuckGo MCP server module can be imported."""
        pytest.importorskip("mcp")
        pytest.importorskip("ddgs")
        from app.mcp.servers import duckduckgo_server

        assert hasattr(duckduckgo_server, "server")

    async def test_list_tools_returns_expected_tools(self):
        """Server exposes web_search and web_search_news tools."""
        pytest.importorskip("mcp")
        pytest.importorskip("ddgs")
        from app.mcp.servers.duckduckgo_server import list_tools

        tools = await list_tools()
        names = {t.name for t in tools}
        assert "web_search" in names
        assert "web_search_news" in names

    async def test_web_search_tool_has_query_param(self):
        """web_search tool requires a 'query' parameter."""
        pytest.importorskip("mcp")
        pytest.importorskip("ddgs")
        from app.mcp.servers.duckduckgo_server import list_tools

        tools = await list_tools()
        search_tool = next(t for t in tools if t.name == "web_search")
        assert "query" in search_tool.inputSchema["properties"]
        assert "query" in search_tool.inputSchema["required"]


class TestWikipediaServerTools:
    def test_server_module_importable(self):
        """The Wikipedia MCP server module can be imported."""
        pytest.importorskip("mcp")
        pytest.importorskip("wikipedia")
        from app.mcp.servers import wikipedia_server

        assert hasattr(wikipedia_server, "server")

    async def test_list_tools_returns_expected_tools(self):
        """Server exposes wikipedia_search and wikipedia_summary tools."""
        pytest.importorskip("mcp")
        pytest.importorskip("wikipedia")
        from app.mcp.servers.wikipedia_server import list_tools

        tools = await list_tools()
        names = {t.name for t in tools}
        assert "wikipedia_search" in names
        assert "wikipedia_summary" in names

    async def test_wikipedia_search_tool_has_query_param(self):
        """wikipedia_search tool requires a 'query' parameter."""
        pytest.importorskip("mcp")
        pytest.importorskip("wikipedia")
        from app.mcp.servers.wikipedia_server import list_tools

        tools = await list_tools()
        search_tool = next(t for t in tools if t.name == "wikipedia_search")
        assert "query" in search_tool.inputSchema["properties"]
        assert "query" in search_tool.inputSchema["required"]

    async def test_wikipedia_summary_tool_has_title_param(self):
        """wikipedia_summary tool requires a 'title' parameter."""
        pytest.importorskip("mcp")
        pytest.importorskip("wikipedia")
        from app.mcp.servers.wikipedia_server import list_tools

        tools = await list_tools()
        summary_tool = next(t for t in tools if t.name == "wikipedia_summary")
        assert "title" in summary_tool.inputSchema["properties"]
        assert "title" in summary_tool.inputSchema["required"]


# ---------------------------------------------------------------------------
# MCP Tool Assignment for Built-in Agents
# ---------------------------------------------------------------------------


class TestAgentMcpToolAssignment:
    async def test_get_tools_for_builtin_agent_returns_empty_by_default(self):
        """Built-in agent with no assignments returns empty list."""
        with patch("app.db.repository.AgentMcpToolsRepository.get_tools", new_callable=AsyncMock, return_value=[]):
            registry = MCPServerRegistry()
            manager = MCPToolManager(registry)
            tools = await manager.get_tools_for_agent("general-agent")
            assert tools == []

    async def test_get_tools_for_builtin_agent_returns_assigned_tools(self):
        """Built-in agent with assignments gets tool descriptors."""
        with patch(
            "app.db.repository.AgentMcpToolsRepository.get_tools",
            new_callable=AsyncMock,
            return_value=[{"server": "duckduckgo-search", "tool": "web_search"}],
        ):
            registry = MCPServerRegistry()
            mock_client = MagicMock()
            mock_client.connected = True
            mock_client.list_tools = AsyncMock(
                return_value=[{"name": "web_search", "description": "Search", "input_schema": {}}]
            )
            registry._clients["duckduckgo-search"] = mock_client
            manager = MCPToolManager(registry)
            tools = await manager.get_tools_for_agent("general-agent")
            assert len(tools) == 1
            assert tools[0]["name"] == "web_search"
            assert tools[0]["_server_name"] == "duckduckgo-search"

    @pytest.mark.integration
    async def test_get_tools_for_custom_agent_uses_runtime_assignments(self, db_repository):
        """Custom agents get MCP tools from agent_mcp_tools after runtime sync."""
        from app.db.repository import CustomAgentRepository

        await CustomAgentRepository.create_with_runtime(
            "searchbot",
            system_prompt="s",
            mcp_tools=[{"server_name": "duckduckgo-search", "tool_name": "web_search"}],
        )
        registry = MCPServerRegistry()
        mock_client = MagicMock()
        mock_client.connected = True
        mock_client.list_tools = AsyncMock(
            return_value=[{"name": "web_search", "description": "Search", "input_schema": {}}]
        )
        registry._clients["duckduckgo-search"] = mock_client
        manager = MCPToolManager(registry)

        tools = await manager.get_tools_for_agent("custom-searchbot")

        assert len(tools) == 1
        assert tools[0]["name"] == "web_search"
        assert tools[0]["_server_name"] == "duckduckgo-search"

    @pytest.mark.integration
    async def test_get_tools_for_custom_agent_reuses_descriptor_cache(self, db_repository):
        """Custom agents use the same cached MCP descriptor path as built-ins."""
        from app.db.repository import CustomAgentRepository

        await CustomAgentRepository.create_with_runtime(
            "cachebot",
            system_prompt="s",
            mcp_tools=[{"server_name": "duckduckgo-search", "tool_name": "web_search"}],
        )
        registry = MCPServerRegistry()
        mock_client = MagicMock()
        mock_client.connected = True
        mock_client.list_tools = AsyncMock(
            return_value=[{"name": "web_search", "description": "Search", "input_schema": {}}]
        )
        registry._clients["duckduckgo-search"] = mock_client
        manager = MCPToolManager(registry)

        first = await manager.get_tools_for_agent("custom-cachebot")
        second = await manager.get_tools_for_agent("custom-cachebot")

        assert first == second
        assert first[0]["name"] == "web_search"
        assert mock_client.list_tools.await_count == 1

    @pytest.mark.integration
    async def test_disabled_custom_agent_has_no_active_mcp_tools(self, db_repository):
        """Disabled custom agents do not fall back to stale custom_agents.mcp_tools JSON."""
        from app.db.repository import CustomAgentRepository

        await CustomAgentRepository.create_with_runtime(
            "disabled-searchbot",
            system_prompt="s",
            mcp_tools=[{"server_name": "duckduckgo-search", "tool_name": "web_search"}],
        )
        await CustomAgentRepository.update_with_runtime("disabled-searchbot", enabled=False)
        registry = MCPServerRegistry()
        mock_client = MagicMock()
        mock_client.connected = True
        mock_client.list_tools = AsyncMock(
            return_value=[{"name": "web_search", "description": "Search", "input_schema": {}}]
        )
        registry._clients["duckduckgo-search"] = mock_client
        manager = MCPToolManager(registry)

        tools = await manager.get_tools_for_agent("custom-disabled-searchbot")

        assert tools == []
        mock_client.list_tools.assert_not_awaited()


# ---------------------------------------------------------------------------
# MCP server admin API auth (SEC-5)
# ---------------------------------------------------------------------------


class TestMcpServerAdminApiAuth:
    """SEC-5: every endpoint that calls ``MCPServerRegistry.add_server`` must
    require an authenticated admin session. Unauthenticated access to
    ``POST /api/admin/mcp-servers`` must be rejected with 401."""

    @pytest.mark.integration
    async def test_add_mcp_server_requires_session(self, db_repository):
        import httpx

        app = build_integration_test_app(setup_complete=True)

        with patch(
            "app.db.repository.SetupStateRepository.is_complete",
            new_callable=AsyncMock,
            return_value=True,
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/api/admin/mcp-servers",
                    json={
                        "name": "evil",
                        "transport": "stdio",
                        "command_or_url": "/bin/sh -c 'rm -rf /'",
                    },
                )
                assert resp.status_code == 401

    @pytest.mark.integration
    async def test_add_mcp_server_rejects_http_transport(self, db_repository):
        import httpx

        app = build_integration_test_app(setup_complete=True, override_admin_session=True)

        with patch(
            "app.db.repository.SetupStateRepository.is_complete",
            new_callable=AsyncMock,
            return_value=True,
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/api/admin/mcp-servers",
                    json={
                        "name": "legacy-http",
                        "transport": "http",
                        "command_or_url": "http://localhost:9000/sse",
                    },
                )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "stdio" in detail
        assert "sse" in detail
