"""Entity aliases CRUD."""

from __future__ import annotations

from typing import Any

from app.db.repositories._utils import _now
from app.db.schema import get_db_read, get_db_write


class AliasRepository:
    """CRUD for entity aliases."""

    @staticmethod
    async def get(alias: str) -> str | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT entity_id FROM aliases WHERE alias = ?", (alias,))
            row = await cursor.fetchone()
            return row[0] if row else None

    @staticmethod
    async def set(alias: str, entity_id: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO aliases (alias, entity_id, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(alias) DO UPDATE SET entity_id=?",
                (alias, entity_id, _now(), entity_id),
            )

    @staticmethod
    async def delete(alias: str) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM aliases WHERE alias = ?", (alias,))

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT alias, entity_id FROM aliases")
            return [dict(row) for row in await cursor.fetchall()]
