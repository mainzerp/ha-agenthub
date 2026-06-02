"""Calendar user mappings, entity settings, and reminder state CRUD."""

from __future__ import annotations

from typing import Any

from app.db.repositories._utils import _normalize_device_name, _now, _phonetic_key, _validate_column_name
from app.db.schema import get_db_read, get_db_write


class CalendarUserMappingRepository:
    """CRUD for calendar user mappings (user name -> calendar entities)."""

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT id, display_name, normalized_name, phonetic_key, "
                "calendar_entity_ids_json, reminder_offsets_json, is_default_user, person_entity_id, created_at "
                "FROM calendar_user_mappings ORDER BY display_name"
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get(mapping_id: int) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM calendar_user_mappings WHERE id = ?", (mapping_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def find_by_name(name: str) -> dict[str, Any] | None:
        name = name.strip()
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT * FROM calendar_user_mappings WHERE display_name = ? COLLATE NOCASE",
                (name,),
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
            normalized = _normalize_device_name(name)
            if normalized:
                cursor = await db.execute(
                    "SELECT * FROM calendar_user_mappings WHERE normalized_name = ?",
                    (normalized,),
                )
                row = await cursor.fetchone()
                if row:
                    return dict(row)
            phonetic = _phonetic_key(name)
            if phonetic:
                cursor = await db.execute(
                    "SELECT * FROM calendar_user_mappings WHERE phonetic_key = ?",
                    (phonetic,),
                )
                row = await cursor.fetchone()
                if row:
                    return dict(row)
            return None

    @staticmethod
    async def find_by_normalized(normalized: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT * FROM calendar_user_mappings WHERE normalized_name = ?",
                (normalized,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def find_default_user() -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM calendar_user_mappings WHERE is_default_user = 1 LIMIT 1")
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def create(
        display_name: str,
        calendar_entity_ids_json: str,
        reminder_offsets_json: str,
        is_default_user: int = 0,
        person_entity_id: str | None = None,
    ) -> int:
        from app.agents.satellite_targeting import _normalize_name

        normalized = _normalize_name(display_name)
        phonetic = _phonetic_key(display_name)
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO calendar_user_mappings (display_name, normalized_name, phonetic_key, "
                "calendar_entity_ids_json, reminder_offsets_json, is_default_user, person_entity_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    display_name.strip(),
                    normalized,
                    phonetic,
                    calendar_entity_ids_json,
                    reminder_offsets_json,
                    is_default_user,
                    person_entity_id,
                    _now(),
                    _now(),
                ),
            )
            return cursor.lastrowid or 0

    @staticmethod
    async def update(mapping_id: int, **kwargs: Any) -> bool:
        allowed = {
            "display_name",
            "calendar_entity_ids_json",
            "reminder_offsets_json",
            "is_default_user",
            "person_entity_id",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        if "display_name" in fields:
            from app.agents.satellite_targeting import _normalize_name

            fields["normalized_name"] = _normalize_name(fields["display_name"])
            fields["phonetic_key"] = _phonetic_key(fields["display_name"])
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{_validate_column_name(k)} = ?" for k in fields)
        values = [*list(fields.values()), mapping_id]
        async with get_db_write() as db:
            cursor = await db.execute(
                f"UPDATE calendar_user_mappings SET {set_clause} WHERE id = ?",
                values,
            )
            return cursor.rowcount > 0

    @staticmethod
    async def delete(mapping_id: int) -> bool:
        async with get_db_write() as db:
            cursor = await db.execute("DELETE FROM calendar_user_mappings WHERE id = ?", (mapping_id,))
            return cursor.rowcount > 0


class CalendarEntitySettingsRepository:
    """CRUD for per-calendar entity enablement (which calendars are active for reminders)."""

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT entity_id, friendly_name, enabled, is_universal, created_at, updated_at "
                "FROM calendar_entity_settings ORDER BY friendly_name, entity_id"
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get(entity_id: str) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT * FROM calendar_entity_settings WHERE entity_id = ?", (entity_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def upsert(entity_id: str, friendly_name: str | None = None, enabled: int = 1, is_universal: int = 0) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO calendar_entity_settings (entity_id, friendly_name, enabled, is_universal, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_id) DO UPDATE SET "
                "friendly_name = COALESCE(EXCLUDED.friendly_name, calendar_entity_settings.friendly_name), "
                "enabled = EXCLUDED.enabled, is_universal = EXCLUDED.is_universal, updated_at = EXCLUDED.updated_at",
                (entity_id, friendly_name, enabled, is_universal, _now(), _now()),
            )

    @staticmethod
    async def set_enabled(entity_id: str, enabled: int) -> bool:
        async with get_db_write() as db:
            cursor = await db.execute(
                "UPDATE calendar_entity_settings SET enabled = ?, updated_at = ? WHERE entity_id = ?",
                (enabled, _now(), entity_id),
            )
            return cursor.rowcount > 0

    @staticmethod
    async def set_universal(entity_id: str, is_universal: int) -> bool:
        async with get_db_write() as db:
            cursor = await db.execute(
                "UPDATE calendar_entity_settings SET is_universal = ?, updated_at = ? WHERE entity_id = ?",
                (is_universal, _now(), entity_id),
            )
            return cursor.rowcount > 0

    @staticmethod
    async def get_enabled_entity_ids() -> list[str]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT entity_id FROM calendar_entity_settings WHERE enabled = 1 ORDER BY entity_id"
            )
            return [row[0] for row in await cursor.fetchall()]

    @staticmethod
    async def get_universal_entity_ids() -> list[str]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT entity_id FROM calendar_entity_settings WHERE enabled = 1 AND is_universal = 1 ORDER BY entity_id"
            )
            return [row[0] for row in await cursor.fetchall()]

    @staticmethod
    async def is_enabled(entity_id: str) -> bool:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT enabled FROM calendar_entity_settings WHERE entity_id = ?", (entity_id,))
            row = await cursor.fetchone()
            return row[0] == 1 if row else True

    @staticmethod
    async def delete(entity_id: str) -> bool:
        async with get_db_write() as db:
            cursor = await db.execute("DELETE FROM calendar_entity_settings WHERE entity_id = ?", (entity_id,))
            return cursor.rowcount > 0


class CalendarReminderStateRepository:
    """Tracks fired reminder offsets per event+user (one-time injection guarantee)."""

    @staticmethod
    async def has_fired(event_uid: str, calendar_entity_id: str, user_mapping_id: int, offset_minutes: int) -> bool:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT 1 FROM calendar_reminder_state "
                "WHERE event_uid = ? AND calendar_entity_id = ? AND user_mapping_id = ? AND offset_minutes = ?",
                (event_uid, calendar_entity_id, user_mapping_id, offset_minutes),
            )
            return (await cursor.fetchone()) is not None

    @staticmethod
    async def mark_fired(event_uid: str, calendar_entity_id: str, user_mapping_id: int, offset_minutes: int) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT OR IGNORE INTO calendar_reminder_state "
                "(event_uid, calendar_entity_id, user_mapping_id, offset_minutes, fired_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_uid, calendar_entity_id, user_mapping_id, offset_minutes, _now()),
            )

    @staticmethod
    async def get_fired_for_event(event_uid: str, calendar_entity_id: str, user_mapping_id: int) -> list[int]:
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT offset_minutes FROM calendar_reminder_state "
                "WHERE event_uid = ? AND calendar_entity_id = ? AND user_mapping_id = ?",
                (event_uid, calendar_entity_id, user_mapping_id),
            )
            return [row[0] for row in await cursor.fetchall()]

    @staticmethod
    async def cleanup_old(before_timestamp: int) -> int:
        async with get_db_write() as db:
            cursor = await db.execute(
                "DELETE FROM calendar_reminder_state WHERE fired_at < ?",
                (before_timestamp,),
            )
            return cursor.rowcount
