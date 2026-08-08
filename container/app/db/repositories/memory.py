"""Session-memory relational metadata CRUD.

Holds the session/turn metadata for the session-memory feature in the MAIN
database. The turn vectors themselves live in the dedicated
``session_memory.db`` sqlite-vec sidecar (``app.memory.vector_store``);
``memory_turns.vec_rowid`` links a turn row to its vector. Snippet text is
never duplicated here -- it is fetched by joining ``conversations`` on
``conversation_row_id``.
"""

from __future__ import annotations

from typing import Any

from app.db.schema import get_db_read, get_db_write


class MemoryRepository:
    """CRUD for the session-memory metadata tables."""

    @staticmethod
    async def upsert_session(
        conversation_id: str,
        user_id: str | None,
        summary_text: str | None,
        language: str | None,
        source: str | None,
        turn_epoch: int,
    ) -> int:
        """Insert or bump the session row for a conversation. Returns the session id.

        ``summary_text`` is the deterministic digest (first user message,
        truncated); on conflict it is intentionally NOT overwritten so the
        digest always reflects the session's opening message.
        """
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO memory_sessions "
                "(conversation_id, user_id, summary_text, turn_count, first_turn_at, last_turn_at, "
                "language, source, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "turn_count = turn_count + 1, "
                "last_turn_at = excluded.last_turn_at, "
                "updated_at = excluded.updated_at",
                (
                    conversation_id,
                    user_id,
                    summary_text,
                    turn_epoch,
                    turn_epoch,
                    language,
                    source,
                    turn_epoch,
                    turn_epoch,
                ),
            )
            cursor = await db.execute(
                "SELECT id FROM memory_sessions WHERE conversation_id = ?",
                (conversation_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            return int(row[0])

    @staticmethod
    async def insert_turn_ref(
        session_id: int,
        conversation_row_id: int,
        user_id: str | None,
        created_at: int,
    ) -> int:
        """Insert a turn reference row. Returns the memory_turns row id."""
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO memory_turns (session_id, conversation_row_id, user_id, created_at) VALUES (?, ?, ?, ?)",
                (session_id, conversation_row_id, user_id, created_at),
            )
            return cursor.lastrowid or 0

    @staticmethod
    async def set_turn_vec(
        turn_id: int,
        vec_rowid: int,
        embedding_model: str | None,
        embedding_dim: int,
    ) -> None:
        """Record the vector-store rowid + embedding provenance for a turn."""
        async with get_db_write() as db:
            await db.execute(
                "UPDATE memory_turns SET vec_rowid = ?, embedding_model = ?, embedding_dim = ? WHERE id = ?",
                (vec_rowid, embedding_model, embedding_dim, turn_id),
            )

    @staticmethod
    async def get_session_turns(conversation_id: str) -> list[dict[str, Any]]:
        """Return all turns of a session (oldest first) with text joined from conversations."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT t.id AS turn_id, t.conversation_row_id, t.user_id, t.created_at, "
                "c.user_text, c.response_text "
                "FROM memory_turns t "
                "JOIN memory_sessions s ON s.id = t.session_id "
                "JOIN conversations c ON c.id = t.conversation_row_id "
                "WHERE s.conversation_id = ? "
                "ORDER BY t.created_at, t.id",
                (conversation_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get_turns_by_ids(turn_ids: list[int]) -> list[dict[str, Any]]:
        """Fetch turn + session metadata and turn text for k-NN result ids."""
        if not turn_ids:
            return []
        placeholders = ", ".join("?" for _ in turn_ids)
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT t.id AS turn_id, t.session_id, t.user_id, t.created_at, "
                "t.conversation_row_id, s.conversation_id, s.last_turn_at, "
                "c.user_text, c.response_text "
                "FROM memory_turns t "
                "JOIN memory_sessions s ON s.id = t.session_id "
                "JOIN conversations c ON c.id = t.conversation_row_id "
                f"WHERE t.id IN ({placeholders})",
                [int(turn_id) for turn_id in turn_ids],
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def list_unembedded_turns(limit: int = 50) -> list[dict[str, Any]]:
        """Return turns without a vector reference (backfill after a vec reset)."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT t.id AS turn_id, t.conversation_row_id, c.user_text, c.response_text "
                "FROM memory_turns t "
                "JOIN conversations c ON c.id = t.conversation_row_id "
                "WHERE t.vec_rowid IS NULL "
                "ORDER BY t.id "
                "LIMIT ?",
                (int(limit),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def count_turns() -> int:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM memory_turns")
            row = await cursor.fetchone()
            assert row is not None
            return int(row[0])

    @staticmethod
    async def reset_vec_refs() -> int:
        """NULL out all vector references after a vector-store dim/model reset.

        Returns the number of affected rows; the startup backfill lazily
        re-embeds them with the active model.
        """
        async with get_db_write() as db:
            cursor = await db.execute("UPDATE memory_turns SET vec_rowid = NULL")
            return cursor.rowcount
