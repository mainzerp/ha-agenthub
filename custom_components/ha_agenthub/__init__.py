"""HA-AgentHub Home Assistant custom integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .const import (
    CONF_NAME,
    CONF_SHIP_LOGS,
    CONF_SHIP_LOGS_LEVEL,
    DEFAULT_SHIP_LOGS,
    DEFAULT_SHIP_LOGS_LEVEL,
    DOMAIN,
    INTEGRATION_TITLE,
)
from .log_shipper import LogShipper

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
    if config_entry.version == 3 and not any(
        key in (config_entry.options or {})
        for key in (CONF_URL, CONF_API_KEY, CONF_NAME)
    ):
        return True
    old_version = config_entry.version
    data = dict(config_entry.data or {})
    original_data = dict(data)
    options = dict(config_entry.options or {})
    original_options = dict(options)

    # Older options flows stored connection fields in entry.options.  Those
    # values are the most recent user input, so they must replace stale data.
    # Keep unrelated options (for example log-shipping settings) intact.
    for key in (CONF_URL, CONF_API_KEY, CONF_NAME):
        if key in options:
            data[key] = options.pop(key)

    try:
        url = _normalize_url(data.get(CONF_URL, ""))
    except (AttributeError, TypeError, ValueError):
        url = ""
    if url:
        data[CONF_URL] = url

    name = data.get(CONF_NAME)
    if isinstance(name, str):
        name = name.strip()
    else:
        name = ""
    if not name:
        name = (
            config_entry.title
            if config_entry.title not in _LEGACY_ENTRY_TITLES
            else INTEGRATION_TITLE
        )
    if not name:
        name = INTEGRATION_TITLE
    if CONF_NAME in original_data or CONF_NAME in original_options:
        data[CONF_NAME] = name

    new_unique_id = url or (config_entry.entry_id if old_version == 1 else None)
    if new_unique_id and new_unique_id != config_entry.unique_id:
        for existing in hass.config_entries.async_entries(DOMAIN):
            if (
                existing.entry_id != config_entry.entry_id
                and existing.unique_id == new_unique_id
            ):
                logger.error(
                    "Cannot migrate HA-AgentHub config entry %s: URL %s is already configured",
                    config_entry.entry_id,
                    new_unique_id,
                )
                return False

    update_kwargs: dict[str, object] = {}
    if data != original_data:
        update_kwargs["data"] = data
    if options != original_options:
        update_kwargs["options"] = options
    if new_unique_id and new_unique_id != config_entry.unique_id:
        update_kwargs["unique_id"] = new_unique_id
    if config_entry.title != name:
        update_kwargs["title"] = name
    if old_version < 3:
        update_kwargs["version"] = 3

    if update_kwargs:
        hass.config_entries.async_update_entry(config_entry, **update_kwargs)

    if old_version < 3:
        logger.info(
            "Migrated HA-AgentHub config entry from version %d to 3",
            old_version,
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
        raise ConfigEntryError("HA-AgentHub config entry missing required URL")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "url": url,
        "api_key": api_key,
    }

    # Opt-in log shipping to the container (entry-scoped lifecycle).
    shipper = None
    if entry.options.get(CONF_SHIP_LOGS, DEFAULT_SHIP_LOGS):
        shipper = LogShipper(
            url,
            api_key,
            entry.options.get(CONF_SHIP_LOGS_LEVEL, DEFAULT_SHIP_LOGS_LEVEL),
        )
        shipper.start(hass, entry)
    hass.data[DOMAIN][entry.entry_id]["log_shipper"] = shipper

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload HA-AgentHub config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        shipper = hass.data[DOMAIN][entry.entry_id].get("log_shipper")
        if shipper:
            await shipper.stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
    return unload_ok
