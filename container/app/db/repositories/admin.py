"""Admin accounts and setup wizard state CRUD."""

from __future__ import annotations

from typing import Any

from app.db.repositories._utils import _now
from app.db.schema import get_db_read, get_db_write


class AdminAccountRepository:
    """CRUD for admin accounts."""

    @staticmethod
    async def create(
        username: str,
        password_hash: str,
        *,
        force_overwrite: bool = False,
    ) -> None:
        """Create an admin account.

        ``force_overwrite=True`` uses ``INSERT OR REPLACE`` (only the
        one-time setup bootstrap should pass this). The default uses
        ``INSERT OR IGNORE`` so an authenticated session cannot silently
        overwrite an existing admin row via an unrelated code path.
        """
        verb = "INSERT OR REPLACE" if force_overwrite else "INSERT OR IGNORE"
        async with get_db_write() as db:
            await db.execute(
                f"{verb} INTO admin_accounts (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, _now()),
            )

    @staticmethod
    async def update_password(username: str, password_hash: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE admin_accounts SET password_hash = ? WHERE username = ?",
                (password_hash, username),
            )

    @staticmethod
    async def get(username: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM admin_accounts WHERE username = ?", (username,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def update_last_login(username: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE admin_accounts SET last_login = ? WHERE username = ?",
                (_now(), username),
            )

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT username, created_at, last_login FROM admin_accounts")
            return [dict(row) for row in await cursor.fetchall()]


class SetupStateRepository:
    """CRUD for setup wizard state tracking."""

    @staticmethod
    async def get_step(step: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM setup_state WHERE step = ?", (step,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def set_step_completed(step: str) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE setup_state SET completed = 1, completed_at = ? WHERE step = ?",
                (_now(), step),
            )

    @staticmethod
    async def is_complete() -> bool:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM setup_state WHERE completed = 0")
            row = await cursor.fetchone()
            assert row is not None
            return row[0] == 0

    @staticmethod
    async def get_all_steps() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM setup_state")
            return [dict(row) for row in await cursor.fetchall()]
