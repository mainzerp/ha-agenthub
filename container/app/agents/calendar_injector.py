"""Proactive calendar reminder injector.

Injects one-time reminders at configured offsets into the orchestration pipeline.
Fires on every user turn. Each marker fires exactly once per event per user.
Reminder text is generated via LLM for natural, context-aware phrasing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents.base import _load_prompt_path, _prompt_path
from app.agents.user_identity import UserIdentityResolver
from app.db.repository import (
    CalendarEntitySettingsRepository,
    CalendarReminderStateRepository,
    SettingsRepository,
)

logger = logging.getLogger(__name__)

_DEFAULT_OFFSETS = [15, 60, 1440]


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
        enabled = await SettingsRepository.get_value("calendar.reminder_injection.enabled", "true")
        if str(enabled).lower() != "true":
            return None

        user = await self._user_resolver.resolve_user(utterance, device_id, area_id, user_id=user_id)
        if user:
            calendar_ids = json.loads(user.get("calendar_entity_ids_json", "[]"))
            offsets = json.loads(user.get("reminder_offsets_json", str(_DEFAULT_OFFSETS)))
            user_mapping_id = user["id"]
        else:
            calendar_ids = []
            offsets = _DEFAULT_OFFSETS
            user_mapping_id = 0

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

        reminders: list[str] = []
        for event in events:
            event_start = self._parse_event_start(event.get("start"))
            if not event_start or event_start <= now:
                continue

            event_uid = event.get("uid", "")
            calendar_entity_id = event.get("_calendar_entity_id", "")
            if not event_uid or not calendar_entity_id:
                continue

            for offset in offsets:
                if self._marker_active(event_start, now, offset):
                    already_fired = await CalendarReminderStateRepository.has_fired(
                        event_uid, calendar_entity_id, user_mapping_id, offset
                    )
                    if not already_fired:
                        reminder_text = await self._generate_reminder_text(
                            summary=event.get("summary", "Event"),
                            offset=offset,
                            event_start=event_start,
                            language=language,
                        )
                        if reminder_text:
                            reminders.append(reminder_text)
                            await CalendarReminderStateRepository.mark_fired(
                                event_uid, calendar_entity_id, user_mapping_id, offset
                            )

        if not reminders:
            return None

        return " ".join(reminders)

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
                if result and entity_id in result:
                    for event in result[entity_id].get("events", []):
                        event["_calendar_entity_id"] = entity_id
                        all_events.append(event)
            except Exception:
                logger.debug("Failed to get events for %s", entity_id, exc_info=True)

        all_events.sort(key=lambda e: e.get("start", ""))
        return all_events

    def _parse_event_start(self, start_value: Any) -> datetime | None:
        if not start_value:
            return None
        if isinstance(start_value, dict):
            dt_str = start_value.get("dateTime") or start_value.get("date")
        else:
            dt_str = str(start_value)
        if not dt_str:
            return None
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.strptime(dt_str, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return None

    def _marker_active(self, event_start: datetime, now: datetime, offset_minutes: int) -> bool:
        time_until = event_start - now
        return timedelta(0) < time_until <= timedelta(minutes=offset_minutes)

    async def _generate_reminder_text(
        self, summary: str, offset: int, event_start: datetime, language: str
    ) -> str | None:
        """Generate natural reminder text via LLM. Falls back to simple text if LLM unavailable."""
        if self._llm_call is None:
            return self._fallback_reminder_text(summary, offset, event_start, language)

        try:
            # Build human-readable time description
            if offset == 15:
                when = "in 15 minutes"
            elif offset == 60:
                when = "in one hour"
            elif offset == 1440:
                when = f"tomorrow at {event_start.strftime('%H:%M')}"
            else:
                when = f"in {offset} minutes"

            user_content = (
                f"Event: '{summary}'\n"
                f"Time until start: {when}\n"
                f"Language: {language}\n\n"
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
            return text if text else self._fallback_reminder_text(summary, offset, event_start, language)
        except Exception:
            logger.debug("LLM reminder generation failed, using fallback", exc_info=True)
            return self._fallback_reminder_text(summary, offset, event_start, language)

    def _fallback_reminder_text(self, summary: str, offset: int, event_start: datetime, language: str) -> str | None:
        """Simple fallback when LLM is not available."""
        lang = language.split("-")[0] if language else "en"
        time_str = event_start.strftime("%H:%M")

        if lang == "de":
            if offset == 15:
                return f"Uebrigens: {summary} ist in 15 Minuten."
            if offset == 60:
                return f"Uebrigens: {summary} ist in einer Stunde."
            if offset == 1440:
                return f"Uebrigens: {summary} ist morgen um {time_str}."
            return f"Uebrigens: {summary} ist in {offset} Minuten."

        # English default
        if offset == 15:
            return f"By the way: {summary} is in 15 minutes."
        if offset == 60:
            return f"By the way: {summary} is in one hour."
        if offset == 1440:
            return f"By the way: {summary} is tomorrow at {time_str}."
        return f"By the way: {summary} is in {offset} minutes."
