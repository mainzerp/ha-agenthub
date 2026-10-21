"""WebSocket command that feeds the Lovelace activity card.

``ha_agenthub/activity/subscribe`` (optional ``entry_id``) answers with a
result and then streams events to the caller::

    {"type": "snapshot", "agents": [...], "connected": bool}   first, once
    {"type": "activity", "event": {...}}                        per upstream event
    {"type": "status", "connected": bool}                       upstream state change

Access decision: the command is open to every authenticated Home Assistant
user (no ``require_admin``). The payload is content-free (span ids, agent ids,
timing, status): no conversation text, entity names or credentials. The API key
never leaves the bridge.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, WS_ACTIVITY_SUBSCRIBE

if TYPE_CHECKING:
    from homeassistant.components.websocket_api import ActiveConnection
    from homeassistant.config_entries import ConfigEntry

    from .activity import ActivityRelay

logger = logging.getLogger(__name__)

# hass.data[DOMAIN] flag: the command is registered once per HA run.
_WS_REGISTERED = "activity_ws_registered"

ERR_NOT_LOADED = "not_loaded"


def _find_relay(hass: HomeAssistant, entry_id: str | None) -> ActivityRelay | None:
    """Return the relay of the requested entry, else of the first loaded entry.

    Entries are visited in creation order. An explicit ``entry_id`` that is
    unknown, of another domain or not loaded yields ``None`` (no fallback).
    """
    from . import _runtime_data

    entries: list[ConfigEntry] = list(hass.config_entries.async_loaded_entries(DOMAIN))
    if entry_id is not None:
        entries = [entry for entry in entries if entry.entry_id == entry_id]
    for entry in entries:
        runtime = _runtime_data(entry)
        if runtime is not None and runtime.activity is not None:
            return runtime.activity
    return None


@callback
def async_register_activity_websocket(hass: HomeAssistant) -> None:
    """Register the subscribe command once per HA run."""
    domain_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_WS_REGISTERED):
        return
    websocket_api.async_register_command(hass, ws_activity_subscribe)
    domain_data[_WS_REGISTERED] = True


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_ACTIVITY_SUBSCRIBE,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_activity_subscribe(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Subscribe the caller to the container's live activity feed."""
    msg_id = msg["id"]
    relay = _find_relay(hass, msg.get("entry_id"))
    if relay is None:
        connection.send_error(msg_id, ERR_NOT_LOADED, "HA-AgentHub is not loaded")
        return

    closed = False
    snapshot_sent = False
    pending: list[dict[str, Any]] = []

    @callback
    def _send(message: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, message))

    @callback
    def _forward(message: dict[str, Any]) -> None:
        if closed:
            return
        if not snapshot_sent:
            # Events that arrive while the snapshot is being fetched wait
            # behind it so the snapshot is always the first event.
            pending.append(message)
            return
        _send(message)

    unsubscribe_relay = relay.async_subscribe(_forward)

    @callback
    def _unsubscribe() -> None:
        nonlocal closed
        closed = True
        pending.clear()
        unsubscribe_relay()

    connection.subscriptions[msg_id] = _unsubscribe
    connection.send_result(msg_id)

    agents = await relay.async_fetch_recent()
    if closed:
        return
    _send({"type": "snapshot", "agents": agents, "connected": relay.connected})
    snapshot_sent = True
    for message in pending:
        _send(message)
    pending.clear()
