---
name: mcp-server-dev
description: Add a new MCP server (tool provider) to agent-assist. Use when adding external tool access (APIs, databases, CLI tools) that agents can call via the MCP tool-calling protocol.
---

# Adding a New MCP Server

MCP servers expose tools that agents (primarily `GeneralAgent`) can call via the LLM tool-use protocol. Existing examples: `container/app/mcp/servers/duckduckgo_server.py`, `container/app/mcp/servers/wikipedia_server.py`.

## Two deployment paths

| Path | Use when |
|------|---------|
| **Built-in server** (stdio subprocess) | Tool ships with agent-assist, packaged dependency |
| **External server** (registered via UI/API) | Third-party MCP server, separate process |

---

## Path A: Built-in stdio server

### 1. Create the server file

`container/app/mcp/servers/<name>_server.py`:

```python
"""<Name> MCP server (stdio transport)."""

import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)
server = Server("<name>-server")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="<tool_name>",
            description="<What this tool does. Be specific — the LLM uses this to decide when to call it.>",
            inputSchema={
                "type": "object",
                "properties": {
                    "param1": {"type": "string", "description": "..."},
                    "param2": {"type": "integer", "description": "...", "default": 5},
                },
                "required": ["param1"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "<tool_name>":
        try:
            result = ...  # call external service / library
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        except Exception as e:
            logger.exception("Tool '%s' failed", name)
            return [TextContent(type="text", text=f"Error: {e}")]
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

### 2. Register in MCPServerRegistry

The registry is backed by the DB (`MCPServerRepository`). Add the server either:

**Via plugin** (recommended for built-in servers):
```python
async def startup(self, ctx: PluginContext) -> None:
    await ctx.mcp_registry.add_server(
        name="<name>-server",
        transport="stdio",
        command_or_url="python3 -m app.mcp.servers.<name>_server",
        env_vars={"API_KEY": "..."},  # optional
        timeout=30,
    )
```

**Via Admin API** (for external/third-party servers):
```bash
BASE="${AA_BASE_URL:-http://localhost:8080}"

curl -X POST "$BASE/api/admin/mcp/servers" \
  -H "Content-Type: application/json" \
  -b /tmp/aa_cookies.txt \
  -d '{"name": "<name>", "transport": "stdio", "command_or_url": "python3 /path/to/server.py"}'
```

### 3. Assign tool to an agent

MCP tools are assigned per-agent in the admin UI or via the API. `GeneralAgent` is the default consumer for web-access tools. Assign tools to a specific agent if the domain warrants it (e.g. a calendar MCP server → calendar-agent).

---

## Path B: External / SSE server

Use `transport="sse"` and set `command_or_url` to the HTTP endpoint:

```python
await ctx.mcp_registry.add_server(
    name="my-remote-server",
    transport="sse",
    command_or_url="http://localhost:8080/sse",
    timeout=30,
)
```

---

## MCPClient internals

`container/app/mcp/client.py` handles connection lifecycle. Key points:
- `connect()` returns `True` on success; the server stays registered even if connection fails (will retry on next tool call)
- `connected` property reflects live status
- Tools are fetched via `list_tools()` on connect and cached

## Tool description quality

The LLM decides when to call a tool based solely on the `description` field. Write descriptions that:
- State the data source explicitly ("Search DuckDuckGo", "Query Wikipedia")
- Describe what is returned ("Returns title, URL, and snippet")
- State limitations ("Real-time data only; no historical queries")

## Adding a dependency

Add the required package to `container/requirements.txt`. The server is run as a subprocess, so it shares the same virtualenv.
