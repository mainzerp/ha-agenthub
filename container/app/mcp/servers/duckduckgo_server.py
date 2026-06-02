"""DuckDuckGo web search MCP server (stdio transport)."""

import json
import logging

from ddgs import DDGS
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)
server = Server("duckduckgo-search")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description="Search the web using DuckDuckGo. Returns search results with title, URL, and snippet.",
            inputSchema={
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
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "max_results": {"type": "integer", "description": "Max results (1-10)", "default": 5},
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    query = arguments.get("query", "")
    max_results = max(1, min(int(arguments.get("max_results", 5)), 10))

    if not query:
        return [TextContent(type="text", text="Error: query is required")]

    try:
        ddgs = DDGS()
        if name == "web_search":
            results = ddgs.text(query, max_results=max_results)
            formatted = [
                {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in results
            ]
        elif name == "web_search_news":
            results = ddgs.news(query, max_results=max_results)
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
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        return [TextContent(type="text", text=json.dumps(formatted, ensure_ascii=False))]
    except Exception as e:
        logger.exception("DuckDuckGo search failed for tool '%s'", name)
        return [TextContent(type="text", text=f"Search error: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
