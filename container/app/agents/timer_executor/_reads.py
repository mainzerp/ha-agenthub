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
    human = _helpers._format_duration_human(remaining)
    return {
        "success": True,
        "entity_id": None,
        "new_state": "active",
        "speech": f"{row['logical_name']} has {human} remaining.",
        "cacheable": False,
    }


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
        parts.append(f"{row['logical_name']} ({_helpers._format_duration_human(remaining)} remaining)")
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
