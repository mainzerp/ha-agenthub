"""Calendar-specific action execution.

Dispatches calendar read/write actions via HA REST API.
"""

from __future__ import annotations

import logging
from typing import Any

from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.entity.visibility import entity_is_visible

logger = logging.getLogger(__name__)

_CALENDAR_DOMAINS: frozenset[str] = frozenset({"calendar"})


async def _calendar_is_visible(agent_id: str | None, entity_id: str, entity_index: Any) -> bool:
    """Fail-closed per-entity visibility check for calendar picks."""
    if not entity_id:
        return False
    if not agent_id:
        return True
    try:
        return await entity_is_visible(agent_id, entity_id, entity_index)
    except Exception:
        logger.warning("Calendar visibility check failed for %s", entity_id, exc_info=True)
        return False


async def _filter_visible_calendars(
    entries: list[Any],
    agent_id: str | None,
    entity_index: Any,
) -> list[Any]:
    visible: list[Any] = []
    for entry in entries:
        entity_id = str(getattr(entry, "entity_id", ""))
        if await _calendar_is_visible(agent_id, entity_id, entity_index):
            visible.append(entry)
    return visible


async def execute_calendar_action(
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
    default_calendar_ids: list[str] | None = None,
) -> dict:
    """Dispatch a parsed calendar action."""
    action_name = action.get("action", "").lower()

    if action_name == "list_events":
        return await _list_events(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector,
            default_calendar_ids=default_calendar_ids,
        )
    if action_name == "query_event":
        return await _query_event(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector,
            default_calendar_ids=default_calendar_ids,
        )
    if action_name == "create_event":
        return await _create_event(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector,
            verbatim_terms=verbatim_terms,
            default_calendar_ids=default_calendar_ids,
        )
    if action_name == "delete_event":
        return await _delete_event(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector,
            verbatim_terms=verbatim_terms,
            default_calendar_ids=default_calendar_ids,
        )
    if action_name == "update_event":
        return await _update_event(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector,
            verbatim_terms=verbatim_terms,
            default_calendar_ids=default_calendar_ids,
        )

    return {
        "success": False,
        "entity_id": None,
        "new_state": None,
        "speech": f"Unknown calendar action: {action_name}",
    }


async def _resolve_calendar_entity(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
    default_calendar_ids: list[str] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve target calendar entity. Returns (entity_id, friendly_name, speech_error)."""
    entity_query = action.get("entity", "")
    params = action.get("parameters") or {}
    explicit_calendar = str(params.get("calendar") or "").strip()
    if explicit_calendar:
        entity_query = explicit_calendar

    if not entity_query:
        # Try the configured default calendar first, then any visible calendar
        if default_calendar_ids:
            default_id = str(default_calendar_ids[0])
            if await _calendar_is_visible(agent_id, default_id, entity_index):
                return default_id, default_id, None
        entries = []
        if entity_index:
            if hasattr(entity_index, "list_entries_async"):
                entries = await entity_index.list_entries_async(domains=_CALENDAR_DOMAINS)
            elif hasattr(entity_index, "list_entries"):
                entries = entity_index.list_entries(domains=_CALENDAR_DOMAINS)
        visible_entries = await _filter_visible_calendars(entries, agent_id, entity_index)
        if visible_entries:
            first = visible_entries[0]
            return str(getattr(first, "entity_id", "")), str(getattr(first, "friendly_name", "")), None
        return None, None, "No calendar entity available."

    resolution = {
        "entity_id": None,
        "friendly_name": entity_query,
        "speech": None,
        "metadata": {"query": entity_query, "match_count": 0, "resolution_path": "not_attempted"},
    }
    try:
        if entity_index or entity_matcher:
            from app.analytics.tracer import _optional_span

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
        return None, None, resolution["speech"] or f"Could not find a calendar entity matching '{entity_query}'."
    return entity_id, friendly_name, None


async def _list_events(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    default_calendar_ids: list[str] | None = None,
) -> dict:
    params = action.get("parameters") or {}
    start_time = str(params.get("start_date_time", ""))
    end_time = str(params.get("end_date_time", ""))

    if not start_time or not end_time:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "start_date_time and end_date_time are required for list_events.",
        }

    entity_id, friendly_name, error = await _resolve_calendar_entity(
        action,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector,
        default_calendar_ids=default_calendar_ids,
    )
    if error:
        return {"success": False, "entity_id": None, "new_state": None, "speech": error}

    try:
        events = await ha_client.get_calendar_events(entity_id, start_time, end_time)
    except Exception as exc:
        logger.error("Failed to list events for %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to list events: {exc}",
        }

    if not events:
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"No events found on {friendly_name}.",
            "metadata": {"events": []},
        }

    lines = []
    for ev in events:
        summary = ev.get("summary", "Event")
        start = ev.get("start", "")
        lines.append(f"{summary} at {start}")

    return {
        "success": True,
        "entity_id": entity_id,
        "new_state": None,
        "speech": f"Events on {friendly_name}: " + "; ".join(lines) + ".",
        "metadata": {"events": events},
    }


async def _query_event(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    default_calendar_ids: list[str] | None = None,
) -> dict:
    params = action.get("parameters") or {}
    summary_query = str(params.get("summary", ""))

    if not summary_query:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "summary is required for query_event.",
        }

    entity_id, friendly_name, error = await _resolve_calendar_entity(
        action,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector,
        default_calendar_ids=default_calendar_ids,
    )
    if error:
        return {"success": False, "entity_id": None, "new_state": None, "speech": error}

    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    start = now.isoformat()
    end = (now + dt.timedelta(days=30)).isoformat()

    try:
        events = await ha_client.get_calendar_events(entity_id, start, end)
    except Exception as exc:
        logger.error("Failed to query events for %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to query events: {exc}",
        }

    query_lower = summary_query.lower()
    matches = [ev for ev in (events or []) if query_lower in str(ev.get("summary", "")).lower()]

    if not matches:
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"No upcoming events matching '{summary_query}' on {friendly_name}.",
            "metadata": {"events": []},
        }

    ev = matches[0]
    return {
        "success": True,
        "entity_id": entity_id,
        "new_state": None,
        "speech": f"Next match: {ev.get('summary')} at {ev.get('start')} on {friendly_name}.",
        "metadata": {"events": matches},
    }


async def _create_event(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
    default_calendar_ids: list[str] | None = None,
) -> dict:
    action_name = action.get("action", "").lower()
    params = action.get("parameters") or {}
    summary = str(params.get("summary", ""))
    start_time = str(params.get("start_date_time", ""))
    end_time = str(params.get("end_date_time", ""))
    description = str(params.get("description", ""))
    location = str(params.get("location", ""))
    rrule = str(params.get("rrule", ""))

    if not summary:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "Summary is required for create_event.",
        }
    if not start_time:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "start_date_time is required for create_event.",
        }

    entity_id, friendly_name, error = await _resolve_calendar_entity(
        action,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector,
        verbatim_terms=verbatim_terms,
        default_calendar_ids=default_calendar_ids,
    )
    if error:
        return {"success": False, "entity_id": None, "new_state": None, "speech": error}

    service_data: dict[str, str] = {"summary": summary, "start_date_time": start_time}
    service_data["end_date_time"] = end_time or start_time
    if description:
        service_data["description"] = description
    if location:
        service_data["location"] = location
    if rrule:
        service_data["rrule"] = rrule

    try:
        await ha_client.call_service("calendar", "create_event", entity_id, service_data)
    except Exception as exc:
        logger.error("Failed to create calendar event on %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to create event: {exc}",
        }

    return {
        "success": True,
        "action": action_name,
        "entity_id": entity_id,
        "new_state": None,
        "speech": f'Created event "{summary}" at {start_time} on {friendly_name}.',
    }


async def _delete_event(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
    default_calendar_ids: list[str] | None = None,
) -> dict:
    action_name = action.get("action", "").lower()
    params = action.get("parameters") or {}
    uid = str(params.get("uid", ""))
    summary = str(params.get("summary", ""))
    start_time = str(params.get("start_date_time", ""))

    if not uid and not summary:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "uid or summary+start_date_time is required for delete_event.",
        }

    entity_id, friendly_name, error = await _resolve_calendar_entity(
        action,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector,
        verbatim_terms=verbatim_terms,
        default_calendar_ids=default_calendar_ids,
    )
    if error:
        return {"success": False, "entity_id": None, "new_state": None, "speech": error}

    if uid:
        try:
            await ha_client.call_service("calendar", "delete_event", entity_id, {"uid": uid})
        except Exception as exc:
            logger.error("Failed to delete event on %s", entity_id, exc_info=True)
            return {
                "success": False,
                "entity_id": entity_id,
                "new_state": None,
                "speech": f"Failed to delete event: {exc}",
            }
        return {
            "success": True,
            "action": action_name,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Deleted event on {friendly_name}.",
        }

    # Search by summary + start_time
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    start = now.isoformat()
    end = (now + dt.timedelta(days=365)).isoformat()
    try:
        events = await ha_client.get_calendar_events(entity_id, start, end)
    except Exception as exc:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to find event: {exc}",
        }

    summary_lower = summary.lower()
    matches = [
        ev
        for ev in (events or [])
        if summary_lower in str(ev.get("summary", "")).lower()
        and (not start_time or start_time in str(ev.get("start", "")))
    ]

    if not matches:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"No matching event found on {friendly_name}.",
        }

    target_uid = matches[0].get("uid", "")
    if not target_uid:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": "Could not determine event UID for deletion.",
        }

    try:
        await ha_client.call_service("calendar", "delete_event", entity_id, {"uid": target_uid})
    except Exception as exc:
        logger.error("Failed to delete event on %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to delete event: {exc}",
        }

    return {
        "success": True,
        "action": action_name,
        "entity_id": entity_id,
        "new_state": None,
        "speech": f"Deleted event on {friendly_name}.",
    }


async def _update_event(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
    default_calendar_ids: list[str] | None = None,
) -> dict:
    action_name = action.get("action", "").lower()
    params = action.get("parameters") or {}
    summary = str(params.get("summary", ""))
    start_time = str(params.get("start_date_time", ""))

    if not summary and not start_time:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": "summary or start_date_time is required to identify the event for update.",
        }

    entity_id, friendly_name, error = await _resolve_calendar_entity(
        action,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector,
        verbatim_terms=verbatim_terms,
        default_calendar_ids=default_calendar_ids,
    )
    if error:
        return {"success": False, "entity_id": None, "new_state": None, "speech": error}

    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    start = now.isoformat()
    end = (now + dt.timedelta(days=365)).isoformat()
    try:
        events = await ha_client.get_calendar_events(entity_id, start, end)
    except Exception as exc:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to find event: {exc}",
        }

    matches = []
    for ev in events or []:
        match = True
        if summary and summary.lower() not in str(ev.get("summary", "")).lower():
            match = False
        if start_time and start_time not in str(ev.get("start", "")):
            match = False
        if match:
            matches.append(ev)

    if not matches:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"No matching event found on {friendly_name}.",
        }

    target_uid = matches[0].get("uid", "")
    if not target_uid:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": "Could not determine event UID for update.",
        }

    service_data: dict[str, str] = {"uid": target_uid}
    for key in ("summary", "start_date_time", "end_date_time", "description", "location", "rrule"):
        if params.get(key):
            service_data[key] = str(params[key])

    try:
        await ha_client.call_service("calendar", "update_event", entity_id, service_data)
    except Exception as exc:
        logger.error("Failed to update event on %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to update event: {exc}",
        }

    return {
        "success": True,
        "action": action_name,
        "entity_id": entity_id,
        "new_state": None,
        "speech": f"Updated event on {friendly_name}.",
    }
