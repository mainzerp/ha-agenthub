"""Shared utility helpers for repository modules."""

from __future__ import annotations

import re
from datetime import UTC, datetime


def _now() -> str:
    """Return current UTC timestamp as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _validate_column_name(col: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", col):
        raise ValueError(f"Invalid column name: {col}")
    return col


def _normalize_device_name(name: str) -> str:
    """Normalize a device display name for fuzzy comparison."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", name.lower())).strip()


def _phonetic_key(name: str) -> str | None:
    try:
        from pyphonetics import Metaphone  # type: ignore[import-untyped]

        meta = Metaphone()
        return meta.phonetics(name.strip())
    except Exception:
        return None
