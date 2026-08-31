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
    async def insert_started(started_at: str) -> int:
        """Insert a run row with zero counts before validation starts.

        ``finished_at`` is a TEXT NOT NULL column, so it is seeded with an
        empty string until ``update_finished`` fills in the real values.
        """
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO cache_validator_runs "
                "(scanned, inconsistent, corrected, deleted, errors, started_at, finished_at) "
                "VALUES (0, 0, 0, 0, 0, ?, '')",
                (started_at,),
            )
            return cursor.lastrowid or 0

    @staticmethod
    async def update_finished(
        run_id: int,
        scanned: int,
        inconsistent: int,
        corrected: int,
        deleted: int,
        errors: int,
        finished_at: str,
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE cache_validator_runs "
                "SET scanned = ?, inconsistent = ?, corrected = ?, deleted = ?, errors = ?, finished_at = ? "
                "WHERE id = ?",
                (scanned, inconsistent, corrected, deleted, errors, finished_at, run_id),
            )

    @staticmethod
    async def list_recent(limit: int = 50) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT * FROM cache_validator_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]


class CacheValidatorAuditRepository:
    """CRUD for per-entry cache validator audit records."""

    @staticmethod
    async def insert_entry(
        run_id: int,
        entry_id: str,
        query_text: str,
        language: str,
        agent_id: str | None,
        service: str | None,
        entity_id: str | None,
        verdict: str,
        llm_verdict: str | None,
        old_response_text: str | None,
        new_response_text: str | None,
        old_original_response_text: str | None,
        new_original_response_text: str | None,
        deleted: bool,
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO cache_validator_audit "
                "(run_id, entry_id, query_text, language, agent_id, service, entity_id, "
                "verdict, llm_verdict, old_response_text, new_response_text, "
                "old_original_response_text, new_original_response_text, deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    entry_id,
                    query_text,
                    language,
                    agent_id,
                    service,
                    entity_id,
                    verdict,
                    llm_verdict,
                    old_response_text,
                    new_response_text,
                    old_original_response_text,
                    new_original_response_text,
                    1 if deleted else 0,
                ),
            )

    @staticmethod
    async def list_for_run(run_id: int, page: int = 1, per_page: int = 50) -> tuple[list[dict[str, Any]], int]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT * FROM cache_validator_audit WHERE run_id = ? ORDER BY id LIMIT ? OFFSET ?",
                (run_id, per_page, (page - 1) * per_page),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            count_cursor = await db.execute(
                "SELECT COUNT(*) FROM cache_validator_audit WHERE run_id = ?",
                (run_id,),
            )
            total = (await count_cursor.fetchone())[0]
            return rows, total

    @staticmethod
    async def cleanup_old(days: int) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "DELETE FROM cache_validator_audit WHERE created_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            return cursor.rowcount
