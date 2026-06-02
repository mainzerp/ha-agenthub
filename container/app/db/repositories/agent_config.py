"""Agent configuration CRUD."""

from __future__ import annotations

from typing import Any

from app.db.repositories._utils import _now, _validate_column_name
from app.db.schema import get_db_read, get_db_write


class AgentConfigRepository:
    """CRUD for agent configurations."""

    @staticmethod
    async def get(agent_id: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM agent_configs WHERE agent_id = ?", (agent_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM agent_configs")
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def list_enabled() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM agent_configs WHERE enabled = 1")
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def upsert(agent_id: str, **kwargs: Any) -> None:
        allowed = {
            "enabled",
            "model",
            "timeout",
            "max_iterations",
            "temperature",
            "max_tokens",
            "description",
            "reasoning_effort",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = _now()

        columns = ", ".join(["agent_id", *[_validate_column_name(k) for k in fields]])
        placeholders = ", ".join(["?"] * (len(fields) + 1))
        updates = ", ".join(f"{_validate_column_name(k)}=excluded.{_validate_column_name(k)}" for k in fields)

        values = [agent_id, *list(fields.values())]
        async with get_db_write() as db:
            await db.execute(
                f"INSERT INTO agent_configs ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(agent_id) DO UPDATE SET {updates}",
                values,
            )

    @staticmethod
    async def delete(agent_id: str) -> None:
        async with get_db_write() as db:
            await db.execute("DELETE FROM agent_configs WHERE agent_id = ?", (agent_id,))
