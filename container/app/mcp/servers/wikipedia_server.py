"""Wikipedia search MCP server (stdio transport)."""

from __future__ import annotations

import asyncio
import json
import logging

import wikipedia  # type: ignore[import-untyped]
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
                name="wikipedia_search",
                description="Search Wikipedia for articles matching a query. Returns titles and short summaries.",
                input_schema={
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
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Exact Wikipedia article title"},
                        "sentences": {"type": "integer", "description": "Number of sentences (1-20)", "default": 5},
                    },
                    "required": ["title"],
                },
            ),
        ]
    )


async def handle_call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
    name = params.name
    arguments = params.arguments or {}
    try:
        if name == "wikipedia_search":
            query = arguments.get("query", "")
            results = max(1, min(int(arguments.get("results", 3)), 10))
            if not query:
                return CallToolResult(
                    content=[TextContent(type="text", text="Error: query is required")], is_error=True
                )
            search_results = await asyncio.to_thread(wikipedia.search, query, results=results)
            output = []
            for title in search_results:
                try:
                    summary = await asyncio.to_thread(wikipedia.summary, title, sentences=1)
                    output.append({"title": title, "summary": summary})
                except Exception:
                    output.append({"title": title, "summary": ""})
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(output, ensure_ascii=False))])

        elif name == "wikipedia_summary":
            title = arguments.get("title", "")
            sentences = max(1, min(int(arguments.get("sentences", 5)), 20))
            if not title:
                return CallToolResult(
                    content=[TextContent(type="text", text="Error: title is required")], is_error=True
                )
            summary = await asyncio.to_thread(wikipedia.summary, title, sentences=sentences)
            return CallToolResult(
                content=[
                    TextContent(type="text", text=json.dumps({"title": title, "summary": summary}, ensure_ascii=False))
                ]
            )

        else:
            return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")], is_error=True)
    except Exception as e:
        logger.exception("Wikipedia tool '%s' failed", name)
        return CallToolResult(content=[TextContent(type="text", text=f"Wikipedia error: {e}")], is_error=True)


server = Server("wikipedia-search", on_list_tools=handle_list_tools, on_call_tool=handle_call_tool)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
