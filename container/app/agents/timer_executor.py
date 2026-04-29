"""Timer-specific action execution.

In 0.26.0 the HA ``timer.*`` helper-pool model was removed entirely.
All timer-shaped actions route to the AgentHub-managed
``TimerScheduler`` (``app.agents.timer_scheduler``).

This module retains:
- read-only handlers (``query_timer``, ``list_timers`` against the
    scheduler; ``list_alarms`` against internal scheduler alarms)
- ``set_datetime`` (internal scheduler-backed alarm create)
- ``cancel_alarm`` (internal scheduler-backed alarm cancel)
- ``create_reminder`` / ``create_recurring_reminder`` (HA
  ``calendar.create_event``)

All HA ``timer.*`` service calls, the ``_TimerPool`` class, the
``_find_idle_timer`` allocator, the ``on_timer_finished`` WebSocket
handler, and the expired-timer tracking deque are deleted.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.analytics.tracer import _optional_span
from app.entity.deterministic_resolver import resolve_entity_deterministic_first

logger = logging.getLogger(__name__)


_ACTION_PHRASES: dict[str, str] = {}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"input_datetime"})

_INPUT_DATETIME_DOMAINS: frozenset[str] = frozenset({"input_datetime"})
_CALENDAR_DOMAINS: frozenset[str] = frozenset({"calendar"})
_ALARM_WEEKDAY_CODES: frozenset[str] = frozenset({"MO", "TU", "WE", "TH", "FR", "SA", "SU"})


def _validate_domain(entity_id: str) -> bool:
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _supports_method(obj: Any, method_name: str) -> bool:
    """Return True when an object or its mock spec exposes a callable method."""
    spec_class = getattr(obj, "_spec_class", None)
    if spec_class and hasattr(spec_class, method_name):
        return callable(getattr(obj, method_name, None))
    return callable(getattr(obj, method_name, None))


async def _list_visible_input_datetime_targets(
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
) -> list[tuple[str, str]]:
    """Return visible input_datetime targets as (entity_id, friendly_name)."""
    if not entity_index:
        return []

    entries: list[Any] = []
    if _supports_method(entity_index, "list_entries_async"):
        entries = await entity_index.list_entries_async(domains=_INPUT_DATETIME_DOMAINS)
    elif _supports_method(entity_index, "list_entries"):
        entries = entity_index.list_entries(domains=_INPUT_DATETIME_DOMAINS)

    if not entries:
        return []

    visible_entries = entries
    if agent_id and entity_matcher and _supports_method(entity_matcher, "filter_visible_results"):
        from app.entity.matcher import MatchResult

        visible = await entity_matcher.filter_visible_results(
            agent_id,
            [
                MatchResult(
                    entity_id=entry.entity_id,
                    friendly_name=entry.friendly_name,
                    score=1.0,
                )
                for entry in entries
            ],
        )
        visible_ids = {result.entity_id for result in visible}
        visible_entries = [entry for entry in entries if entry.entity_id in visible_ids]

    targets = [
        (
            entry.entity_id,
            (entry.friendly_name or entry.entity_id),
        )
        for entry in visible_entries
        if getattr(entry, "entity_id", "").startswith("input_datetime.")
    ]
    targets.sort(key=lambda item: (item[1].casefold(), item[0]))
    return targets


def _should_attempt_set_datetime_fallback(action_name: str, action: dict) -> bool:
    """Gate unresolved set_datetime fallback without keyword heuristics."""
    return False


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
    behaviour patch ``app.agents.timer_executor._get_scheduler``
    directly.
    """
    try:
        from app.main import app

        return getattr(app.state, "timer_scheduler", None)
    except Exception:
        return None


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


def _extract_cancel_alarm_selectors(action: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    """Extract and normalize supported cancel_alarm selectors."""
    params = action.get("parameters") or {}
    raw_id = str(params.get("id") or "").strip()
    raw_datetime = str(params.get("datetime") or "").strip()
    raw_time = str(params.get("time") or "").strip()
    raw_date = str(params.get("date") or "").strip()
    raw_name = str(params.get("name") or action.get("entity") or "").strip()

    normalized: dict[str, str] = {"id": raw_id, "name": raw_name}

    if raw_datetime:
        candidate = raw_datetime.replace("T", " ")
        try:
            normalized["datetime"] = datetime.fromisoformat(candidate).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return {}, "Invalid datetime format. Use YYYY-MM-DD HH:MM:SS."

    if raw_time:
        parsed_time = None
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(raw_time, fmt).time()
                break
            except ValueError:
                continue
        if parsed_time is None:
            return {}, "Invalid time format. Use HH:MM or HH:MM:SS."
        normalized["time"] = parsed_time.strftime("%H:%M:%S")

    if raw_date:
        try:
            normalized["date"] = datetime.fromisoformat(raw_date).date().isoformat()
        except ValueError:
            return {}, "Invalid date format. Use YYYY-MM-DD."

    return normalized, None


def _filter_alarm_rows_by_schedule(
    rows: list[dict[str, Any]],
    *,
    target_datetime: str = "",
    target_time: str = "",
    target_date: str = "",
    timezone: str | None = None,
) -> list[dict[str, Any]]:
    """Filter pending internal alarm rows by local scheduled datetime or time/date."""
    matches: list[dict[str, Any]] = []
    tz = _get_timezone_info(timezone)
    for row in rows:
        fires_at = int(row.get("fires_at") or 0)
        local_dt = datetime.fromtimestamp(fires_at, tz=tz) if tz is not None else datetime.fromtimestamp(fires_at)
        local_datetime = local_dt.strftime("%Y-%m-%d %H:%M:%S")
        local_time = local_dt.strftime("%H:%M:%S")
        local_date = local_dt.date().isoformat()

        if target_datetime:
            if local_datetime == target_datetime:
                matches.append(row)
            continue

        if target_time and local_time != target_time:
            continue
        if target_date and local_date != target_date:
            continue
        if target_time:
            matches.append(row)

    return matches


def _parse_alarm_target_epoch(
    params: dict[str, Any],
    *,
    now_ts: int,
    timezone: str | None = None,
) -> tuple[int | None, str | None]:
    raw_datetime = str(params.get("datetime", "")).strip()
    raw_time = str(params.get("time", "")).strip()
    raw_date = str(params.get("date", "")).strip()

    tz = _get_timezone_info(timezone)

    if raw_datetime:
        candidate = raw_datetime.replace("T", " ")
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            return None, "Invalid datetime format. Use YYYY-MM-DD HH:MM:SS."
        if dt.tzinfo is None and tz is not None:
            dt = dt.replace(tzinfo=tz)
            now_ref = datetime.fromtimestamp(now_ts, tz=tz)
            if dt <= now_ref:
                return None, "Alarm datetime must be in the future."
            return int(dt.timestamp()), None
        epoch = int(dt.timestamp())
        if epoch <= now_ts:
            return None, "Alarm datetime must be in the future."
        return epoch, None

    if raw_time:
        parsed_time = None
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(raw_time, fmt).time()
                break
            except ValueError:
                continue
        if parsed_time is None:
            return None, "Invalid time format. Use HH:MM or HH:MM:SS."
        if tz is not None:
            now_local = datetime.fromtimestamp(now_ts, tz=tz)
            scheduled = datetime.combine(now_local.date(), parsed_time, tzinfo=tz)
            if scheduled <= now_local:
                scheduled = scheduled + timedelta(days=1)
            return int(scheduled.timestamp()), None

        now_local = datetime.fromtimestamp(now_ts)
        scheduled = datetime.combine(now_local.date(), parsed_time)
        if int(scheduled.timestamp()) <= now_ts:
            scheduled = scheduled + timedelta(days=1)
        return int(scheduled.timestamp()), None

    if raw_date:
        try:
            target_date = datetime.fromisoformat(raw_date).date()
        except ValueError:
            return None, "Invalid date format. Use YYYY-MM-DD."
        if tz is not None:
            scheduled = datetime.combine(target_date, datetime.min.time(), tzinfo=tz)
            now_local = datetime.fromtimestamp(now_ts, tz=tz)
            if scheduled <= now_local:
                return None, "Alarm date must be in the future."
            return int(scheduled.timestamp()), None

        scheduled = datetime.combine(target_date, datetime.min.time())
        epoch = int(scheduled.timestamp())
        if epoch <= now_ts:
            return None, "Alarm date must be in the future."
        return epoch, None

    return None, "Provide one of datetime, time, or date for set_datetime."


def _build_recurring_alarm_payload(
    params: dict[str, Any],
    *,
    target_epoch: int,
    timezone: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    recurrence = params.get("recurrence")
    if recurrence is None:
        return None, None
    if not isinstance(recurrence, dict):
        return None, 'Invalid recurrence payload. Use an object like {"freq": "daily"}.'

    raw_freq = str(recurrence.get("freq") or "").strip().casefold()
    if raw_freq not in {"daily", "weekly"}:
        return None, "Invalid recurrence frequency. Supported values are 'daily' and 'weekly'."

    try:
        interval = int(recurrence.get("interval", 1))
    except (TypeError, ValueError):
        return None, "Invalid recurrence interval. Use a positive integer."
    if interval < 1:
        return None, "Invalid recurrence interval. Use a positive integer."

    tz = _get_timezone_info(timezone)
    target_dt = (
        datetime.fromtimestamp(int(target_epoch), tz=tz)
        if tz is not None
        else datetime.fromtimestamp(int(target_epoch))
    )
    normalized: dict[str, Any] = {
        "freq": raw_freq,
        "interval": interval,
        "anchor_time": target_dt.strftime("%H:%M:%S"),
    }
    if timezone:
        normalized["timezone"] = str(timezone)

    if raw_freq == "weekly":
        raw_byweekday = recurrence.get("byweekday")
        if not isinstance(raw_byweekday, list) or not raw_byweekday:
            return None, "Weekly recurrence requires a non-empty byweekday list (e.g. ['MO','WE'])."

        seen: set[str] = set()
        normalized_weekdays: list[str] = []
        for item in raw_byweekday:
            code = str(item or "").strip().upper()
            if code not in _ALARM_WEEKDAY_CODES:
                return None, "Invalid weekday code in recurrence.byweekday. Use MO,TU,WE,TH,FR,SA,SU."
            if code in seen:
                continue
            seen.add(code)
            normalized_weekdays.append(code)

        if not normalized_weekdays:
            return None, "Weekly recurrence requires at least one valid weekday code."
        normalized["byweekday"] = normalized_weekdays

    return normalized, None


# ---------------------------------------------------------------------------
# Read-only action handlers
# ---------------------------------------------------------------------------


async def _handle_read_action(
    action_name: str,
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    area_id: str | None = None,
    timezone: str | None = None,
) -> dict:
    if action_name == "query_timer":
        return await _query_timer(entity_query, area_id=area_id)
    if action_name == "list_timers":
        return await _list_timers(area_id=area_id)
    if action_name == "list_alarms":
        return await _list_alarms(area_id=area_id, timezone=timezone)
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}


async def _query_timer(entity_query: str, *, area_id: str | None = None) -> dict:
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
            "cacheable": False,
        }
    rows = await scheduler.list(logical_name=entity_query or None, area=area_id)
    if not rows:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"No timer named '{entity_query}' is currently running.",
            "cacheable": False,
        }
    row = rows[0]
    remaining = max(0, int(row["fires_at"]) - int(datetime.now().timestamp()))
    human = _format_duration_human(remaining)
    return {
        "success": True,
        "entity_id": None,
        "new_state": "active",
        "speech": f"{row['logical_name']} has {human} remaining.",
        "cacheable": False,
    }


async def _list_timers(*, area_id: str | None = None) -> dict:
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": True,
            "entity_id": "",
            "new_state": None,
            "speech": "No timers are currently running.",
            "cacheable": False,
        }
    rows = await scheduler.list(area=area_id)
    if not rows:
        return {
            "success": True,
            "entity_id": "",
            "new_state": None,
            "speech": "No timers are currently running.",
            "cacheable": False,
        }
    now = int(datetime.now().timestamp())
    parts: list[str] = []
    for row in rows:
        remaining = max(0, int(row["fires_at"]) - now)
        parts.append(f"{row['logical_name']} ({_format_duration_human(remaining)} remaining)")
    return {
        "success": True,
        "entity_id": "",
        "new_state": None,
        "speech": "Active: " + ", ".join(parts) + ".",
        "cacheable": False,
    }


async def _list_alarms(*, area_id: str | None = None, timezone: str | None = None) -> dict:
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
            "cacheable": False,
        }

    rows = await scheduler.list(area=area_id, kinds={"alarm"})
    if not rows:
        return {
            "success": True,
            "entity_id": "",
            "new_state": None,
            "speech": "No internal alarms are currently scheduled.",
            "cacheable": False,
        }

    alarm_rows: list[dict[str, Any]] = []
    lines: list[str] = []
    for row in rows:
        fires_at = int(row.get("fires_at") or 0)
        local_time = _format_alarm_time_local(fires_at, timezone=timezone)
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except Exception:
            payload = {}

        alarm_rows.append(
            {
                "id": row.get("id"),
                "logical_name": row.get("logical_name") or "alarm",
                "fires_at": fires_at,
                "local_time": local_time,
                "state": row.get("state") or "pending",
                "source": "internal",
                **({"recurrence": payload.get("recurrence")} if isinstance(payload.get("recurrence"), dict) else {}),
            }
        )
        lines.append(f"{row.get('logical_name') or 'alarm'} at {local_time} (id {row.get('id')})")

    return {
        "success": True,
        "entity_id": "",
        "new_state": None,
        "speech": "Internal alarms: " + "; ".join(lines) + ".",
        "cacheable": False,
        "metadata": {"alarms": alarm_rows},
    }


async def _set_alarm(
    action: dict,
    *,
    device_id: str | None,
    area_id: str | None,
    language: str | None,
    timezone: str | None,
) -> dict:
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }

    params = action.get("parameters") or {}
    now_ts = int(time.time())
    target_epoch, error = _parse_alarm_target_epoch(params, now_ts=now_ts, timezone=timezone)
    if target_epoch is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": error or "Could not parse alarm target time.",
        }

    recurrence_payload, recurrence_error = _build_recurring_alarm_payload(
        params,
        target_epoch=int(target_epoch),
        timezone=timezone,
    )
    if recurrence_error:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": recurrence_error,
        }

    entity_query = (action.get("entity") or "").strip()
    logical_name = entity_query or str(params.get("label", "")).strip() or "alarm"
    duration_seconds = max(0, int(target_epoch) - now_ts)
    raw_briefing = params.get("briefing", False)
    briefing = (
        raw_briefing
        if isinstance(raw_briefing, bool)
        else str(raw_briefing).strip().lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
    )
    timer_id = await scheduler.schedule(
        logical_name=logical_name,
        kind="alarm",
        duration_seconds=duration_seconds,
        origin_device_id=device_id,
        origin_area=area_id,
        briefing=briefing,
        payload={
            "alarm_label": logical_name,
            "briefing": briefing,
            "language": language,
            "scheduled_for_epoch": int(target_epoch),
            "timezone": timezone,
            **({"recurrence": recurrence_payload} if recurrence_payload is not None else {}),
        },
    )
    local_time = _format_alarm_time_local(target_epoch, timezone=timezone)
    return {
        "success": True,
        "entity_id": None,
        "new_state": "scheduled",
        "speech": f"Scheduled alarm '{logical_name}' for {local_time}.",
        "metadata": {
            "scheduler_alarm_id": timer_id,
            "fires_at": int(target_epoch),
            "source": "internal",
            **({"recurrence": recurrence_payload} if recurrence_payload is not None else {}),
        },
    }


async def _cancel_alarm(action: dict, *, area_id: str | None, timezone: str | None) -> dict:
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }

    selectors, error = _extract_cancel_alarm_selectors(action)
    if error:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": error,
        }

    raw_id = selectors.get("id", "")
    pending = await scheduler.list(kinds={"alarm"})

    if raw_id:
        by_id = [row for row in pending if str(row.get("id")) == raw_id]
        if not by_id:
            return {
                "success": False,
                "entity_id": None,
                "new_state": None,
                "speech": f"No pending internal alarm found for id {raw_id}.",
                "metadata": {"status": "not_found", "source": "internal"},
            }
        await scheduler.cancel(id_=raw_id)
        logical_name = by_id[0].get("logical_name") or "alarm"
        return {
            "success": True,
            "entity_id": None,
            "new_state": "cancelled",
            "speech": f"Cancelled alarm '{logical_name}' ({raw_id}).",
            "metadata": {"status": "cancelled", "id": raw_id, "source": "internal"},
        }

    scope = [row for row in pending if (area_id is None or row.get("origin_area") == area_id)]
    target_datetime = selectors.get("datetime", "")
    target_time = selectors.get("time", "")
    target_date = selectors.get("date", "")
    target_name = selectors.get("name", "")
    matches: list[dict[str, Any]] = []

    if target_datetime:
        matches = _filter_alarm_rows_by_schedule(scope, target_datetime=target_datetime, timezone=timezone)
        matches.sort(key=lambda r: (int(r.get("fires_at") or 0), str(r.get("id") or "")))
        if len(matches) == 1:
            match = matches[0]
            await scheduler.cancel(id_=str(match.get("id")))
            return {
                "success": True,
                "entity_id": None,
                "new_state": "cancelled",
                "speech": f"Cancelled alarm '{match.get('logical_name') or 'alarm'}'.",
                "metadata": {"status": "cancelled", "id": match.get("id"), "source": "internal"},
            }
        if len(matches) > 1:
            candidates = [
                {
                    "id": row.get("id"),
                    "logical_name": row.get("logical_name") or "alarm",
                    "fires_at": int(row.get("fires_at") or 0),
                    "local_time": _format_alarm_time_local(int(row.get("fires_at") or 0), timezone=timezone),
                }
                for row in matches
            ]
            choices = "; ".join(f"{c['logical_name']} at {c['local_time']} (id {c['id']})" for c in candidates)
            return {
                "success": False,
                "entity_id": None,
                "new_state": None,
                "speech": (
                    f"Multiple alarms are scheduled for {target_datetime}: {choices}. Please specify the alarm id."
                ),
                "metadata": {"status": "ambiguous", "candidates": candidates, "source": "internal"},
            }

    if target_time:
        matches = _filter_alarm_rows_by_schedule(
            scope,
            target_time=target_time,
            target_date=target_date,
            timezone=timezone,
        )
        matches.sort(key=lambda r: (int(r.get("fires_at") or 0), str(r.get("id") or "")))
        if len(matches) == 1:
            match = matches[0]
            await scheduler.cancel(id_=str(match.get("id")))
            return {
                "success": True,
                "entity_id": None,
                "new_state": "cancelled",
                "speech": f"Cancelled alarm '{match.get('logical_name') or 'alarm'}'.",
                "metadata": {"status": "cancelled", "id": match.get("id"), "source": "internal"},
            }
        if len(matches) > 1:
            candidates = [
                {
                    "id": row.get("id"),
                    "logical_name": row.get("logical_name") or "alarm",
                    "fires_at": int(row.get("fires_at") or 0),
                    "local_time": _format_alarm_time_local(int(row.get("fires_at") or 0), timezone=timezone),
                }
                for row in matches
            ]
            request_label = target_time if not target_date else f"{target_date} {target_time}"
            choices = "; ".join(f"{c['logical_name']} at {c['local_time']} (id {c['id']})" for c in candidates)
            return {
                "success": False,
                "entity_id": None,
                "new_state": None,
                "speech": (
                    f"Multiple alarms match {request_label}: {choices}. "
                    "Please specify the alarm id or exact scheduled datetime."
                ),
                "metadata": {"status": "ambiguous", "candidates": candidates, "source": "internal"},
            }

    if target_name:
        normalized_target = _normalize_alarm_name(target_name)
        matches = [
            row for row in scope if _normalize_alarm_name(str(row.get("logical_name") or "")) == normalized_target
        ]
        matches.sort(key=lambda r: (int(r.get("fires_at") or 0), str(r.get("id") or "")))
    else:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Please provide an alarm id, scheduled time, or alarm name to cancel.",
        }

    if not matches:
        if target_datetime and target_name:
            message = f"No pending internal alarm scheduled for {target_datetime} or named '{target_name}' was found."
        elif target_datetime:
            message = f"No pending internal alarm scheduled for {target_datetime} was found."
        elif target_time and target_name:
            request_label = target_time if not target_date else f"{target_date} {target_time}"
            message = f"No pending internal alarm matching {request_label} or named '{target_name}' was found."
        elif target_time:
            request_label = target_time if not target_date else f"{target_date} {target_time}"
            message = f"No pending internal alarm matching {request_label} was found."
        else:
            message = f"No pending internal alarm named '{target_name}' was found."
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": message,
            "metadata": {"status": "not_found", "source": "internal"},
        }

    if len(matches) > 1:
        candidates = [
            {
                "id": row.get("id"),
                "logical_name": row.get("logical_name") or "alarm",
                "fires_at": int(row.get("fires_at") or 0),
                "local_time": _format_alarm_time_local(int(row.get("fires_at") or 0), timezone=timezone),
            }
            for row in matches
        ]
        choices = "; ".join(f"{c['logical_name']} at {c['local_time']} (id {c['id']})" for c in candidates)
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": (
                f"Multiple alarms match '{target_name}': {choices}. "
                "Please specify the alarm id or exact scheduled time."
            ),
            "metadata": {"status": "ambiguous", "candidates": candidates, "source": "internal"},
        }

    match = matches[0]
    await scheduler.cancel(id_=str(match.get("id")))
    return {
        "success": True,
        "entity_id": None,
        "new_state": "cancelled",
        "speech": f"Cancelled alarm '{match.get('logical_name') or 'alarm'}'.",
        "metadata": {"status": "cancelled", "id": match.get("id"), "source": "internal"},
    }


# ---------------------------------------------------------------------------
# Calendar action handlers
# ---------------------------------------------------------------------------


async def _create_reminder(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    verbatim_terms: list[str] | None = None,
) -> dict:
    entity_query = action.get("entity", "")
    params = action.get("parameters") or {}
    summary = str(params.get("summary", ""))
    start_time = str(params.get("start_date_time", ""))
    end_time = str(params.get("end_date_time", ""))
    description = str(params.get("description", ""))

    if not summary:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Summary is required for create_reminder.",
        }
    if not start_time:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "start_date_time is required for create_reminder.",
        }

    resolution = {
        "entity_id": None,
        "friendly_name": entity_query,
        "speech": None,
        "metadata": {"query": entity_query, "match_count": 0, "resolution_path": "not_attempted"},
    }
    try:
        if entity_index or entity_matcher:
            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    agent_id,
                    allowed_domains=_CALENDAR_DOMAINS,
                    verbatim_terms=verbatim_terms,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    if not entity_id:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution["speech"] or f"Could not find a calendar entity matching '{entity_query}'.",
        }

    service_data: dict[str, str] = {"summary": summary, "start_date_time": start_time}
    service_data["end_date_time"] = end_time or start_time
    if description:
        service_data["description"] = description

    try:
        await ha_client.call_service("calendar", "create_event", entity_id, service_data)
    except Exception as exc:
        logger.error("Failed to create calendar event on %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to create reminder: {exc}",
        }

    return {
        "success": True,
        "entity_id": entity_id,
        "new_state": None,
        "speech": f'Created reminder "{summary}" at {start_time} on {friendly_name}.',
    }


_ALARM_LIKE_TERMS: frozenset[str] = frozenset({"alarm", "wecker", "wake"})


def _parse_rrule_to_recurrence(rrule: str) -> dict[str, Any] | None:
    """Parse a simple RRULE string into a recurrence dict for _set_alarm."""
    parts: dict[str, str] = {}
    for part in rrule.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            parts[key.strip().upper()] = value.strip().upper()
    freq_map = {"DAILY": "daily", "WEEKLY": "weekly"}
    freq = freq_map.get(parts.get("FREQ", ""))
    if not freq:
        return None
    result: dict[str, Any] = {"freq": freq}
    if "INTERVAL" in parts:
        with contextlib.suppress(ValueError):
            result["interval"] = int(parts["INTERVAL"])
    if "BYDAY" in parts:
        result["byweekday"] = [d.strip().upper() for d in parts["BYDAY"].split(",")]
    return result


async def _create_recurring_reminder(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    device_id: str | None = None,
    area_id: str | None = None,
    language: str | None = None,
    timezone: str | None = None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    entity_query = action.get("entity", "")
    params = action.get("parameters") or {}
    summary = str(params.get("summary", ""))
    start_time = str(params.get("start_date_time", ""))
    end_time = str(params.get("end_date_time", ""))
    rrule = str(params.get("rrule", ""))

    if not summary:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Summary is required for create_recurring_reminder.",
        }
    if not start_time:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "start_date_time is required for create_recurring_reminder.",
        }
    if not rrule:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "rrule is required for create_recurring_reminder (e.g. 'FREQ=DAILY', 'FREQ=WEEKLY;BYDAY=MO,WE,FR').",
        }

    # Validate RRULE frequency up-front so unsupported values fail before
    # any entity lookup or side-effect (calendar create_event / scheduler).
    recurrence = _parse_rrule_to_recurrence(rrule)
    if recurrence is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Unsupported RRULE frequency. Only DAILY and WEEKLY are supported.",
        }

    resolution = {
        "entity_id": None,
        "friendly_name": entity_query,
        "speech": None,
        "metadata": {"query": entity_query, "match_count": 0, "resolution_path": "not_attempted"},
    }
    try:
        if entity_index or entity_matcher:
            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    agent_id,
                    allowed_domains=_CALENDAR_DOMAINS,
                    verbatim_terms=verbatim_terms,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]

    if not entity_id:
        # Alarm-like entities (e.g. "Wecker", "alarm") with valid RRULE are
        # rerouted to the internal scheduler alarm flow instead of HA calendar.
        is_alarm_like = any(term in entity_query.lower() for term in _ALARM_LIKE_TERMS)
        if is_alarm_like:
            alarm_action = {
                "action": "set_datetime",
                "entity": entity_query,
                "parameters": {
                    "datetime": start_time,
                    "recurrence": recurrence,
                    "label": summary,
                },
            }
            return await _set_alarm(
                alarm_action,
                device_id=device_id,
                area_id=area_id,
                language=language,
                timezone=timezone,
            )
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution["speech"] or f"Could not find a calendar entity matching '{entity_query}'.",
        }

    service_data: dict[str, str] = {
        "summary": summary,
        "start_date_time": start_time,
        "rrule": rrule,
    }
    service_data["end_date_time"] = end_time or start_time

    try:
        await ha_client.call_service("calendar", "create_event", entity_id, service_data)
    except Exception as exc:
        logger.error("Failed to create recurring event on %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to create recurring reminder: {exc}",
        }

    freq_map = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly", "YEARLY": "yearly"}
    freq = "recurring"
    for key, val in freq_map.items():
        if key in rrule.upper():
            freq = val
            break

    return {
        "success": True,
        "entity_id": entity_id,
        "new_state": None,
        "speech": f'Created {freq} reminder "{summary}" at {start_time} on {friendly_name}.',
    }


# ---------------------------------------------------------------------------
# Scheduler-routed action handlers
# ---------------------------------------------------------------------------


_DEFAULT_SNOOZE_DURATION = "00:05:00"


async def _start_timer(
    action: dict,
    *,
    device_id: str | None,
    area_id: str | None,
    language: str | None,
) -> dict:
    entity_query = (action.get("entity") or "").strip()
    params = action.get("parameters") or {}
    duration = str(params.get("duration", ""))
    seconds = _parse_duration_seconds(duration)
    if not duration or seconds is None or seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Duration is required for start_timer.",
        }
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    logical_name = entity_query or f"{seconds // 60}-minute timer"
    timer_id = await scheduler.schedule(
        logical_name=logical_name,
        kind="plain",
        duration_seconds=seconds,
        origin_device_id=device_id,
        origin_area=area_id,
        payload={"duration": duration, "language": language},
    )
    human = _format_duration_human(seconds)
    return {
        "success": True,
        "entity_id": None,
        "new_state": "active",
        "speech": f"Started {logical_name} for {human}.",
        "metadata": {"scheduler_timer_id": timer_id},
    }


async def _cancel_timer(
    action: dict,
    *,
    area_id: str | None,
) -> dict:
    entity_query = (action.get("entity") or "").strip()
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    if not entity_query:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Please specify which timer to cancel.",
        }
    count = await scheduler.cancel(logical_name=entity_query, area=area_id)
    if count == 0:
        # Normalized fallback: try casefold+separator-strip matching
        norm_query = _normalize_timer_name(entity_query)
        all_pending = await scheduler.list(area=area_id)
        matched = [r for r in all_pending if _normalize_timer_name(r["logical_name"]) == norm_query]
        for row in matched:
            await scheduler.cancel(id_=row["id"])
        count = len(matched)
    if count == 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"No timer named '{entity_query}' is running.",
        }
    return {
        "success": True,
        "entity_id": None,
        "new_state": "idle",
        "speech": f"Cancelled {entity_query}.",
    }


async def _snooze_timer(
    action: dict,
    *,
    device_id: str | None,
    area_id: str | None,
    language: str | None,
) -> dict:
    entity_query = (action.get("entity") or "").strip()
    params = action.get("parameters") or {}
    snooze_duration = str(params.get("duration", _DEFAULT_SNOOZE_DURATION))
    seconds = _parse_duration_seconds(snooze_duration) or 0
    if seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Invalid snooze duration.",
        }
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    if entity_query:
        await scheduler.cancel(logical_name=entity_query, area=area_id)
    logical_name = entity_query or "snoozed timer"
    await scheduler.schedule(
        logical_name=logical_name,
        kind="snooze",
        duration_seconds=seconds,
        origin_device_id=device_id,
        origin_area=area_id,
        payload={"snooze_seconds": seconds, "language": language},
    )
    human = _format_duration_human(seconds)
    return {
        "success": True,
        "entity_id": None,
        "new_state": "active",
        "speech": f"Snoozed {logical_name} for {human}.",
    }


async def _extend_timer(
    action: dict,
    *,
    device_id: str | None,
    area_id: str | None,
    language: str | None,
) -> dict:
    """Extend an active scheduler timer by a delta duration."""
    generic_entities = {
        "timer",
        "current timer",
        "aktueller timer",
        "den timer",
        "the timer",
        "my timer",
        "meinen timer",
    }
    entity_query = (action.get("entity") or "").strip()
    params = action.get("parameters") or {}
    duration = str(params.get("duration", ""))
    delta_seconds = _parse_duration_seconds(duration)
    if not duration or delta_seconds is None or delta_seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Duration is required to extend a timer.",
        }

    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }

    is_generic = not entity_query or entity_query.lower() in generic_entities
    target_row: dict | None = None

    if is_generic:
        pending = await scheduler.list(area=area_id)
        if not pending:
            return {
                "success": False,
                "entity_id": None,
                "new_state": None,
                "speech": "No active timer found to extend.",
            }
        if len(pending) > 1:
            names = ", ".join(r["logical_name"] for r in pending)
            return {
                "success": False,
                "entity_id": None,
                "new_state": None,
                "speech": f"Multiple timers are running: {names}. Please specify which one to extend.",
            }
        target_row = pending[0]
    else:
        rows = await scheduler.list(logical_name=entity_query, area=area_id)
        if not rows:
            norm_query = _normalize_timer_name(entity_query)
            all_pending = await scheduler.list(area=area_id)
            rows = [r for r in all_pending if _normalize_timer_name(r["logical_name"]) == norm_query]
        if not rows:
            return {
                "success": False,
                "entity_id": None,
                "new_state": None,
                "speech": f"No timer named '{entity_query}' is running.",
            }
        if len(rows) > 1:
            names = ", ".join(r["logical_name"] for r in rows)
            return {
                "success": False,
                "entity_id": None,
                "new_state": None,
                "speech": f"Multiple matching timers: {names}. Please be more specific.",
            }
        target_row = rows[0]

    now = int(time.time())
    current_remaining = max(0, target_row["fires_at"] - now)
    new_duration_seconds = current_remaining + delta_seconds
    logical_name = target_row["logical_name"]
    kind = target_row.get("kind", "plain")
    old_payload = json.loads(target_row.get("payload_json") or "{}")
    origin_device_id = target_row.get("origin_device_id") or device_id
    origin_area = target_row.get("origin_area") or area_id

    await scheduler.cancel(id_=target_row["id"])
    await scheduler.schedule(
        logical_name=logical_name,
        kind=kind,
        duration_seconds=new_duration_seconds,
        origin_device_id=origin_device_id,
        origin_area=origin_area,
        payload={**old_payload, "language": language or old_payload.get("language")},
    )
    human = _format_duration_human(new_duration_seconds)
    return {
        "success": True,
        "entity_id": None,
        "new_state": "active",
        "speech": f"Extended {logical_name}. New time remaining: {human}.",
    }


async def _start_timer_with_notification(
    action: dict,
    *,
    device_id: str | None,
    area_id: str | None,
    language: str | None,
) -> dict:
    entity_query = (action.get("entity") or "").strip()
    params = action.get("parameters") or {}
    duration = str(params.get("duration", ""))
    notification_message = str(params.get("notification_message", "Timer finished!"))
    seconds = _parse_duration_seconds(duration)
    if not duration or seconds is None or seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Duration is required for start_timer_with_notification.",
        }
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    logical_name = entity_query or f"{seconds // 60}-minute timer"
    await scheduler.schedule(
        logical_name=logical_name,
        kind="notification",
        duration_seconds=seconds,
        origin_device_id=device_id,
        origin_area=area_id,
        payload={"notification_message": notification_message, "duration": duration, "language": language},
    )
    human = _format_duration_human(seconds)
    return {
        "success": True,
        "entity_id": None,
        "new_state": "active",
        "speech": f'Started timer for {human} with notification: "{notification_message}".',
    }


async def _delayed_action(
    action: dict,
    *,
    device_id: str | None,
    area_id: str | None,
    language: str | None,
) -> dict:
    entity_query = (action.get("entity") or "delay timer").strip() or "delay timer"
    params = action.get("parameters") or {}
    delay_duration = str(params.get("delay_duration", ""))
    target_entity = str(params.get("target_entity", ""))
    target_action = str(params.get("target_action", ""))

    if not delay_duration:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "delay_duration is required for delayed_action.",
        }
    if not target_entity:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "target_entity is required for delayed_action.",
        }
    if not target_action or "/" not in target_action:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "target_action is required in 'domain/service' format (e.g. 'light/turn_off').",
        }
    seconds = _parse_duration_seconds(delay_duration)
    if seconds is None or seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Invalid delay_duration.",
        }
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    await scheduler.schedule(
        logical_name=entity_query,
        kind="delayed_action",
        duration_seconds=seconds,
        origin_device_id=device_id,
        origin_area=area_id,
        payload={"target_entity": target_entity, "target_action": target_action, "language": language},
    )
    human = _format_duration_human(seconds)
    return {
        "success": True,
        "entity_id": None,
        "new_state": "active",
        "speech": f"Scheduled {target_action.replace('/', ' ')} on {target_entity} in {human}.",
    }


async def _sleep_timer(
    action: dict,
    *,
    device_id: str | None,
    area_id: str | None,
    language: str | None,
) -> dict:
    entity_query = (action.get("entity") or "sleep timer").strip() or "sleep timer"
    params = action.get("parameters") or {}
    duration = str(params.get("duration", ""))
    media_player_entity = str(params.get("media_player", ""))
    seconds = _parse_duration_seconds(duration)
    if not duration or seconds is None or seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Duration is required for sleep_timer.",
        }
    if not media_player_entity:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "media_player entity_id is required for sleep_timer.",
        }
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    await scheduler.schedule(
        logical_name=entity_query,
        kind="sleep",
        duration_seconds=seconds,
        origin_device_id=device_id,
        origin_area=area_id,
        payload={"media_player": media_player_entity, "duration": duration, "language": language},
    )
    human = _format_duration_human(seconds)
    return {
        "success": True,
        "entity_id": None,
        "new_state": "active",
        "speech": (f"Sleep timer set for {human}. Media on {media_player_entity} will stop when the timer ends."),
    }


async def _pause_or_resume_or_finish(
    action: dict,
    *,
    area_id: str | None,
) -> dict:
    """``pause_timer``/``resume_timer``/``finish_timer`` against the scheduler.

    The scheduler does not yet model true pause/resume; the simplest
    correct behaviour is: ``pause`` cancels the pending timer (so it
    will not fire), ``resume`` is rejected with a clear message
    (the user must restart), ``finish`` cancels and reports done.
    """
    action_name = action.get("action", "")
    entity_query = (action.get("entity") or "").strip()
    scheduler = _get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    if action_name == "resume_timer":
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Resume is not supported for AgentHub timers; please start a new timer.",
        }
    if not entity_query:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"Please specify which timer to {action_name.replace('_timer', '')}.",
        }
    count = await scheduler.cancel(logical_name=entity_query, area=area_id)
    if count == 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"No timer named '{entity_query}' is running.",
        }
    if action_name == "finish_timer":
        return {
            "success": True,
            "entity_id": None,
            "new_state": "idle",
            "speech": f"Finished {entity_query}.",
        }
    return {
        "success": True,
        "entity_id": None,
        "new_state": "paused",
        "speech": f"Paused {entity_query}.",
    }


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------


async def execute_timer_action(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None = None,
    device_id: str | None = None,
    area_id: str | None = None,
    language: str | None = None,
    timezone: str | None = None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Dispatch a parsed timer action.

    All non-plain timer-shaped flows route to ``TimerScheduler``; HA
    ``timer.*`` services are no longer used. ``set_datetime``,
    ``list_alarms``, and the calendar reminders still go to HA.
    """
    action_name = action.get("action", "").lower()
    entity_query = action.get("entity", "")

    if action_name in ("query_timer", "list_timers", "list_alarms"):
        return await _handle_read_action(
            action_name,
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            area_id=area_id,
            timezone=timezone,
        )

    if action_name == "start_timer":
        return await _start_timer(action, device_id=device_id, area_id=area_id, language=language)
    if action_name == "cancel_timer":
        return await _cancel_timer(action, area_id=area_id)
    if action_name == "extend_timer":
        return await _extend_timer(action, device_id=device_id, area_id=area_id, language=language)
    if action_name in ("pause_timer", "resume_timer", "finish_timer"):
        return await _pause_or_resume_or_finish(action, area_id=area_id)
    if action_name == "snooze_timer":
        return await _snooze_timer(action, device_id=device_id, area_id=area_id, language=language)
    if action_name == "start_timer_with_notification":
        return await _start_timer_with_notification(action, device_id=device_id, area_id=area_id, language=language)
    if action_name == "delayed_action":
        return await _delayed_action(action, device_id=device_id, area_id=area_id, language=language)
    if action_name == "sleep_timer":
        return await _sleep_timer(action, device_id=device_id, area_id=area_id, language=language)
    if action_name == "create_reminder":
        return await _create_reminder(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "create_recurring_reminder":
        return await _create_recurring_reminder(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            device_id=device_id,
            area_id=area_id,
            language=language,
            timezone=timezone,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "set_datetime":
        return await _set_alarm(
            action,
            device_id=device_id,
            area_id=area_id,
            language=language,
            timezone=timezone,
        )
    if action_name == "cancel_alarm":
        return await _cancel_alarm(action, area_id=area_id, timezone=timezone)

    return {
        "success": False,
        "entity_id": None,
        "new_state": None,
        "speech": f"Unknown timer action: {action_name}",
    }
