"""Helpers for Home Assistant Recorder history windows and speech summaries."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app.models.agent import TaskContext

logger = logging.getLogger(__name__)

MAX_HISTORY_SPAN_DAYS = 7
DEFAULT_LOOKBACK_HOURS = 24
MAX_SUMMARY_POINTS = 400


def display_zone_for_context(task_context: TaskContext | None) -> tzinfo:
    """Timezone for formatting history timestamps (speech / traces)."""
    return _zoneinfo(getattr(task_context, "timezone", None) if task_context else None)


def _zoneinfo(tz_name: str | None) -> tzinfo:
    if not tz_name or not str(tz_name).strip():
        return UTC
    try:
        return ZoneInfo(str(tz_name).strip())
    except Exception:
        logger.debug("Invalid timezone %r, using UTC", tz_name)
        return UTC


def parse_history_window(
    parameters: dict[str, Any],
    task_context: TaskContext | None,
) -> tuple[datetime, datetime] | tuple[None, None]:
    """Return aware UTC (start, end) for a recorder query, or (None, None) on error.

    Accepts:
    - ``period``: ``yesterday`` | ``last_24_hours`` | ``last_7_days`` (default last_24_hours)
    - ``start`` / ``end``: ISO-8601 strings (optional). When both set, ``period`` is ignored.
    """
    params = parameters or {}
    tz = _zoneinfo(getattr(task_context, "timezone", None) if task_context else None)
    now_local = datetime.now(tz)
    period = str(params.get("period") or "last_24_hours").lower().strip()

    start_s = params.get("start")
    end_s = params.get("end")
    if start_s and end_s:
        try:
            start = datetime.fromisoformat(str(start_s).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(end_s).replace("Z", "+00:00"))
        except ValueError:
            return None, None
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)
        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
    elif period == "yesterday":
        start_local = (now_local - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(UTC)
        end_utc = end_local.astimezone(UTC)
    elif period in ("last_7_days", "last_week"):
        end_utc = now_local.astimezone(UTC)
        start_utc = end_utc - timedelta(days=7)
    else:
        # last_24_hours and default
        end_utc = now_local.astimezone(UTC)
        start_utc = end_utc - timedelta(hours=DEFAULT_LOOKBACK_HOURS)

    if end_utc <= start_utc:
        return None, None
    if (end_utc - start_utc).total_seconds() > MAX_HISTORY_SPAN_DAYS * 86400:
        return None, None
    return start_utc, end_utc


def _try_float(state: str) -> float | None:
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


def summarize_history_for_speech(
    entity_id: str,
    friendly_name: str,
    history_groups: list[list[dict[str, Any]]],
    *,
    display_tz: tzinfo,
) -> str:
    """Turn HA /api/history response into a concise sentence for TTS/chat."""
    if not history_groups or not history_groups[0]:
        return (
            f"No recorder history for {friendly_name} ({entity_id}) in that time range. "
            "The entity may be new, recorder retention may exclude it, or the window was empty."
        )

    states = history_groups[0]
    if len(states) > MAX_SUMMARY_POINTS:
        step = max(1, len(states) // MAX_SUMMARY_POINTS)
        states = states[::step]

    values_f: list[float] = []
    for row in states:
        v = _try_float(str(row.get("state", "")))
        if v is not None:
            values_f.append(v)

    def _fmt_ts(iso_s: str) -> str:
        if not iso_s or "T" not in iso_s:
            return iso_s or "?"
        try:
            dt = datetime.fromisoformat(iso_s.replace("Z", "+00:00"))
            return dt.astimezone(display_tz).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return iso_s[:16]

    first = states[0]
    last = states[-1]
    t_first = _fmt_ts(str(first.get("last_changed", "") or first.get("last_updated", "")))
    t_last = _fmt_ts(str(last.get("last_changed", "") or last.get("last_updated", "")))

    if len(values_f) >= 2:
        lo, hi = min(values_f), max(values_f)
        avg = sum(values_f) / len(values_f)
        unit = ""
        attrs = last.get("attributes") or {}
        if isinstance(attrs, dict):
            unit = str(attrs.get("unit_of_measurement") or "").strip()
        u = f" {unit}" if unit else ""
        return (
            f"{friendly_name}: from {t_first} to {t_last}, "
            f"values ranged from {lo:.2f}{u} to {hi:.2f}{u} "
            f"(average {avg:.2f}{u}, {len(values_f)} samples)."
        )

    # Non-numeric or sparse: describe transitions
    uniq: list[tuple[str, str]] = []
    for row in states:
        st = str(row.get("state", ""))
        ts = _fmt_ts(str(row.get("last_changed", "") or row.get("last_updated", "")))
        if not uniq or uniq[-1][0] != st:
            uniq.append((st, ts))
        if len(uniq) >= 12:
            break
    parts = [f"{st} (from {ts})" for st, ts in uniq]
    more = len(states) - len(uniq)
    suffix = f"; … plus {more} more updates" if more > 0 else ""
    return f"{friendly_name}: " + "; ".join(parts) + suffix + f". Window {t_first} - {t_last}."
