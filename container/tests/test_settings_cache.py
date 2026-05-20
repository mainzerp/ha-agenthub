"""P3-6 regression tests for ``SettingsRepository.get_value`` TTL cache."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from app.db.repositories import settings as settings_mod
from app.db.repository import SettingsRepository

pytestmark = pytest.mark.asyncio


def _counting_get_db_read(real):
    """Wrap ``get_db_read`` so callers can count how often the DB was hit."""
    counter = {"calls": 0}

    @asynccontextmanager
    async def _wrapped():
        counter["calls"] += 1
        async with real() as db:
            yield db

    return _wrapped, counter


class TestSettingsValueCache:
    async def test_cache_hit_avoids_db(self, db_repository):
        await SettingsRepository.set("p36.cache.test", "first", value_type="string")

        wrapped, counter = _counting_get_db_read(settings_mod.get_db_read)
        with patch.object(settings_mod, "get_db_read", wrapped):
            assert await SettingsRepository.get_value("p36.cache.test") == "first"
            first_calls = counter["calls"]
            # Subsequent reads must be served from cache.
            for _ in range(5):
                assert await SettingsRepository.get_value("p36.cache.test") == "first"
            assert counter["calls"] == first_calls

    async def test_set_invalidates_entry(self, db_repository):
        await SettingsRepository.set("p36.cache.invalidate", "v1")
        assert await SettingsRepository.get_value("p36.cache.invalidate") == "v1"
        # ``set`` must drop the cached value so the next read sees the update.
        await SettingsRepository.set("p36.cache.invalidate", "v2")
        assert await SettingsRepository.get_value("p36.cache.invalidate") == "v2"

    async def test_ttl_expiry_refetches(self, db_repository, monkeypatch):
        await SettingsRepository.set("p36.cache.ttl", "fresh")

        wrapped, counter = _counting_get_db_read(settings_mod.get_db_read)
        with patch.object(settings_mod, "get_db_read", wrapped):
            assert await SettingsRepository.get_value("p36.cache.ttl") == "fresh"
            primed_calls = counter["calls"]
            # Confirm the cache is warm (no extra DB hit).
            assert await SettingsRepository.get_value("p36.cache.ttl") == "fresh"
            assert counter["calls"] == primed_calls

            # Force every cached entry to look expired without sleeping.
            base = time.monotonic()
            monkeypatch.setattr(settings_mod.time, "monotonic", lambda: base + 9999)

            # Expired entry must miss the cache and trigger a fresh DB read.
            assert await SettingsRepository.get_value("p36.cache.ttl") == "fresh"
            assert counter["calls"] == primed_calls + 1

    async def test_missing_key_is_cached(self, db_repository):
        wrapped, counter = _counting_get_db_read(settings_mod.get_db_read)
        with patch.object(settings_mod, "get_db_read", wrapped):
            # First lookup hits the DB and stores the "missing" sentinel.
            assert await SettingsRepository.get_value("p36.cache.absent", "fallback") == "fallback"
            primed_calls = counter["calls"]

            # Second lookup must serve the cached miss without touching the DB,
            # and a different default applied to the cached miss.
            assert await SettingsRepository.get_value("p36.cache.absent", "different-default") == "different-default"
            assert counter["calls"] == primed_calls

    async def test_full_invalidate_clears_all(self, db_repository):
        await SettingsRepository.set("p36.cache.k1", "a")
        await SettingsRepository.set("p36.cache.k2", "b")
        assert await SettingsRepository.get_value("p36.cache.k1") == "a"
        assert await SettingsRepository.get_value("p36.cache.k2") == "b"

        await SettingsRepository._cache_invalidate()
        assert SettingsRepository._value_cache == {}
