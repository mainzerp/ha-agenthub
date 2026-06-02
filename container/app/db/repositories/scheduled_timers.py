"""Scheduled timers CRUD."""

from __future__ import annotations

from typing import Any

from app.db.schema import get_db_read, get_db_write


class ScheduledTimersRepository:
    """CRUD for the AgentHub-managed timer scheduler.

    Backs ``app.agents.timer_scheduler.TimerScheduler``. Rows survive
    container restart so pending timers are rehydrated on startup.
    """

    @staticmethod
    async def insert(
        *,
        id: str,
        logical_name: str,
        kind: str,
        created_at: int,
        fires_at: int,
        duration_seconds: int,
        origin_device_id: str | None,
        origin_area: str | None,
        briefing: bool = False,
        payload_json: str,
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO scheduled_timers "
                "(id, logical_name, kind, created_at, fires_at, duration_seconds, "
                "origin_device_id, origin_area, briefing, payload_json, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    id,
                    logical_name,
                    kind,
                    int(created_at),
                    int(fires_at),
                    int(duration_seconds),
                    origin_device_id,
                    origin_area,
                    1 if briefing else 0,
                    payload_json,
                ),
            )

    @staticmethod
    async def list_pending(*, kinds: set[str] | frozenset[str] | None = None) -> list[dict]:
        async with get_db_read() as db:
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                sql = (
                    "SELECT * FROM scheduled_timers WHERE state = 'pending' "
                    f"AND kind IN ({placeholders}) ORDER BY fires_at ASC, id ASC"
                )
                cursor = await db.execute(sql, tuple(sorted(kinds)))
            else:
                cursor = await db.execute(
                    "SELECT * FROM scheduled_timers WHERE state = 'pending' ORDER BY fires_at ASC, id ASC"
                )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def list_pending_for(
        *,
        logical_name: str | None = None,
        area: str | None = None,
        kinds: set[str] | frozenset[str] | None = None,
    ) -> list[dict]:
        clauses = ["state = 'pending'"]
        params: list[Any] = []
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(sorted(kinds))
        if logical_name is not None:
            clauses.append("LOWER(logical_name) = LOWER(?)")
            params.append(logical_name)
        if area is not None:
            clauses.append("origin_area = ?")
            params.append(area)
        sql = "SELECT * FROM scheduled_timers WHERE " + " AND ".join(clauses) + " ORDER BY fires_at ASC, id ASC"
        async with get_db_read() as db:
            cursor = await db.execute(sql, tuple(params))
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get(id_: str) -> dict | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM scheduled_timers WHERE id = ?", (id_,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def mark_fired(id_: str, fired_at: int) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE scheduled_timers SET state = 'fired', fired_at = ? WHERE id = ?",
                (int(fired_at), id_),
            )

    @staticmethod
    async def mark_cancelled(id_: str, cancelled_at: int) -> None:
        async with get_db_write() as db:
            await db.execute(
                "UPDATE scheduled_timers SET state = 'cancelled', cancelled_at = ? WHERE id = ? AND state = 'pending'",
                (int(cancelled_at), id_),
            )

    @staticmethod
    async def cancel_by_logical_name(logical_name: str, cancelled_at: int) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "UPDATE scheduled_timers SET state = 'cancelled', cancelled_at = ? "
                "WHERE state = 'pending' AND LOWER(logical_name) = LOWER(?)",
                (int(cancelled_at), logical_name),
            )
            return cursor.rowcount

    @staticmethod
    async def purge_terminal_older_than(cutoff_epoch: int) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "DELETE FROM scheduled_timers "
                "WHERE state IN ('fired', 'cancelled', 'expired') "
                "AND COALESCE(fired_at, cancelled_at, created_at) < ?",
                (int(cutoff_epoch),),
            )
            return cursor.rowcount

    @staticmethod
    async def update_scheduled_timer(
        id_: str,
        *,
        logical_name: str | None = None,
        fires_at: int | None = None,
        duration_seconds: int | None = None,
        briefing: bool | None = None,
        payload_json: str | None = None,
    ) -> bool:
        """Update mutable fields on a pending scheduled_timers row.

        Only rows with ``state = 'pending'`` are affected; already-fired or
        cancelled rows return ``False`` without touching the DB.

        Returns ``True`` if exactly one row was updated, ``False`` otherwise.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if logical_name is not None:
            clauses.append("logical_name = ?")
            params.append(logical_name)
        if fires_at is not None:
            clauses.append("fires_at = ?")
            params.append(int(fires_at))
        if duration_seconds is not None:
            clauses.append("duration_seconds = ?")
            params.append(int(duration_seconds))
        if briefing is not None:
            clauses.append("briefing = ?")
            params.append(1 if briefing else 0)
        if payload_json is not None:
            clauses.append("payload_json = ?")
            params.append(payload_json)
        if not clauses:
            return False
        params.append(id_)
        sql = "UPDATE scheduled_timers SET " + ", ".join(clauses) + " WHERE id = ? AND state = 'pending'"
        async with get_db_write() as db:
            cursor = await db.execute(sql, tuple(params))
            return cursor.rowcount > 0
