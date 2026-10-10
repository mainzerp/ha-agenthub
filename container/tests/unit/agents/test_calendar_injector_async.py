"""Async tests for app.agents.calendar_injector."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.calendar_injector import CalendarReminderInjector

pytestmark = pytest.mark.asyncio


def _patch_common(
    stack: ExitStack,
    *,
    offsets: str = "[15]",
    calendars: str = '["calendar.work"]',
    has_fired: AsyncMock | None = None,
) -> AsyncMock:
    """Patch settings, user resolution and DB state; return the mark_fired mock."""
    stack.enter_context(
        patch(
            "app.agents.calendar_injector.SettingsRepository.get_value",
            new_callable=AsyncMock,
            side_effect=lambda key, default=None: {
                "calendar.reminder_injection.enabled": "true",
                "calendar.reminder_injection.lookahead_hours": "24",
            }.get(key, default),
        )
    )
    stack.enter_context(
        patch(
            "app.agents.calendar_injector.UserIdentityResolver.resolve_user",
            new_callable=AsyncMock,
            return_value={
                "id": 42,
                "calendar_entity_ids_json": calendars,
                "reminder_offsets_json": offsets,
            },
        )
    )
    stack.enter_context(
        patch(
            "app.agents.calendar_injector.CalendarEntitySettingsRepository.get_universal_entity_ids",
            new_callable=AsyncMock,
            return_value=[],
        )
    )
    stack.enter_context(
        patch(
            "app.agents.calendar_injector.CalendarEntitySettingsRepository.get_enabled_entity_ids",
            new_callable=AsyncMock,
            return_value=["calendar.work", "calendar.home"],
        )
    )
    stack.enter_context(
        patch(
            "app.agents.calendar_injector.CalendarReminderStateRepository.has_fired",
            new=has_fired or AsyncMock(return_value=False),
        )
    )
    stack.enter_context(
        patch.object(CalendarReminderInjector, "_home_timezone", new=AsyncMock(return_value=UTC)),
    )
    return stack.enter_context(
        patch(
            "app.agents.calendar_injector.CalendarReminderStateRepository.mark_fired",
            new_callable=AsyncMock,
        )
    )


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
        """Real client shape (a list per entity), offset active, mark_fired called."""
        ha_client = AsyncMock()
        injector = CalendarReminderInjector(ha_client, MagicMock())

        event_start = datetime.now(UTC) + timedelta(minutes=10)
        # HARestClient.get_calendar_events returns the event list of one entity.
        ha_client.get_calendar_events = AsyncMock(
            return_value=[{"summary": "Meeting", "start": event_start.isoformat(), "uid": "evt-1"}]
        )

        with ExitStack() as stack:
            mock_mark = _patch_common(stack)
            result = await injector.inject_reminders("hello")

        assert result is not None
        assert "Meeting" in result
        mock_mark.assert_awaited_once_with("evt-1", "calendar.work", 42, 15)

    async def test_event_without_uid_fires_keyed_on_summary_and_start(self):
        """HA ``calendar.get_events`` returns no uid; the reminder must still fire."""
        ha_client = AsyncMock()
        injector = CalendarReminderInjector(ha_client, MagicMock())

        event_start = datetime.now(UTC) + timedelta(minutes=10)
        ha_client.get_calendar_events = AsyncMock(
            return_value=[{"summary": "Dentist", "start": event_start.isoformat(), "end": event_start.isoformat()}]
        )

        with ExitStack() as stack:
            mock_mark = _patch_common(stack)
            result = await injector.inject_reminders("hello")

        assert result is not None
        assert "Dentist" in result
        mock_mark.assert_awaited_once()
        key = mock_mark.await_args.args[0]
        assert key.startswith("sum:")

        # The key is stable across turns for the same event.
        with ExitStack() as stack:
            mock_mark_2 = _patch_common(stack)
            await injector.inject_reminders("hello again")
        assert mock_mark_2.await_args.args[0] == key

    async def test_only_closest_offset_fires_once(self):
        """An event 10 minutes away fires the 15-minute reminder only."""
        ha_client = AsyncMock()
        injector = CalendarReminderInjector(ha_client, MagicMock())

        event_start = datetime.now(UTC) + timedelta(minutes=10)
        ha_client.get_calendar_events = AsyncMock(
            return_value=[{"summary": "Standup", "start": event_start.isoformat()}]
        )

        with ExitStack() as stack:
            mock_mark = _patch_common(stack, offsets="[15, 60, 1440]")
            result = await injector.inject_reminders("hello")

        assert result is not None
        assert result.count("Standup") == 1
        mock_mark.assert_awaited_once()
        assert mock_mark.await_args.args[3] == 15

    async def test_closest_offset_already_fired_does_not_fall_back_to_larger(self):
        ha_client = AsyncMock()
        injector = CalendarReminderInjector(ha_client, MagicMock())

        event_start = datetime.now(UTC) + timedelta(minutes=10)
        ha_client.get_calendar_events = AsyncMock(
            return_value=[{"summary": "Standup", "start": event_start.isoformat()}]
        )

        with ExitStack() as stack:
            mock_mark = _patch_common(
                stack,
                offsets="[15, 60, 1440]",
                has_fired=AsyncMock(side_effect=lambda _k, _c, _u, offset: offset == 15),
            )
            result = await injector.inject_reminders("hello")

        assert result is None
        mock_mark.assert_not_awaited()

    async def test_all_day_event_does_not_raise(self):
        """Date-only starts must not raise TypeError against an aware now."""
        ha_client = AsyncMock()
        injector = CalendarReminderInjector(ha_client, MagicMock())

        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
        ha_client.get_calendar_events = AsyncMock(
            return_value=[{"summary": "Holiday", "start": tomorrow, "end": tomorrow}]
        )

        with ExitStack() as stack:
            mock_mark = _patch_common(stack, offsets="[15, 60, 1440]")
            result = await injector.inject_reminders("hello")

        assert result is not None
        assert "Holiday" in result
        mock_mark.assert_awaited_once()
        assert mock_mark.await_args.args[3] == 1440

    async def test_internal_failure_never_raises(self):
        ha_client = AsyncMock()
        injector = CalendarReminderInjector(ha_client, MagicMock())
        with patch(
            "app.agents.calendar_injector.SettingsRepository.get_value",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            result = await injector.inject_reminders("hello")
        assert result is None


class TestGetUpcomingEventsAndFilter:
    async def test_get_upcoming_events_and_filter_enabled(self):
        """_get_upcoming_events fetches and sorts; _filter_enabled intersects; _get_enabled_calendar_entities queries DB."""
        ha_client = AsyncMock()
        entity_index = MagicMock()
        injector = CalendarReminderInjector(ha_client, entity_index)

        now = datetime.now(UTC)
        end = now + timedelta(hours=24)

        per_entity = {
            "calendar.a": [{"summary": "A", "start": (now + timedelta(hours=2)).isoformat()}],
            "calendar.b": [{"summary": "B", "start": (now + timedelta(hours=1)).isoformat()}],
        }
        ha_client.get_calendar_events = AsyncMock(side_effect=lambda eid, _s, _e: per_entity[eid])

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

    async def test_mixed_all_day_and_timed_events_sort(self):
        ha_client = AsyncMock()
        injector = CalendarReminderInjector(ha_client, MagicMock())
        now = datetime.now(UTC)
        today = now.date().isoformat()
        ha_client.get_calendar_events = AsyncMock(
            return_value=[
                {"summary": "Timed", "start": (now + timedelta(hours=3)).isoformat()},
                {"summary": "AllDay", "start": today},
            ]
        )
        events = await injector._get_upcoming_events(["calendar.a"], now, now + timedelta(hours=24))
        assert [e["summary"] for e in events] == ["AllDay", "Timed"]
