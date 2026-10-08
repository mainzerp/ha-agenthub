"""Send device mapping CRUD."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.db.repositories._utils import _normalize_device_name, _now, _validate_column_name
from app.db.schema import get_db_read, get_db_write

# Apostrophes join tokens ("Patric's" == "Patrics"); every other non-word
# character and the underscore separate tokens in the scan.
_SCAN_APOSTROPHE_RE = re.compile("['`\u00b4\u2018\u2019\u02bc]")
_SCAN_SEPARATOR_RE = re.compile(r"[\W_]+")


def _normalize_scan_text(text: str) -> str:
    """Unicode-aware normalization for the ``find_in_text`` containment scan.

    Casefold, drop apostrophes, NFKD with combining marks removed
    (``Küche`` -> ``kuche``), then every run of other non-word characters or
    underscores becomes one space. Unlike ``_normalize_device_name`` it keeps
    non-ASCII letters, so Cyrillic or Greek names neither vanish nor collapse
    onto a shared ASCII remainder. Applied to both the target text and the
    mapping names.
    """
    joined = _SCAN_APOSTROPHE_RE.sub("", (text or "").casefold())
    decomposed = unicodedata.normalize("NFKD", joined)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _SCAN_SEPARATOR_RE.sub(" ", stripped).strip()


def _name_spans(haystack: str, name: str) -> list[tuple[int, int]]:
    """Return every word-boundary span of ``name`` in the space-padded ``haystack``."""
    needle = f" {name} "
    spans: list[tuple[int, int]] = []
    pos = haystack.find(needle)
    while pos != -1:
        spans.append((pos + 1, pos + 1 + len(name)))
        pos = haystack.find(needle, pos + 1)
    return spans


def _longest_name_match(text: str, mappings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the single mapping whose display_name occurs in ``text`` at word boundaries.

    Both sides use ``_normalize_scan_text``, which leaves single-space
    separated word tokens, so a space-padded containment check is an exact
    word-boundary match. The longest match wins only when every other
    mapping's match lies inside one of its spans ("Laura" inside "Laura
    Handy"). A different name outside that span (a second recipient) or an
    equal-length match of another mapping is ambiguous and yields None.
    """
    haystack = f" {_normalize_scan_text(text)} "
    if not haystack.strip():
        return None
    matches: list[tuple[int, int, int]] = []  # (start, end, mapping index)
    for index, mapping in enumerate(mappings):
        name = _normalize_scan_text(mapping.get("display_name") or "")
        if name:
            matches.extend((start, end, index) for start, end in _name_spans(haystack, name))
    if not matches:
        return None
    best_len = max(end - start for start, end, _ in matches)
    winners = {index for start, end, index in matches if end - start == best_len}
    if len(winners) != 1:
        return None
    winner = winners.pop()
    winner_spans = [(start, end) for start, end, index in matches if index == winner]
    for start, end, index in matches:
        if index != winner and not any(w_start <= start and end <= w_end for w_start, w_end in winner_spans):
            return None
    return mappings[winner]


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
    async def find_in_text(text: str) -> dict[str, Any] | None:
        """Find the mapping whose display_name occurs inside free target text.

        Deterministic fallback for target phrasings that wrap the device
        name in other words (e.g. a condensed task naming the recipient).
        Contained names prefer the longest; separate names or equal-length
        ties are ambiguous and return None (see ``_longest_name_match``).
        """
        if not _normalize_scan_text(text):
            return None
        return _longest_name_match(text, await SendDeviceMappingRepository.list_all())

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
