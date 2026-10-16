"""Diagnostics support for HA-AgentHub.

The download never contains the API key or the full container URL: the URL
may embed credentials (``http://user:pass@host``), so only its scheme, host
and port are reported.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from . import PLATFORMS, AgentHubConfigEntry, AgentHubRuntimeData
from .const import DOMAIN

TO_REDACT = {CONF_API_KEY, CONF_URL}


def _container_endpoint(url: str) -> dict[str, Any]:
    """Return the non-secret parts of the container URL."""
    try:
        parsed = urlparse(url or "")
        port = parsed.port
    except ValueError:
        return {"scheme": None, "host": None, "port": None}
    return {"scheme": parsed.scheme or None, "host": parsed.hostname, "port": port}


def _runtime_diagnostics(runtime: AgentHubRuntimeData | None) -> dict[str, Any]:
    if runtime is None:
        return {"loaded": False}
    shipper = runtime.log_shipper
    return {
        "loaded": True,
        "connection_status": runtime.connection_status,
        "connection_status_changed": datetime.fromtimestamp(
            runtime.connection_status_changed, UTC
        ).isoformat(),
        "reauth_requested": runtime.reauth_requested,
        "log_shipping": (
            {"enabled": True, **shipper.stats()}
            if shipper is not None
            else {"enabled": False}
        ),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AgentHubConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    integration = await async_get_integration(hass, DOMAIN)
    runtime = getattr(entry, "runtime_data", None)
    if not isinstance(runtime, AgentHubRuntimeData):
        runtime = None
    url = runtime.url if runtime is not None else entry.data.get(CONF_URL, "")

    registry = er.async_get(hass)
    entities = [
        {
            "entity_id": reg_entry.entity_id,
            "platform_domain": reg_entry.domain,
            "disabled": reg_entry.disabled_by is not None,
        }
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
    ]

    return {
        "integration_version": str(integration.version)
        if integration.version
        else None,
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "state": getattr(entry.state, "value", str(entry.state)),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "container": _container_endpoint(url),
        "runtime": _runtime_diagnostics(runtime),
        "platforms": [str(platform) for platform in PLATFORMS],
        "entities": entities,
    }
