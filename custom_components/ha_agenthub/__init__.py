"""HA-AgentHub Home Assistant custom integration."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv

from .activity import ActivityRelay
from .activity_ws import async_register_activity_websocket
from .card import async_register_card
from .client import (
    AgentHubAuthError,
    AgentHubConnectionError,
    AgentHubResponseError,
    async_get_json,
)
from .const import (
    CONF_NAME,
    CONF_SHIP_LOGS,
    CONF_SHIP_LOGS_LEVEL,
    DEFAULT_SHIP_LOGS,
    DEFAULT_SHIP_LOGS_LEVEL,
    DOMAIN,
    HEALTH_PATH,
    INTEGRATION_TITLE,
    SETUP_PROBE_TIMEOUT,
)
from .log_shipper import LogShipper

# Config entries created by the old ``agent_assist`` integration.
_LEGACY_ENTRY_TITLES = frozenset({"Agent Assist"})

logger = logging.getLogger(__name__)

# The STT platform only adds an entity when the ``stt_entity_id`` option is set.
PLATFORMS: list[Platform] = [Platform.AI_TASK, Platform.CONVERSATION, Platform.STT]

# Values of ``AgentHubRuntimeData.connection_status`` (shown in diagnostics).
CONNECTION_OK = "ok"
CONNECTION_AUTH_FAILED = "auth_failed"


@dataclass(slots=True)
class AgentHubRuntimeData:
    """Per-entry state, stored in ``entry.runtime_data`` while loaded."""

    url: str
    api_key: str
    log_shipper: LogShipper | None = None
    # Relay of the container's live activity stream for the Lovelace card.
    activity: ActivityRelay | None = None
    # Result of the last container call that told us something about the
    # connection (setup probe, runtime auth failure, recovered request).
    connection_status: str = CONNECTION_OK
    connection_status_changed: float = field(default_factory=time.time)
    # Guards the runtime reauth trigger: one reauth flow per auth-failure
    # episode, shared by the conversation, AI Task and STT entities.
    reauth_requested: bool = False

    def set_connection_status(self, status: str) -> None:
        if status != self.connection_status:
            self.connection_status = status
            self.connection_status_changed = time.time()


type AgentHubConfigEntry = ConfigEntry[AgentHubRuntimeData]


def _runtime_data(entry: ConfigEntry) -> AgentHubRuntimeData | None:
    """Return the entry's runtime data, or None while it is not loaded."""
    runtime = getattr(entry, "runtime_data", None)
    return runtime if isinstance(runtime, AgentHubRuntimeData) else None


@callback
def async_report_auth_failure(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Start the reauth flow after the container rejected the API key.

    Called by the entities on a runtime 401/403. Starts at most one reauth
    flow per failure episode; :func:`async_report_auth_ok` ends the episode.
    HA itself also skips the start while a reauth flow is in progress.
    """
    runtime = _runtime_data(entry)
    if runtime is not None:
        runtime.set_connection_status(CONNECTION_AUTH_FAILED)
        if runtime.reauth_requested:
            return
        runtime.reauth_requested = True
    logger.warning(
        "HA-AgentHub container rejected the API key; starting re-authentication"
    )
    entry.async_start_reauth(hass)


@callback
def async_report_auth_ok(entry: ConfigEntry) -> None:
    """Record an authenticated container answer (ends a failure episode)."""
    runtime = _runtime_data(entry)
    if runtime is not None:
        runtime.reauth_requested = False
        runtime.set_connection_status(CONNECTION_OK)


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
    # Reauth and reconfigure flows finish with ``async_update_reload_and_abort``,
    # which schedules its own reload. HA runs update listeners eagerly while
    # that flow is still in progress, so skipping here avoids a double reload.
    if any(entry.async_get_active_flows(hass, {SOURCE_REAUTH, SOURCE_RECONFIGURE})):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_probe_container(hass: HomeAssistant, url: str, api_key: str) -> None:
    """Make one authenticated call so setup fails fast with the right state.

    ``GET /api/health`` requires the API key (unlike ``/healthz``), so it
    tells a rejected key apart from an unreachable container. Messages name
    the host only: the URL may embed credentials.
    """
    try:
        host = urlparse(url).hostname or "container"
    except ValueError:
        host = "container"
    try:
        data = await async_get_json(
            hass, url, api_key, HEALTH_PATH, timeout=SETUP_PROBE_TIMEOUT
        )
    except AgentHubAuthError as err:
        raise ConfigEntryAuthFailed(
            f"HA-AgentHub container rejected the API key (HTTP {err.status})"
        ) from err
    except AgentHubConnectionError as err:
        raise ConfigEntryNotReady(
            f"HA-AgentHub container at {host} is not reachable: {err}"
        ) from err
    except AgentHubResponseError as err:
        raise ConfigEntryNotReady(
            f"HA-AgentHub container at {host} is not ready: {err}"
        ) from err
    if data.get("status") != "ok":
        raise ConfigEntryNotReady(f"HA-AgentHub container at {host} is not healthy")


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the Lovelace activity card pieces once per HA run.

    Both registrations are entry-independent (HA cannot remove them again), so
    the card loads and reports "offline" even while the entry is retrying.
    """
    async_register_activity_websocket(hass)
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AgentHubConfigEntry) -> bool:
    """Set up HA-AgentHub from a config entry."""
    if entry.title in _LEGACY_ENTRY_TITLES:
        hass.config_entries.async_update_entry(entry, title=INTEGRATION_TITLE)

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

    # Raises ConfigEntryAuthFailed (HA starts the reauth flow) or
    # ConfigEntryNotReady (HA retries setup with backoff).
    await _async_probe_container(hass, url, api_key)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry_on_update))

    runtime = AgentHubRuntimeData(url=url, api_key=api_key)
    # The upstream stream connection only opens on the first card subscriber.
    runtime.activity = ActivityRelay(hass, url, api_key)
    # Opt-in log shipping to the container (entry-scoped lifecycle).
    if entry.options.get(CONF_SHIP_LOGS, DEFAULT_SHIP_LOGS):
        runtime.log_shipper = LogShipper(
            url,
            api_key,
            entry.options.get(CONF_SHIP_LOGS_LEVEL, DEFAULT_SHIP_LOGS_LEVEL),
        )
        runtime.log_shipper.start(hass, entry)
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AgentHubConfigEntry) -> bool:
    """Unload HA-AgentHub config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime = _runtime_data(entry)
        if runtime is not None and runtime.activity is not None:
            await runtime.activity.async_stop()
        if runtime is not None and runtime.log_shipper is not None:
            await runtime.log_shipper.stop()
    return unload_ok
