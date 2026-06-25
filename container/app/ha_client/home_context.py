"""Home context provider -- caches HA config for location/time awareness."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class HomeContext(BaseModel):
    """Cached home context from HA /api/config."""

    timezone: str = "UTC"
    location_name: str = ""


class HomeContextProvider:
    """Singleton provider that caches HA home context with a configurable TTL."""

    def __init__(self) -> None:
        self._context: HomeContext | None = None
        self._last_fetched: float = 0.0
        self._ttl_seconds: int = 3600  # 1 hour
        self._refresh_lock = asyncio.Lock()

    async def get(self, ha_client: Any) -> HomeContext:
        """Return cached HomeContext, refreshing from HA if stale."""
        now = time.monotonic()
        if self._context and (now - self._last_fetched) < self._ttl_seconds:
            return self._context
        return await self.refresh(ha_client)

    async def refresh(self, ha_client: Any) -> HomeContext:
        """Force-fetch from HA /api/config and update cache.

        FLOW-MED-2: de-duplicate concurrent refresh calls. On cold
        start / TTL expiry, `_dispatch_single` + the streaming branch
        + the dashboard can all call `refresh` at once. We serialize
        them behind a single lock and re-check TTL inside the lock so
        only the first caller actually hits HA; the rest return the
        freshly cached context.
        """
        async with self._refresh_lock:
            now = time.monotonic()
            if self._context and (now - self._last_fetched) < self._ttl_seconds:
                return self._context

            ctx = HomeContext()
            try:
                config = await ha_client.get_config()
                if config:
                    ctx = HomeContext(
                        timezone=config.get("time_zone", "UTC") or "UTC",
                        location_name=config.get("location_name", "") or "",
                    )
                    self._context = ctx
                    self._last_fetched = time.monotonic()
                    logger.info(
                        "HomeContext refreshed: tz=%s location=%s",
                        ctx.timezone,
                        ctx.location_name,
                    )
                    return ctx
            except Exception:
                logger.warning("Failed to fetch HA config for HomeContext", exc_info=True)

            overrides = await self._load_overrides()
            if overrides:
                self._context = overrides
                self._last_fetched = time.monotonic()
                return overrides

            if not self._context:
                self._context = ctx
                self._last_fetched = time.monotonic()
            return self._context

    async def _load_overrides(self) -> HomeContext | None:
        """Check DB settings for manual overrides."""
        try:
            from app.db.repository import SettingsRepository

            tz = await SettingsRepository.get_value("home.timezone", "")
            loc = await SettingsRepository.get_value("home.location_name", "")
            if tz or loc:
                return HomeContext(
                    timezone=tz or "UTC",
                    location_name=loc or "",
                )
        except Exception:
            logger.debug("DB override lookup failed", exc_info=True)
        return None


home_context_provider = HomeContextProvider()


async def populate_task_context_home_context(context: Any, ha_client: Any) -> None:
    """Populate timezone, location_name, and local_time on a TaskContext.

    Reads from the cached HomeContext provider and formats ``local_time``
    in the home timezone. Failures are logged at debug level and swallowed
    so that a missing HA config does not block the request.
    """
    try:
        from zoneinfo import ZoneInfo

        home_ctx = await home_context_provider.get(ha_client)
        context.timezone = home_ctx.timezone
        context.location_name = home_ctx.location_name
        try:
            tz = ZoneInfo(home_ctx.timezone)
            now = datetime.now(tz)
            context.local_time = now.strftime("%Y-%m-%d %H:%M")
        except asyncio.CancelledError:
            raise
        except Exception:
            context.local_time = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Failed to populate home context", exc_info=True)
