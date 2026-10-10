"""Scheduler-routed timer set/cancel/snooze/extend handlers."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from app.agents.action_executor import resolve_and_validate_entity

from . import _helpers

_DEFAULT_SNOOZE_DURATION = "00:05:00"


@dataclass(frozen=True)
class DeferredDomainPolicy:
    """Who owns a deferred-action domain and which services may run later."""

    owner_agent_id: str
    services: frozenset[str]


# H-2: write-capable domains a deferred action may target. Each domain names
# the agent that owns it (its visibility rules apply, not the timer agent's)
# and an allow-list of argument-free services. Deferred writes are
# fail-closed: anything outside this policy is rejected at schedule time and
# re-checked at fire time (``background_actions``). Unlocking and disarming
# are deliberately not schedulable.
DEFERRED_ACTION_POLICY: dict[str, DeferredDomainPolicy] = {
    "light": DeferredDomainPolicy("light-agent", frozenset({"turn_on", "turn_off", "toggle"})),
    "switch": DeferredDomainPolicy("light-agent", frozenset({"turn_on", "turn_off", "toggle"})),
    "climate": DeferredDomainPolicy("climate-agent", frozenset({"turn_on", "turn_off", "toggle"})),
    "cover": DeferredDomainPolicy("cover-agent", frozenset({"open_cover", "close_cover", "stop_cover", "toggle"})),
    "vacuum": DeferredDomainPolicy("vacuum-agent", frozenset({"start", "pause", "stop", "return_to_base"})),
    "scene": DeferredDomainPolicy("scene-agent", frozenset({"turn_on"})),
    "media_player": DeferredDomainPolicy(
        "media-agent",
        frozenset({"turn_on", "turn_off", "toggle", "media_play", "media_pause", "media_stop", "media_play_pause"}),
    ),
    "automation": DeferredDomainPolicy("automation-agent", frozenset({"turn_on", "turn_off", "toggle", "trigger"})),
    "script": DeferredDomainPolicy("automation-agent", frozenset({"turn_on", "turn_off"})),
    "lock": DeferredDomainPolicy("security-agent", frozenset({"lock"})),
    "alarm_control_panel": DeferredDomainPolicy(
        "security-agent", frozenset({"alarm_arm_home", "alarm_arm_away", "alarm_arm_night"})
    ),
    "input_boolean": DeferredDomainPolicy("timer-agent", frozenset({"turn_on", "turn_off", "toggle"})),
}

_DEFERRED_ALLOWED_DOMAINS: frozenset[str] = frozenset(DEFERRED_ACTION_POLICY)

# Sleep timers only ever stop media players.
_SLEEP_TIMER_DOMAINS: frozenset[str] = frozenset({"media_player"})


def deferred_action_rejection(domain: str, service: str) -> str | None:
    """Return a rejection reason for ``domain/service``, or None when it is allowed."""
    policy = DEFERRED_ACTION_POLICY.get(domain)
    if policy is None:
        return f"Delayed actions are not supported for the '{domain}' domain."
    if service not in policy.services:
        return f"The '{domain}/{service}' service cannot be scheduled as a delayed action."
    return None


def deferred_owner_agent(domain: str) -> str | None:
    """Return the agent whose visibility rules govern a deferred action on ``domain``."""
    policy = DEFERRED_ACTION_POLICY.get(domain)
    return policy.owner_agent_id if policy else None


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
    logical_name = entity_query or _helpers._default_timer_label(seconds)
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


def _not_found(entity_query: str) -> dict:
    if _helpers._is_unnamed(entity_query, "timer"):
        speech = "No timer is running."
    else:
        speech = f"No timer named '{entity_query}' is running."
    return {"success": False, "entity_id": None, "new_state": None, "speech": speech}


def _ambiguous(rows: list[dict], verb: str) -> dict:
    names = ", ".join(str(r.get("logical_name") or "timer") for r in rows)
    return {
        "success": False,
        "entity_id": None,
        "new_state": None,
        "speech": f"Multiple timers are running: {names}. Please specify which one to {verb}.",
        # A clarifying question: the user's answer is re-dispatched.
        "voice_followup": True,
        "metadata": {"status": "ambiguous", "candidates": [r.get("logical_name") for r in rows]},
    }


def _needs_target(speech: str) -> dict:
    """Clarifying question for an action whose target device is missing."""
    return {
        "success": False,
        "entity_id": None,
        "new_state": None,
        "speech": speech,
        "voice_followup": True,
        "metadata": {"status": "needs_target"},
    }


async def _find_timer_rows(
    scheduler: Any,
    entity_query: str,
    *,
    area_id: str | None,
    states: frozenset[str] | None = None,
) -> list[dict]:
    return await _helpers._find_rows(
        scheduler,
        entity_query,
        area_id=area_id,
        kinds=_helpers.TIMER_KINDS,
        unnamed_kinds=_helpers.COUNTDOWN_KINDS,
        kind_word="timer",
        states=states,
    )


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
    rows = await _find_timer_rows(scheduler, entity_query, area_id=area_id, states=frozenset({"pending", "paused"}))
    if not rows:
        return _not_found(entity_query)
    # Several rows sharing one name are cancelled together (one logical timer);
    # different names need a clarification.
    if len(rows) > 1 and not _helpers._same_name(rows):
        return _ambiguous(rows, "cancel")
    for row in rows:
        await scheduler.cancel(id_=row["id"])
    return {
        "success": True,
        "action": action_name,
        "entity_id": None,
        "new_state": "idle",
        "speech": f"Cancelled {rows[0].get('logical_name') or entity_query}.",
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
        # Only a running countdown of that name is replaced. Alarms (and the
        # next occurrence of a recurring alarm series) are never touched.
        await scheduler.cancel(logical_name=entity_query, area=area_id, kinds={"plain", "notification"})
    logical_name = entity_query or "snoozed timer"
    # The snooze itself is the countdown: a plain timer that rings once
    # after ``seconds``.
    await scheduler.schedule(
        logical_name=logical_name,
        kind="plain",
        duration_seconds=seconds,
        origin_device_id=device_id,
        origin_area=area_id,
        payload={"duration": snooze_duration, "snoozed": True, "language": language},
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

    rows = await _find_timer_rows(scheduler, entity_query, area_id=area_id)
    if not rows:
        return _not_found(entity_query)
    if len(rows) > 1:
        return _ambiguous(rows, "extend")
    target_row = rows[0]

    now = int(time.time())
    current_remaining = max(0, int(target_row["fires_at"]) - now)
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
    logical_name = entity_query or _helpers._default_timer_label(seconds)
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
        return _needs_target("Which device should I control when the delay ends?")
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
    # H-2 schedule-time validation (fail-closed): the service must be on the
    # domain's allow-list and the target must resolve, within that domain, to
    # an entity visible to the agent that owns the domain (the timer agent
    # only schedules; it does not widen what the owning agent may touch).
    action_domain, action_service = target_action.split("/", 1)
    rejection = deferred_action_rejection(action_domain, action_service)
    if rejection:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": rejection,
        }
    owner_agent_id = deferred_owner_agent(action_domain) or agent_id
    scheduler = _helpers._get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    action_domains = frozenset({action_domain})
    resolved = await resolve_and_validate_entity(
        target_entity,
        entity_index,
        entity_matcher,
        owner_agent_id,
        action_domains,
        lambda entity_id: entity_id.split(".", 1)[0] == action_domain if "." in entity_id else False,
        preferred_area_id=area_id,
        direct_entity_id=action.get("entity_id"),
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
            "agent_id": owner_agent_id,
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
        return _needs_target("Which media player should the sleep timer stop?")
    scheduler = _helpers._get_scheduler()
    if scheduler is None:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Timer scheduler is unavailable.",
        }
    # H-2 schedule-time validation (fail-closed): the media player must
    # resolve to an entity visible to the agent that owns media players.
    owner_agent_id = deferred_owner_agent("media_player") or agent_id
    resolved = await resolve_and_validate_entity(
        media_player_entity,
        entity_index,
        entity_matcher,
        owner_agent_id,
        _SLEEP_TIMER_DOMAINS,
        _validate_media_player_domain,
        preferred_area_id=area_id,
        direct_entity_id=action.get("entity_id"),
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
            "agent_id": owner_agent_id,
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

    ``pause`` moves the running timer to the ``paused`` state and keeps its
    remaining time; ``resume`` restarts a paused timer with that remaining
    time; ``finish`` cancels a running or paused timer and reports it done.
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
    verb = action_name.replace("_timer", "")
    if action_name == "resume_timer":
        states = frozenset({"paused"})
    elif action_name == "pause_timer":
        states = frozenset({"pending"})
    else:
        states = frozenset({"pending", "paused"})
    rows = await _find_timer_rows(scheduler, entity_query, area_id=area_id, states=states)
    if not rows:
        if action_name == "resume_timer":
            speech = (
                "No paused timer was found."
                if _helpers._is_unnamed(entity_query, "timer")
                else f"No paused timer named '{entity_query}' was found."
            )
            return {"success": False, "entity_id": None, "new_state": None, "speech": speech}
        return _not_found(entity_query)
    if len(rows) > 1:
        return _ambiguous(rows, verb)
    row = rows[0]
    name = row.get("logical_name") or entity_query or "timer"

    if action_name == "finish_timer":
        await scheduler.cancel(id_=row["id"])
        return {
            "success": True,
            "action": action_name,
            "entity_id": None,
            "new_state": "idle",
            "speech": f"Finished {name}.",
        }
    if action_name == "pause_timer":
        remaining = await scheduler.pause(row["id"])
        if remaining is None:
            return _not_found(entity_query)
        return {
            "success": True,
            "action": action_name,
            "entity_id": None,
            "new_state": "paused",
            "speech": f"Paused {name} with {_helpers._format_duration_human(remaining)} remaining.",
        }
    remaining = await scheduler.resume(row["id"])
    if remaining is None:
        return {"success": False, "entity_id": None, "new_state": None, "speech": f"{name} is not paused."}
    return {
        "success": True,
        "action": action_name,
        "entity_id": None,
        "new_state": "active",
        "speech": f"Resumed {name} with {_helpers._format_duration_human(remaining)} remaining.",
    }
