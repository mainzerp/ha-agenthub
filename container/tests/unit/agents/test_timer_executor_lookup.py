"""Timer/alarm executor regressions: alarm dates, recurrence start, lookup, snooze, pause, delayed actions."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.agents.timer_executor import _parse_alarm_target_epoch, execute_timer_action
from app.agents.timer_scheduler import TimerScheduler
from app.db.repository import ScheduledTimersRepository

pytestmark = pytest.mark.asyncio

BERLIN = "Europe/Berlin"


def _epoch(year, month, day, hour, minute, tz=BERLIN) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp())


def _local(epoch: int, tz=BERLIN) -> datetime:
    return datetime.fromtimestamp(epoch, tz=ZoneInfo(tz))


class TestAlarmDate:
    async def test_tomorrow_with_time_is_not_today(self):
        """'Wake me tomorrow at 7' said at 06:00 rings tomorrow, not in one hour."""
        now = _epoch(2026, 10, 10, 6, 0)
        epoch, error = _parse_alarm_target_epoch({"time": "07:00:00", "date": "tomorrow"}, now_ts=now, timezone=BERLIN)
        assert error is None
        assert _local(epoch) == datetime(2026, 10, 11, 7, 0, tzinfo=ZoneInfo(BERLIN))

    async def test_iso_date_with_time_is_combined(self):
        now = _epoch(2026, 10, 10, 6, 0)
        epoch, error = _parse_alarm_target_epoch({"time": "06:30", "date": "2026-10-14"}, now_ts=now, timezone=BERLIN)
        assert error is None
        assert _local(epoch) == datetime(2026, 10, 14, 6, 30, tzinfo=ZoneInfo(BERLIN))

    async def test_past_explicit_date_and_time_is_rejected(self):
        now = _epoch(2026, 10, 10, 8, 0)
        epoch, error = _parse_alarm_target_epoch({"time": "07:00", "date": "today"}, now_ts=now, timezone=BERLIN)
        assert epoch is None
        assert "future" in error

    async def test_relative_day_offset(self):
        now = _epoch(2026, 10, 10, 22, 0)
        epoch, error = _parse_alarm_target_epoch({"time": "07:00", "date": "+2"}, now_ts=now, timezone=BERLIN)
        assert error is None
        assert _local(epoch).date().isoformat() == "2026-10-12"

    async def test_weekday_code_picks_next_such_day(self):
        # 2026-10-10 is a Saturday.
        now = _epoch(2026, 10, 10, 9, 0)
        epoch, error = _parse_alarm_target_epoch({"time": "06:30", "date": "MO"}, now_ts=now, timezone=BERLIN)
        assert error is None
        assert _local(epoch) == datetime(2026, 10, 12, 6, 30, tzinfo=ZoneInfo(BERLIN))

    async def test_weekday_code_for_today_after_the_time_rolls_a_week(self):
        now = _epoch(2026, 10, 10, 9, 0)  # Saturday 09:00
        epoch, error = _parse_alarm_target_epoch({"time": "07:00", "date": "SA"}, now_ts=now, timezone=BERLIN)
        assert error is None
        assert _local(epoch).date().isoformat() == "2026-10-17"

    async def test_tomorrow_across_spring_dst_keeps_wall_clock(self):
        # Berlin switches to CEST on 2026-03-29: the night is one hour shorter.
        now = _epoch(2026, 3, 28, 6, 0)
        epoch, error = _parse_alarm_target_epoch({"time": "07:00", "date": "tomorrow"}, now_ts=now, timezone=BERLIN)
        assert error is None
        assert _local(epoch) == datetime(2026, 3, 29, 7, 0, tzinfo=ZoneInfo(BERLIN))
        assert epoch - now == 24 * 3600

    async def test_time_only_across_autumn_dst_keeps_wall_clock(self):
        # Berlin switches back to CET on 2026-10-25: the night is one hour longer.
        now = _epoch(2026, 10, 24, 23, 0)
        epoch, error = _parse_alarm_target_epoch({"time": "07:00"}, now_ts=now, timezone=BERLIN)
        assert error is None
        assert _local(epoch) == datetime(2026, 10, 25, 7, 0, tzinfo=ZoneInfo(BERLIN))
        assert epoch - now == 9 * 3600

    async def test_invalid_date_token_is_rejected(self):
        epoch, error = _parse_alarm_target_epoch(
            {"time": "07:00", "date": "someday"}, now_ts=int(time.time()), timezone=BERLIN
        )
        assert epoch is None
        assert "Invalid date" in error


@pytest.fixture
async def scheduler(db_repository):
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value={})
    sched = TimerScheduler(ScheduledTimersRepository, dispatcher=dispatcher)
    with patch("app.agents.timer_executor._helpers._get_scheduler", return_value=sched):
        yield sched
    await sched.stop()


async def _run(action: dict, **kwargs) -> dict:
    return await execute_timer_action(action, AsyncMock(), MagicMock(), MagicMock(), agent_id="timer-agent", **kwargs)


class TestWeeklyAlarmFirstOccurrence:
    async def test_weekly_alarm_set_on_friday_first_rings_on_a_listed_weekday(self, scheduler):
        # Friday 2026-10-09 10:00 Berlin; Mon-Wed 06:00 must not ring on Saturday.
        friday = _epoch(2026, 10, 9, 10, 0)
        with patch("app.agents.timer_executor._alarms.time.time", return_value=friday):
            result = await _run(
                {
                    "action": "set_datetime",
                    "entity": "alarm",
                    "parameters": {
                        "time": "06:00:00",
                        "recurrence": {"freq": "weekly", "byweekday": ["MO", "TU", "WE"]},
                    },
                },
                timezone=BERLIN,
            )
        assert result["success"] is True
        first = _local(result["metadata"]["fires_at"])
        assert first == datetime(2026, 10, 12, 6, 0, tzinfo=ZoneInfo(BERLIN))
        assert result["metadata"]["recurrence"]["anchor_week"] == "2026-10-05"


class TestTimerLookup:
    async def test_generic_reference_cancels_the_only_running_timer(self, scheduler):
        await _run({"action": "start_timer", "entity": "3-minute timer", "parameters": {"duration": "00:03:00"}})
        result = await _run({"action": "cancel_timer", "entity": "timer", "parameters": {}})
        assert result["success"] is True
        assert await scheduler.list() == []

    async def test_empty_reference_extends_the_only_running_timer(self, scheduler):
        await _run({"action": "start_timer", "entity": "pasta", "parameters": {"duration": "00:10:00"}})
        result = await _run({"action": "extend_timer", "entity": "", "parameters": {"duration": "00:05:00"}})
        assert result["success"] is True
        (row,) = await scheduler.list()
        assert row["fires_at"] - int(time.time()) >= 14 * 60

    async def test_extend_never_moves_an_alarm(self, scheduler):
        await scheduler.schedule(logical_name="timer", kind="alarm", duration_seconds=3600)
        result = await _run({"action": "extend_timer", "entity": "timer", "parameters": {"duration": "00:05:00"}})
        assert result["success"] is False
        (alarm,) = await scheduler.list(kinds={"alarm"})
        assert alarm["fires_at"] - int(time.time()) <= 3600

    async def test_generic_reference_with_two_timers_asks(self, scheduler):
        await _run({"action": "start_timer", "entity": "pasta", "parameters": {"duration": "00:10:00"}})
        await _run({"action": "start_timer", "entity": "eggs", "parameters": {"duration": "00:05:00"}})
        result = await _run({"action": "cancel_timer", "entity": "timer", "parameters": {}})
        assert result["success"] is False
        assert result["metadata"]["status"] == "ambiguous"
        assert len(await scheduler.list()) == 2

    async def test_named_reference_does_not_match_another_timer(self, scheduler):
        await _run({"action": "start_timer", "entity": "egg timer", "parameters": {"duration": "00:05:00"}})
        result = await _run({"action": "cancel_timer", "entity": "pasta timer", "parameters": {}})
        assert result["success"] is False
        assert len(await scheduler.list()) == 1

    async def test_timer_from_another_room_is_found(self, scheduler):
        await _run(
            {"action": "start_timer", "entity": "pasta", "parameters": {"duration": "00:10:00"}}, area_id="kitchen"
        )
        query = await _run({"action": "query_timer", "entity": "pasta", "parameters": {}}, area_id="living_room")
        assert query["success"] is True
        listing = await _run({"action": "list_timers", "entity": "", "parameters": {}}, area_id="living_room")
        assert "pasta" in listing["speech"]
        cancel = await _run({"action": "cancel_timer", "entity": "pasta", "parameters": {}}, area_id="living_room")
        assert cancel["success"] is True

    async def test_room_scoped_match_wins_over_other_rooms(self, scheduler):
        await _run({"action": "start_timer", "entity": "tea", "parameters": {"duration": "00:10:00"}}, area_id="a")
        await _run({"action": "start_timer", "entity": "tea", "parameters": {"duration": "00:20:00"}}, area_id="b")
        result = await _run(
            {"action": "extend_timer", "entity": "tea", "parameters": {"duration": "00:01:00"}}, area_id="a"
        )
        assert result["success"] is True
        rows = await scheduler.list(area="b")
        assert rows[0]["fires_at"] - int(time.time()) <= 20 * 60

    async def test_list_timers_excludes_alarms(self, scheduler):
        await scheduler.schedule(logical_name="wake", kind="alarm", duration_seconds=3600)
        result = await _run({"action": "list_timers", "entity": "", "parameters": {}})
        assert result["speech"] == "No timers are currently running."

    async def test_cancel_alarm_from_another_room_by_generic_name(self, scheduler):
        await scheduler.schedule(logical_name="morning alarm", kind="alarm", duration_seconds=3600, origin_area="bed")
        result = await _run({"action": "cancel_alarm", "entity": "alarm", "parameters": {}}, area_id="kitchen")
        assert result["success"] is True
        assert await scheduler.list(kinds={"alarm"}) == []

    async def test_short_timer_label_is_not_zero_minutes(self, scheduler):
        result = await _run({"action": "start_timer", "entity": "", "parameters": {"duration": "00:00:30"}})
        assert result["success"] is True
        (row,) = await scheduler.list()
        assert row["logical_name"] == "30 seconds timer"


class TestSnooze:
    async def test_snooze_keeps_recurring_alarm_series_and_rings_once(self, scheduler):
        alarm_id = await scheduler.schedule(
            logical_name="wake",
            kind="alarm",
            duration_seconds=86400,
            payload={"recurrence": {"freq": "daily", "interval": 1, "anchor_time": "07:00:00"}},
        )
        result = await _run({"action": "snooze_timer", "entity": "wake", "parameters": {"duration": "00:05:00"}})
        assert result["success"] is True
        assert (await ScheduledTimersRepository.get(alarm_id))["state"] == "pending"
        (snooze,) = await scheduler.list(kinds={"plain", "snooze"})
        assert snooze["kind"] == "plain"
        assert 295 <= snooze["fires_at"] - int(time.time()) <= 300


class TestPauseResume:
    async def test_pause_then_resume_through_the_executor(self, scheduler):
        await _run({"action": "start_timer", "entity": "bread", "parameters": {"duration": "00:20:00"}})
        paused = await _run({"action": "pause_timer", "entity": "bread", "parameters": {}})
        assert paused["success"] is True
        assert paused["new_state"] == "paused"
        assert "remaining" in paused["speech"]
        assert await scheduler.list() == []
        listing = await _run({"action": "list_timers", "entity": "", "parameters": {}})
        assert "paused" in listing["speech"]

        resumed = await _run({"action": "resume_timer", "entity": "timer", "parameters": {}})
        assert resumed["success"] is True
        (row,) = await scheduler.list()
        assert 19 * 60 <= row["fires_at"] - int(time.time()) <= 20 * 60

    async def test_cancel_reaches_a_paused_timer(self, scheduler):
        await _run({"action": "start_timer", "entity": "bread", "parameters": {"duration": "00:20:00"}})
        await _run({"action": "pause_timer", "entity": "bread", "parameters": {}})
        result = await _run({"action": "cancel_timer", "entity": "bread", "parameters": {}})
        assert result["success"] is True
        assert await scheduler.list(states={"paused", "pending"}) == []


class TestDelayedActionPolicy:
    @pytest.mark.parametrize(
        "target_action",
        ["lock/unlock", "alarm_control_panel/alarm_disarm", "script/reload", "light/set_level"],
    )
    async def test_disallowed_services_are_rejected(self, target_action):
        sched = MagicMock()
        sched.schedule = AsyncMock()
        with patch("app.agents.timer_executor._helpers._get_scheduler", return_value=sched):
            result = await _run(
                {
                    "action": "delayed_action",
                    "entity": "later",
                    "parameters": {
                        "delay_duration": "00:10:00",
                        "target_entity": "front door",
                        "target_action": target_action,
                    },
                }
            )
        assert result["success"] is False
        sched.schedule.assert_not_called()

    async def test_visibility_is_checked_for_the_owning_agent(self):
        sched = MagicMock()
        sched.schedule = AsyncMock(return_value="t1")
        resolved = {"entity_id": "lock.front_door", "friendly_name": "Front Door", "resolution": {}}
        with (
            patch("app.agents.timer_executor._helpers._get_scheduler", return_value=sched),
            patch(
                "app.agents.timer_executor._timers.resolve_and_validate_entity",
                new=AsyncMock(return_value=resolved),
            ) as mock_resolve,
        ):
            result = await _run(
                {
                    "action": "delayed_action",
                    "entity": "lock later",
                    "parameters": {
                        "delay_duration": "00:10:00",
                        "target_entity": "front door",
                        "target_action": "lock/lock",
                    },
                }
            )
        assert result["success"] is True
        assert mock_resolve.await_args.args[3] == "security-agent"
        assert mock_resolve.await_args.args[4] == frozenset({"lock"})
        assert sched.schedule.await_args.kwargs["payload"]["agent_id"] == "security-agent"


async def test_alarm_epoch_helper_uses_utc_when_timezone_missing():
    now = int(datetime(2026, 10, 10, 6, 0, tzinfo=UTC).timestamp())
    epoch, error = _parse_alarm_target_epoch({"time": "07:00", "date": "2026-10-11"}, now_ts=now, timezone=None)
    assert error is None
    assert epoch > now
