"""Proactive calendar reminder injector.

Injects one-time reminders at configured offsets into the orchestration pipeline.
Fires on every user turn. Per event and user only the closest applicable
offset fires, and each offset fires at most once.
Reminder text is generated via LLM for natural, context-aware phrasing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.base import _load_prompt_path, _prompt_path, language_code_to_name
from app.agents.user_identity import UserIdentityResolver
from app.db.repository import (
    CalendarEntitySettingsRepository,
    CalendarReminderStateRepository,
    SettingsRepository,
)
from app.security.sanitization import wrap_user_input

logger = logging.getLogger(__name__)

_DEFAULT_OFFSETS = [15, 60, 1440]

# All-day events have no meaningful minute-scale lead time: only offsets of
# at least one day apply to them.
_ALL_DAY_MIN_OFFSET = 1440


def _parse_offsets(raw: Any) -> list[int]:
    """Coerce configured offsets to a sorted list of unique positive ints."""
    offsets: set[int] = set()
    for value in raw if isinstance(raw, list) else []:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            continue
        if minutes > 0:
            offsets.add(minutes)
    return sorted(offsets)


def _event_key(event: dict[str, Any]) -> str:
    """Stable per-event key for the fired-state table.

    ``calendar.get_events`` does not return a ``uid``. Use it when present,
    otherwise key on summary plus the raw start value.
    """
    uid = str(event.get("uid") or "").strip()
    if uid:
        return uid
    raw_start = event.get("start")
    if isinstance(raw_start, dict):
        raw_start = raw_start.get("dateTime") or raw_start.get("date")
    basis = f"{event.get('summary', '')}|{raw_start or ''}"
    return "sum:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


class CalendarReminderInjector:
    """Injects proactive calendar reminders into the response pipeline.

    Reminder text is generated via LLM for natural phrasing.
    Only calendars explicitly enabled in calendar_entity_settings are considered.
    """

    def __init__(
        self,
        ha_client: Any,
        entity_index: Any,
        llm_call: Callable | None = None,
    ) -> None:
        self._ha_client = ha_client
        self._entity_index = entity_index
        self._user_resolver = UserIdentityResolver(ha_client=ha_client)
        self._llm_call = llm_call

    async def inject_reminders(
        self,
        utterance: str | None,
        device_id: str | None = None,
        area_id: str | None = None,
        user_id: str | None = None,
        language: str = "en",
    ) -> str | None:
        """Return reminder text for this turn, or None.

        Never raises (except on cancellation): a reminder failure must not
        break the user's turn, whichever pipeline path calls it.
        """
        try:
            return await self._inject_reminders(utterance, device_id, area_id, user_id, language)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Calendar reminder injection failed", exc_info=True)
            return None

    async def _inject_reminders(
        self,
        utterance: str | None,
        device_id: str | None,
        area_id: str | None,
        user_id: str | None,
        language: str,
    ) -> str | None:
        enabled = await SettingsRepository.get_value("calendar.reminder_injection.enabled", "true")
        if str(enabled).lower() != "true":
            return None

        user = await self._user_resolver.resolve_user(utterance, device_id, area_id, user_id=user_id)
        if user:
            calendar_ids = json.loads(user.get("calendar_entity_ids_json") or "[]")
            offsets = _parse_offsets(json.loads(user.get("reminder_offsets_json") or json.dumps(_DEFAULT_OFFSETS)))
            user_mapping_id = user["id"]
        else:
            calendar_ids = []
            offsets = list(_DEFAULT_OFFSETS)
            user_mapping_id = 0
        if not isinstance(calendar_ids, list):
            calendar_ids = []
        if not offsets:
            return None

        # Always include universal calendars (e.g. birthdays, holidays)
        universal_ids = await CalendarEntitySettingsRepository.get_universal_entity_ids()
        for uid in universal_ids:
            if uid not in calendar_ids:
                calendar_ids.append(uid)

        if not calendar_ids:
            return None

        # Filter to only enabled calendars
        calendar_ids = await self._filter_enabled(calendar_ids)
        if not calendar_ids:
            return None

        raw_lookahead = await SettingsRepository.get_value("calendar.reminder_injection.lookahead_hours", "24")
        lookahead_hours = int(raw_lookahead or "24")
        now = datetime.now(UTC)
        end = now + timedelta(hours=lookahead_hours)

        events = await self._get_upcoming_events(calendar_ids, now, end)
        if not events:
            return None

        local_tz = await self._home_timezone()
        reminders: list[str] = []
        for event in events:
            all_day = self._is_all_day(event.get("start"))
            event_start = self._parse_event_start(event.get("start"), local_tz)
            if not event_start or event_start <= now:
                continue

            calendar_entity_id = event.get("_calendar_entity_id", "")
            if not calendar_entity_id:
                continue

            # Only the closest applicable offset may fire. Larger offsets
            # whose window also contains "now" are stale and stay silent.
            offset = self._closest_active_offset(event_start, now, offsets, all_day=all_day)
            if offset is None:
                continue
            event_key = _event_key(event)
            if await CalendarReminderStateRepository.has_fired(event_key, calendar_entity_id, user_mapping_id, offset):
                continue
            reminder_text = await self._generate_reminder_text(
                summary=event.get("summary", "Event"),
                offset=offset,
                event_start=event_start.astimezone(local_tz),
                language=language,
                now=now.astimezone(local_tz),
                all_day=all_day,
            )
            if reminder_text:
                reminders.append(reminder_text)
                await CalendarReminderStateRepository.mark_fired(event_key, calendar_entity_id, user_mapping_id, offset)

        if not reminders:
            return None

        return " ".join(reminders)

    async def _home_timezone(self) -> tzinfo:
        """Home timezone from the cached HA config (UTC fallback)."""
        try:
            from app.ha_client.home_context import home_context_provider

            ctx = await home_context_provider.get(self._ha_client)
            return ZoneInfo(ctx.timezone or "UTC")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Home timezone lookup failed; using UTC", exc_info=True)
            return UTC

    async def _get_enabled_calendar_entities(self) -> list[str]:
        """Return enabled calendar entity IDs from DB + visible index."""
        entries = []
        if hasattr(self._entity_index, "list_entries_async"):
            entries = await self._entity_index.list_entries_async(domains={"calendar"})
        elif hasattr(self._entity_index, "list_entries"):
            entries = self._entity_index.list_entries(domains={"calendar"})

        entity_ids = [str(getattr(e, "entity_id", "")) for e in entries if getattr(e, "entity_id", "")]

        # Ensure all visible calendars have a DB row (default enabled)
        for e in entries:
            eid = getattr(e, "entity_id", "")
            fname = getattr(e, "friendly_name", "")
            if eid:
                existing = await CalendarEntitySettingsRepository.get(eid)
                if existing is None:
                    await CalendarEntitySettingsRepository.upsert(eid, friendly_name=fname or None, enabled=1)

        # Return only those explicitly enabled
        enabled_ids = await CalendarEntitySettingsRepository.get_enabled_entity_ids()
        return [eid for eid in entity_ids if eid in enabled_ids]

    async def _filter_enabled(self, calendar_ids: list[str]) -> list[str]:
        """Filter a list of calendar IDs to only those enabled in settings."""
        enabled_ids = await CalendarEntitySettingsRepository.get_enabled_entity_ids()
        enabled_set = set(enabled_ids)
        return [cid for cid in calendar_ids if cid in enabled_set]

    async def _get_upcoming_events(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        all_events: list[dict[str, Any]] = []
        start_str = start.isoformat()
        end_str = end.isoformat()

        for entity_id in calendar_ids:
            try:
                result = await self._ha_client.get_calendar_events(entity_id, start_str, end_str)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Failed to get events for %s", entity_id, exc_info=True)
                continue
            # ``HARestClient.get_calendar_events`` returns the event list of
            # one entity. Tolerate the raw service-response mapping as well.
            if isinstance(result, dict):
                entry = result.get(entity_id) or {}
                result = entry.get("events") if isinstance(entry, dict) else None
            if not isinstance(result, list):
                continue
            for event in result:
                if isinstance(event, dict):
                    all_events.append({**event, "_calendar_entity_id": entity_id})

        def _sort_key(event: dict[str, Any]) -> datetime:
            parsed = self._parse_event_start(event.get("start"))
            return parsed or datetime.max.replace(tzinfo=UTC)

        all_events.sort(key=_sort_key)
        return all_events

    @staticmethod
    def _is_all_day(start_value: Any) -> bool:
        """True for a date-only start (all-day event)."""
        if isinstance(start_value, dict):
            return bool(start_value.get("date")) and not start_value.get("dateTime")
        text = str(start_value or "").strip()
        return len(text) == 10 and "T" not in text and " " not in text

    def _parse_event_start(self, start_value: Any, local_tz: tzinfo | None = None) -> datetime | None:
        """Parse an event start into an aware datetime.

        Date-only values (all-day events) and naive datetimes are
        interpreted in ``local_tz`` (UTC when not given), so they compare
        safely against an aware ``now``.
        """
        if not start_value:
            return None
        if isinstance(start_value, dict):
            dt_str = start_value.get("dateTime") or start_value.get("date")
        else:
            dt_str = str(start_value)
        if not dt_str:
            return None
        tz = local_tz or UTC
        dt_str = dt_str.strip()
        try:
            if len(dt_str) == 10:
                day = date.fromisoformat(dt_str)
                return datetime(day.year, day.month, day.day, tzinfo=tz)
            parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed

    def _marker_active(self, event_start: datetime, now: datetime, offset_minutes: int) -> bool:
        time_until = event_start - now
        return timedelta(0) < time_until <= timedelta(minutes=offset_minutes)

    def _closest_active_offset(
        self,
        event_start: datetime,
        now: datetime,
        offsets: list[int],
        *,
        all_day: bool = False,
    ) -> int | None:
        """Return the smallest offset whose window contains ``now`` (None if none)."""
        candidates = [o for o in offsets if not all_day or o >= _ALL_DAY_MIN_OFFSET]
        active = [o for o in candidates if self._marker_active(event_start, now, o)]
        return min(active) if active else None

    @staticmethod
    def _day_offset(event_start: datetime, now: datetime | None) -> int | None:
        if now is None:
            return None
        return (event_start.date() - now.date()).days

    @classmethod
    def _describe_when(
        cls,
        offset: int,
        event_start: datetime,
        now: datetime | None = None,
        all_day: bool = False,
    ) -> str:
        """English time phrase for the LLM prompt (the LLM localizes it)."""
        if offset >= 1440:
            days = cls._day_offset(event_start, now)
            if days == 0:
                day_phrase = "today"
            elif days is None or days == 1:
                day_phrase = "tomorrow"
            else:
                day_phrase = f"on {event_start.strftime('%Y-%m-%d')}"
            if all_day:
                return day_phrase
            return f"{day_phrase} at {event_start.strftime('%H:%M')}"
        if offset == 15:
            return "in 15 minutes"
        if offset == 60:
            return "in one hour"
        return f"in {offset} minutes"

    async def _generate_reminder_text(
        self,
        summary: str,
        offset: int,
        event_start: datetime,
        language: str,
        now: datetime | None = None,
        all_day: bool = False,
    ) -> str | None:
        """Generate natural reminder text via LLM. Falls back to simple text if LLM unavailable."""
        if self._llm_call is None:
            return self._fallback_reminder_text(summary, offset, event_start, language, now=now, all_day=all_day)

        try:
            when = self._describe_when(offset, event_start, now, all_day)

            user_content = (
                f"Event: {wrap_user_input(summary)}\n"
                f"Time until start: {when}\n"
                f"Language: {language_code_to_name(language)}\n\n"
                f"Write a brief, natural reminder sentence."
            )

            messages = [
                {"role": "system", "content": _load_prompt_path(_prompt_path("calendar_reminder"))},
                {"role": "user", "content": user_content},
            ]

            result = await self._llm_call(
                messages,
                temperature=0.5,
                max_tokens=128,
            )
            text = result.strip() if result else ""
            # Remove quotes if present
            text = text.strip('"').strip("'")
            if text:
                return text
            return self._fallback_reminder_text(summary, offset, event_start, language, now=now, all_day=all_day)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("LLM reminder generation failed, using fallback", exc_info=True)
            return self._fallback_reminder_text(summary, offset, event_start, language, now=now, all_day=all_day)

    def _fallback_reminder_text(
        self,
        summary: str,
        offset: int,
        event_start: datetime,
        language: str,
        *,
        now: datetime | None = None,
        all_day: bool = False,
    ) -> str | None:
        """Simple fallback when LLM is not available."""
        lang = language.split("-")[0] if language else "en"
        time_str = event_start.strftime("%H:%M")
        days = self._day_offset(event_start, now)
        date_str = event_start.strftime("%Y-%m-%d")

        if lang == "de":
            if offset == 15:
                return f"Uebrigens: {summary} ist in 15 Minuten."
            if offset == 60:
                return f"Uebrigens: {summary} ist in einer Stunde."
            if offset >= 1440:
                day_phrase = "heute" if days == 0 else ("morgen" if days in (None, 1) else f"am {date_str}")
                if all_day:
                    return f"Uebrigens: {summary} ist {day_phrase}."
                return f"Uebrigens: {summary} ist {day_phrase} um {time_str}."
            return f"Uebrigens: {summary} ist in {offset} Minuten."

        # English default
        if offset == 15:
            return f"By the way: {summary} is in 15 minutes."
        if offset == 60:
            return f"By the way: {summary} is in one hour."
        if offset >= 1440:
            day_phrase = "today" if days == 0 else ("tomorrow" if days in (None, 1) else f"on {date_str}")
            if all_day:
                return f"By the way: {summary} is {day_phrase}."
            return f"By the way: {summary} is {day_phrase} at {time_str}."
        return f"By the way: {summary} is in {offset} minutes."
