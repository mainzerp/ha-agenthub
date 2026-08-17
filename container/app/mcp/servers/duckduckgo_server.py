"""DuckDuckGo web search MCP server (stdio transport)."""

import asyncio
import json
import logging

from ddgs import DDGS
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

logger = logging.getLogger(__name__)


async def handle_list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
    return ListToolsResult(
        tools=[
            Tool(
                name="web_search",
                description="Search the web using DuckDuckGo. Returns search results with title, URL, and snippet.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query"},
                        "max_results": {"type": "integer", "description": "Max results (1-10)", "default": 5},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="web_search_news",
                description="Search DuckDuckGo News for recent news articles.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query"},
                        "max_results": {"type": "integer", "description": "Max results (1-10)", "default": 5},
                    },
                    "required": ["query"],
                },
            ),
        ]
    )


async def handle_call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
    name = params.name
    arguments = params.arguments or {}
    query = arguments.get("query", "")
    max_results = max(1, min(int(arguments.get("max_results", 5)), 10))

    if not query:
        return CallToolResult(content=[TextContent(type="text", text="Error: query is required")], is_error=True)

    try:
        ddgs = DDGS()
        if name == "web_search":
            results = await asyncio.to_thread(ddgs.text, query, max_results=max_results)
            formatted = [
                {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in results
            ]
        elif name == "web_search_news":
            results = await asyncio.to_thread(ddgs.news, query, max_results=max_results)
            formatted = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("body", ""),
                    "date": r.get("date", ""),
                    "source": r.get("source", ""),
                }
                for r in results
            ]
        else:
            return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")], is_error=True)

        return CallToolResult(content=[TextContent(type="text", text=json.dumps(formatted, ensure_ascii=False))])
    except Exception as e:
        logger.exception("DuckDuckGo search failed for tool '%s'", name)
        return CallToolResult(content=[TextContent(type="text", text=f"Search error: {e}")], is_error=True)


server = Server("duckduckgo-search", on_list_tools=handle_list_tools, on_call_tool=handle_call_tool)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
