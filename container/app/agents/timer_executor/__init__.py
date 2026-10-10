"""Timer-specific action execution.

In 0.26.0 the HA ``timer.*`` helper-pool model was removed entirely.
All timer-shaped actions route to the AgentHub-managed
``TimerScheduler`` (``app.agents.timer_scheduler``).

This module retains:
- read-only handlers (``query_timer``, ``list_timers`` against the
    scheduler; ``list_alarms`` against internal scheduler alarms)
- ``set_datetime`` (internal scheduler-backed alarm create)
- ``cancel_alarm`` (internal scheduler-backed alarm cancel)

All HA ``timer.*`` service calls, the ``_TimerPool`` class, the
``_find_idle_timer`` allocator, the ``on_timer_finished`` WebSocket
handler, and the expired-timer tracking deque are deleted.
"""

from __future__ import annotations

from typing import Any

from ._alarms import (
    _build_recurring_alarm_payload,
    _cancel_alarm,
    _extract_cancel_alarm_selectors,
    _filter_alarm_rows_by_schedule,
    _parse_alarm_target_epoch,
    _set_alarm,
)
from ._helpers import (
    _build_timer_service_data,
    _format_alarm_time_local,
    _format_duration_human,
    _get_timezone_info,
    _normalize_alarm_name,
    _normalize_timer_name,
    _parse_duration_seconds,
    _supports_method,
)
from ._reads import _handle_read_action
from ._timers import (
    _cancel_timer,
    _delayed_action,
    _extend_timer,
    _pause_or_resume_or_finish,
    _sleep_timer,
    _snooze_timer,
    _start_timer,
    _start_timer_with_notification,
)

__all__ = [
    "ENTITY_ACTIONS",
    "ENTITY_FREE_ACTIONS",
    "_build_recurring_alarm_payload",
    "_build_timer_service_data",
    "_extract_cancel_alarm_selectors",
    "_filter_alarm_rows_by_schedule",
    "_format_alarm_time_local",
    "_format_duration_human",
    "_get_timezone_info",
    "_handle_read_action",
    "_normalize_alarm_name",
    "_normalize_timer_name",
    "_parse_alarm_target_epoch",
    "_parse_duration_seconds",
    "_supports_method",
    "execute_timer_action",
]

# Entity-candidate declaration (read by ``TimerAgent``'s ``@agent`` call, see
# ``ActionableAgent._entity_actions``). Timers and alarms are AgentHub-internal
# logical labels, so no timer action acts on a recalled entity candidate: an
# empty or kind-equal ``entity`` means "no name" (the only running timer, or a
# question when that is ambiguous). ``delayed_action`` and ``sleep_timer`` name
# their device in ``parameters`` and resolve it through
# ``resolve_and_validate_entity`` (visibility checked) at schedule time.
_READ_ACTIONS: frozenset[str] = frozenset({"query_timer", "list_timers", "list_alarms"})
_WRITE_ACTIONS: frozenset[str] = frozenset(
    {
        "start_timer",
        "cancel_timer",
        "extend_timer",
        "pause_timer",
        "resume_timer",
        "finish_timer",
        "snooze_timer",
        "start_timer_with_notification",
        "delayed_action",
        "sleep_timer",
        "set_datetime",
        "cancel_alarm",
    }
)
ENTITY_ACTIONS: frozenset[str] = frozenset()
ENTITY_FREE_ACTIONS: frozenset[str] = _READ_ACTIONS | _WRITE_ACTIONS


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
) -> dict:
    """Dispatch a parsed timer action.

    All timer and alarm operations route to the internal ``TimerScheduler``;
    HA ``timer.*`` services are no longer used.
    """
    action_name = action.get("action", "").lower()
    entity_query = action.get("entity", "")

    if action_name not in ENTITY_FREE_ACTIONS:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"Unknown timer action: {action_name}",
        }
    if action_name in _READ_ACTIONS:
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
        return await _delayed_action(
            action,
            device_id=device_id,
            area_id=area_id,
            language=language,
            ha_client=ha_client,
            entity_index=entity_index,
            entity_matcher=entity_matcher,
            agent_id=agent_id,
        )
    if action_name == "sleep_timer":
        return await _sleep_timer(
            action,
            device_id=device_id,
            area_id=area_id,
            language=language,
            ha_client=ha_client,
            entity_index=entity_index,
            entity_matcher=entity_matcher,
            agent_id=agent_id,
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
