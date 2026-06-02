"""Query synonym cache CRUD."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.db.schema import get_db_read, get_db_write

logger = logging.getLogger(__name__)


class QuerySynonymCacheRepository:
    """0.23.0: organic LLM-expansion cache for cold query tokens.

    Storage is the empty ``query_synonym_cache`` table created by
    migration v18. Entries are added at query time; nothing is seeded
    for any language.
    """

    @staticmethod
    async def get(token: str, language: str) -> list[str] | None:
        token = (token or "").strip().lower()
        language = (language or "").strip().lower()
        if not token:
            return None
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT expansions FROM query_synonym_cache WHERE token = ? AND language = ?",
                (token, language),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
            if isinstance(data, list):
                return [str(x) for x in data if isinstance(x, str) and x]
        except json.JSONDecodeError:
            logger.warning(
                "Malformed JSON in query_synonym_cache for token=%r language=%r raw=%r",
                token,
                language,
                row[0],
            )
            return []
        return []

    @staticmethod
    async def put(token: str, language: str, expansions: list[str]) -> None:
        token = (token or "").strip().lower()
        language = (language or "").strip().lower()
        if not token:
            return
        cleaned = [str(x).strip() for x in (expansions or []) if isinstance(x, str) and x.strip()]
        payload = json.dumps(cleaned[:8])
        now = int(time.time())
        async with get_db_write() as db:
            await db.execute(
                """
                INSERT INTO query_synonym_cache
                    (token, language, expansions, created_at, last_used_at, hit_count)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(token, language) DO UPDATE SET
                    expansions = excluded.expansions,
                    last_used_at = excluded.last_used_at
                """,
                (token, language, payload, now, now),
            )

    @staticmethod
    async def touch(token: str, language: str) -> None:
        token = (token or "").strip().lower()
        language = (language or "").strip().lower()
        if not token:
            return
        now = int(time.time())
        async with get_db_write() as db:
            await db.execute(
                """
                UPDATE query_synonym_cache
                SET last_used_at = ?, hit_count = hit_count + 1
                WHERE token = ? AND language = ?
                """,
                (now, token, language),
            )

    @staticmethod
    async def evict_lru(max_rows: int = 5000) -> int:
        async with get_db_write() as db:
            cur = await db.execute("SELECT COUNT(*) FROM query_synonym_cache")
            row = await cur.fetchone()
            total = int(row[0]) if row else 0
            if total <= max_rows:
                return 0
            to_drop = total - max_rows
            await db.execute(
                """
                DELETE FROM query_synonym_cache
                WHERE rowid IN (
                    SELECT rowid FROM query_synonym_cache
                    ORDER BY last_used_at ASC
                    LIMIT ?
                )
                """,
                (to_drop,),
            )
            return to_drop

    @staticmethod
    async def purge_expired(ttl_seconds: int) -> int:
        if ttl_seconds <= 0:
            return 0
        cutoff = int(time.time()) - int(ttl_seconds)
        async with get_db_write() as db:
            cur = await db.execute(
                "DELETE FROM query_synonym_cache WHERE last_used_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0

    @staticmethod
    async def clear_all() -> int:
        async with get_db_write() as db:
            cur = await db.execute("DELETE FROM query_synonym_cache")
            return cur.rowcount or 0

    @staticmethod
    async def count() -> int:
        async with get_db_read() as db:
            cur = await db.execute("SELECT COUNT(*) FROM query_synonym_cache")
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    async def list_top(limit: int = 50) -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cur = await db.execute(
                """
                SELECT token, language, expansions, created_at, last_used_at, hit_count
                FROM query_synonym_cache
                ORDER BY hit_count DESC, last_used_at DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            try:
                d["expansions"] = json.loads(d["expansions"])
            except Exception:
                d["expansions"] = []
            out.append(d)
        return out
