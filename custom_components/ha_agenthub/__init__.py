"""HA-AgentHub Home Assistant custom integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, INTEGRATION_TITLE

# Config entries created by the old ``agent_assist`` integration.
_LEGACY_ENTRY_TITLES = frozenset({"Agent Assist"})

logger = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CONVERSATION]


def _normalize_url(url: str) -> str:
    normalized = (url or "").strip().rstrip("/")
    if normalized and not normalized.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    return normalized


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries to the current version."""
    if config_entry.version > 2:
        logger.error(
            "HA-AgentHub config entry version %d is newer than supported (max 2). "
            "Skipping migration.",
            config_entry.version,
        )
        return False
    if config_entry.version == 1:
        url = _normalize_url(config_entry.data.get(CONF_URL, ""))
        new_unique_id = url if url else config_entry.entry_id

        hass.config_entries.async_update_entry(
            config_entry,
            unique_id=new_unique_id,
            version=2,
        )
        old_unique_id = config_entry.unique_id
        logger.info(
            "Migrated HA-AgentHub config entry from version 1 to 2 (unique_id: %s -> %s)",
            old_unique_id,
            new_unique_id,
        )
    return True


async def _async_reload_entry_on_update(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
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
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
    return unload_ok
