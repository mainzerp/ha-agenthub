"""Voice profiles and enrollment samples for speaker identification."""

from __future__ import annotations

from typing import Any

import aiosqlite

from app.db.repositories._utils import _now
from app.db.schema import get_db_read, get_db_write

MAX_PROFILE_NAME_LENGTH = 64
MAX_HA_USER_ID_LENGTH = 128

_PROFILE_COLUMNS = "id, name, ha_user_id, created_at, updated_at"

# Sentinel for "field not provided" in partial updates.
_UNSET: Any = object()


class VoiceProfileNameConflictError(ValueError):
    """A voice profile with the same name already exists."""


def normalize_profile_name(value: str | None) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` when empty or too long."""
    cleaned = " ".join((value or "").split())
    if not cleaned:
        raise ValueError("name is required")
    if len(cleaned) > MAX_PROFILE_NAME_LENGTH:
        raise ValueError(f"name exceeds {MAX_PROFILE_NAME_LENGTH} characters")
    return cleaned


def normalize_ha_user_id(value: str | None) -> str | None:
    """Return the stripped HA user id, ``None`` for empty, or raise when too long."""
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_HA_USER_ID_LENGTH:
        raise ValueError(f"ha_user_id exceeds {MAX_HA_USER_ID_LENGTH} characters")
    return cleaned


class VoiceProfileRepository:
    """CRUD for ``voice_profiles`` and ``voice_samples``."""

    @staticmethod
    async def list_profiles(model: str | None = None) -> list[dict[str, Any]]:
        """Return all profiles with ``sample_count`` (samples of ``model`` only when given)."""
        async with get_db_read() as db:
            if model is None:
                cursor = await db.execute(
                    "SELECT p.id, p.name, p.ha_user_id, p.created_at, p.updated_at, COUNT(s.id) AS sample_count "
                    "FROM voice_profiles p LEFT JOIN voice_samples s ON s.profile_id = p.id "
                    "GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
                )
            else:
                cursor = await db.execute(
                    "SELECT p.id, p.name, p.ha_user_id, p.created_at, p.updated_at, COUNT(s.id) AS sample_count "
                    "FROM voice_profiles p LEFT JOIN voice_samples s ON s.profile_id = p.id AND s.model = ? "
                    "GROUP BY p.id ORDER BY p.name COLLATE NOCASE",
                    (model,),
                )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def get_profile(profile_id: int) -> dict[str, Any] | None:
        async with get_db_read() as db:
            cursor = await db.execute(f"SELECT {_PROFILE_COLUMNS} FROM voice_profiles WHERE id = ?", (profile_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def create_profile(name: str, ha_user_id: str | None = None) -> dict[str, Any]:
        """Create a profile. Raises ``VoiceProfileNameConflictError`` on a duplicate name."""
        cleaned_name = normalize_profile_name(name)
        cleaned_user = normalize_ha_user_id(ha_user_id)
        now = _now()
        try:
            async with get_db_write() as db:
                cursor = await db.execute(
                    "INSERT INTO voice_profiles (name, ha_user_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (cleaned_name, cleaned_user, now, now),
                )
                profile_id = cursor.lastrowid
        except aiosqlite.IntegrityError as exc:
            raise VoiceProfileNameConflictError(f"A voice profile named '{cleaned_name}' already exists") from exc
        return {
            "id": profile_id,
            "name": cleaned_name,
            "ha_user_id": cleaned_user,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    async def update_profile(
        profile_id: int,
        *,
        name: str | None = _UNSET,
        ha_user_id: str | None = _UNSET,
    ) -> dict[str, Any] | None:
        """Rename and/or (un)map a profile. Returns the updated row, or None when missing."""
        assignments: list[str] = []
        params: list[Any] = []
        if name is not _UNSET:
            assignments.append("name = ?")
            params.append(normalize_profile_name(name))
        if ha_user_id is not _UNSET:
            assignments.append("ha_user_id = ?")
            params.append(normalize_ha_user_id(ha_user_id))
        assignments.append("updated_at = ?")
        params.append(_now())
        params.append(profile_id)
        try:
            async with get_db_write() as db:
                cursor = await db.execute(
                    f"UPDATE voice_profiles SET {', '.join(assignments)} WHERE id = ?",
                    tuple(params),
                )
                if cursor.rowcount == 0:
                    return None
        except aiosqlite.IntegrityError as exc:
            raise VoiceProfileNameConflictError("A voice profile with this name already exists") from exc
        return await VoiceProfileRepository.get_profile(profile_id)

    @staticmethod
    async def rename_profile(profile_id: int, name: str) -> dict[str, Any] | None:
        return await VoiceProfileRepository.update_profile(profile_id, name=name)

    @staticmethod
    async def set_ha_user(profile_id: int, ha_user_id: str | None) -> dict[str, Any] | None:
        return await VoiceProfileRepository.update_profile(profile_id, ha_user_id=ha_user_id)

    @staticmethod
    async def delete_profile(profile_id: int) -> bool:
        """Delete a profile and its samples. Returns True if the profile existed."""
        async with get_db_write() as db:
            # Explicit delete: the cascade only fires on connections with
            # ``PRAGMA foreign_keys=ON``.
            await db.execute("DELETE FROM voice_samples WHERE profile_id = ?", (profile_id,))
            cursor = await db.execute("DELETE FROM voice_profiles WHERE id = ?", (profile_id,))
            return cursor.rowcount > 0

    @staticmethod
    async def add_sample(
        profile_id: int,
        embedding: bytes,
        dim: int,
        model: str,
        duration_s: float | None = None,
    ) -> int:
        """Store one enrollment sample (float32 embedding bytes) and return its id."""
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO voice_samples (profile_id, embedding, dim, model, duration_s, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (profile_id, embedding, dim, model, duration_s, _now()),
            )
            return int(cursor.lastrowid or 0)

    @staticmethod
    async def count_samples(profile_id: int, model: str | None = None) -> int:
        async with get_db_read() as db:
            if model is None:
                cursor = await db.execute("SELECT COUNT(*) FROM voice_samples WHERE profile_id = ?", (profile_id,))
            else:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM voice_samples WHERE profile_id = ? AND model = ?",
                    (profile_id, model),
                )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    async def delete_samples(profile_id: int) -> int:
        """Delete every sample of a profile. Returns the number of deleted rows."""
        async with get_db_write() as db:
            cursor = await db.execute("DELETE FROM voice_samples WHERE profile_id = ?", (profile_id,))
            return cursor.rowcount

    @staticmethod
    async def list_samples_for_model(model: str) -> list[dict[str, Any]]:
        """Return ``profile_id, name, ha_user_id, embedding, dim`` for every sample of ``model``."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT s.profile_id, p.name, p.ha_user_id, s.embedding, s.dim "
                "FROM voice_samples s JOIN voice_profiles p ON p.id = s.profile_id "
                "WHERE s.model = ? ORDER BY s.profile_id, s.id",
                (model,),
            )
            return [dict(row) for row in await cursor.fetchall()]
