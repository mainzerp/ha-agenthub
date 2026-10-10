"""Calendar-specific action execution.

- Reads (``list_events``, ``query_event``) use the ``calendar.get_events``
  service. Without an explicit calendar they read every visible default
  calendar of the user (or every visible calendar when none is configured)
  and merge the events sorted by start.
- ``create_event`` uses the ``calendar.create_event`` service (timed events
  default to one hour, date-only values create all-day events).
- ``update_event`` / ``delete_event`` need event uids, which
  ``calendar.get_events`` does not return. They read
  ``/api/calendars/<entity_id>``, call the ``calendar/event/update`` /
  ``calendar/event/delete`` WebSocket commands, and verify the effect by
  re-reading the calendar.

Every result is ``cacheable=False``: calendar writes must never be replayed
from the action cache.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.entity.visibility import entity_is_visible

logger = logging.getLogger(__name__)

_CALENDAR_DOMAINS: frozenset[str] = frozenset({"calendar"})
_DEFAULT_AGENT_ID = "calendar-agent"
_DEFAULT_EVENT_DURATION = timedelta(hours=1)
_QUERY_HORIZON = timedelta(days=30)
_SEARCH_HORIZON = timedelta(days=365)
_START_MATCH_TOLERANCE = timedelta(seconds=59)
_MAX_LISTED_CHOICES = 3

_WS_UNAVAILABLE_SPEECH = (
    "Changing or deleting calendar events needs the Home Assistant WebSocket connection, "
    "which is not available right now."
)


def _result(success: bool, speech: str, entity_id: str | None = None, **extra: Any) -> dict:
    result: dict[str, Any] = {
        "success": success,
        "entity_id": entity_id,
        "new_state": None,
        "speech": speech,
        # Calendar results depend on live calendar content; writes must
        # never be replayed from the action cache.
        "cacheable": False,
    }
    result.update(extra)
    return result


def _choice_result(speech: str, path: str) -> dict:
    """Clarifying question: requests a voice follow-up and is never rewritten as 'not found'."""
    return _result(False, speech, None, voice_followup=True, metadata={"resolution_path": path})


def _zone(name: str | None) -> tzinfo:
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except Exception:
        logger.debug("Unknown timezone %r; using UTC", name)
        return UTC


def _is_date_only(value: Any) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _parse_when(value: Any, tz: tzinfo) -> datetime | date | None:
    """Parse a user or HA time value.

    Date-only strings become ``date`` (all-day). Datetimes become aware
    datetimes; naive values are interpreted in ``tz``. Accepts the HA REST
    ``{"dateTime": ...}`` / ``{"date": ...}`` shape.
    """
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _is_naive_text(value: Any) -> bool:
    text = str(value or "").strip()
    if len(text) <= 10:
        return False
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).tzinfo is None
    except ValueError:
        return False


def _as_datetime(value: datetime | date, tz: tzinfo) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=tz)


def _format_when(value: datetime | date | None, tz: tzinfo) -> str:
    if value is None:
        return "an unknown time"
    if _is_date_only(value):
        return value.isoformat()
    assert isinstance(value, datetime)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _format_service_datetime(value: datetime, naive: bool) -> str:
    """Format for the ``calendar.create_event`` service.

    Naive user input stays naive (HA interprets it in its local timezone);
    otherwise the aware ISO form is sent.
    """
    if naive:
        return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    return value.isoformat()


def _normalize(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _calendar_query(action: dict) -> str:
    params = action.get("parameters") or {}
    explicit_calendar = str(params.get("calendar") or "").strip()
    return explicit_calendar or str(action.get("entity") or "").strip()


async def _calendar_is_visible(agent_id: str | None, entity_id: str, entity_index: Any) -> bool:
    """Fail-closed per-entity domain + visibility check for calendar picks."""
    if not entity_id or not entity_id.startswith("calendar."):
        return False
    try:
        return await entity_is_visible(agent_id or _DEFAULT_AGENT_ID, entity_id, entity_index)
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


async def _list_calendar_entries(entity_index: Any) -> list[Any]:
    if not entity_index:
        return []
    try:
        if hasattr(entity_index, "list_entries_async"):
            return list(await entity_index.list_entries_async(domains=_CALENDAR_DOMAINS))
        if hasattr(entity_index, "list_entries"):
            return list(entity_index.list_entries(domains=_CALENDAR_DOMAINS))
    except Exception:
        logger.warning("Listing calendar entities failed", exc_info=True)
    return []


async def _candidate_calendars(
    entity_index: Any,
    agent_id: str | None,
    default_calendar_ids: list[str] | None,
) -> list[tuple[str, str]]:
    """Calendars used when the user names none.

    The user's visible default calendars, or every visible calendar when no
    default is configured (or none of them is visible).
    """
    entries = await _list_calendar_entries(entity_index)
    names: dict[str, str] = {}
    for entry in entries:
        entity_id = str(getattr(entry, "entity_id", "") or "")
        if entity_id:
            names[entity_id] = str(getattr(entry, "friendly_name", "") or entity_id)

    candidates: list[tuple[str, str]] = []
    for raw in default_calendar_ids or []:
        entity_id = str(raw or "").strip()
        if not entity_id or any(entity_id == eid for eid, _ in candidates):
            continue
        if await _calendar_is_visible(agent_id, entity_id, entity_index):
            candidates.append((entity_id, names.get(entity_id, entity_id)))
    if candidates:
        return candidates

    for entity_id, name in names.items():
        if await _calendar_is_visible(agent_id, entity_id, entity_index):
            candidates.append((entity_id, name))
    return candidates


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
    default_calendar_ids: list[str] | None = None,
) -> dict:
    """Dispatch a parsed calendar action."""
    action_name = action.get("action", "").lower()
    handlers = {
        "list_events": _list_events,
        "query_event": _query_event,
        "create_event": _create_event,
        "delete_event": _delete_event,
        "update_event": _update_event,
    }
    handler = handlers.get(action_name)
    if handler is None:
        return _result(False, f"Unknown calendar action: {action_name}")
    return await handler(
        action,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id or _DEFAULT_AGENT_ID,
        span_collector,
        default_calendar_ids=default_calendar_ids,
        tz=_zone(timezone),
    )


async def _resolve_calendar_entity(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    default_calendar_ids: list[str] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve ONE target calendar. Returns (entity_id, friendly_name, speech_error).

    Without a calendar in the request this succeeds only when exactly one
    candidate calendar exists; several candidates are an error, never a
    silent first pick.
    """
    entity_query = _calendar_query(action)

    if not entity_query:
        candidates = await _candidate_calendars(entity_index, agent_id, default_calendar_ids)
        if not candidates:
            return None, None, "No calendar entity available."
        if len(candidates) > 1:
            names = ", ".join(name for _, name in candidates)
            return None, None, f"Several calendars are available ({names}). Which one do you mean?"
        return candidates[0][0], candidates[0][1], None

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
                    agent_id or _DEFAULT_AGENT_ID,
                    allowed_domains=_CALENDAR_DOMAINS,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    if entity_id and not str(entity_id).startswith("calendar."):
        logger.warning("Resolved entity %s is not a calendar; rejecting", entity_id)
        entity_id = None
    if not entity_id:
        return None, None, resolution["speech"] or f"Could not find a calendar entity matching '{entity_query}'."
    return entity_id, friendly_name, None


async def _target_calendars(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector,
    default_calendar_ids: list[str] | None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Calendars an action works on: the named one, else all candidates."""
    if _calendar_query(action):
        entity_id, friendly_name, error = await _resolve_calendar_entity(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector,
            default_calendar_ids=default_calendar_ids,
        )
        if error or not entity_id:
            return [], error or "No calendar entity available."
        return [(entity_id, friendly_name or entity_id)], None
    candidates = await _candidate_calendars(entity_index, agent_id, default_calendar_ids)
    if not candidates:
        return [], "No calendar entity available."
    return candidates, None


async def _gather_per_calendar(
    fetch,
    calendars: list[tuple[str, str]],
    start: str,
    end: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run ``fetch(entity_id, start, end)`` for every calendar; tag and merge events."""
    results = await asyncio.gather(*(fetch(eid, start, end) for eid, _ in calendars), return_exceptions=True)
    events: list[dict[str, Any]] = []
    failed: list[str] = []
    for (entity_id, name), result in zip(calendars, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            logger.warning("Reading events from %s failed: %s", entity_id, result)
            failed.append(name)
            continue
        for event in result or []:
            if isinstance(event, dict):
                events.append({**event, "_calendar_entity_id": entity_id, "_calendar_name": name})
    return events, failed


def _sort_events(events: list[dict[str, Any]], tz: tzinfo) -> list[dict[str, Any]]:
    def _key(event: dict[str, Any]) -> datetime:
        parsed = _parse_when(event.get("start"), tz)
        return _as_datetime(parsed, tz) if parsed is not None else datetime.max.replace(tzinfo=UTC)

    return sorted(events, key=_key)


def _describe_event(event: dict[str, Any], tz: tzinfo, with_calendar: bool) -> str:
    text = f"{event.get('summary', 'Event')} at {_format_when(_parse_when(event.get('start'), tz), tz)}"
    if with_calendar:
        text += f" ({event.get('_calendar_name') or event.get('_calendar_entity_id')})"
    return text


async def _list_events(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    default_calendar_ids: list[str] | None = None,
    tz: tzinfo = UTC,
) -> dict:
    params = action.get("parameters") or {}
    start_time = str(params.get("start_date_time", ""))
    end_time = str(params.get("end_date_time", ""))

    if not start_time or not end_time:
        return _result(False, "start_date_time and end_date_time are required for list_events.")

    calendars, error = await _target_calendars(
        action, ha_client, entity_index, entity_matcher, agent_id, span_collector, default_calendar_ids
    )
    if error:
        return _result(False, error)

    events, failed = await _gather_per_calendar(ha_client.get_calendar_events, calendars, start_time, end_time)
    entity_id = calendars[0][0] if len(calendars) == 1 else None
    if failed and len(failed) == len(calendars):
        return _result(False, f"Failed to list events from {', '.join(failed)}.", calendars[0][0])

    single_name = calendars[0][1] if len(calendars) == 1 else None
    events = _sort_events(events, tz)
    if not events:
        speech = f"No events found on {single_name}." if single_name else "No events found."
    else:
        lines = [_describe_event(ev, tz, with_calendar=single_name is None) for ev in events]
        prefix = f"Events on {single_name}: " if single_name else "Events: "
        speech = prefix + "; ".join(lines) + "."
    if failed:
        speech += f" Could not read {', '.join(failed)}."
    return _result(True, speech, entity_id, metadata={"events": events})


async def _query_event(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    default_calendar_ids: list[str] | None = None,
    tz: tzinfo = UTC,
) -> dict:
    params = action.get("parameters") or {}
    summary_query = str(params.get("summary", ""))

    if not summary_query:
        return _result(False, "summary is required for query_event.")

    calendars, error = await _target_calendars(
        action, ha_client, entity_index, entity_matcher, agent_id, span_collector, default_calendar_ids
    )
    if error:
        return _result(False, error)

    now = datetime.now(UTC)
    events, failed = await _gather_per_calendar(
        ha_client.get_calendar_events, calendars, now.isoformat(), (now + _QUERY_HORIZON).isoformat()
    )
    entity_id = calendars[0][0] if len(calendars) == 1 else None
    if failed and len(failed) == len(calendars):
        return _result(False, f"Failed to query events from {', '.join(failed)}.", calendars[0][0])

    query_norm = _normalize(summary_query)
    matches = _sort_events([ev for ev in events if query_norm in _normalize(ev.get("summary"))], tz)
    where = calendars[0][1] if len(calendars) == 1 else "your calendars"
    if not matches:
        return _result(
            True,
            f"No upcoming events matching '{summary_query}' on {where}.",
            entity_id,
            metadata={"events": []},
        )

    ev = matches[0]
    when = _format_when(_parse_when(ev.get("start"), tz), tz)
    calendar_name = ev.get("_calendar_name") or where
    return _result(
        True,
        f"Next match: {ev.get('summary')} at {when} on {calendar_name}.",
        ev.get("_calendar_entity_id") or entity_id,
        metadata={"events": matches},
    )


async def _pick_single_calendar(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector,
    default_calendar_ids: list[str] | None,
    question: str,
) -> tuple[tuple[str, str] | None, dict | None]:
    calendars, error = await _target_calendars(
        action, ha_client, entity_index, entity_matcher, agent_id, span_collector, default_calendar_ids
    )
    if error:
        return None, _result(False, error)
    if len(calendars) > 1:
        names = ", ".join(name for _, name in calendars)
        return None, _choice_result(f"{question} {names}?", "calendar_ambiguous")
    return calendars[0], None


async def _create_event(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    default_calendar_ids: list[str] | None = None,
    tz: tzinfo = UTC,
) -> dict:
    action_name = action.get("action", "").lower()
    params = action.get("parameters") or {}
    summary = str(params.get("summary", "")).strip()
    start_raw = str(params.get("start_date_time") or "").strip()
    end_raw = str(params.get("end_date_time") or "").strip()
    start_date_raw = str(params.get("start_date") or "").strip()
    end_date_raw = str(params.get("end_date") or "").strip()

    if not summary:
        return _result(False, "Summary is required for create_event.")
    if not start_raw and not start_date_raw:
        return _result(False, "start_date_time is required for create_event.")

    service_data: dict[str, str] = {"summary": summary}
    start_value = _parse_when(start_date_raw or start_raw, tz)
    if start_value is None:
        return _result(False, f"Could not understand the start time '{start_date_raw or start_raw}'.")

    if _is_date_only(start_value):
        # All-day event: HA's end_date is exclusive, so a one-day event
        # ends on the following day.
        end_value = _parse_when(end_date_raw or end_raw, tz) if (end_date_raw or end_raw) else None
        if end_value is not None and not _is_date_only(end_value):
            end_value = end_value.astimezone(tz).date() if isinstance(end_value, datetime) else end_value
        if end_value is None or end_value <= start_value:
            end_value = start_value + timedelta(days=1)
        service_data["start_date"] = start_value.isoformat()
        service_data["end_date"] = end_value.isoformat()
        when = f"on {start_value.isoformat()} (all day)"
    else:
        assert isinstance(start_value, datetime)
        naive = _is_naive_text(start_raw)
        if end_raw:
            end_value = _parse_when(end_raw, tz)
            if not isinstance(end_value, datetime):
                return _result(False, f"Could not understand the end time '{end_raw}'.")
            naive = naive and _is_naive_text(end_raw)
        else:
            end_value = start_value + _DEFAULT_EVENT_DURATION
        if end_value <= start_value:
            return _result(False, "The event end must be after its start.")
        service_data["start_date_time"] = _format_service_datetime(start_value, naive)
        service_data["end_date_time"] = _format_service_datetime(end_value, naive)
        when = f"at {_format_when(start_value, tz)}"

    for key in ("description", "location", "rrule"):
        value = str(params.get(key) or "").strip()
        if value:
            service_data[key] = value

    target, problem = await _pick_single_calendar(
        action,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector,
        default_calendar_ids,
        f'Which calendar should I add "{summary}" to:',
    )
    if problem is not None:
        return problem
    assert target is not None
    entity_id, friendly_name = target

    try:
        await ha_client.call_service("calendar", "create_event", entity_id, service_data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Failed to create calendar event on %s", entity_id, exc_info=True)
        return _result(False, f"Failed to create event: {exc}", entity_id)

    return _result(
        True,
        f'Created event "{summary}" {when} on {friendly_name}.',
        entity_id,
        action=action_name,
        service_data=service_data,
    )


def _start_matches(event_start: datetime | date | None, target: datetime | date, tz: tzinfo) -> bool:
    if event_start is None:
        return False
    if _is_date_only(target):
        event_day = event_start if _is_date_only(event_start) else event_start.astimezone(tz).date()
        return event_day == target
    assert isinstance(target, datetime)
    if _is_date_only(event_start):
        local = target.astimezone(tz)
        return local.date() == event_start and local.hour == 0 and local.minute == 0
    assert isinstance(event_start, datetime)
    return abs(event_start - target) <= _START_MATCH_TOLERANCE


def _match_events(
    events: list[dict[str, Any]],
    summary: str,
    target_start: datetime | date | None,
    tz: tzinfo,
) -> list[dict[str, Any]]:
    """Filter by start (when given), then prefer exact summary over substring matches."""
    candidates = [
        ev
        for ev in events
        if target_start is None or _start_matches(_parse_when(ev.get("start"), tz), target_start, tz)
    ]
    if not summary:
        return candidates
    norm = _normalize(summary)
    exact = [ev for ev in candidates if _normalize(ev.get("summary")) == norm]
    if exact:
        return exact
    return [ev for ev in candidates if norm in _normalize(ev.get("summary"))]


def _search_window(target_start: datetime | date | None, tz: tzinfo) -> tuple[str, str]:
    if target_start is None:
        now = datetime.now(UTC)
        return now.isoformat(), (now + _SEARCH_HORIZON).isoformat()
    day = target_start if _is_date_only(target_start) else target_start.astimezone(tz).date()
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


async def _locate_event(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector,
    default_calendar_ids: list[str] | None,
    tz: tzinfo,
    *,
    verb: str,
) -> tuple[dict[str, Any] | None, dict | None, tuple[str, str] | None]:
    """Find exactly one event (with uid) to update or delete.

    Returns ``(event, error_result, window)``.
    """
    params = action.get("parameters") or {}
    summary = str(params.get("summary") or "").strip()
    start_raw = str(params.get("start_date_time") or params.get("start_date") or "").strip()
    target_start = _parse_when(start_raw, tz) if start_raw else None
    if start_raw and target_start is None:
        return None, _result(False, f"Could not understand the time '{start_raw}'."), None

    calendars, error = await _target_calendars(
        action, ha_client, entity_index, entity_matcher, agent_id, span_collector, default_calendar_ids
    )
    if error:
        return None, _result(False, error), None

    window = _search_window(target_start, tz)
    fetch = getattr(ha_client, "get_calendar_event_details", None)
    if fetch is None:
        return None, _result(False, f"I cannot {verb} calendar events with this Home Assistant connection."), None
    events, failed = await _gather_per_calendar(fetch, calendars, *window)
    first_id = calendars[0][0]
    if failed and len(failed) == len(calendars):
        return None, _result(False, f"Failed to read events from {', '.join(failed)}.", first_id), None

    matches = _match_events(events, summary, target_start, tz)
    names = ", ".join(name for _, name in calendars)
    if not matches:
        return None, _result(False, f"No matching event found on {names}.", first_id), None
    if len(matches) > 1:
        listed = "; ".join(
            _describe_event(ev, tz, with_calendar=len(calendars) > 1) for ev in matches[:_MAX_LISTED_CHOICES]
        )
        more = f" and {len(matches) - _MAX_LISTED_CHOICES} more" if len(matches) > _MAX_LISTED_CHOICES else ""
        return (
            None,
            _choice_result(
                f"I found several matching events: {listed}{more}. Which one do you mean?", "event_ambiguous"
            ),
            None,
        )
    event = matches[0]
    if not event.get("uid"):
        return (
            None,
            _result(
                False,
                f"This calendar does not expose event identifiers, so I cannot {verb} the event.",
                event["_calendar_entity_id"],
            ),
            None,
        )
    return event, None, window


async def _reread(ha_client: Any, entity_id: str, start: str, end: str) -> list[dict[str, Any]] | None:
    try:
        events = await ha_client.get_calendar_event_details(entity_id, start, end)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Calendar re-read for verification failed on %s", entity_id, exc_info=True)
        return None
    return events if isinstance(events, list) else None


async def _send_calendar_ws(ha_client: Any, msg_type: str, payload: dict[str, Any], entity_id: str) -> dict | None:
    """Send a calendar WS command; return an error result or None on dispatch success."""
    try:
        await ha_client.send_ws_command(msg_type, **payload)
    except asyncio.CancelledError:
        raise
    except RuntimeError:
        logger.warning("%s unavailable: no WebSocket connection", msg_type)
        return _result(False, _WS_UNAVAILABLE_SPEECH, entity_id)
    except Exception as exc:
        logger.error("%s failed on %s", msg_type, entity_id, exc_info=True)
        return _result(False, f"Home Assistant rejected the calendar change: {exc}", entity_id)
    return None


async def _delete_event(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    default_calendar_ids: list[str] | None = None,
    tz: tzinfo = UTC,
) -> dict:
    action_name = action.get("action", "").lower()
    params = action.get("parameters") or {}
    summary = str(params.get("summary") or "").strip()
    if not summary and not str(params.get("start_date_time") or params.get("start_date") or "").strip():
        return _result(False, "summary or start_date_time is required for delete_event.")

    event, problem, window = await _locate_event(
        action,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector,
        default_calendar_ids,
        tz,
        verb="delete",
    )
    if problem is not None:
        return problem
    assert event is not None and window is not None
    entity_id = event["_calendar_entity_id"]
    calendar_name = event.get("_calendar_name") or entity_id
    uid = str(event["uid"])
    recurrence_id = event.get("recurrence_id")

    payload: dict[str, Any] = {"entity_id": entity_id, "uid": uid}
    if recurrence_id:
        payload["recurrence_id"] = recurrence_id
    error = await _send_calendar_ws(ha_client, "calendar/event/delete", payload, entity_id)
    if error is not None:
        return error

    remaining = await _reread(ha_client, entity_id, *window)
    if remaining is not None and any(
        str(ev.get("uid")) == uid and (not recurrence_id or ev.get("recurrence_id") == recurrence_id)
        for ev in remaining
    ):
        return _result(False, f'Home Assistant did not delete "{event.get("summary")}".', entity_id)

    return _result(
        True,
        f'Deleted "{event.get("summary")}" at {_format_when(_parse_when(event.get("start"), tz), tz)} '
        f"from {calendar_name}.",
        entity_id,
        action=action_name,
    )


def _ws_time(value: datetime | date) -> str:
    return value.isoformat()


async def _update_event(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    default_calendar_ids: list[str] | None = None,
    tz: tzinfo = UTC,
) -> dict:
    """Update one event.

    ``summary`` / ``start_date_time`` identify the event (current values);
    ``new_summary``, ``new_start_date_time``, ``new_end_date_time``,
    ``new_description`` and ``new_location`` carry the changes. For
    compatibility ``end_date_time``, ``description`` and ``location`` are
    treated as new values as well.
    """
    action_name = action.get("action", "").lower()
    params = action.get("parameters") or {}
    summary = str(params.get("summary") or "").strip()
    start_raw = str(params.get("start_date_time") or "").strip()

    if not summary and not start_raw:
        return _result(False, "summary or start_date_time is required to identify the event for update.")

    new_summary = str(params.get("new_summary") or "").strip()
    new_start_raw = str(params.get("new_start_date_time") or params.get("new_start_date") or "").strip()
    new_end_raw = str(
        params.get("new_end_date_time") or params.get("new_end_date") or params.get("end_date_time") or ""
    ).strip()
    new_description = str(params.get("new_description") or params.get("description") or "").strip()
    new_location = str(params.get("new_location") or params.get("location") or "").strip()
    if not any((new_summary, new_start_raw, new_end_raw, new_description, new_location)):
        return _result(False, "No changes were specified for update_event.")

    new_start = _parse_when(new_start_raw, tz) if new_start_raw else None
    new_end = _parse_when(new_end_raw, tz) if new_end_raw else None
    if (new_start_raw and new_start is None) or (new_end_raw and new_end is None):
        return _result(False, "Could not understand the new event time.")

    event, problem, window = await _locate_event(
        action,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector,
        default_calendar_ids,
        tz,
        verb="change",
    )
    if problem is not None:
        return problem
    assert event is not None and window is not None
    entity_id = event["_calendar_entity_id"]
    calendar_name = event.get("_calendar_name") or entity_id

    old_start = _parse_when(event.get("start"), tz)
    old_end = _parse_when(event.get("end"), tz)
    if old_start is None:
        return _result(False, "Could not read the current time of that event.", entity_id)
    if old_end is None:
        old_end = old_start + (timedelta(days=1) if _is_date_only(old_start) else _DEFAULT_EVENT_DURATION)

    start_value: datetime | date = new_start if new_start is not None else old_start
    if new_end is not None:
        end_value: datetime | date = new_end
    elif new_start is not None:
        if _is_date_only(new_start) == _is_date_only(old_start):
            end_value = new_start + (old_end - old_start)  # type: ignore[operator]
        else:
            end_value = new_start + (timedelta(days=1) if _is_date_only(new_start) else _DEFAULT_EVENT_DURATION)
    else:
        end_value = old_end
    if _is_date_only(start_value) != _is_date_only(end_value):
        return _result(False, "Start and end must both be dates or both be times.", entity_id)
    if end_value <= start_value:  # type: ignore[operator]
        return _result(False, "The event end must be after its start.", entity_id)

    final_summary = new_summary or str(event.get("summary") or "")
    ws_event: dict[str, Any] = {
        "summary": final_summary,
        "dtstart": _ws_time(start_value),
        "dtend": _ws_time(end_value),
    }
    # The update replaces the event, so unchanged optional fields are kept.
    for key, new_value in (("description", new_description), ("location", new_location)):
        value = new_value or str(event.get(key) or "").strip()
        if value:
            ws_event[key] = value

    payload: dict[str, Any] = {"entity_id": entity_id, "uid": str(event["uid"]), "event": ws_event}
    if event.get("recurrence_id"):
        payload["recurrence_id"] = event["recurrence_id"]
    error = await _send_calendar_ws(ha_client, "calendar/event/update", payload, entity_id)
    if error is not None:
        return error

    verify_start = min(_as_datetime(old_start, tz), _as_datetime(start_value, tz))
    verify_end = max(_as_datetime(old_end, tz), _as_datetime(end_value, tz)) + timedelta(days=1)
    current = await _reread(ha_client, entity_id, verify_start.isoformat(), verify_end.isoformat())
    if current is not None and not any(
        str(ev.get("uid")) == str(event["uid"])
        and _normalize(ev.get("summary")) == _normalize(final_summary)
        and _start_matches(_parse_when(ev.get("start"), tz), start_value, tz)
        for ev in current
    ):
        return _result(False, f'Home Assistant did not confirm the change to "{final_summary}".', entity_id)

    return _result(
        True,
        f'Updated "{final_summary}" on {calendar_name}, now at {_format_when(start_value, tz)}.',
        entity_id,
        action=action_name,
    )
