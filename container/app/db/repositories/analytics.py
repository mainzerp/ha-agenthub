"""Analytics events and cache validator CRUD."""

from __future__ import annotations

import json
from typing import Any

from app.db.schema import get_db_read, get_db_write


class AnalyticsRepository:
    """CRUD for analytics events."""

    @staticmethod
    async def insert(event_type: str, agent_id: str | None = None, data: dict | None = None) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO analytics (event_type, agent_id, data) VALUES (?, ?, ?)",
                (event_type, agent_id, json.dumps(data) if data else None),
            )

    @staticmethod
    async def query_by_range(
        event_type: str | None = None, start: str | None = None, end: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if start:
            conditions.append("created_at >= ?")
            params.append(start)
        if end:
            conditions.append("created_at <= ?")
            params.append(end)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        async with get_db_read() as db:
            cursor = await db.execute(
                f"SELECT * FROM analytics {where} ORDER BY created_at DESC LIMIT ?",
                params,
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            for row in rows:
                if row.get("data"):
                    row["data"] = json.loads(row["data"])
            return rows


class CacheValidatorRepository:
    """CRUD for cache validator run history."""

    @staticmethod
    async def insert(
        scanned: int,
        inconsistent: int,
        corrected: int,
        deleted: int,
        errors: int,
        started_at: str,
        finished_at: str,
    ) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO cache_validator_runs "
                "(scanned, inconsistent, corrected, deleted, errors, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scanned, inconsistent, corrected, deleted, errors, started_at, finished_at),
            )
            return cursor.lastrowid or 0

    @staticmethod
    async def list_recent(limit: int = 50) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT * FROM cache_validator_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]
