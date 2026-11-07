"""Shared helpers for the timer action package (parsing, formatting, scheduler access)."""

from __future__ import annotations

import json
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
# Longest key first, so "eine" is not consumed as "ein" + "e" and
# "einminuten" wins over "ein".
_WORD_DIGIT_ITEMS: tuple[tuple[str, str], ...] = tuple(
    sorted(_WORD_DIGIT_MAP.items(), key=lambda item: len(item[0]), reverse=True)
)


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
    for word, digit in _WORD_DIGIT_ITEMS:
        normalized = normalized.replace(word, digit)
    return normalized


def _normalize_alarm_name(s: str) -> str:
    """Normalize alarm names for deterministic cancellation matching."""
    text = str(s or "").strip().casefold()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


# Scheduler row kinds by user-facing concept. ``TIMER_KINDS`` are everything a
# timer action may address by name; ``COUNTDOWN_KINDS`` are what an unnamed
# "the timer" refers to (sleep timers and delayed actions must be named).
TIMER_KINDS: frozenset[str] = frozenset({"plain", "notification", "snooze", "sleep", "delayed_action"})
COUNTDOWN_KINDS: frozenset[str] = frozenset({"plain", "notification", "snooze"})
ALARM_KINDS: frozenset[str] = frozenset({"alarm"})


def _default_timer_label(seconds: int) -> str:
    """Label for an unnamed timer (``30 seconds timer``, ``5 minutes timer``)."""
    return f"{_format_duration_human(int(seconds))} timer"


# Label of the countdown an unnamed ``snooze_timer`` schedules.
SNOOZE_LABEL = "snoozed timer"


def _duration_name_keys(seconds: int) -> frozenset[str]:
    """Normalized forms of a name that only states a timer's duration.

    Built from the unit codes ``_normalize_timer_name`` produces (``h``,
    ``min``, ``s``), so ``5-minute timer``, ``5 Minuten Timer`` and the
    default label ``5 minutes timer`` all compare equal for 300 seconds.
    """
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    compact = f"{hours}h" if hours else ""
    compact += f"{minutes}min" if minutes else ""
    compact += f"{secs}s" if secs else ""
    default = _normalize_timer_name(_default_timer_label(seconds))
    keys = {default, compact + "timer"}
    if compact:
        keys.add(compact)
    return frozenset(keys)


def _names_duration(name: str | None, seconds: int | None) -> bool:
    """True when ``name`` only states the duration ``seconds`` (no own identity)."""
    if not name or not seconds or seconds <= 0:
        return False
    return _normalize_timer_name(str(name)) in _duration_name_keys(int(seconds))


def _timer_label(name: str | None, seconds: int) -> str:
    """Name to store for a new timer: a real name verbatim, otherwise the default label.

    The kind word ("timer") and a name that only restates the duration
    ("5-minute timer") carry no identity, so separate unnamed timers keep
    distinct, duration-based labels instead of all being called "timer".
    """
    text = (name or "").strip()
    if _is_unnamed(text, "timer") or _names_duration(text, seconds):
        return _default_timer_label(seconds)
    return text


def _timer_duration_selector(params: dict[str, Any] | None) -> int | None:
    """Seconds of the structured ``timer_duration`` selector ("the 5-minute timer"), if valid."""
    raw = str((params or {}).get("timer_duration") or "").strip()
    seconds = _parse_duration_seconds(raw) if raw else None
    return seconds if seconds and seconds > 0 else None


def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _row_set_seconds(row: dict[str, Any]) -> int | None:
    """Duration a timer row was set for (kept across extend, pause and resume)."""
    seconds = _parse_duration_seconds(str(_row_payload(row).get("duration") or ""))
    if seconds and seconds > 0:
        return seconds
    try:
        stored = int(row.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        return None
    return stored if stored > 0 else None


def _row_is_unnamed(row: dict[str, Any]) -> bool:
    """True for a timer row without a user-given name.

    Covers the default label, the snooze label, a name that only restates the
    duration, and legacy rows stored under the kind word ("timer").
    """
    name = str(row.get("logical_name") or "")
    if _is_unnamed(name, "timer") or _normalize_alarm_name(name) == SNOOZE_LABEL:
        return True
    return _names_duration(name, _row_set_seconds(row))


def _row_label(row: dict[str, Any]) -> str:
    """User-facing label of a timer row; legacy rows named "timer" get their default label."""
    name = str(row.get("logical_name") or "").strip()
    if _is_unnamed(name, "timer"):
        seconds = _row_set_seconds(row)
        return _default_timer_label(seconds) if seconds else "timer"
    return name


def _remaining_seconds(row: dict[str, Any], now: int) -> int | None:
    """Remaining seconds of a pending or paused row; None when unknown."""
    if row.get("state") == "paused":
        try:
            return max(0, int(_row_payload(row).get("paused_remaining_seconds") or 0))
        except (TypeError, ValueError):
            return 0
    if row.get("fires_at") is None:
        return None
    return max(0, int(row["fires_at"]) - now)


def _describe_timer_row(row: dict[str, Any], now: int) -> str:
    """Distinguishable description for a clarifying question (label plus time left)."""
    label = _row_label(row)
    remaining = _remaining_seconds(row, now)
    if remaining is None:
        return label
    human = _format_duration_human(remaining)
    if row.get("state") == "paused":
        return f"{label}, paused with {human} left"
    return f"{label} with {human} left"


def _name_tokens(name: str) -> frozenset[str]:
    return frozenset(_normalize_alarm_name(name).split())


def _is_unnamed(name: str | None, kind_word: str) -> bool:
    """True when ``name`` carries no identity: empty, or just the kind word itself.

    ``kind_word`` is the canonical internal kind (``timer`` or ``alarm``); the
    check is structural and not a per-language phrase list.
    """
    normalized = _normalize_alarm_name(name or "")
    return not normalized or normalized == kind_word


def _match_rows_by_name(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Match rows by name: exact, separator-insensitive, duration, then token subset.

    The duration stage lets a reference that only states a duration
    ("5-minute timer") address an unnamed timer set for that duration. The
    token-subset stage lets a short reference ("pasta") address a longer
    stored label ("pasta timer").
    """
    wanted = name.strip().casefold()
    exact = [r for r in rows if str(r.get("logical_name") or "").strip().casefold() == wanted]
    if exact:
        return exact
    norm = _normalize_timer_name(name)
    normalized = [r for r in rows if _normalize_timer_name(str(r.get("logical_name") or "")) == norm]
    if normalized:
        return normalized
    by_duration = [r for r in rows if _row_is_unnamed(r) and _names_duration(name, _row_set_seconds(r))]
    if by_duration:
        return by_duration
    tokens = _name_tokens(name)
    if not tokens:
        return []
    return [r for r in rows if tokens <= _name_tokens(str(r.get("logical_name") or ""))]


async def _find_rows(
    scheduler: Any,
    name: str | None,
    *,
    area_id: str | None,
    kinds: frozenset[str],
    unnamed_kinds: frozenset[str],
    kind_word: str,
    states: frozenset[str] | None = None,
    set_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """Find scheduler rows for a user reference, room-scoped first.

    The origin room is searched first; when it yields nothing, every room is
    searched (timers set from chat, another room, or via ``target_satellite``
    carry a different ``origin_area``). An unnamed reference resolves to the
    rows of ``unnamed_kinds``; the caller acts when exactly one row remains
    and asks otherwise. ``set_seconds`` (the structured ``timer_duration``
    selector) keeps only rows set for that duration; when a name matches
    nothing it is applied to the ``unnamed_kinds`` rows instead.
    """
    unnamed = _is_unnamed(name, kind_word)
    scopes: list[str | None] = [area_id, None] if area_id is not None else [None]
    for scope in scopes:
        rows = await scheduler.list(area=scope, kinds=set(kinds), **({"states": set(states)} if states else {}))
        # Rows without a kind predate kinds and are plain timers.
        rows = [r for r in rows if (r.get("kind") or "plain") in kinds]
        unnamed_rows = [r for r in rows if (r.get("kind") or "plain") in unnamed_kinds]
        matches = unnamed_rows if unnamed else _match_rows_by_name(rows, str(name))
        if set_seconds is not None:
            pool = matches or unnamed_rows
            matches = [r for r in pool if _row_set_seconds(r) == set_seconds]
        if matches:
            return matches
    return []


def _same_logical_timer(rows: list[dict[str, Any]]) -> bool:
    """True when several rows are one user-named timer (same name, none unnamed)."""
    return _same_name(rows) and not any(_row_is_unnamed(r) for r in rows)


def _same_name(rows: list[dict[str, Any]]) -> bool:
    names = {_normalize_timer_name(str(r.get("logical_name") or "")) for r in rows}
    return len(names) == 1


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
