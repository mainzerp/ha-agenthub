"""Small async TTL cache with invalidate-on-write support.

Used for hot-path memoization (secrets, agent configs, provider params).
Mirrors the ``SettingsRepository._value_cache`` pattern
(``db/repositories/settings.py``): values are served from memory until
the TTL expires, and every write path must invalidate the affected key
(or the whole cache) so readers never see stale data across writes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


class AsyncTtlCache:
    """In-memory TTL cache guarded by an asyncio lock.

    A lock per cache instance (not per key) is sufficient: the critical
    sections are dict operations, and the worst case for concurrent
    misses is a duplicate underlying read, which is harmless.
    """

    def __init__(self, ttl_sec: float) -> None:
        self._ttl_sec = ttl_sec
        self._entries: dict[Any, tuple[Any, float]] = {}
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def get(self, key: Any) -> tuple[bool, Any]:
        """Return ``(True, value)`` on a fresh hit, else ``(False, None)``."""
        async with self._get_lock():
            entry = self._entries.get(key)
            if entry is None:
                return False, None
            value, expires_at = entry
            if expires_at <= time.monotonic():
                self._entries.pop(key, None)
                return False, None
            return True, value

    async def put(self, key: Any, value: Any) -> None:
        async with self._get_lock():
            self._entries[key] = (value, time.monotonic() + self._ttl_sec)

    async def invalidate(self, key: Any = None) -> None:
        """Drop a single key, or the whole cache when ``key`` is ``None``."""
        async with self._get_lock():
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)
