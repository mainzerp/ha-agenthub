"""Error-path tests for app.agents.timer_executor."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.timer_executor import (
    _build_recurring_alarm_payload,
    _build_timer_service_data,
    _extract_cancel_alarm_selectors,
    _filter_alarm_rows_by_schedule,
    _format_alarm_time_local,
    _format_duration_human,
    _get_timezone_info,
    _handle_read_action,
    _normalize_alarm_name,
    _normalize_timer_name,
    _parse_alarm_target_epoch,
    _parse_duration_seconds,
    _supports_method,
    execute_timer_action,
)

pytestmark = pytest.mark.asyncio


class TestSchedulerUnavailable:
    async def test_dispatch_scheduler_none_returns_503(self):
        """_get_scheduler returning None yields unavailable message."""
        with patch("app.agents.timer_executor._get_scheduler", return_value=None):
            result = await execute_timer_action(
                {
                    "action": "start_timer",
                    "entity": "",
                    "parameters": {"duration": "00:05:00"},
                },
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "unavailable" in result["speech"].lower()


class TestStartTimerErrors:
    async def test_set_timer_invalid_duration_non_numeric(self):
        result = await execute_timer_action(
            {"action": "start_timer", "entity": "", "parameters": {"duration": "abc"}},
            AsyncMock(),
            None,
            None,
            agent_id="timer-agent",
        )
        assert result["success"] is False
        assert "Duration is required" in result["speech"]

    async def test_set_timer_invalid_duration_negative(self):
        result = await execute_timer_action(
            {"action": "start_timer", "entity": "", "parameters": {"duration": "-300"}},
            AsyncMock(),
            None,
            None,
            agent_id="timer-agent",
        )
        assert result["success"] is False
        assert "Duration is required" in result["speech"]


class TestCancelTimerErrors:
    async def test_set_timer_missing_entity_id(self):
        """cancel_timer rejects when entity_query is empty."""
        scheduler = MagicMock()
        scheduler.list = AsyncMock(return_value=[])
        scheduler.cancel = AsyncMock(return_value=0)
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "cancel_timer", "entity": "", "parameters": {}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "specify which timer" in result["speech"].lower()


class TestDispatchUnknownAction:
    async def test_dispatch_unknown_action(self):
        result = await execute_timer_action(
            {"action": "bogus_action", "entity": "timer"},
            AsyncMock(),
            None,
            None,
            agent_id="timer-agent",
        )
        assert result["success"] is False
        assert "Unknown timer action" in result["speech"]


class TestParseAlarmTargetEpoch:
    async def test_parse_alarm_target_epoch_invalid_format(self):
        now_ts = int(datetime(2026, 6, 8, 10, 0, 0, tzinfo=UTC).timestamp())
        epoch, error = _parse_alarm_target_epoch(
            {"datetime": "not-a-date"},
            now_ts=now_ts,
            timezone=None,
        )
        assert epoch is None
        assert "Invalid datetime format" in error

    async def test_parse_alarm_target_epoch_timezone_edge(self):
        """Ambiguous/invalid timezone falls back gracefully."""
        now_ts = int(datetime(2026, 6, 8, 10, 0, 0, tzinfo=UTC).timestamp())
        epoch, error = _parse_alarm_target_epoch(
            {"time": "12:00:00"},
            now_ts=now_ts,
            timezone="NotA/RealZone",
        )
        assert epoch is not None
        assert error is None


class TestBuildRecurringAlarmPayload:
    async def test_build_recurring_alarm_payload_daily(self):
        target_epoch = int(datetime(2026, 6, 8, 10, 0, 0, tzinfo=UTC).timestamp())
        payload, error = _build_recurring_alarm_payload(
            {"recurrence": {"freq": "daily", "interval": 2}},
            target_epoch=target_epoch,
            timezone="UTC",
        )
        assert error is None
        assert payload["freq"] == "daily"
        assert payload["interval"] == 2
        assert payload["anchor_time"] == "10:00:00"
        assert payload["timezone"] == "UTC"

    async def test_build_recurring_alarm_payload_weekly(self):
        target_epoch = int(datetime(2026, 6, 8, 10, 0, 0, tzinfo=UTC).timestamp())
        payload, error = _build_recurring_alarm_payload(
            {"recurrence": {"freq": "weekly", "byweekday": ["MO", "WE"]}},
            target_epoch=target_epoch,
            timezone="UTC",
        )
        assert error is None
        assert payload["freq"] == "weekly"
        assert payload["byweekday"] == ["MO", "WE"]


class TestCancelAlarmErrors:
    async def test_cancel_alarm_no_match(self):
        """cancel_alarm reports not_found when no alarm matches by id."""
        scheduler = MagicMock()
        scheduler.list = AsyncMock(return_value=[])
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {
                    "action": "cancel_alarm",
                    "entity": "",
                    "parameters": {"id": "nonexistent"},
                },
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert result["metadata"]["status"] == "not_found"
        assert "No pending internal alarm" in result["speech"]

    async def test_cancel_alarm_multiple_matches(self):
        """cancel_alarm reports ambiguity when multiple alarms match by datetime."""
        scheduler = MagicMock()
        scheduler.list = AsyncMock(
            return_value=[
                {
                    "id": "a1",
                    "logical_name": "Alarm",
                    "fires_at": int(datetime(2026, 4, 26, 14, 35, 0).timestamp()),
                    "origin_area": None,
                },
                {
                    "id": "a2",
                    "logical_name": "Alarm",
                    "fires_at": int(datetime(2026, 4, 26, 14, 35, 0).timestamp()),
                    "origin_area": None,
                },
            ]
        )
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {
                    "action": "cancel_alarm",
                    "entity": "",
                    "parameters": {"datetime": "2026-04-26 14:35:00"},
                },
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert result["metadata"]["status"] == "ambiguous"
        assert len(result["metadata"]["candidates"]) == 2


class TestHelperFunctions:
    def test_format_duration_human(self):
        assert _format_duration_human(0) == "0 seconds"
        assert _format_duration_human(1) == "1 second"
        assert _format_duration_human(2) == "2 seconds"
        assert _format_duration_human(60) == "1 minute"
        assert _format_duration_human(61) == "1 minute and 1 second"
        assert _format_duration_human(120) == "2 minutes"
        assert _format_duration_human(3661) == "1 hour, 1 minute, and 1 second"

    def test_parse_duration_seconds(self):
        assert _parse_duration_seconds("") is None
        assert _parse_duration_seconds("abc") is None
        assert _parse_duration_seconds("00:05:00") == 300
        assert _parse_duration_seconds("05:00") == 300
        assert _parse_duration_seconds("300") == 300
        assert _parse_duration_seconds("300.5") == 300

    def test_normalize_timer_name(self):
        assert _normalize_timer_name("Kitchen Timer") == "kitchentimer"
        assert _normalize_timer_name("Ein-Minuten-Timer") == "1mintimer"
        assert _normalize_timer_name("Zwei Stunden") == "2h"

    def test_normalize_alarm_name(self):
        assert _normalize_alarm_name("Morning_Alarm") == "morning alarm"
        assert _normalize_alarm_name("  Wake-Up  ") == "wake up"

    def test_get_timezone_info_and_format_alarm_time_local(self):
        tz = _get_timezone_info("UTC")
        assert tz is not None
        assert _get_timezone_info("") is None
        assert _get_timezone_info("NotA/Zone") is None
        epoch = int(datetime(2026, 6, 8, 10, 30, 0, tzinfo=UTC).timestamp())
        assert _format_alarm_time_local(epoch, timezone="UTC") == "2026-06-08 10:30:00"
        local_formatted = _format_alarm_time_local(epoch, timezone=None)
        assert "2026-06-08" in local_formatted
        assert ":30:00" in local_formatted

    def test_extract_cancel_alarm_selectors(self):
        action = {
            "parameters": {
                "id": "timer1",
                "datetime": "2026-06-08T10:30:00",
                "time": "10:30",
                "date": "2026-06-08",
                "name": "Morning",
            },
            "entity": "Alarm",
        }
        selectors, error = _extract_cancel_alarm_selectors(action)
        assert error is None
        assert selectors["id"] == "timer1"
        assert selectors["datetime"] == "2026-06-08 10:30:00"
        assert selectors["time"] == "10:30:00"
        assert selectors["date"] == "2026-06-08"
        assert selectors["name"] == "Morning"

    def test_extract_cancel_alarm_selectors_invalid_datetime(self):
        selectors, error = _extract_cancel_alarm_selectors({"parameters": {"datetime": "bad"}, "entity": ""})
        assert selectors == {}
        assert "Invalid datetime format" in error

    def test_extract_cancel_alarm_selectors_invalid_time(self):
        selectors, error = _extract_cancel_alarm_selectors({"parameters": {"time": "bad"}, "entity": ""})
        assert selectors == {}
        assert "Invalid time format" in error

    def test_extract_cancel_alarm_selectors_invalid_date(self):
        selectors, error = _extract_cancel_alarm_selectors({"parameters": {"date": "bad"}, "entity": ""})
        assert selectors == {}
        assert "Invalid date format" in error

    def test_filter_alarm_rows_by_schedule_datetime(self):
        rows = [
            {"fires_at": int(datetime(2026, 6, 8, 10, 30, 0, tzinfo=UTC).timestamp())},
            {"fires_at": int(datetime(2026, 6, 8, 11, 0, 0, tzinfo=UTC).timestamp())},
        ]
        matches = _filter_alarm_rows_by_schedule(rows, target_datetime="2026-06-08 10:30:00", timezone="UTC")
        assert len(matches) == 1

    def test_filter_alarm_rows_by_schedule_time_and_date(self):
        rows = [
            {"fires_at": int(datetime(2026, 6, 8, 10, 30, 0, tzinfo=UTC).timestamp())},
            {"fires_at": int(datetime(2026, 6, 9, 10, 30, 0, tzinfo=UTC).timestamp())},
        ]
        matches = _filter_alarm_rows_by_schedule(rows, target_time="10:30:00", target_date="2026-06-08", timezone="UTC")
        assert len(matches) == 1

    def test_build_timer_service_data(self):
        assert _build_timer_service_data(
            {"parameters": {"duration": "5min", "datetime": "2026-06-08 10:00", "time": "10:00", "date": "2026-06-08"}}
        ) == {
            "duration": "5min",
            "datetime": "2026-06-08 10:00",
            "time": "10:00",
            "date": "2026-06-08",
        }
        assert _build_timer_service_data({}) == {}

    def test_supports_method(self):
        class Foo:
            def bar(self):
                pass

        assert _supports_method(Foo(), "bar") is True
        assert _supports_method(Foo(), "baz") is False
        assert _supports_method(None, "bar") is False

        class Spec:
            def qux(self):
                pass

        class MockObj:
            _spec_class = Spec

            def qux(self):
                pass

        assert _supports_method(MockObj(), "qux") is True
        assert _supports_method(MockObj(), "nope") is False


class TestHandleReadAction:
    async def test_handle_read_action_unknown(self):
        result = await _handle_read_action("bogus_read", "", AsyncMock(), None, None, "timer-agent")
        assert result["success"] is False
        assert "Unknown read action" in result["speech"]


class TestQueryTimer:
    async def test_query_timer_scheduler_none(self):
        with patch("app.agents.timer_executor._get_scheduler", return_value=None):
            result = await execute_timer_action(
                {"action": "query_timer", "entity": "Kitchen", "parameters": {}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "unavailable" in result["speech"].lower()


class TestListAlarms:
    async def test_list_alarms_scheduler_none(self):
        with patch("app.agents.timer_executor._get_scheduler", return_value=None):
            result = await execute_timer_action(
                {"action": "list_alarms", "entity": "", "parameters": {}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "unavailable" in result["speech"].lower()

    async def test_list_alarms_empty_rows(self):
        scheduler = MagicMock()
        scheduler.list = AsyncMock(return_value=[])
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "list_alarms", "entity": "", "parameters": {}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is True
        assert "No internal alarms" in result["speech"]


class TestSetAlarmErrors:
    async def test_set_alarm_invalid_datetime(self):
        scheduler = MagicMock()
        scheduler.schedule = AsyncMock(return_value="alarm-id")
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "set_datetime", "entity": "", "parameters": {"datetime": "bad-date"}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "Invalid datetime format" in result["speech"]


class TestCancelAlarmBranches:
    async def test_cancel_alarm_no_selectors(self):
        scheduler = MagicMock()
        scheduler.list = AsyncMock(return_value=[])
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "cancel_alarm", "entity": "", "parameters": {}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "Please provide an alarm id" in result["speech"]

    async def test_cancel_alarm_name_no_match(self):
        scheduler = MagicMock()
        scheduler.list = AsyncMock(return_value=[])
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "cancel_alarm", "entity": "", "parameters": {"name": "Missing"}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "not_found" in result["metadata"]["status"]
        assert "No pending internal alarm named 'Missing'" in result["speech"]

    async def test_cancel_alarm_name_multiple_matches(self):
        scheduler = MagicMock()
        scheduler.list = AsyncMock(
            return_value=[
                {
                    "id": "a1",
                    "logical_name": "Morning",
                    "fires_at": int(datetime(2026, 6, 8, 10, 0, 0, tzinfo=UTC).timestamp()),
                    "origin_area": None,
                },
                {
                    "id": "a2",
                    "logical_name": "Morning",
                    "fires_at": int(datetime(2026, 6, 8, 11, 0, 0, tzinfo=UTC).timestamp()),
                    "origin_area": None,
                },
            ]
        )
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "cancel_alarm", "entity": "", "parameters": {"name": "Morning"}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "ambiguous" in result["metadata"]["status"]
        assert len(result["metadata"]["candidates"]) == 2

    async def test_cancel_alarm_name_single_match(self):
        scheduler = MagicMock()
        scheduler.cancel = AsyncMock(return_value=None)
        scheduler.list = AsyncMock(
            return_value=[
                {
                    "id": "a1",
                    "logical_name": "Morning",
                    "fires_at": int(datetime(2026, 6, 8, 10, 0, 0, tzinfo=UTC).timestamp()),
                    "origin_area": None,
                },
            ]
        )
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "cancel_alarm", "entity": "", "parameters": {"name": "Morning"}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is True
        assert "cancelled" in result["metadata"]["status"]
        scheduler.cancel.assert_awaited_once_with(id_="a1")

    async def test_cancel_alarm_scheduler_none(self):
        with patch("app.agents.timer_executor._get_scheduler", return_value=None):
            result = await execute_timer_action(
                {"action": "cancel_alarm", "entity": "", "parameters": {}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "unavailable" in result["speech"].lower()

    async def test_cancel_alarm_invalid_selectors(self):
        scheduler = MagicMock()
        scheduler.list = AsyncMock(return_value=[])
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "cancel_alarm", "entity": "", "parameters": {"datetime": "bad-date"}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "Invalid datetime format" in result["speech"]


class TestStartTimerWithNotification:
    async def test_start_timer_with_notification_scheduler_none(self):
        with patch("app.agents.timer_executor._get_scheduler", return_value=None):
            result = await execute_timer_action(
                {
                    "action": "start_timer_with_notification",
                    "entity": "",
                    "parameters": {"duration": "00:05:00", "notification_message": "Done"},
                },
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "unavailable" in result["speech"].lower()


class TestDelayedAction:
    async def test_delayed_action_scheduler_none(self):
        with patch("app.agents.timer_executor._get_scheduler", return_value=None):
            result = await execute_timer_action(
                {
                    "action": "delayed_action",
                    "entity": "",
                    "parameters": {
                        "delay_duration": "00:05:00",
                        "target_entity": "light.kitchen",
                        "target_action": "light/turn_off",
                    },
                },
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "unavailable" in result["speech"].lower()


class TestSleepTimer:
    async def test_sleep_timer_scheduler_none(self):
        with patch("app.agents.timer_executor._get_scheduler", return_value=None):
            result = await execute_timer_action(
                {
                    "action": "sleep_timer",
                    "entity": "",
                    "parameters": {"duration": "00:05:00", "media_player": "media_player.bedroom"},
                },
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "unavailable" in result["speech"].lower()


class TestPauseOrResume:
    async def test_resume_timer(self):
        scheduler = MagicMock()
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "resume_timer", "entity": "Kitchen", "parameters": {}},
                AsyncMock(),
                None,
                None,
                agent_id="timer-agent",
            )
        assert result["success"] is False
        assert "Resume is not supported" in result["speech"]
