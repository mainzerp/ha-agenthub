"""Settings repository: CRUD for the key-value settings store."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.db.schema import get_db_read, get_db_write

logger = logging.getLogger(__name__)


def _now() -> str:
    """Return current UTC timestamp as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


# P3-6: in-memory TTL cache for ``SettingsRepository.get_value``.
# Settings change rarely but ``get_value`` is called per-request from
# many hot paths (orchestrator, filler thresholds, dispatch timeouts,
# routing thresholds). One DB hit per call adds up; the TTL keeps the
# cache from going stale across long-running processes / out-of-band
# DB writes.
_SETTINGS_VALUE_CACHE_TTL_SEC = 60.0
# Sentinel used to cache "key absent" results so we don't re-hit the DB
# for unset keys every call. Stored with the same TTL.
_MISSING = object()


class SettingsRepository:
    """CRUD for the settings key-value store."""

    # ``{key: (value_or_MISSING, expires_at_monotonic)}``.
    # Class-level on purpose: ``SettingsRepository`` is a stateless
    # collection of staticmethods used as a namespace.
    _value_cache: ClassVar[dict[str, tuple[Any, float]]] = {}
    _value_cache_lock: ClassVar[asyncio.Lock | None] = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._value_cache_lock is None:
            cls._value_cache_lock = asyncio.Lock()
        return cls._value_cache_lock

    @classmethod
    async def _cache_get(cls, key: str) -> tuple[bool, Any]:
        async with cls._get_lock():
            entry = cls._value_cache.get(key)
            if entry is None:
                return False, None
            value, expires_at = entry
            if expires_at <= time.monotonic():
                cls._value_cache.pop(key, None)
                return False, None
            return True, value

    @classmethod
    async def _cache_put(cls, key: str, value: Any) -> None:
        async with cls._get_lock():
            cls._value_cache[key] = (value, time.monotonic() + _SETTINGS_VALUE_CACHE_TTL_SEC)

    @classmethod
    async def _cache_invalidate(cls, key: str | None = None) -> None:
        """Drop a single key (or the whole cache when ``key`` is ``None``)."""
        async with cls._get_lock():
            if key is None:
                cls._value_cache.clear()
            else:
                cls._value_cache.pop(key, None)

    @staticmethod
    async def get(key: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT key, value, value_type, category, description FROM settings WHERE key = ?",
                (key,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    @staticmethod
    async def get_value(key: str, default: str | None = None) -> str | None:
        # P3-6: serve from in-memory TTL cache when available. The
        # cached entry stores either the actual DB value or ``_MISSING``
        # (key absent in DB); ``default`` is applied to ``_MISSING``
        # hits at call time so different callers can use different
        # defaults.
        hit, cached = await SettingsRepository._cache_get(key)
        if hit:
            return default if cached is _MISSING else cached
        async with get_db_read() as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
        if row is None:
            await SettingsRepository._cache_put(key, _MISSING)
            return default
        value = row[0]
        await SettingsRepository._cache_put(key, value)
        return value

    @staticmethod
    async def set(
        key: str, value: str, value_type: str = "string", category: str = "general", description: str | None = None
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO settings (key, value, value_type, category, description, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?",
                (key, value, value_type, category, description, _now(), value, _now()),
            )
        # P3-6: invalidate so subsequent ``get_value`` reflects the write.
        await SettingsRepository._cache_invalidate(key)

    @staticmethod
    async def get_by_category(category: str) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT key, value, value_type, description FROM settings WHERE category = ?",
                (category,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT key, value, value_type, category, description FROM settings")
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def delete(key: str) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM settings WHERE key = ?", (key,))
        await SettingsRepository._cache_invalidate(key)


async def _settings_float(key: str, *, default: float) -> float:
    """Read a float setting by key, falling back to ``default`` on any error."""
    try:
        raw = await SettingsRepository.get_value(key, str(default))
        if raw is None:
            return default
        return float(raw)
    except (TypeError, ValueError, Exception):
        return default


async def _settings_int(key: str, *, default: int) -> int:
    """Read an int setting by key, falling back to ``default`` on any error."""
    try:
        raw = await SettingsRepository.get_value(key, str(default))
        if raw is None:
            return default
        return int(raw)
    except (TypeError, ValueError, Exception):
        return default
