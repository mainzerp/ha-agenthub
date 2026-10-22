"""Serve the Lovelace activity card from the integration.

The card module ships in ``frontend/`` next to this file. It is served by a
small view and added as an extra frontend module, so every dashboard loads it
without a manually created dashboard resource.

Caching: the extra module URL carries ``?v=<first 12 hex chars of the SHA-256
of the file content>``, so a changed file gets a new URL. The view itself
answers with ``Cache-Control: no-cache`` plus ETag/Last-Modified, so browsers,
the HA service worker and the Companion apps revalidate on every load and a
stale copy under an old URL is replaced as soon as the file changes. (The stock
static-path registration would send ``public, max-age=2678400``, i.e. 31 days.)

Home Assistant cannot remove routes or extra modules at runtime, so nothing is
unregistered on unload; registration happens once per HA run and is idempotent.
The content hash is computed at setup: a file replaced without an HA restart is
served fresh by the view, but the index keeps the old ``?v=`` until the next
start (the revalidating view makes that harmless).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import CARD_FILENAME, CARD_URL_PATH, DOMAIN

logger = logging.getLogger(__name__)

CARD_PATH = Path(__file__).parent / "frontend" / CARD_FILENAME

# Length of the content hash used as the ``?v=`` cache-busting value.
CARD_HASH_LENGTH = 12

# hass.data[DOMAIN] flag: the view and the extra module are registered.
_CARD_REGISTERED = "card_registered"

# Revalidate on every load; FileResponse adds ETag and Last-Modified and
# answers conditional requests with 304. The content type is set explicitly
# because the platform mimetypes table may map ``.js`` to
# ``application/javascript`` or to nothing.
CARD_CACHE_CONTROL = "no-cache"
CARD_CONTENT_TYPE = "text/javascript"
CARD_CHARSET = "utf-8"


class AgentHubCardView(HomeAssistantView):
    """Serve the card module with revalidation (no long-lived caching).

    Public on purpose: like HA's other frontend resources the module must load
    from the page without credentials, and it contains no secrets.
    """

    url = CARD_URL_PATH
    name = f"{DOMAIN}:card"
    requires_auth = False
    cors_allowed = False

    def __init__(self, path: Path) -> None:
        self._path = path

    async def get(self, request: web.Request) -> web.FileResponse:
        # FileResponse stats and opens the file in the executor and streams it
        # with sendfile: the event loop is never blocked. It also evaluates
        # If-None-Match / If-Modified-Since (304) and omits the body for HEAD.
        response = web.FileResponse(
            self._path, headers={"Cache-Control": CARD_CACHE_CONTROL}
        )
        response.content_type = CARD_CONTENT_TYPE
        response.charset = CARD_CHARSET
        return response

    # HomeAssistantView registers HEAD only when the view defines it.
    head = get


def _inspect_card(path: Path) -> tuple[bool, str | None]:
    """Blocking helper (run in the executor): ``(exists, content_hash)``.

    ``content_hash`` is ``None`` when the file exists but cannot be read.
    """
    if not path.is_file():
        return False, None
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        logger.debug(
            "Could not read the activity card module for hashing", exc_info=True
        )
        return True, None
    return True, digest[:CARD_HASH_LENGTH]


async def _async_integration_version(hass: HomeAssistant) -> str:
    """Return the integration version, the fallback ``?v=`` value."""
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
        # File system access (existence check and hashing) stays off the loop.
        exists, content_hash = await hass.async_add_executor_job(
            _inspect_card, CARD_PATH
        )
        if not exists:
            logger.warning(
                "Activity card module %s is missing; the Lovelace card is unavailable",
                CARD_PATH.name,
            )
            return
        version = content_hash or await _async_integration_version(hass)
        hass.http.register_view(AgentHubCardView(CARD_PATH))
        # Set right after the route exists: registering it twice raises.
        domain_data[_CARD_REGISTERED] = True
        add_extra_js_url(hass, f"{CARD_URL_PATH}?v={version}")
    except Exception:
        logger.warning("Could not register the Lovelace activity card", exc_info=True)
