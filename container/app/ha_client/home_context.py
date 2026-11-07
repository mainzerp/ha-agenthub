"""Home context provider -- caches HA config for location/time awareness."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class HomeContext(BaseModel):
    """Cached home context from HA /api/config."""

    timezone: str = "UTC"
    location_name: str = ""


class HomeContextProvider:
    """Singleton provider that caches HA home context with a configurable TTL.

    Precedence per field: a non-empty DB override (``home.timezone`` /
    ``home.location_name``) always wins; otherwise HA ``/api/config``;
    otherwise the last good HA value; otherwise the default (``UTC`` /
    empty). A successful HA fetch is cached for ``_ttl_seconds``; a failed
    one only for ``_failure_retry_seconds`` so a cold-start outage does not
    pin UTC for an hour.
    """

    def __init__(self) -> None:
        self._context: HomeContext | None = None
        self._last_fetched: float = 0.0
        self._ttl_seconds: int = 3600  # 1 hour
        self._failure_retry_seconds: int = 60
        self._last_refresh_ok: bool = True
        # Last context fetched successfully from HA (without overrides).
        self._last_ha_context: HomeContext | None = None
        self._refresh_lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Drop the cached context so the next ``get`` re-reads overrides and HA.

        Called when the ``home.timezone`` / ``home.location_name`` overrides
        change; the last good HA context is kept as the fallback.
        """
        self._context = None
        self._last_fetched = 0.0

    def _is_fresh(self, now: float) -> bool:
        ttl = self._ttl_seconds if self._last_refresh_ok else self._failure_retry_seconds
        return self._context is not None and (now - self._last_fetched) < ttl

    async def get(self, ha_client: Any) -> HomeContext:
        """Return cached HomeContext, refreshing from HA if stale."""
        if self._is_fresh(time.monotonic()):
            assert self._context is not None
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
            if self._is_fresh(time.monotonic()):
                assert self._context is not None
                return self._context

            ha_ctx: HomeContext | None = None
            try:
                config = await ha_client.get_config()
                if config:
                    ha_ctx = HomeContext(
                        timezone=config.get("time_zone", "UTC") or "UTC",
                        location_name=config.get("location_name", "") or "",
                    )
            except Exception:
                logger.warning("Failed to fetch HA config for HomeContext", exc_info=True)

            refresh_ok = ha_ctx is not None
            if ha_ctx is not None:
                self._last_ha_context = ha_ctx
            base = ha_ctx or self._last_ha_context or HomeContext()

            overrides = await self._load_overrides()
            ctx = HomeContext(
                timezone=(overrides.timezone if overrides and overrides.timezone else base.timezone),
                location_name=(
                    overrides.location_name if overrides and overrides.location_name else base.location_name
                ),
            )
            self._context = ctx
            self._last_fetched = time.monotonic()
            self._last_refresh_ok = refresh_ok
            if refresh_ok:
                logger.info("HomeContext refreshed: tz=%s location=%s", ctx.timezone, ctx.location_name)
            else:
                logger.info(
                    "HomeContext: HA config unavailable, using tz=%s location=%s; retrying in %ds",
                    ctx.timezone,
                    ctx.location_name,
                    self._failure_retry_seconds,
                )
            return ctx

    async def _load_overrides(self) -> HomeContext | None:
        """Return DB overrides; an unset field is returned as an empty string.

        Returns ``None`` when neither override is set or the lookup fails.
        """
        try:
            from app.db.repository import SettingsRepository

            tz = await SettingsRepository.get_value("home.timezone", "")
            loc = await SettingsRepository.get_value("home.location_name", "")
            if tz or loc:
                return HomeContext(
                    timezone=(tz or "").strip(),
                    location_name=(loc or "").strip(),
                )
        except Exception:
            logger.debug("DB override lookup failed", exc_info=True)
        return None


home_context_provider = HomeContextProvider()

DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_zone(name: Any) -> ZoneInfo | None:
    """Return the ``ZoneInfo`` for ``name``, or ``None`` when it is empty or not a valid IANA zone."""
    text = str(name or "").strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except Exception:
        return None


async def resolve_home_zone(ha_client: Any) -> ZoneInfo | None:
    """Return the home time zone from the cached HomeContext, or ``None`` when it is unknown.

    Uses the shared provider cache (no extra Home Assistant call while the cache is fresh).
    ``None`` means the lookup failed or the configured name is not a valid zone; callers then
    fall back explicitly (see ``format_epoch_in_zone``). Without an HA client the zone is unknown.
    """
    if ha_client is None:
        return None
    try:
        ctx = await home_context_provider.get(ha_client)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Home timezone lookup failed", exc_info=True)
        return None
    return parse_zone(ctx.timezone)


def format_epoch_in_zone(epoch: Any, zone: tzinfo | None, fmt: str = DEFAULT_TIME_FORMAT) -> str:
    """Format a UNIX epoch as wall-clock time in ``zone``.

    When ``zone`` is ``None`` (home zone unknown) the container's local zone is used, which
    is the pre-existing behavior and UTC in the shipped image.
    """
    return datetime.fromtimestamp(int(epoch), tz=zone).strftime(fmt)


async def populate_task_context_home_context(context: Any, ha_client: Any) -> None:
    """Populate timezone, location_name, and local_time on a TaskContext.

    Reads from the cached HomeContext provider and formats ``local_time``
    in the home timezone. Failures are logged at debug level and swallowed
    so that a missing HA config does not block the request.
    """
    try:
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
