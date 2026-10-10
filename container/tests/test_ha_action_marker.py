"""The HA client flags the per-dispatch action marker on every HA write (#132)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from app.ha_client.action_marker import is_read_only_ha_call, note_ha_action_started, track_ha_actions
from app.ha_client.rest import HARestClient
from app.ha_client.websocket import HAWebSocketClient


def _rest_client(ws=None) -> HARestClient:
    client = HARestClient()
    client._base_url = "http://ha.local"
    client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})
    client._state_observer = ws
    return client


@respx.mock
async def test_rest_call_service_flags_marker_before_request():
    """A calendar/lists/send/timer executor calling ha_client.call_service
    directly is covered: the client itself flags the marker."""
    seen: dict[str, bool] = {}
    with track_ha_actions() as marker:

        def _respond(request):
            seen["started_before_send"] = marker.started
            return httpx.Response(200, json=[])

        respx.post("http://ha.local/api/services/todo/add_item").mock(side_effect=_respond)
        client = _rest_client()
        await client.call_service("todo", "add_item", "todo.shopping", {"item": "milk"})
        await client.close()

    assert seen["started_before_send"] is True
    assert marker.started is True


@respx.mock
async def test_rest_read_only_service_does_not_flag_marker():
    respx.post("http://ha.local/api/services/calendar/get_events").mock(
        return_value=httpx.Response(200, json={"service_response": {}})
    )
    with track_ha_actions() as marker:
        client = _rest_client()
        await client.call_service("calendar", "get_events", "calendar.home", {}, return_response=True)
        await client.close()

    assert marker.started is False


@respx.mock
async def test_rest_failure_after_send_still_flags_marker():
    """A 5xx means HA may have executed the call: the marker stays set."""
    respx.post("http://ha.local/api/services/notify/mobile_app").mock(return_value=httpx.Response(500))
    with track_ha_actions() as marker:
        client = _rest_client()
        with pytest.raises(httpx.HTTPStatusError):
            await client.call_service("notify", "mobile_app", None, {"message": "hi"})
        await client.close()

    assert marker.started is True


async def test_send_ws_command_flags_marker_for_writes_only():
    ws = MagicMock()
    ws.is_connected.return_value = True
    ws.send_command = AsyncMock(return_value=None)
    client = _rest_client(ws)

    with track_ha_actions() as read_marker:
        await client.send_ws_command("todo/item/list", entity_id="todo.shopping")
    with track_ha_actions() as write_marker:
        await client.send_ws_command("calendar/event/delete", entity_id="calendar.home", uid="abc")
    await client.close()

    assert read_marker.started is False
    assert write_marker.started is True


async def test_ws_call_service_flags_marker():
    ws = HAWebSocketClient()
    ws.is_connected = MagicMock(return_value=True)
    ws.send_command = AsyncMock(return_value={"response": None})

    with track_ha_actions() as marker:
        await ws.call_service("timer", "start", entity_id="timer.kitchen")

    assert marker.started is True


async def test_automation_config_writes_flag_marker():
    client = HARestClient()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"result": "ok"})
    client._client = MagicMock()
    client._client.post = AsyncMock(return_value=response)
    client._client.delete = AsyncMock(return_value=response)

    with track_ha_actions() as save_marker:
        await client.save_automation_config("abc", {"alias": "x"})
    with track_ha_actions() as delete_marker:
        await client.delete_automation_config("abc")

    assert save_marker.started is True
    assert delete_marker.started is True


def test_marker_is_noop_outside_tracked_dispatch():
    note_ha_action_started("turn_on")  # must not raise


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("get_events", True),
        ("search", True),
        ("browse_media", True),
        ("config/entity_registry/list", True),
        ("turn_on", False),
        ("calendar/event/delete", False),
        ("add_item", False),
    ],
)
def test_is_read_only_ha_call(name, expected):
    assert is_read_only_ha_call(name) is expected
