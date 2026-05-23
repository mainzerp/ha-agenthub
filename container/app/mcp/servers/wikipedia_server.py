"""Wikipedia search MCP server (stdio transport)."""

from __future__ import annotations

import json
import logging

import wikipedia  # type: ignore[import-untyped]
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)
server = Server("wikipedia-search")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="wikipedia_search",
            description="Search Wikipedia for articles matching a query. Returns titles and short summaries.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "results": {"type": "integer", "description": "Max results (1-10)", "default": 3},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="wikipedia_summary",
            description="Get a summary of a Wikipedia article by exact title.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Exact Wikipedia article title"},
                    "sentences": {"type": "integer", "description": "Number of sentences (1-20)", "default": 5},
                },
                "required": ["title"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "wikipedia_search":
            query = arguments.get("query", "")
            results = max(1, min(int(arguments.get("results", 3)), 10))
            if not query:
                return [TextContent(type="text", text="Error: query is required")]
            search_results = wikipedia.search(query, results=results)
            output = []
            for title in search_results:
                try:
                    summary = wikipedia.summary(title, sentences=1)
                    output.append({"title": title, "summary": summary})
                except Exception:
                    output.append({"title": title, "summary": ""})
            return [TextContent(type="text", text=json.dumps(output, ensure_ascii=False))]

        elif name == "wikipedia_summary":
            title = arguments.get("title", "")
            sentences = max(1, min(int(arguments.get("sentences", 5)), 20))
            if not title:
                return [TextContent(type="text", text="Error: title is required")]
            summary = wikipedia.summary(title, sentences=sentences)
            return [TextContent(type="text", text=json.dumps({"title": title, "summary": summary}, ensure_ascii=False))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        logger.exception("Wikipedia tool '%s' failed", name)
        return [TextContent(type="text", text=f"Wikipedia error: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
