"""Entity matching configuration CRUD."""

from __future__ import annotations

from typing import Any

from app.db.repositories._utils import _now
from app.db.schema import get_db_read, get_db_write


class EntityMatchingConfigRepository:
    """CRUD for entity matching configuration."""

    @staticmethod
    async def get(key: str) -> str | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT value FROM entity_matching_config WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else None

    @staticmethod
    async def set(key: str, value: str, description: str | None = None) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO entity_matching_config (key, value, description, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?",
                (key, value, description, _now(), value, _now()),
            )

    @staticmethod
    async def get_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT key, value, description FROM entity_matching_config")
            return [dict(row) for row in await cursor.fetchall()]
