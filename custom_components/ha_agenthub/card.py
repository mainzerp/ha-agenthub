"""Serve the Lovelace activity card from the integration.

The card module ships in ``frontend/`` next to this file. It is exposed as a
static path and added as an extra frontend module, so every dashboard loads it
without a manually created dashboard resource. Home Assistant cannot remove
static paths or extra modules at runtime, so nothing is unregistered on
unload; registration happens once per HA run and is idempotent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import CARD_FILENAME, CARD_URL_PATH, DOMAIN

logger = logging.getLogger(__name__)

CARD_PATH = Path(__file__).parent / "frontend" / CARD_FILENAME

# hass.data[DOMAIN] flag: static path and extra module are registered.
_CARD_REGISTERED = "card_registered"


async def _async_integration_version(hass: HomeAssistant) -> str:
    """Return the integration version used for cache busting (``?v=``)."""
    try:
        integration = await async_get_integration(hass, DOMAIN)
    except Exception:
        logger.debug("Could not resolve the integration version", exc_info=True)
        return "0"
    return str(integration.version or "0")


async def async_register_card(hass: HomeAssistant) -> None:
    """Register the card module once per HA run; never raises."""
    domain_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_CARD_REGISTERED):
        return
    try:
        # File system access stays off the event loop.
        if not await hass.async_add_executor_job(CARD_PATH.is_file):
            logger.warning(
                "Activity card module %s is missing; the Lovelace card is unavailable",
                CARD_PATH.name,
            )
            return
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL_PATH, str(CARD_PATH), True)]
        )
        # Set right after the static path exists: registering it twice raises.
        domain_data[_CARD_REGISTERED] = True
        version = await _async_integration_version(hass)
        add_extra_js_url(hass, f"{CARD_URL_PATH}?v={version}")
    except Exception:
        logger.warning("Could not register the Lovelace activity card", exc_info=True)
