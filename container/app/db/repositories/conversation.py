"""Conversation history CRUD."""

from __future__ import annotations

from typing import Any

from app.db.schema import get_db_read, get_db_write


class ConversationRepository:
    """CRUD for conversation history."""

    @staticmethod
    async def insert(
        conversation_id: str,
        user_text: str,
        agent_id: str | None = None,
        response_text: str | None = None,
        action_executed: str | None = None,
        cache_hit: str | None = None,
        latency_ms: float | None = None,
    ) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO conversations "
                "(conversation_id, user_text, agent_id, response_text, "
                "action_executed, cache_hit, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, user_text, agent_id, response_text, action_executed, cache_hit, latency_ms),
            )
            return cursor.lastrowid or 0

    @staticmethod
    async def list_recent(limit: int = 50) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT * FROM conversations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get_by_conversation_id(conversation_id: str) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT * FROM conversations WHERE conversation_id = ? ORDER BY created_at",
                (conversation_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def search(
        agent_id: str | None = None,
        search_text: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if search_text:
            conditions.append("(user_text LIKE ? OR response_text LIKE ?)")
            like = f"%{search_text}%"
            params.extend([like, like])
        if start_date:
            conditions.append("created_at >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("created_at <= ?")
            params.append(end_date)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = (page - 1) * per_page
        params.extend([per_page, offset])

        async with get_db_read() as db:
            cursor = await db.execute(
                f"SELECT * FROM conversations {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def count(
        agent_id: str | None = None,
        search_text: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        conditions: list[str] = []
        params: list[Any] = []
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if search_text:
            conditions.append("(user_text LIKE ? OR response_text LIKE ?)")
            like = f"%{search_text}%"
            params.extend([like, like])
        if start_date:
            conditions.append("created_at >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("created_at <= ?")
            params.append(end_date)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with get_db_read() as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM conversations {where}",
                params,
            )
            row = await cursor.fetchone()
            assert row is not None
            return row[0]
