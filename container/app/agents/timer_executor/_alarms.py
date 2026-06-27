"""Internal scheduler-backed alarm create/cancel logic."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from . import _helpers

_ALARM_WEEKDAY_CODES: frozenset[str] = frozenset({"MO", "TU", "WE", "TH", "FR", "SA", "SU"})


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
    tz = _helpers._get_timezone_info(timezone)
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

    tz = _helpers._get_timezone_info(timezone)

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

    tz = _helpers._get_timezone_info(timezone)
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


async def _set_alarm(
    action: dict,
    *,
    device_id: str | None,
    area_id: str | None,
    language: str | None,
    timezone: str | None,
) -> dict:
    action_name = action.get("action", "").lower()
    scheduler = _helpers._get_scheduler()
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
    raw_calendars = params.get("calendar_entity_ids")
    calendar_entity_ids = None
    if isinstance(raw_calendars, list):
        calendar_entity_ids = [str(c).strip() for c in raw_calendars if str(c).strip()]

    payload: dict[str, Any] = {
        "alarm_label": logical_name,
        "briefing": briefing,
        "language": language,
        "scheduled_for_epoch": int(target_epoch),
        "timezone": timezone,
    }
    if recurrence_payload is not None:
        payload["recurrence"] = recurrence_payload
    if calendar_entity_ids:
        payload["calendar_entity_ids"] = calendar_entity_ids

    timer_id = await scheduler.schedule(
        logical_name=logical_name,
        kind="alarm",
        duration_seconds=duration_seconds,
        origin_device_id=device_id,
        origin_area=area_id,
        briefing=briefing,
        payload=payload,
    )
    local_time = _helpers._format_alarm_time_local(target_epoch, timezone=timezone)
    return {
        "success": True,
        "action": action_name,
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
    action_name = action.get("action", "").lower()
    scheduler = _helpers._get_scheduler()
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
            "action": action_name,
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
                "action": action_name,
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
                    "local_time": _helpers._format_alarm_time_local(int(row.get("fires_at") or 0), timezone=timezone),
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
                "action": action_name,
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
                    "local_time": _helpers._format_alarm_time_local(int(row.get("fires_at") or 0), timezone=timezone),
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
        normalized_target = _helpers._normalize_alarm_name(target_name)
        matches = [
            row
            for row in scope
            if _helpers._normalize_alarm_name(str(row.get("logical_name") or "")) == normalized_target
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
                "local_time": _helpers._format_alarm_time_local(int(row.get("fires_at") or 0), timezone=timezone),
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
        "action": action_name,
        "entity_id": None,
        "new_state": "cancelled",
        "speech": f"Cancelled alarm '{match.get('logical_name') or 'alarm'}'.",
        "metadata": {"status": "cancelled", "id": match.get("id"), "source": "internal"},
    }
