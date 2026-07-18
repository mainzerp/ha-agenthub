"""Scheduler-routed timer set/cancel/snooze/extend handlers."""

from __future__ import annotations

import json
import time
from typing import Any

from app.agents.action_executor import resolve_and_validate_entity

from . import _helpers

_DEFAULT_SNOOZE_DURATION = "00:05:00"

# H-2: write-capable domains a deferred action may target (union of the
# write-capable ``@agent`` domains). Deferred writes are fail-closed: an
# LLM-supplied ``target_action``/entity outside this list is rejected at
# schedule time and re-checked at fire time (``background_actions``).
_DEFERRED_ALLOWED_DOMAINS: frozenset[str] = frozenset(
    {
        "light",
        "switch",
        "climate",
        "cover",
        "vacuum",
        "scene",
        "media_player",
        "automation",
        "script",
        "lock",
        "alarm_control_panel",
        "input_boolean",
    }
)

# Sleep timers only ever stop media players.
_SLEEP_TIMER_DOMAINS: frozenset[str] = frozenset({"media_player"})


def _validate_deferred_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to a deferred-write-allowed domain."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _DEFERRED_ALLOWED_DOMAINS


def _validate_media_player_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to the media_player domain."""
    return entity_id.split(".")[0] == "media_player" if "." in entity_id else False


async def _start_timer(
    action: dict,
    *,
    device_id: str | None,
    area_id: str | None,
    language: str | None,
) -> dict:
    action_name = action.get("action", "").lower()
    entity_query = (action.get("entity") or "").strip()
    params = action.get("parameters") or {}
    duration = str(params.get("duration", ""))
    seconds = _helpers._parse_duration_seconds(duration)
    if not duration or seconds is None or seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Duration is required for start_timer.",
        }
    scheduler = _helpers._get_scheduler()
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
    human = _helpers._format_duration_human(seconds)
    return {
        "success": True,
        "action": action_name,
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
    action_name = action.get("action", "").lower()
    entity_query = (action.get("entity") or "").strip()
    scheduler = _helpers._get_scheduler()
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
        norm_query = _helpers._normalize_timer_name(entity_query)
        all_pending = await scheduler.list(area=area_id)
        matched = [r for r in all_pending if _helpers._normalize_timer_name(r["logical_name"]) == norm_query]
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
        "action": action_name,
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
    action_name = action.get("action", "").lower()
    entity_query = (action.get("entity") or "").strip()
    params = action.get("parameters") or {}
    snooze_duration = str(params.get("duration", _DEFAULT_SNOOZE_DURATION))
    seconds = _helpers._parse_duration_seconds(snooze_duration) or 0
    if seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Invalid snooze duration.",
        }
    scheduler = _helpers._get_scheduler()
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
    human = _helpers._format_duration_human(seconds)
    return {
        "success": True,
        "action": action_name,
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
    action_name = action.get("action", "").lower()
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
    delta_seconds = _helpers._parse_duration_seconds(duration)
    if not duration or delta_seconds is None or delta_seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Duration is required to extend a timer.",
        }

    scheduler = _helpers._get_scheduler()
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
            norm_query = _helpers._normalize_timer_name(entity_query)
            all_pending = await scheduler.list(area=area_id)
            rows = [r for r in all_pending if _helpers._normalize_timer_name(r["logical_name"]) == norm_query]
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
    human = _helpers._format_duration_human(new_duration_seconds)
    return {
        "success": True,
        "action": action_name,
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
    action_name = action.get("action", "").lower()
    entity_query = (action.get("entity") or "").strip()
    params = action.get("parameters") or {}
    duration = str(params.get("duration", ""))
    notification_message = str(params.get("notification_message", "Timer finished!"))
    seconds = _helpers._parse_duration_seconds(duration)
    if not duration or seconds is None or seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Duration is required for start_timer_with_notification.",
        }
    scheduler = _helpers._get_scheduler()
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
    human = _helpers._format_duration_human(seconds)
    return {
        "success": True,
        "action": action_name,
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
    ha_client: Any = None,
    entity_index: Any = None,
    entity_matcher: Any = None,
    agent_id: str | None = None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    action_name = action.get("action", "").lower()
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
    seconds = _helpers._parse_duration_seconds(delay_duration)
    if seconds is None or seconds <= 0:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Invalid delay_duration.",
        }
    # H-2 schedule-time validation (fail-closed): the service domain must be
    # write-capable and the target must resolve to an entity visible to the
    # scheduling agent (mirrors the foreground executor pattern).
    action_domain = target_action.split("/", 1)[0]
    if action_domain not in _DEFERRED_ALLOWED_DOMAINS:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"Delayed actions are not supported for the '{action_domain}' domain.",
        }
    scheduler = _helpers._get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    resolved = await resolve_and_validate_entity(
        target_entity,
        entity_index,
        entity_matcher,
        agent_id,
        _DEFERRED_ALLOWED_DOMAINS,
        _validate_deferred_domain,
        preferred_area_id=area_id,
        verbatim_terms=verbatim_terms,
    )
    resolved_entity = resolved.get("entity_id")
    if not resolved_entity:
        not_found = resolved.get("not_found_result") or {}
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": not_found.get("speech") or f"Could not find an entity matching '{target_entity}'.",
            "metadata": not_found.get("metadata"),
        }
    await scheduler.schedule(
        logical_name=entity_query,
        kind="delayed_action",
        duration_seconds=seconds,
        origin_device_id=device_id,
        origin_area=area_id,
        payload={
            "target_entity": resolved_entity,
            "target_action": target_action,
            "language": language,
            "agent_id": agent_id,
        },
    )
    human = _helpers._format_duration_human(seconds)
    return {
        "success": True,
        "action": action_name,
        "entity_id": None,
        "new_state": "active",
        "speech": f"Scheduled {target_action.replace('/', ' ')} on {resolved_entity} in {human}.",
    }


async def _sleep_timer(
    action: dict,
    *,
    device_id: str | None,
    area_id: str | None,
    language: str | None,
    ha_client: Any = None,
    entity_index: Any = None,
    entity_matcher: Any = None,
    agent_id: str | None = None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    action_name = action.get("action", "").lower()
    entity_query = (action.get("entity") or "sleep timer").strip() or "sleep timer"
    params = action.get("parameters") or {}
    duration = str(params.get("duration", ""))
    media_player_entity = str(params.get("media_player", ""))
    seconds = _helpers._parse_duration_seconds(duration)
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
    scheduler = _helpers._get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    # H-2 schedule-time validation (fail-closed): the media player must
    # resolve to an entity visible to the scheduling agent.
    resolved = await resolve_and_validate_entity(
        media_player_entity,
        entity_index,
        entity_matcher,
        agent_id,
        _SLEEP_TIMER_DOMAINS,
        _validate_media_player_domain,
        preferred_area_id=area_id,
        verbatim_terms=verbatim_terms,
    )
    resolved_player = resolved.get("entity_id")
    if not resolved_player:
        not_found = resolved.get("not_found_result") or {}
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": not_found.get("speech") or f"Could not find an entity matching '{media_player_entity}'.",
            "metadata": not_found.get("metadata"),
        }
    await scheduler.schedule(
        logical_name=entity_query,
        kind="sleep",
        duration_seconds=seconds,
        origin_device_id=device_id,
        origin_area=area_id,
        payload={
            "media_player": resolved_player,
            "duration": duration,
            "language": language,
            "agent_id": agent_id,
        },
    )
    human = _helpers._format_duration_human(seconds)
    return {
        "success": True,
        "action": action_name,
        "entity_id": None,
        "new_state": "active",
        "speech": (f"Sleep timer set for {human}. Media on {resolved_player} will stop when the timer ends."),
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
    scheduler = _helpers._get_scheduler()
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
            "action": action_name,
            "entity_id": None,
            "new_state": "idle",
            "speech": f"Finished {entity_query}.",
        }
    return {
        "success": True,
        "action": action_name,
        "entity_id": None,
        "new_state": "paused",
        "speech": f"Paused {entity_query}.",
    }
