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
from homeassistant.const import CONF_URL, CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_NAME,
    CONF_WS_RECEIVE_TIMEOUT,
    DEFAULT_CONTAINER_URL,
    DEFAULT_WS_RECEIVE_TIMEOUT,
    DOMAIN,
    HEALTH_PATH,
    INTEGRATION_TITLE,
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


def _build_options_schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=current.get(CONF_NAME, "")): TextSelector(),
            vol.Required(
                CONF_URL, default=current.get(CONF_URL, DEFAULT_CONTAINER_URL)
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Optional(CONF_API_KEY, default=""): _password_selector(),
            vol.Optional(
                CONF_WS_RECEIVE_TIMEOUT,
                default=current.get(
                    CONF_WS_RECEIVE_TIMEOUT, DEFAULT_WS_RECEIVE_TIMEOUT
                ),
            ): TextSelector(),
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

        if user_input is not None:
            try:
                url = _normalize_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                try:
                    ws_receive_timeout = float(
                        user_input.get(
                            CONF_WS_RECEIVE_TIMEOUT,
                            current.get(
                                CONF_WS_RECEIVE_TIMEOUT,
                                DEFAULT_WS_RECEIVE_TIMEOUT,
                            ),
                        )
                    )
                except (TypeError, ValueError):
                    errors[CONF_WS_RECEIVE_TIMEOUT] = "invalid_timeout"
                else:
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
                                    data_schema=_build_options_schema(current),
                                    errors=errors,
                                )
                        self.hass.config_entries.async_update_entry(
                            self._entry, **entry_updates
                        )
                        return self.async_create_entry(
                            data={CONF_WS_RECEIVE_TIMEOUT: ws_receive_timeout}
                        )

        return self.async_show_form(
            step_id="init",
            data_schema=_build_options_schema(current),
            errors=errors,
        )
