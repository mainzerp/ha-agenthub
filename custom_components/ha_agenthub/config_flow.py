"""Config flow for HA-AgentHub integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_URL, CONF_API_KEY
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import (
    DOMAIN,
    CONF_NAME,
    DEFAULT_CONTAINER_URL,
    HEALTH_PATH,
    INTEGRATION_TITLE,
)

logger = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    normalized = (url or "").strip().rstrip("/")
    if normalized and not normalized.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    return normalized


def _password_selector() -> TextSelector:
    return TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _build_user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=INTEGRATION_TITLE): TextSelector(),
            vol.Required(CONF_URL, default=DEFAULT_CONTAINER_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Required(CONF_API_KEY): _password_selector(),
        }
    )


def _build_options_schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=current.get(CONF_NAME, "")): TextSelector(),
            vol.Required(CONF_URL, default=current.get(CONF_URL, DEFAULT_CONTAINER_URL)): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Optional(CONF_API_KEY, default=""): _password_selector(),
        }
    )


async def _validate_connection(url: str, api_key: str) -> str | None:
    """Test connection to the container. Returns error key or None."""
    normalized_url = _normalize_url(url)
    trimmed_key = (api_key or "").strip()
    if not normalized_url or not trimmed_key:
        return "invalid_auth"

    try:
        async with aiohttp.ClientSession() as session:
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
                data = await resp.json()
                if data.get("status") != "ok":
                    return "cannot_connect"
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return "cannot_connect"
    return None


async def async_migrate_entry(hass, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries to the current version."""
    if config_entry.version == 1:
        # Migrate from version 1: unique_id was DOMAIN, now it should be the URL
        url = _normalize_url(config_entry.data.get(CONF_URL, ""))
        if url:
            new_unique_id = url
        else:
            new_unique_id = config_entry.entry_id

        hass.config_entries.async_update_entry(
            config_entry,
            unique_id=new_unique_id,
            version=2,
        )
        logger.info(
            "Migrated HA-AgentHub config entry from version 1 to 2 (unique_id: %s -> %s)",
            DOMAIN,
            new_unique_id,
        )
    return True


class HaAgentHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for HA-AgentHub."""

    VERSION = 2

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> HaAgentHubOptionsFlow:
        return HaAgentHubOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial user configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = _normalize_url(user_input[CONF_URL])
            api_key = (user_input[CONF_API_KEY] or "").strip()
            name = (user_input.get(CONF_NAME) or INTEGRATION_TITLE).strip()

            error = await _validate_connection(url, api_key)
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
            url = _normalize_url(user_input[CONF_URL])
            new_api_key = (user_input.get(CONF_API_KEY) or "").strip()
            api_key = new_api_key or current.get(CONF_API_KEY, "")
            new_name = (user_input.get(CONF_NAME) or "").strip()
            name = new_name or current.get(CONF_NAME, self._entry.title)

            error = await _validate_connection(url, api_key)
            if error:
                errors["base"] = error
            else:
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    title=name,
                    data={
                        CONF_NAME: name,
                        CONF_URL: url,
                        CONF_API_KEY: api_key,
                    },
                    options={},
                )
                return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="init",
            data_schema=_build_options_schema(current),
            errors=errors,
        )
