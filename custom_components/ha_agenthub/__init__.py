"""HA-AgentHub Home Assistant custom integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant

from .config_flow import async_migrate_entry
from .const import DOMAIN, INTEGRATION_TITLE

# Config entries created by the old ``agent_assist`` integration.
_LEGACY_ENTRY_TITLES = frozenset({"Agent Assist"})

logger = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CONVERSATION]


async def _async_reload_entry_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when config entry data changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA-AgentHub from a config entry."""
    if entry.title in _LEGACY_ENTRY_TITLES:
        hass.config_entries.async_update_entry(entry, title=INTEGRATION_TITLE)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry_on_update))

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "url": entry.data[CONF_URL],
        "api_key": entry.data[CONF_API_KEY],
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload HA-AgentHub config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
