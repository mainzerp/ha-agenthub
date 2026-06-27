"""Shared helpers for the timer action package (parsing, formatting, scheduler access)."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


_ACTION_PHRASES: dict[str, str] = {}


_WORD_DIGIT_MAP = {
    "ein": "1",
    "eine": "1",
    "einminuten": "1minuten",
    "zwei": "2",
    "drei": "3",
    "vier": "4",
    "funf": "5",
    "f\u00fcnf": "5",
    "sechs": "6",
    "sieben": "7",
    "acht": "8",
    "neun": "9",
    "zehn": "10",
    "minuten": "min",
    "minute": "min",
    "stunden": "h",
    "stunde": "h",
    "sekunden": "s",
    "sekunde": "s",
}


def _supports_method(obj: Any, method_name: str) -> bool:
    """Return True when an object or its mock spec exposes a callable method."""
    spec_class = getattr(obj, "_spec_class", None)
    if spec_class and hasattr(spec_class, method_name):
        return callable(getattr(obj, method_name, None))
    return callable(getattr(obj, method_name, None))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_timer_service_data(action: dict) -> dict[str, Any]:
    params = action.get("parameters") or {}
    data: dict[str, Any] = {}
    if "duration" in params:
        data["duration"] = str(params["duration"])
    if "datetime" in params:
        data["datetime"] = str(params["datetime"])
    if "time" in params:
        data["time"] = str(params["time"])
    if "date" in params:
        data["date"] = str(params["date"])
    return data


def _parse_duration_seconds(duration_str: str) -> int | None:
    """Parse HH:MM:SS or MM:SS or seconds string into total seconds."""
    if not duration_str:
        return None
    parts = str(duration_str).split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(float(parts[0]))
    except (ValueError, IndexError):
        return None


def _format_duration_human(total_seconds: int) -> str:
    if total_seconds <= 0:
        return "0 seconds"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return " and ".join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + f", and {parts[-1]}"


# ---------------------------------------------------------------------------
# Scheduler accessor
# ---------------------------------------------------------------------------


def _get_scheduler() -> Any | None:
    """Return the process-wide ``TimerScheduler``, if available.

    Falls back to ``None`` in unit-test contexts that exercise this
    module without a running FastAPI app. Tests that need scheduler
    behaviour patch ``app.agents.timer_executor._helpers._get_scheduler``
    directly.
    """
    try:
        from app.main import app

        return getattr(app.state, "timer_scheduler", None)
    except Exception:
        return None


def _normalize_timer_name(s: str) -> str:
    """Casefold and strip separators for fuzzy timer-name matching.

    Applied only as a fallback after exact (LOWER=LOWER) match fails.
    Handles German compound variants: Einminutentimer, 1-Minuten-Timer, etc.
    """
    normalized = s.casefold().replace("-", "").replace(" ", "").replace("_", "")
    for word, digit in _WORD_DIGIT_MAP.items():
        normalized = normalized.replace(word, digit)
    return normalized


def _normalize_alarm_name(s: str) -> str:
    """Normalize alarm names for deterministic cancellation matching."""
    text = str(s or "").strip().casefold()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _get_timezone_info(timezone: str | None) -> ZoneInfo | None:
    if not timezone:
        return None
    try:
        return ZoneInfo(str(timezone))
    except Exception:
        return None


def _format_alarm_time_local(epoch: int, *, timezone: str | None = None) -> str:
    tz = _get_timezone_info(timezone)
    if tz is not None:
        return datetime.fromtimestamp(int(epoch), tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M:%S")
