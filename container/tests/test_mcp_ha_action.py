"""Tests for the HA action MCP server."""

from __future__ import annotations

import json
import os

import httpx
import pytest
import respx

TEST_HA_URL = "http://ha.local"
TEST_HA_TOKEN = "test-token-abc123"


def _set_env():
    os.environ["HA_URL"] = TEST_HA_URL
    os.environ["HA_TOKEN"] = TEST_HA_TOKEN


def _clear_env():
    os.environ.pop("HA_URL", None)
    os.environ.pop("HA_TOKEN", None)


@pytest.mark.asyncio
@respx.mock
async def test_call_service_success():
    _set_env()

    respx.get("http://ha.local/api/services").mock(
        return_value=httpx.Response(200, json={"light": {}, "switch": {}, "climate": {}})
    )
    respx.post("http://ha.local/api/services/light/turn_on?return_response=true").mock(
        return_value=httpx.Response(200, json=[{"entity_id": "light.kitchen", "state": "on"}])
    )

    from app.mcp.servers.ha_action_server import call_tool

    result = await call_tool(
        "ha_call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.kitchen",
        },
    )

    assert len(result) == 1
    data = json.loads(result[0].text)
    assert data[0]["entity_id"] == "light.kitchen"
    assert data[0]["state"] == "on"

    _clear_env()


@pytest.mark.asyncio
@respx.mock
async def test_call_service_invalid_domain():
    _set_env()

    respx.get("http://ha.local/api/services").mock(return_value=httpx.Response(200, json={"light": {}, "switch": {}}))

    from app.mcp.servers.ha_action_server import call_tool

    result = await call_tool(
        "ha_call_service",
        {
            "domain": "vacuum",
            "service": "start",
            "entity_id": "vacuum.downstairs",
        },
    )

    assert len(result) == 1
    assert "domain 'vacuum' not found" in result[0].text
    assert "light" in result[0].text
    assert "switch" in result[0].text

    _clear_env()


@pytest.mark.asyncio
@respx.mock
async def test_call_service_ha_error():
    _set_env()

    respx.get("http://ha.local/api/services").mock(return_value=httpx.Response(200, json={"light": {}}))
    respx.post("http://ha.local/api/services/light/turn_on?return_response=true").mock(
        return_value=httpx.Response(400, json={"message": "Entity not found"})
    )

    from app.mcp.servers.ha_action_server import call_tool

    result = await call_tool(
        "ha_call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.nonexistent",
        },
    )

    assert len(result) == 1
    assert "HA API error" in result[0].text

    _clear_env()


@pytest.mark.asyncio
@respx.mock
async def test_get_states_all():
    _set_env()

    states = [
        {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
        {"entity_id": "light.living_room", "state": "off", "attributes": {}},
    ]
    respx.get("http://ha.local/api/states").mock(return_value=httpx.Response(200, json=states))

    from app.mcp.servers.ha_action_server import call_tool

    result = await call_tool("ha_get_states", {})

    assert len(result) == 1
    data = json.loads(result[0].text)
    assert len(data) == 2
    assert data[0]["entity_id"] == "light.kitchen"
    assert data[1]["entity_id"] == "light.living_room"

    _clear_env()


@pytest.mark.asyncio
@respx.mock
async def test_get_states_single():
    _set_env()

    state = {"entity_id": "light.kitchen", "state": "on", "attributes": {"brightness": 255}}
    respx.get("http://ha.local/api/states/light.kitchen").mock(return_value=httpx.Response(200, json=state))

    from app.mcp.servers.ha_action_server import call_tool

    result = await call_tool("ha_get_states", {"entity_id": "light.kitchen"})

    assert len(result) == 1
    data = json.loads(result[0].text)
    assert data["entity_id"] == "light.kitchen"
    assert data["state"] == "on"

    _clear_env()


@pytest.mark.asyncio
@respx.mock
async def test_get_services_all():
    _set_env()

    services = {"light": {}, "switch": {}, "climate": {}}
    respx.get("http://ha.local/api/services").mock(return_value=httpx.Response(200, json=services))

    from app.mcp.servers.ha_action_server import call_tool

    result = await call_tool("ha_get_services", {})

    assert len(result) == 1
    data = json.loads(result[0].text)
    assert "light" in data
    assert "switch" in data
    assert "climate" in data

    _clear_env()


@pytest.mark.asyncio
@respx.mock
async def test_get_services_by_domain():
    _set_env()

    services = {
        "light": {"services": {"turn_on": {}, "turn_off": {}}},
        "switch": {"services": {"turn_on": {}, "turn_off": {}}},
    }
    respx.get("http://ha.local/api/services").mock(return_value=httpx.Response(200, json=services))

    from app.mcp.servers.ha_action_server import call_tool

    result = await call_tool("ha_get_services", {"domain": "light"})

    assert len(result) == 1
    data = json.loads(result[0].text)
    assert "light" in data
    assert "switch" not in data

    _clear_env()


@pytest.mark.asyncio
async def test_missing_env_vars():
    _clear_env()

    from app.mcp.servers.ha_action_server import call_tool

    result = await call_tool("ha_get_states", {})

    assert len(result) == 1
    assert "HA is not configured" in result[0].text
