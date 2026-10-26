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

# Unlinks recorded voice turns from a deleted profile or sample (they become assignable again).
_CLEAR_ASSIGNMENT = "UPDATE voice_turns SET assigned_profile_id = NULL, assigned_sample_id = NULL, assigned_at = NULL"

# A recorded turn whose result was never consumed by a speaker lookup (the
# lookup timed out, an action-cache replay) has no ``trace_id``. The listing
# then falls back to the first trace of the same conversation created from
# a few seconds before up to a minute after the recording that no other
# recorded turn is linked to.
TRACE_FALLBACK_BEFORE_S = 5.0
TRACE_FALLBACK_AFTER_S = 60.0

# Listing columns of ``voice_turns`` (no embedding) plus ``linked_trace_id``
# (the stored link, else the time-window fallback); filters are appended.
_TURN_LIST_INNER = (
    "SELECT t.id, t.created_at, t.conversation_id, t.trace_id, t.duration_s, t.speech_s, t.model, t.status, "
    "t.speaker_profile_id, t.score, t.margin, t.audio_path, t.assigned_profile_id, t.assigned_sample_id, "
    "t.assigned_at, COALESCE(t.trace_id, ("
    "SELECT f.trace_id FROM trace_summary f "
    "WHERE t.trace_id IS NULL AND t.conversation_id IS NOT NULL AND f.conversation_id = t.conversation_id "
    f"AND julianday(f.created_at) >= julianday(t.created_at) - {TRACE_FALLBACK_BEFORE_S / 86400.0!r} "
    f"AND julianday(f.created_at) <= julianday(t.created_at) + {TRACE_FALLBACK_AFTER_S / 86400.0!r} "
    "AND NOT EXISTS (SELECT 1 FROM voice_turns o WHERE o.trace_id = f.trace_id) "
    "ORDER BY f.created_at, f.id LIMIT 1)) AS linked_trace_id "
    "FROM voice_turns t"
)

# Joins the identified and assigned profile names and the user input of the linked trace.
_TURN_LIST_OUTER = (
    "SELECT x.*, sp.name AS speaker_name, ap.name AS assigned_name, ts.user_input AS transcript FROM ({inner}) x "
    "LEFT JOIN voice_profiles sp ON sp.id = x.speaker_profile_id "
    "LEFT JOIN voice_profiles ap ON ap.id = x.assigned_profile_id "
    "LEFT JOIN trace_summary ts ON ts.trace_id = x.linked_trace_id "
    "ORDER BY x.created_at DESC, x.id"
)


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
            # Explicit delete and unlink: the cascade and SET NULL only fire on
            # connections with ``PRAGMA foreign_keys=ON``.
            await db.execute(f"{_CLEAR_ASSIGNMENT} WHERE assigned_profile_id = ?", (profile_id,))
            await db.execute(
                "UPDATE voice_turns SET speaker_profile_id = NULL WHERE speaker_profile_id = ?", (profile_id,)
            )
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
        speech_s: float | None = None,
    ) -> int:
        """Store one enrollment sample (float32 embedding bytes) and return its id.

        ``duration_s`` is the raw turn length, ``speech_s`` the speech the
        voice-activity detector kept (None when the VAD did not run).
        """
        async with get_db_write() as db:
            cursor = await db.execute(
                "INSERT INTO voice_samples (profile_id, embedding, dim, model, duration_s, speech_s, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (profile_id, embedding, dim, model, duration_s, speech_s, _now()),
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
            # Turns assigned to these samples become assignable again.
            await db.execute(f"{_CLEAR_ASSIGNMENT} WHERE assigned_profile_id = ?", (profile_id,))
            cursor = await db.execute("DELETE FROM voice_samples WHERE profile_id = ?", (profile_id,))
            return cursor.rowcount

    @staticmethod
    async def list_samples(profile_id: int) -> list[dict[str, Any]]:
        """Return every sample of a profile (all models, oldest first), embedding bytes included."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT id, profile_id, embedding, dim, model, duration_s, speech_s, created_at "
                "FROM voice_samples WHERE profile_id = ? ORDER BY id",
                (profile_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    async def delete_sample(profile_id: int, sample_id: int) -> bool:
        """Delete one sample of a profile. Returns True when it existed."""
        async with get_db_write() as db:
            # Before the delete: with foreign keys on, the FK would null ``assigned_sample_id`` first.
            await db.execute(
                f"{_CLEAR_ASSIGNMENT} WHERE assigned_sample_id = ? AND assigned_profile_id = ?",
                (sample_id, profile_id),
            )
            cursor = await db.execute(
                "DELETE FROM voice_samples WHERE id = ? AND profile_id = ?",
                (sample_id, profile_id),
            )
            return cursor.rowcount > 0

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


class VoiceTurnRepository:
    """CRUD for ``voice_turns`` (recorded voice turns; the WAV files are handled by the caller)."""

    @staticmethod
    async def add_turn(
        turn_id: str,
        *,
        embedding: bytes,
        dim: int,
        model: str,
        status: str,
        audio_path: str,
        conversation_id: str | None = None,
        duration_s: float | None = None,
        speech_s: float | None = None,
        speaker_profile_id: int | None = None,
        score: float | None = None,
        margin: float | None = None,
        created_at: str | None = None,
    ) -> None:
        async with get_db_write() as db:
            await db.execute(
                "INSERT INTO voice_turns (id, created_at, conversation_id, duration_s, speech_s, embedding, dim, "
                "model, status, speaker_profile_id, score, margin, audio_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    created_at or _now(),
                    conversation_id,
                    duration_s,
                    speech_s,
                    embedding,
                    dim,
                    model,
                    status,
                    speaker_profile_id,
                    score,
                    margin,
                    audio_path,
                ),
            )

    @staticmethod
    def _filters(status: str | None, assigned: bool | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("t.status = ?")
            params.append(status)
        if assigned is True:
            clauses.append("t.assigned_sample_id IS NOT NULL")
        elif assigned is False:
            clauses.append("t.assigned_sample_id IS NULL")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    @staticmethod
    async def list_turns(
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        assigned: bool | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Newest first, without embeddings, plus the filtered total. ``assigned`` filters on the sample link.

        ``trace_id`` is the stored link; ``linked_trace_id`` adds the
        time-window fallback (see :data:`TRACE_FALLBACK_AFTER_S`) and selects ``transcript``.
        """
        where, params = VoiceTurnRepository._filters(status, assigned)
        async with get_db_read() as db:
            cursor = await db.execute(f"SELECT COUNT(*) FROM voice_turns t{where}", tuple(params))
            row = await cursor.fetchone()
            total = int(row[0]) if row else 0
            inner = f"{_TURN_LIST_INNER}{where} ORDER BY t.created_at DESC, t.id LIMIT ? OFFSET ?"
            cursor = await db.execute(_TURN_LIST_OUTER.format(inner=inner), (*params, limit, offset))
            return [dict(item) for item in await cursor.fetchall()], total

    @staticmethod
    async def get_turn(turn_id: str) -> dict[str, Any] | None:
        """One turn with its embedding bytes, or None."""
        async with get_db_read() as db:
            cursor = await db.execute(
                "SELECT id, created_at, conversation_id, trace_id, duration_s, speech_s, embedding, dim, model, "
                "status, speaker_profile_id, score, margin, audio_path, assigned_profile_id, assigned_sample_id, "
                "assigned_at FROM voice_turns WHERE id = ?",
                (turn_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def count() -> int:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM voice_turns")
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    async def set_trace_id(turn_id: str, trace_id: str) -> bool:
        async with get_db_write() as db:
            cursor = await db.execute("UPDATE voice_turns SET trace_id = ? WHERE id = ?", (trace_id, turn_id))
            return cursor.rowcount > 0

    @staticmethod
    async def mark_assigned(turn_id: str, profile_id: int, sample_id: int) -> bool:
        """Link the turn to its new enrollment sample. False when missing or already assigned."""
        async with get_db_write() as db:
            cursor = await db.execute(
                "UPDATE voice_turns SET assigned_profile_id = ?, assigned_sample_id = ?, assigned_at = ? "
                "WHERE id = ? AND assigned_sample_id IS NULL",
                (profile_id, sample_id, _now(), turn_id),
            )
            return cursor.rowcount > 0

    @staticmethod
    async def delete_turn(turn_id: str) -> str | None:
        """Delete one turn. Returns its audio file name, or None when it did not exist."""
        async with get_db_write() as db:
            cursor = await db.execute("SELECT audio_path FROM voice_turns WHERE id = ?", (turn_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            await db.execute("DELETE FROM voice_turns WHERE id = ?", (turn_id,))
            return str(row[0])

    @staticmethod
    async def delete_for_profile(profile_id: int) -> list[str]:
        """Delete turns identified as or assigned to a profile. Returns the audio file names."""
        async with get_db_write() as db:
            cursor = await db.execute(
                "SELECT audio_path FROM voice_turns WHERE speaker_profile_id = ? OR assigned_profile_id = ?",
                (profile_id, profile_id),
            )
            paths = [str(row[0]) for row in await cursor.fetchall()]
            if paths:
                await db.execute(
                    "DELETE FROM voice_turns WHERE speaker_profile_id = ? OR assigned_profile_id = ?",
                    (profile_id, profile_id),
                )
            return paths

    @staticmethod
    async def delete_all() -> list[str]:
        """Delete every turn. Returns the audio file names."""
        async with get_db_write() as db:
            cursor = await db.execute("SELECT audio_path FROM voice_turns")
            paths = [str(row[0]) for row in await cursor.fetchall()]
            await db.execute("DELETE FROM voice_turns")
            return paths

    @staticmethod
    async def delete_older_than(cutoff: str) -> list[str]:
        """Delete turns created before the ISO timestamp ``cutoff``. Returns the audio file names."""
        async with get_db_write() as db:
            cursor = await db.execute("SELECT audio_path FROM voice_turns WHERE created_at < ?", (cutoff,))
            paths = [str(row[0]) for row in await cursor.fetchall()]
            if paths:
                await db.execute("DELETE FROM voice_turns WHERE created_at < ?", (cutoff,))
            return paths

    @staticmethod
    async def list_audio_paths() -> set[str]:
        async with get_db_read() as db:
            cursor = await db.execute("SELECT audio_path FROM voice_turns")
            return {str(row[0]) for row in await cursor.fetchall()}
