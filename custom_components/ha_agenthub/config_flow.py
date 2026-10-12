"""Config flow for HA-AgentHub integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_NAME,
    CONF_SHIP_LOGS,
    CONF_SHIP_LOGS_LEVEL,
    CONF_STT_ENTITY_ID,
    CONF_WS_RECEIVE_TIMEOUT,
    DEFAULT_CONTAINER_URL,
    DEFAULT_SHIP_LOGS,
    DEFAULT_SHIP_LOGS_LEVEL,
    DEFAULT_WS_RECEIVE_TIMEOUT,
    DOMAIN,
    HEALTH_PATH,
    INTEGRATION_TITLE,
    SHIP_LOGS_LEVELS,
    parse_positive_timeout,
    resolve_ws_receive_timeout,
    stt_unique_id,
)

logger = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    normalized = (url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("URL is required")
    if " " in normalized or "\t" in normalized or "\n" in normalized:
        raise ValueError("URL must not contain whitespace")
    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("URL must start with http:// or https://")
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    return normalized


def _password_selector() -> TextSelector:
    return TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _timeout_default_str(value: Any) -> str:
    """Format the stored numeric timeout as string for the TextSelector default.

    Stored options hold the timeout as float; a numeric default in a text
    selector makes the HA frontend submit a number for the untouched field,
    which its selector validation rejects with "expected str".
    """
    return f"{float(value):g}"


def _build_user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=INTEGRATION_TITLE): TextSelector(),
            vol.Required(CONF_URL, default=DEFAULT_CONTAINER_URL): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            vol.Required(CONF_API_KEY): _password_selector(),
        }
    )


def _own_stt_entity_id(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Return the entity_id of this entry's STT wrapper entity, if registered."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "stt", DOMAIN, stt_unique_id(entry.entry_id)
    )
    return entity_id if isinstance(entity_id, str) and entity_id else None


def _validate_stt_entity_id(stt_entity_id: str, own_entity_id: str | None) -> bool:
    """Accept an stt entity other than the wrapper itself (no recursion)."""
    if not stt_entity_id:
        return True
    domain, _, object_id = stt_entity_id.partition(".")
    return domain == "stt" and bool(object_id) and stt_entity_id != own_entity_id


def _build_options_schema(
    current: dict[str, Any], own_stt_entity_id: str | None = None
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=current.get(CONF_NAME, "")): TextSelector(),
            vol.Required(
                CONF_URL, default=current.get(CONF_URL, DEFAULT_CONTAINER_URL)
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Optional(CONF_API_KEY, default=""): _password_selector(),
            vol.Optional(
                CONF_WS_RECEIVE_TIMEOUT,
                default=_timeout_default_str(
                    resolve_ws_receive_timeout(current.get(CONF_WS_RECEIVE_TIMEOUT))
                ),
            ): TextSelector(),
            vol.Optional(
                CONF_SHIP_LOGS,
                default=current.get(CONF_SHIP_LOGS, DEFAULT_SHIP_LOGS),
            ): bool,
            vol.Optional(
                CONF_SHIP_LOGS_LEVEL,
                default=current.get(CONF_SHIP_LOGS_LEVEL, DEFAULT_SHIP_LOGS_LEVEL),
            ): SelectSelector(
                SelectSelectorConfig(options=SHIP_LOGS_LEVELS, mode="dropdown")
            ),
            # suggested_value (not default) so the field can be cleared to
            # disable the STT wrapper entity.
            vol.Optional(
                CONF_STT_ENTITY_ID,
                description={"suggested_value": current.get(CONF_STT_ENTITY_ID)},
            ): EntitySelector(
                EntitySelectorConfig(
                    domain="stt",
                    exclude_entities=[own_stt_entity_id] if own_stt_entity_id else [],
                )
            ),
        }
    )


async def _validate_connection(
    hass: HomeAssistant, url: str, api_key: str
) -> str | None:
    """Test connection to the container. Returns error key or None."""
    try:
        normalized_url = _normalize_url(url)
    except ValueError:
        return "invalid_url"
    trimmed_key = (api_key or "").strip()
    if not normalized_url or not trimmed_key:
        return "invalid_auth"

    session = async_get_clientsession(hass)
    try:
        headers = {"Authorization": f"Bearer {trimmed_key}"}
        async with session.get(
            f"{normalized_url}{HEALTH_PATH}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status in {401, 403}:
                return "invalid_auth"
            if resp.status != 200:
                return "cannot_connect"
            try:
                data = await resp.json()
            except ValueError:
                return "cannot_connect"
            if not isinstance(data, dict) or data.get("status") != "ok":
                return "cannot_connect"
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "cannot_connect"
    return None


class HaAgentHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for HA-AgentHub."""

    VERSION = 3

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> HaAgentHubOptionsFlow:
        return HaAgentHubOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial user configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                url = _normalize_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                api_key = (user_input[CONF_API_KEY] or "").strip()
                name = (user_input.get(CONF_NAME) or INTEGRATION_TITLE).strip()

                error = await _validate_connection(self.hass, url, api_key)
                if error:
                    errors["base"] = error
                else:
                    await self.async_set_unique_id(url)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=name,
                        data={CONF_NAME: name, CONF_URL: url, CONF_API_KEY: api_key},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_user_schema(),
            errors=errors,
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle re-authentication when the API key is rejected."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            try:
                url = _normalize_url(
                    user_input.get(CONF_URL, entry.data.get(CONF_URL, ""))
                )
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                api_key = (user_input.get(CONF_API_KEY) or "").strip()
                error = await _validate_connection(self.hass, url, api_key)
                if error:
                    errors["base"] = error
                else:
                    entry_updates: dict[str, Any] = {
                        "data": {
                            **entry.data,
                            CONF_URL: url,
                            CONF_API_KEY: api_key,
                        },
                    }
                    # Update unique_id when URL changes to keep deduplication correct.
                    if url != entry.unique_id:
                        for existing in self.hass.config_entries.async_entries(DOMAIN):
                            if (
                                existing.unique_id == url
                                and existing.entry_id != entry.entry_id
                            ):
                                return self.async_abort(reason="already_configured")
                        entry_updates["unique_id"] = url
                    self.hass.config_entries.async_update_entry(entry, **entry_updates)
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth",
            data_schema=_build_user_schema(),
            errors=errors,
        )


class HaAgentHubOptionsFlow(OptionsFlow):
    """Options flow for reconfiguring HA-AgentHub."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        errors: dict[str, str] = {}
        current = {
            **(self._entry.data or {}),
            **(self._entry.options or {}),
        }
        own_stt_entity_id = _own_stt_entity_id(self.hass, self._entry)

        if user_input is not None:
            try:
                url = _normalize_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                timeout_provided = CONF_WS_RECEIVE_TIMEOUT in user_input
                raw_timeout = user_input.get(
                    CONF_WS_RECEIVE_TIMEOUT,
                    current.get(CONF_WS_RECEIVE_TIMEOUT),
                )
                ws_receive_timeout = parse_positive_timeout(raw_timeout)
                if ws_receive_timeout is None and not timeout_provided:
                    ws_receive_timeout = float(DEFAULT_WS_RECEIVE_TIMEOUT)
                # The frontend omits a cleared optional field, so a missing
                # key means "disabled" and does not fall back to the stored
                # value.
                stt_entity_id = str(user_input.get(CONF_STT_ENTITY_ID) or "").strip()
                if ws_receive_timeout is None:
                    errors[CONF_WS_RECEIVE_TIMEOUT] = "invalid_timeout"
                elif not _validate_stt_entity_id(stt_entity_id, own_stt_entity_id):
                    errors[CONF_STT_ENTITY_ID] = "invalid_stt_entity"
                else:
                    ship_logs = bool(
                        user_input.get(
                            CONF_SHIP_LOGS,
                            current.get(CONF_SHIP_LOGS, DEFAULT_SHIP_LOGS),
                        )
                    )
                    ship_logs_level = str(
                        user_input.get(
                            CONF_SHIP_LOGS_LEVEL,
                            current.get(CONF_SHIP_LOGS_LEVEL, DEFAULT_SHIP_LOGS_LEVEL),
                        )
                    )
                    # The selector constrains UI input; fall back to the
                    # default for out-of-list values instead of a new error.
                    if ship_logs_level not in SHIP_LOGS_LEVELS:
                        ship_logs_level = DEFAULT_SHIP_LOGS_LEVEL
                    new_api_key = (user_input.get(CONF_API_KEY) or "").strip()
                    api_key = new_api_key or current.get(CONF_API_KEY, "")
                    new_name = (user_input.get(CONF_NAME) or "").strip()
                    name = new_name or current.get(CONF_NAME, self._entry.title)

                    error = await _validate_connection(self.hass, url, api_key)
                    if error:
                        errors["base"] = error
                    else:
                        # Update unique_id when URL changes to keep deduplication correct.
                        entry_updates: dict[str, Any] = {
                            "title": name,
                            "data": {
                                **self._entry.data,
                                CONF_NAME: name,
                                CONF_URL: url,
                                CONF_API_KEY: api_key,
                            },
                        }
                        if url != self._entry.unique_id:
                            for existing in self.hass.config_entries.async_entries(
                                DOMAIN
                            ):
                                if (
                                    existing.unique_id == url
                                    and existing.entry_id != self._entry.entry_id
                                ):
                                    errors["base"] = "already_configured"
                                    break
                            else:
                                entry_updates["unique_id"] = url
                            if errors:
                                return self.async_show_form(
                                    step_id="init",
                                    data_schema=_build_options_schema(
                                        current, own_stt_entity_id
                                    ),
                                    errors=errors,
                                )
                        self.hass.config_entries.async_update_entry(
                            self._entry, **entry_updates
                        )
                        # The flow manager replaces entry.options with this
                        # dict: every option key must be written here.
                        options: dict[str, Any] = {
                            CONF_WS_RECEIVE_TIMEOUT: ws_receive_timeout,
                            CONF_SHIP_LOGS: ship_logs,
                            CONF_SHIP_LOGS_LEVEL: ship_logs_level,
                        }
                        if stt_entity_id:
                            options[CONF_STT_ENTITY_ID] = stt_entity_id
                        return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=_build_options_schema(current, own_stt_entity_id),
            errors=errors,
        )
