"""Send device mapping CRUD."""

from __future__ import annotations

from typing import Any

from app.db.repositories._utils import _normalize_device_name, _now, _validate_column_name
from app.db.schema import get_db_read, get_db_write


class SendDeviceMappingRepository:
    """CRUD for send device name-to-service mappings."""

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        """Return all device mappings."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT id, display_name, device_type, ha_service_target, person_entity_id, created_at "
                "FROM send_device_mappings ORDER BY display_name"
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get(mapping_id: int) -> dict[str, Any] | None:
        """Get a single mapping by ID."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT id, display_name, device_type, ha_service_target, person_entity_id, created_at "
                "FROM send_device_mappings WHERE id = ?",
                (mapping_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def find_by_name(name: str) -> dict[str, Any] | None:
        """Find a mapping by display_name (case-insensitive, with normalized fallback)."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT id, display_name, device_type, ha_service_target, person_entity_id, created_at "
                "FROM send_device_mappings WHERE display_name = ? COLLATE NOCASE",
                (name.strip(),),
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)
            normalized_input = _normalize_device_name(name)
            if not normalized_input:
                return None
            cursor = await db.execute(
                "SELECT id, display_name, device_type, ha_service_target, person_entity_id, created_at FROM send_device_mappings"
            )
            for row in await cursor.fetchall():
                if _normalize_device_name(row["display_name"]) == normalized_input:
                    return dict(row)
            return None

    @staticmethod
    async def create(
        display_name: str, device_type: str, ha_service_target: str, person_entity_id: str | None = None
    ) -> int:
        """Insert a new mapping. Returns the new row ID."""
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO send_device_mappings (display_name, device_type, ha_service_target, person_entity_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (display_name.strip(), device_type, ha_service_target, person_entity_id, _now()),
            )
            return cursor.lastrowid or 0

    @staticmethod
    async def update(mapping_id: int, **kwargs: Any) -> bool:
        """Update fields of an existing mapping. Returns True if row existed."""
        allowed = {"display_name", "device_type", "ha_service_target", "person_entity_id"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        set_clause = ", ".join(f"{_validate_column_name(k)} = ?" for k in fields)
        values = [*list(fields.values()), mapping_id]
        async with get_db_write() as db:
            cursor = await db.execute(
                f"UPDATE send_device_mappings SET {set_clause} WHERE id = ?",
                values,
            )
            return cursor.rowcount > 0

    @staticmethod
    async def delete(mapping_id: int) -> bool:
        """Delete a mapping by ID. Returns True if row existed."""
        async with get_db_write() as db:
            cursor = await db.execute(
                "DELETE FROM send_device_mappings WHERE id = ?",
                (mapping_id,),
            )
            return cursor.rowcount > 0
