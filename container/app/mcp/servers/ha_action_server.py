"""Home Assistant action MCP server (stdio transport)."""

import json
import logging
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)
server = Server("ha-action")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="ha_call_service",
            description="Call any Home Assistant service on a given entity.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "HA domain (e.g. light, switch, climate)"},
                    "service": {"type": "string", "description": "Service name (e.g. turn_on, turn_off)"},
                    "entity_id": {"type": "string", "description": "Full entity ID (e.g. light.kitchen)"},
                    "service_data": {
                        "type": "object",
                        "description": "Optional additional data for the service call",
                        "default": {},
                    },
                },
                "required": ["domain", "service", "entity_id"],
            },
        ),
        Tool(
            name="ha_get_states",
            description="Get current state of one or all Home Assistant entities.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Optional entity ID to filter by"},
                },
            },
        ),
        Tool(
            name="ha_get_services",
            description="List available Home Assistant services, optionally filtered by domain.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Optional domain to filter by (e.g. light, switch)"},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    ha_url = os.environ.get("HA_URL")
    ha_token = os.environ.get("HA_TOKEN")

    if not ha_url or not ha_token:
        return [TextContent(type="text", text="Error: HA is not configured. Set HA_URL and HA_TOKEN.")]

    auth_headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(
            base_url=ha_url.rstrip("/"),
            headers=auth_headers,
            timeout=30.0,
        ) as client:
            if name == "ha_call_service":
                domain = arguments.get("domain")
                service = arguments.get("service")
                entity_id = arguments.get("entity_id")
                service_data = arguments.get("service_data", {})

                if not domain or not service or not entity_id:
                    return [TextContent(type="text", text="Error: domain, service, and entity_id are required")]

                services_resp = await client.get("/api/services")
                services_resp.raise_for_status()
                services_data = services_resp.json()
                available_domains = list(services_data) if isinstance(services_data, dict) else []
                if domain not in available_domains:
                    return [
                        TextContent(
                            type="text",
                            text=f"Error: domain '{domain}' not found. Available domains: {', '.join(sorted(available_domains))}",
                        )
                    ]

                body = {"entity_id": entity_id}
                body.update(service_data)
                resp = await client.post(
                    f"/api/services/{domain}/{service}",
                    json=body,
                    params={"return_response": "true"},
                )
                resp.raise_for_status()
                return [TextContent(type="text", text=json.dumps(resp.json(), ensure_ascii=False))]

            elif name == "ha_get_states":
                entity_id = arguments.get("entity_id")
                if entity_id:
                    resp = await client.get(f"/api/states/{entity_id}")
                else:
                    resp = await client.get("/api/states")
                resp.raise_for_status()
                return [TextContent(type="text", text=json.dumps(resp.json(), ensure_ascii=False))]

            elif name == "ha_get_services":
                domain = arguments.get("domain")
                resp = await client.get("/api/services")
                resp.raise_for_status()
                result = resp.json()
                if domain:
                    result = {domain: result.get(domain, [])} if isinstance(result, dict) else result
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except httpx.HTTPStatusError as e:
        logger.exception("HA API HTTP error for tool '%s'", name)
        return [TextContent(type="text", text=f"HA API error ({e.response.status_code}): {e.response.text}")]
    except Exception as e:
        logger.exception("HA action failed for tool '%s'", name)
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
