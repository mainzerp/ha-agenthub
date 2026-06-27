"""Shared state and helpers for the admin REST API route package.

Holds the registry injection point (``set_registry`` / ``_registry``) and the
helpers reused across several per-domain sub-routers. External symbols that are
patched in tests (``get_ha_token`` etc.) are intentionally NOT defined here;
they are bound on the package itself (see ``__init__``) so ``mock.patch`` on
``app.api.routes.admin.<name>`` keeps affecting the call sites.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.a2a.registry import AgentRegistry

logger = logging.getLogger(__name__)

# The registry is set by main.py during startup via ``set_registry``.
_registry: AgentRegistry | None = None


def set_registry(reg) -> None:
    """Called by main.py to inject the A2A registry."""
    global _registry
    _registry = reg


def _bool_from_setting(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def _reload_ha_clients_after_settings_change(request: Request) -> None:
    """Rebuild REST client and drop WS so ``run()`` reconnects with new URL/token."""
    ha_client = getattr(request.app.state, "ha_client", None)
    if ha_client is not None:
        try:
            await ha_client.reload()
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError):  # legitimate fail-soft: reload failure is non-fatal
            logger.warning("HARestClient.reload() after HA settings change failed", exc_info=True)
    ws_client = getattr(request.app.state, "ws_client", None)
    if ws_client is not None:
        try:
            await ws_client.drop_connection()
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError):  # legitimate fail-soft: drop_connection failure is non-fatal
            logger.warning("HA WebSocket drop_connection() failed", exc_info=True)
