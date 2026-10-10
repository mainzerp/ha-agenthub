"""Read-only timer/alarm query and list handlers."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import _helpers


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
    scheduler = _helpers._get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
            "cacheable": False,
        }
    rows = await _helpers._find_rows(
        scheduler,
        entity_query,
        area_id=area_id,
        kinds=_helpers.TIMER_KINDS,
        unnamed_kinds=_helpers.COUNTDOWN_KINDS,
        kind_word="timer",
        states=_LISTED_STATES,
    )
    if not rows:
        speech = (
            "No timer is currently running."
            if _helpers._is_unnamed(entity_query, "timer")
            else f"No timer named '{entity_query}' is currently running."
        )
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": speech,
            "cacheable": False,
        }
    now = int(datetime.now().timestamp())
    parts = [_describe_row(row, now) for row in rows]
    return {
        "success": True,
        "entity_id": None,
        "new_state": "paused" if all(r.get("state") == "paused" for r in rows) else "active",
        "speech": "; ".join(parts) + ".",
        "cacheable": False,
    }


_LISTED_STATES = frozenset({"pending", "paused"})


def _remaining_seconds(row: dict[str, Any], now: int) -> int:
    if row.get("state") == "paused":
        try:
            payload = json.loads(row.get("payload_json") or "{}")
            return max(0, int(payload.get("paused_remaining_seconds") or 0))
        except (TypeError, ValueError):
            return 0
    return max(0, int(row["fires_at"]) - now)


def _describe_row(row: dict[str, Any], now: int) -> str:
    human = _helpers._format_duration_human(_remaining_seconds(row, now))
    if row.get("state") == "paused":
        return f"{row['logical_name']} is paused with {human} remaining"
    return f"{row['logical_name']} has {human} remaining"


async def _list_scoped(
    scheduler: Any,
    *,
    area_id: str | None,
    kinds: frozenset[str],
    states: frozenset[str] | None = None,
) -> list[dict]:
    """List rows of ``kinds`` in the origin room, falling back to every room."""
    extra = {"states": set(states)} if states else {}
    if area_id is not None:
        rows = await scheduler.list(area=area_id, kinds=set(kinds), **extra)
        if rows:
            return rows
    return await scheduler.list(area=None, kinds=set(kinds), **extra)


async def _list_timers(*, area_id: str | None = None) -> dict:
    scheduler = _helpers._get_scheduler()
    if scheduler is None:
        return {
            "success": True,
            "entity_id": "",
            "new_state": None,
            "speech": "No timers are currently running.",
            "cacheable": False,
        }
    # Room-scoped first; timers set from chat or another room are listed when
    # the origin room has none. Alarms are listed by list_alarms.
    rows = await _list_scoped(scheduler, area_id=area_id, kinds=_helpers.TIMER_KINDS, states=_LISTED_STATES)
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
        human = _helpers._format_duration_human(_remaining_seconds(row, now))
        suffix = "paused, " if row.get("state") == "paused" else ""
        parts.append(f"{row['logical_name']} ({suffix}{human} remaining)")
    return {
        "success": True,
        "entity_id": "",
        "new_state": None,
        "speech": "Active: " + ", ".join(parts) + ".",
        "cacheable": False,
    }


async def _list_alarms(*, area_id: str | None = None, timezone: str | None = None) -> dict:
    scheduler = _helpers._get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
            "cacheable": False,
        }

    rows = await _list_scoped(scheduler, area_id=area_id, kinds=_helpers.ALARM_KINDS)
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
        local_time = _helpers._format_alarm_time_local(fires_at, timezone=timezone)
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
