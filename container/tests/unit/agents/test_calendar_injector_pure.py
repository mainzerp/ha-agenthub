"""Unit tests for CalendarReminderInjector pure helper methods.

Tests synchronous helper methods directly without mocking external dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from app.agents.calendar_injector import CalendarReminderInjector


class TestParseEventStart:
    def test_parse_event_start_all_formats(self):
        injector = CalendarReminderInjector(ha_client=None, entity_index=None)

        # ISO datetime with Z
        result = injector._parse_event_start("2026-06-08T12:00:00Z")
        assert result is not None
        assert result == datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)

        # ISO datetime with offset
        result = injector._parse_event_start("2026-06-08T12:00:00+02:00")
        assert result is not None
        assert result == datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))

        # dict with dateTime
        result = injector._parse_event_start({"dateTime": "2026-06-08T10:30:00Z"})
        assert result is not None
        assert result == datetime(2026, 6, 8, 10, 30, 0, tzinfo=UTC)

        # dict with date only (all-day event): must be aware (local midnight,
        # UTC by default) so it compares against an aware ``now``.
        result = injector._parse_event_start({"date": "2026-06-08"})
        assert result is not None
        assert result == datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)
        assert result < datetime.now(UTC) + timedelta(days=36500)

        # plain date string, interpreted in the given local timezone
        tz = timezone(timedelta(hours=2))
        result = injector._parse_event_start("2026-06-08", tz)
        assert result is not None
        assert result == datetime(2026, 6, 8, 0, 0, 0, tzinfo=tz)

        # naive datetime string gets the local timezone
        result = injector._parse_event_start("2026-06-08T09:00:00", tz)
        assert result == datetime(2026, 6, 8, 9, 0, 0, tzinfo=tz)

        # None / falsy input
        assert injector._parse_event_start(None) is None
        assert injector._parse_event_start("") is None
        assert injector._parse_event_start(0) is None

        # Invalid format
        assert injector._parse_event_start("not-a-date") is None
        assert injector._parse_event_start({"dateTime": ""}) is None


class TestMarkerActive:
    def test_marker_active_all_cases(self):
        injector = CalendarReminderInjector(ha_client=None, entity_index=None)

        now = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)

        # Event is in the future but within the offset window
        event_start = now + timedelta(minutes=10)
        assert injector._marker_active(event_start, now, 15) is True

        # Event is exactly at the offset boundary
        event_start = now + timedelta(minutes=15)
        assert injector._marker_active(event_start, now, 15) is True

        # Event is past the offset window
        event_start = now + timedelta(minutes=16)
        assert injector._marker_active(event_start, now, 15) is False

        # Event is in the past (should not fire)
        event_start = now - timedelta(minutes=5)
        assert injector._marker_active(event_start, now, 15) is False

        # Event is exactly now (should not fire -- strictly greater than 0)
        assert injector._marker_active(now, now, 15) is False

        # Large offset window
        event_start = now + timedelta(minutes=30)
        assert injector._marker_active(event_start, now, 60) is True

        # Event is far in the future but within 1440-minute window
        event_start = now + timedelta(hours=12)
        assert injector._marker_active(event_start, now, 1440) is True
        assert injector._marker_active(event_start, now, 60) is False


class TestClosestActiveOffset:
    def test_only_closest_offset_is_selected(self):
        injector = CalendarReminderInjector(ha_client=None, entity_index=None)
        now = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        offsets = [15, 60, 1440]

        # 10 minutes ahead: 15, 60 and 1440 are all "active" -- only 15 applies.
        assert injector._closest_active_offset(now + timedelta(minutes=10), now, offsets) == 15
        # 45 minutes ahead: 60 and 1440 active -- only 60 applies.
        assert injector._closest_active_offset(now + timedelta(minutes=45), now, offsets) == 60
        # 5 hours ahead: only 1440.
        assert injector._closest_active_offset(now + timedelta(hours=5), now, offsets) == 1440
        # Beyond every window.
        assert injector._closest_active_offset(now + timedelta(days=2), now, offsets) is None

    def test_all_day_events_skip_minute_offsets(self):
        injector = CalendarReminderInjector(ha_client=None, entity_index=None)
        now = datetime(2026, 6, 8, 23, 50, 0, tzinfo=UTC)
        start = datetime(2026, 6, 9, 0, 0, 0, tzinfo=UTC)
        assert injector._closest_active_offset(start, now, [15, 60, 1440], all_day=True) == 1440
        assert injector._closest_active_offset(start, now, [15, 60], all_day=True) is None

    def test_is_all_day(self):
        assert CalendarReminderInjector._is_all_day("2026-06-08") is True
        assert CalendarReminderInjector._is_all_day({"date": "2026-06-08"}) is True
        assert CalendarReminderInjector._is_all_day("2026-06-08T09:00:00+02:00") is False
        assert CalendarReminderInjector._is_all_day({"dateTime": "2026-06-08T09:00:00Z"}) is False


class TestFallbackAndGenerateReminderText:
    def test_day_phrase_uses_today_when_same_day(self):
        injector = CalendarReminderInjector(ha_client=None, entity_index=None)
        now = datetime(2026, 6, 8, 8, 0, 0, tzinfo=UTC)
        start = datetime(2026, 6, 8, 20, 0, 0, tzinfo=UTC)
        assert injector._fallback_reminder_text("Party", 1440, start, "en", now=now) == (
            "By the way: Party is today at 20:00."
        )
        assert injector._fallback_reminder_text("Holiday", 1440, start, "en", now=now, all_day=True) == (
            "By the way: Holiday is today."
        )

    def test_fallback_and_generate_reminder_text(self):
        injector = CalendarReminderInjector(ha_client=None, entity_index=None)

        event_start = datetime(2026, 6, 8, 14, 30, 0, tzinfo=UTC)

        # English fallbacks
        assert "Dentist appointment is in 15 minutes." in injector._fallback_reminder_text(
            "Dentist appointment", 15, event_start, "en"
        )
        assert "Meeting is in one hour." in injector._fallback_reminder_text("Meeting", 60, event_start, "en")
        assert "Party is tomorrow at 14:30." in injector._fallback_reminder_text("Party", 1440, event_start, "en")
        assert "Custom event is in 30 minutes." in injector._fallback_reminder_text(
            "Custom event", 30, event_start, "en"
        )

        # German fallbacks
        assert "Dentist appointment ist in 15 Minuten." in injector._fallback_reminder_text(
            "Dentist appointment", 15, event_start, "de"
        )
        assert "Meeting ist in einer Stunde." in injector._fallback_reminder_text("Meeting", 60, event_start, "de")
        assert "Party ist morgen um 14:30." in injector._fallback_reminder_text("Party", 1440, event_start, "de")
        assert "Custom event ist in 30 Minuten." in injector._fallback_reminder_text(
            "Custom event", 30, event_start, "de"
        )

        # Unknown language falls back to English
        assert "Meeting is in one hour." in injector._fallback_reminder_text("Meeting", 60, event_start, "fr")

        # Language with region code (e.g., en-US, de-DE)
        assert "Dentist appointment is in 15 minutes." in injector._fallback_reminder_text(
            "Dentist appointment", 15, event_start, "en-US"
        )
        assert "Meeting ist in einer Stunde." in injector._fallback_reminder_text("Meeting", 60, event_start, "de-DE")
