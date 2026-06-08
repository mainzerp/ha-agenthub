"""Async tests for app.agents.calendar_injector."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.calendar_injector import CalendarReminderInjector

pytestmark = pytest.mark.asyncio


class TestInjectReminders:
    async def test_inject_reminders_disabled_and_no_user(self):
        """Settings disabled returns None; enabled but no user/no calendars returns None."""
        ha_client = AsyncMock()
        entity_index = MagicMock()
        injector = CalendarReminderInjector(ha_client, entity_index)

        # Scenario 1: disabled globally
        with patch(
            "app.agents.calendar_injector.SettingsRepository.get_value",
            new_callable=AsyncMock,
            return_value="false",
        ):
            result = await injector.inject_reminders("hello")
        assert result is None

        # Scenario 2: enabled but no user and no universal calendars
        with (
            patch(
                "app.agents.calendar_injector.SettingsRepository.get_value",
                new_callable=AsyncMock,
                side_effect=lambda key, default=None: default,
            ),
            patch(
                "app.agents.calendar_injector.UserIdentityResolver.resolve_user",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.agents.calendar_injector.CalendarEntitySettingsRepository.get_universal_entity_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await injector.inject_reminders("hello")
        assert result is None

    async def test_inject_reminders_with_events_and_offsets(self):
        """Events exist, offsets used, marker active, mark_fired called."""
        ha_client = AsyncMock()
        entity_index = MagicMock()
        injector = CalendarReminderInjector(ha_client, entity_index)

        now = datetime.now(UTC)
        event_start = now + timedelta(minutes=10)

        ha_client.get_calendar_events = AsyncMock(
            return_value={
                "calendar.work": {
                    "events": [
                        {
                            "summary": "Meeting",
                            "start": event_start.isoformat(),
                            "uid": "evt-1",
                        }
                    ]
                }
            }
        )

        with (
            patch(
                "app.agents.calendar_injector.SettingsRepository.get_value",
                new_callable=AsyncMock,
                side_effect=lambda key, default=None: {
                    "calendar.reminder_injection.enabled": "true",
                    "calendar.reminder_injection.lookahead_hours": "24",
                }.get(key, default),
            ),
            patch(
                "app.agents.calendar_injector.UserIdentityResolver.resolve_user",
                new_callable=AsyncMock,
                return_value={
                    "id": 42,
                    "calendar_entity_ids_json": '["calendar.work"]',
                    "reminder_offsets_json": "[15]",
                },
            ),
            patch(
                "app.agents.calendar_injector.CalendarEntitySettingsRepository.get_universal_entity_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.agents.calendar_injector.CalendarEntitySettingsRepository.get_enabled_entity_ids",
                new_callable=AsyncMock,
                return_value=["calendar.work"],
            ),
            patch(
                "app.agents.calendar_injector.CalendarReminderStateRepository.has_fired",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "app.agents.calendar_injector.CalendarReminderStateRepository.mark_fired",
                new_callable=AsyncMock,
            ) as mock_mark,
        ):
            result = await injector.inject_reminders("hello")

        assert result is not None
        assert "Meeting" in result
        mock_mark.assert_awaited_once_with("evt-1", "calendar.work", 42, 15)


class TestGetUpcomingEventsAndFilter:
    async def test_get_upcoming_events_and_filter_enabled(self):
        """_get_upcoming_events fetches and sorts; _filter_enabled intersects; _get_enabled_calendar_entities queries DB."""
        ha_client = AsyncMock()
        entity_index = MagicMock()
        injector = CalendarReminderInjector(ha_client, entity_index)

        now = datetime.now(UTC)
        end = now + timedelta(hours=24)

        ha_client.get_calendar_events = AsyncMock(
            return_value={
                "calendar.a": {
                    "events": [{"summary": "A", "start": (now + timedelta(hours=2)).isoformat(), "uid": "a1"}]
                },
                "calendar.b": {
                    "events": [{"summary": "B", "start": (now + timedelta(hours=1)).isoformat(), "uid": "b1"}]
                },
            }
        )

        events = await injector._get_upcoming_events(["calendar.a", "calendar.b"], now, end)
        assert len(events) == 2
        assert events[0]["summary"] == "B"  # sorted by start time
        assert events[1]["summary"] == "A"
        assert events[0]["_calendar_entity_id"] == "calendar.b"

        with patch(
            "app.agents.calendar_injector.CalendarEntitySettingsRepository.get_enabled_entity_ids",
            new_callable=AsyncMock,
            return_value=["calendar.a"],
        ):
            filtered = await injector._filter_enabled(["calendar.a", "calendar.b"])
        assert filtered == ["calendar.a"]

        # _get_enabled_calendar_entities with async list_entries_async
        entry_a = MagicMock()
        entry_a.entity_id = "calendar.a"
        entry_a.friendly_name = "Calendar A"
        entry_b = MagicMock()
        entry_b.entity_id = "calendar.b"
        entry_b.friendly_name = "Calendar B"
        entity_index.list_entries_async = AsyncMock(return_value=[entry_a, entry_b])

        with (
            patch(
                "app.agents.calendar_injector.CalendarEntitySettingsRepository.get",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_get,
            patch(
                "app.agents.calendar_injector.CalendarEntitySettingsRepository.upsert",
                new_callable=AsyncMock,
            ) as mock_upsert,
            patch(
                "app.agents.calendar_injector.CalendarEntitySettingsRepository.get_enabled_entity_ids",
                new_callable=AsyncMock,
                return_value=["calendar.a"],
            ),
        ):
            enabled = await injector._get_enabled_calendar_entities()

        assert enabled == ["calendar.a"]
        mock_get.assert_awaited()
        mock_upsert.assert_awaited()
