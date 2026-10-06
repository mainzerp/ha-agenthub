"""External chat-client user to Home Assistant user mappings."""

from __future__ import annotations

from typing import Any

from app.db.repositories._utils import _now
from app.db.schema import get_db_read, get_db_write

# Maximum length of ``source``, ``external_user_id`` and ``ha_user_id``.
MAX_ID_LENGTH = 128
# Display metadata is informational only; overly long values are truncated.
MAX_LABEL_LENGTH = 256

_COLUMNS = "source, external_user_id, display_name, email, ha_user_id, first_seen_at, last_seen_at, updated_at"


def _require_id(value: str | None, field: str) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` when empty or too long."""
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    if len(cleaned) > MAX_ID_LENGTH:
        raise ValueError(f"{field} exceeds {MAX_ID_LENGTH} characters")
    return cleaned


def _optional_label(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned[:MAX_LABEL_LENGTH] or None


class ExternalUserMappingRepository:
    """CRUD for users seen on external chat clients (e.g. Open WebUI).

    Rows are created on first contact by :meth:`touch`; the admin maps a row
    to a Home Assistant user id on the dashboard Persons page.
    """

    @staticmethod
    async def touch(
        source: str,
        external_user_id: str,
        display_name: str | None = None,
        email: str | None = None,
    ) -> str | None:
        """Record the user as seen (upsert) and return the mapped HA user id.

        Display name and email are refreshed only when the client sent them,
        so a request without user-info headers never erases stored values.
        """
        source = _require_id(source, "source")
        external_user_id = _require_id(external_user_id, "external_user_id")
        name = _optional_label(display_name)
        mail = _optional_label(email)
        now = _now()
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO external_user_mappings "
                "(source, external_user_id, display_name, email, ha_user_id, first_seen_at, last_seen_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?) "
                "ON CONFLICT(source, external_user_id) DO UPDATE SET "
                "display_name = COALESCE(excluded.display_name, external_user_mappings.display_name), "
                "email = COALESCE(excluded.email, external_user_mappings.email), "
                "last_seen_at = excluded.last_seen_at",
                (source, external_user_id, name, mail, now, now, now),
            )
            cursor = await db.execute(
                "SELECT ha_user_id FROM external_user_mappings WHERE source = ? AND external_user_id = ?",
                (source, external_user_id),
            )
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    @staticmethod
    async def get(source: str, external_user_id: str) -> dict[str, Any] | None:
        """Return one mapping row or ``None``."""
        async with get_db_read() as db:
            cursor = await db.execute(
                f"SELECT {_COLUMNS} FROM external_user_mappings WHERE source = ? AND external_user_id = ?",
                (source, external_user_id),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def list_all(source: str | None = None) -> list[dict[str, Any]]:
        """Return all mappings, optionally filtered by source, most recently seen first."""
        async with get_db_read() as db:
            if source:
                cursor = await db.execute(
                    f"SELECT {_COLUMNS} FROM external_user_mappings WHERE source = ? ORDER BY last_seen_at DESC",
                    (source,),
                )
            else:
                cursor = await db.execute(f"SELECT {_COLUMNS} FROM external_user_mappings ORDER BY last_seen_at DESC")
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def set_mapping(source: str, external_user_id: str, ha_user_id: str | None) -> bool:
        """Map (or with ``None`` unmap) a user. Returns True if the row existed."""
        mapped = _require_id(ha_user_id, "ha_user_id") if ha_user_id is not None else None
        async with get_db_write() as db:
            cursor = await db.execute(
                "UPDATE external_user_mappings SET ha_user_id = ?, updated_at = ? "
                "WHERE source = ? AND external_user_id = ?",
                (mapped, _now(), source, external_user_id),
            )
            return cursor.rowcount > 0

    @staticmethod
    async def delete(source: str, external_user_id: str) -> bool:
        """Delete a mapping row. Returns True if the row existed."""
        async with get_db_write() as db:
            cursor = await db.execute(
                "DELETE FROM external_user_mappings WHERE source = ? AND external_user_id = ?",
                (source, external_user_id),
            )
            return cursor.rowcount > 0
