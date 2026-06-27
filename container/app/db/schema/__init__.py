"""SQLite table definitions and initialization.

Manages the SQLite database schema for all structured data: configuration,
secrets, user accounts, conversation history, and analytics.

The schema logic is split across submodules:

- :mod:`app.db.schema._tables`  -- table creation DDL
- :mod:`app.db.schema._indexes` -- index creation
- :mod:`app.db.schema._seed`    -- default seed data
- :mod:`app.db.schema._migrations` -- incremental schema migrations (registry)

This package keeps the connection layer (shared write connection, read/write
context managers, locking and retry helpers) so that the module-level write
connection state remains addressable as ``app.db.schema._write_conn``.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)

_write_conn: aiosqlite.Connection | None = None
_write_lock: asyncio.Lock | None = None


def _get_write_lock() -> asyncio.Lock:
    """Return the module-level write lock, creating it lazily in the current event loop."""
    global _write_lock
    if _write_lock is None:
        _write_lock = asyncio.Lock()
    return _write_lock


_DB_WRITE_MAX_RETRIES = 3
_DB_WRITE_BASE_DELAY = 0.5


async def _db_path() -> Path:
    """Resolve the SQLite database path and ensure the parent directory exists."""
    p = Path(settings.sqlite_db_path)
    # Off-load directory creation to a thread to avoid blocking the event loop.
    await asyncio.to_thread(p.parent.mkdir, parents=True, exist_ok=True)
    return p


async def _open_write_connection() -> aiosqlite.Connection:
    """Open a fresh write connection with retry on OperationalError."""
    for attempt in range(1, _DB_WRITE_MAX_RETRIES + 1):
        try:
            conn = await aiosqlite.connect(str(await _db_path()), isolation_level=None)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except aiosqlite.OperationalError:
            logger.warning("DB write connection failed (attempt %d/%d)", attempt, _DB_WRITE_MAX_RETRIES, exc_info=True)
            if attempt < _DB_WRITE_MAX_RETRIES:
                await asyncio.sleep(_DB_WRITE_BASE_DELAY * (2 ** (attempt - 1)))
    raise aiosqlite.OperationalError("Failed to open write connection after all retries")


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    """Check whether a column exists in a given table."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return any(row[1] == column for row in rows)


async def _get_or_create_write_connection() -> aiosqlite.Connection:
    """Get or create the shared write connection."""
    global _write_conn
    if _write_conn is not None:
        try:
            await _write_conn.execute("SELECT 1")
        except aiosqlite.OperationalError:
            logger.warning("DB write connection stale, recreating")
            _write_conn = None
    if _write_conn is None:
        _write_conn = await _open_write_connection()
    return _write_conn


@asynccontextmanager
async def get_db_read() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager returning a per-call read-only database connection.

    A fresh ``aiosqlite`` connection is opened for every read scope and
    closed on exit. WAL mode is persistent on the database file (set on
    the write connection at startup), so concurrent readers do not block
    each other and do not block writers. ``PRAGMA query_only=ON`` enforces
    read-only access at the connection level.
    """
    db = await aiosqlite.connect(str(await _db_path()))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def get_db_write() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager returning the write database connection.

    Acquires _write_lock to serialize writes and begins an explicit
    transaction so that every block inside the context is atomic.
    """
    async with _get_write_lock():
        db = await _get_or_create_write_connection()
        await db.execute("BEGIN")
        try:
            yield db
        except BaseException:
            # BaseException ensures rollback on KeyboardInterrupt / SystemExit
            await db.rollback()
            raise
        else:
            await db.commit()


# Backward-compatible alias -- points to the write path (safe default).
get_db = get_db_write


async def close_db() -> None:
    """Close the shared write connection. Call on shutdown."""
    global _write_conn
    if _write_conn is not None:
        await _write_conn.close()
        _write_conn = None


async def init_db() -> None:
    """Initialize database schema and seed default data.

    Called at container startup. All operations are idempotent.
    """
    async with get_db() as db:
        await _create_tables(db)
        await _create_indexes(db)
        await _seed_defaults(db)
        await _run_migrations(db)
        await db.commit()


# Re-export the schema-building helpers from their submodules. These imports
# are placed at the bottom (after _column_exists is defined) so the migration
# submodule can import _column_exists from this package without a circular
# import error.
from app.db.schema._indexes import _create_indexes  # noqa: E402
from app.db.schema._migrations import _run_migrations  # noqa: E402
from app.db.schema._seed import _seed_defaults  # noqa: E402
from app.db.schema._tables import _create_tables  # noqa: E402

__all__ = [
    "close_db",
    "get_db",
    "get_db_read",
    "get_db_write",
    "init_db",
]
