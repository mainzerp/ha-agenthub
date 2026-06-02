"""Secrets (Fernet-encrypted) CRUD."""

from __future__ import annotations

from app.db.repositories._utils import _now
from app.db.schema import get_db_read, get_db_write


class SecretsRepository:
    """CRUD for Fernet-encrypted secrets."""

    @staticmethod
    async def get(key: str) -> bytes | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT encrypted_value FROM secrets WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else None

    @staticmethod
    async def set(key: str, encrypted_value: bytes) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO secrets (key, encrypted_value, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET encrypted_value=?, updated_at=?",
                (key, encrypted_value, _now(), encrypted_value, _now()),
            )

    @staticmethod
    async def delete(key: str) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM secrets WHERE key = ?", (key,))

    @staticmethod
    async def list_keys() -> list[str]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT key FROM secrets")
            return [row[0] for row in await cursor.fetchall()]
