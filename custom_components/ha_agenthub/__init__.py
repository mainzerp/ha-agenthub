"""HA-AgentHub Home Assistant custom integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_NAME, DOMAIN, INTEGRATION_TITLE

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
    if config_entry.version > 3:
        logger.error(
            "HA-AgentHub config entry version %d is newer than supported (max 3). "
            "Skipping migration.",
            config_entry.version,
        )
        return False
    if config_entry.version == 1:
        try:
            url = _normalize_url(config_entry.data.get(CONF_URL, ""))
        except ValueError:
            url = ""
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
    if config_entry.version == 2:
        # P3-6: URL/API key are the source of truth in entry.data. Move any
        # values that were previously written to entry.options by older options
        # flows back into entry.data and clear them from options.
        options = dict(config_entry.options or {})
        data = dict(config_entry.data or {})
        migrated = False
        for key in (CONF_URL, CONF_API_KEY, CONF_NAME):
            if key in options:
                data.setdefault(key, options.pop(key))
                migrated = True
        if migrated:
            hass.config_entries.async_update_entry(
                config_entry,
                data=data,
                options=options,
            )
        hass.config_entries.async_update_entry(
            config_entry,
            version=3,
        )
        logger.info(
            "Migrated HA-AgentHub config entry from version 2 to 3 (URL/API key moved to entry.data)"
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

    # P3-6: URL and API key are the single source of truth in entry.data.
    # Options-based URL/API key are migrated to data by async_migrate_entry.
    url = entry.data.get(CONF_URL, "")
    api_key = entry.data.get(CONF_API_KEY, "")
    if not url:
        # Fall back to options only for entries that have not been migrated yet.
        url = entry.options.get(CONF_URL, "")
        api_key = entry.options.get(CONF_API_KEY, "")
    if not url:
        logger.error("HA-AgentHub config entry missing required URL")
        return False

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "url": url,
        "api_key": api_key,
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
