"""Concurrency tests for the read-side database layer.

Regression test for CRIT-2 (deep code review): a single shared aiosqlite
connection on the read path serialized all reads through the same SQLite
cursor; concurrent calls could trip thread-safety. Reads are now per-call
connections (see app/db/schema/).
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.db.repository import SettingsRepository
from app.db.schema import get_db_read


@pytest.mark.asyncio
async def test_get_db_read_query_only_pragma_set(db_repository):
    """PRAGMA query_only should be ON inside the read scope."""
    async with get_db_read() as db:
        cursor = await db.execute("PRAGMA query_only")
        row = await cursor.fetchone()
        assert int(row[0]) == 1


@pytest.mark.asyncio
async def test_get_db_read_writes_blocked(db_repository):
    """Writes through a read connection must fail (query_only enforced)."""
    async with get_db_read() as db:
        with pytest.raises(sqlite3.OperationalError):
            await db.execute(
                "INSERT INTO settings (key, value, value_type) VALUES (?, ?, ?)",
                ("crit2.write_attempt", "x", "string"),
            )


@pytest.mark.asyncio
async def test_concurrent_reads_succeed(db_repository):
    """50 concurrent SettingsRepository.get_value calls must all return."""
    await SettingsRepository.set("crit2.test_key", "the_value", value_type="string")

    async def fetch():
        return await SettingsRepository.get_value("crit2.test_key")

    results = await asyncio.gather(*[fetch() for _ in range(50)])
    assert all(r == "the_value" for r in results)
    assert len(results) == 50


@pytest.mark.asyncio
async def test_concurrent_reads_during_write(db_repository):
    """Reads must not deadlock or fail when interleaved with a write."""
    await SettingsRepository.set("crit2.interleave", "v0", value_type="string")

    async def reader():
        return await SettingsRepository.get_value("crit2.interleave")

    async def writer():
        for i in range(5):
            await SettingsRepository.set("crit2.interleave", f"v{i}", value_type="string")
            await asyncio.sleep(0)

    tasks = [writer()] + [reader() for _ in range(20)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # No exceptions allowed
    for r in results:
        assert not isinstance(r, BaseException), f"unexpected exception: {r!r}"
