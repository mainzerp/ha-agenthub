"""Plugin metadata CRUD."""

from __future__ import annotations

from typing import Any

from app.db.repositories._utils import _now
from app.db.schema import get_db_read, get_db_write


class PluginRepository:
    """CRUD for plugin metadata."""

    @staticmethod
    async def get(name: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM plugins WHERE name = ?", (name,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM plugins")
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def upsert(name: str, file_path: str, **kwargs: Any) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO plugins (name, file_path, enabled, version, description, loaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET file_path=?, enabled=?, version=?, description=?, loaded_at=?",
                (
                    name,
                    file_path,
                    kwargs.get("enabled", 1),
                    kwargs.get("version"),
                    kwargs.get("description"),
                    _now(),
                    file_path,
                    kwargs.get("enabled", 1),
                    kwargs.get("version"),
                    kwargs.get("description"),
                    _now(),
                ),
            )
