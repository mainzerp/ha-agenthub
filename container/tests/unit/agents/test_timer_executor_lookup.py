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
        assert result["metadata"]["recurrence"]["anchor_week"] == "2026-10-12"


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


# ---------------------------------------------------------------------------
# Unnamed timers stay distinct (FIX-11)
# ---------------------------------------------------------------------------

_GENERIC_TIMER_REFERENCES = ["", "timer", "Timer"]


async def _pending_timers(scheduler) -> list[dict]:
    return await scheduler.list(kinds={"plain", "notification", "snooze"}, states={"pending", "paused"})


class TestUnnamedTimersStayDistinct:
    @pytest.mark.parametrize("generic", _GENERIC_TIMER_REFERENCES)
    async def test_unnamed_timers_get_duration_labels_not_the_kind_word(self, scheduler, generic):
        await _run({"action": "start_timer", "entity": generic, "parameters": {"duration": "00:10:00"}})
        await _run(
            {"action": "start_timer_with_notification", "entity": generic, "parameters": {"duration": "00:20:00"}}
        )
        names = sorted(row["logical_name"] for row in await _pending_timers(scheduler))
        assert names == ["10 minutes timer", "20 minutes timer"]

    @pytest.mark.parametrize("generic", _GENERIC_TIMER_REFERENCES)
    async def test_cancel_with_two_unnamed_timers_asks_and_cancels_nothing(self, scheduler, generic):
        await _run({"action": "start_timer", "entity": generic, "parameters": {"duration": "00:10:00"}})
        await _run({"action": "start_timer", "entity": generic, "parameters": {"duration": "00:20:00"}})
        result = await _run({"action": "cancel_timer", "entity": generic, "parameters": {}})
        assert result["success"] is False
        assert result["metadata"]["status"] == "ambiguous"
        assert result["voice_followup"] is True
        assert result["speech"].endswith("Which one should I cancel?")
        assert "10 minutes timer with" in result["speech"]
        assert "20 minutes timer with" in result["speech"]
        assert len(await _pending_timers(scheduler)) == 2

    async def test_two_unnamed_timers_of_the_same_duration_are_not_cancelled_together(self, scheduler):
        await _run({"action": "start_timer", "entity": "", "parameters": {"duration": "00:05:00"}})
        await _run({"action": "start_timer", "entity": "Timer", "parameters": {"duration": "00:05:00"}})
        result = await _run({"action": "cancel_timer", "entity": "5-minute timer", "parameters": {}})
        assert result["success"] is False
        assert result["metadata"]["status"] == "ambiguous"
        assert result["speech"].endswith("?")
        assert len(await _pending_timers(scheduler)) == 2

    async def test_rows_sharing_a_user_given_name_are_still_cancelled_together(self, scheduler):
        await _run({"action": "start_timer", "entity": "pasta", "parameters": {"duration": "00:10:00"}})
        await _run({"action": "start_timer", "entity": "pasta", "parameters": {"duration": "00:12:00"}})
        result = await _run({"action": "cancel_timer", "entity": "pasta", "parameters": {}})
        assert result["success"] is True
        assert await _pending_timers(scheduler) == []

    @pytest.mark.parametrize(
        ("action", "parameters", "verb"),
        [
            ("pause_timer", {}, "pause"),
            ("finish_timer", {}, "finish"),
            ("extend_timer", {"duration": "00:01:00"}, "extend"),
        ],
    )
    async def test_pause_finish_extend_ask_with_distinguishable_descriptions(self, scheduler, action, parameters, verb):
        await _run({"action": "start_timer", "entity": "timer", "parameters": {"duration": "00:10:00"}})
        await _run({"action": "start_timer", "entity": "Timer", "parameters": {"duration": "00:20:00"}})
        result = await _run({"action": action, "entity": "timer", "parameters": parameters})
        assert result["success"] is False
        assert result["speech"].endswith(f"Which one should I {verb}?")
        assert "timer, timer" not in result["speech"]
        assert "10 minutes timer" in result["speech"] and "20 minutes timer" in result["speech"]
        rows = await _pending_timers(scheduler)
        assert len(rows) == 2
        assert all(row["state"] == "pending" for row in rows)

    @pytest.mark.parametrize("generic", _GENERIC_TIMER_REFERENCES)
    async def test_snooze_never_cancels_other_running_timers(self, scheduler, generic):
        await _run({"action": "start_timer", "entity": generic, "parameters": {"duration": "00:10:00"}})
        await _run({"action": "start_timer", "entity": generic, "parameters": {"duration": "00:20:00"}})
        result = await _run({"action": "snooze_timer", "entity": generic, "parameters": {"duration": "00:05:00"}})
        assert result["success"] is True
        assert result["speech"] == "Snoozed the timer for 5 minutes."
        names = sorted(row["logical_name"] for row in await _pending_timers(scheduler))
        assert names == ["10 minutes timer", "20 minutes timer", "snoozed timer"]

    async def test_snooze_of_a_named_timer_replaces_only_that_timer(self, scheduler):
        await _run({"action": "start_timer", "entity": "pasta", "parameters": {"duration": "00:10:00"}})
        await _run({"action": "start_timer", "entity": "", "parameters": {"duration": "00:20:00"}})
        result = await _run({"action": "snooze_timer", "entity": "pasta", "parameters": {"duration": "00:05:00"}})
        assert result["success"] is True
        rows = await _pending_timers(scheduler)
        assert sorted(row["logical_name"] for row in rows) == ["20 minutes timer", "pasta"]
        pasta = next(row for row in rows if row["logical_name"] == "pasta")
        assert pasta["fires_at"] - int(time.time()) <= 300


class TestLegacyTimerRowsNamedTimer:
    """Rows stored as "timer" before the fix behave as unnamed timers."""

    async def _legacy(self, scheduler, seconds: int) -> str:
        hms = f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
        return await scheduler.schedule(
            logical_name="timer", kind="plain", duration_seconds=seconds, payload={"duration": hms}
        )

    async def test_cancel_asks_with_duration_labels(self, scheduler):
        await self._legacy(scheduler, 600)
        await self._legacy(scheduler, 1200)
        result = await _run({"action": "cancel_timer", "entity": "timer", "parameters": {}})
        assert result["metadata"]["status"] == "ambiguous"
        assert result["metadata"]["candidates"] == ["10 minutes timer", "20 minutes timer"]
        assert len(await _pending_timers(scheduler)) == 2

    async def test_snooze_keeps_legacy_rows(self, scheduler):
        await self._legacy(scheduler, 600)
        await self._legacy(scheduler, 1200)
        await _run({"action": "snooze_timer", "entity": "timer", "parameters": {}})
        assert len(await _pending_timers(scheduler)) == 3

    async def test_duration_reference_picks_one_legacy_row(self, scheduler):
        await self._legacy(scheduler, 600)
        twenty = await self._legacy(scheduler, 1200)
        by_selector = await _run({"action": "cancel_timer", "entity": "", "parameters": {"timer_duration": "00:20:00"}})
        assert by_selector["success"] is True
        assert by_selector["speech"] == "Cancelled 20 minutes timer."
        assert (await ScheduledTimersRepository.get(twenty))["state"] == "cancelled"
        by_name = await _run({"action": "cancel_timer", "entity": "Zehn-Minuten-Timer", "parameters": {}})
        assert by_name["success"] is True
        assert await _pending_timers(scheduler) == []

    async def test_single_legacy_row_is_listed_by_its_duration_label(self, scheduler):
        await self._legacy(scheduler, 600)
        listing = await _run({"action": "list_timers", "entity": "", "parameters": {}})
        assert listing["speech"].startswith("Active: 10 minutes timer (")


class TestDurationReferences:
    @pytest.mark.parametrize(
        "reference",
        ["Fünf-Minuten-Timer", "5-minute timer", "5-Minuten-Timer", "5 minute timer", "fünf Minuten Timer"],
    )
    async def test_duration_reference_resolves_the_default_labelled_timer(self, scheduler, reference):
        await _run({"action": "start_timer", "entity": "", "parameters": {"duration": "00:05:00"}})
        await _run({"action": "start_timer", "entity": "pasta", "parameters": {"duration": "00:10:00"}})
        result = await _run({"action": "cancel_timer", "entity": reference, "parameters": {}})
        assert result["success"] is True
        assert result["speech"] == "Cancelled 5 minutes timer."
        assert [row["logical_name"] for row in await _pending_timers(scheduler)] == ["pasta"]

    async def test_german_one_minute_reference_resolves(self, scheduler):
        await _run({"action": "start_timer", "entity": "", "parameters": {"duration": "00:01:00"}})
        result = await _run({"action": "pause_timer", "entity": "Eine-Minute-Timer", "parameters": {}})
        assert result["success"] is True
        assert result["new_state"] == "paused"

    @pytest.mark.parametrize(
        ("entity", "duration", "expected"),
        [
            ("Fünf-Minuten-Timer", "00:05:00", "5 minutes timer"),
            ("3-minute timer", "00:03:00", "3 minutes timer"),
            ("Tea", "00:05:00", "Tea"),
        ],
    )
    async def test_duration_only_name_is_stored_as_the_default_label(self, scheduler, entity, duration, expected):
        await _run({"action": "start_timer", "entity": entity, "parameters": {"duration": duration}})
        (row,) = await _pending_timers(scheduler)
        assert row["logical_name"] == expected

    async def test_timer_duration_selector_pauses_only_that_timer(self, scheduler):
        await _run({"action": "start_timer", "entity": "", "parameters": {"duration": "00:05:00"}})
        await _run({"action": "start_timer", "entity": "", "parameters": {"duration": "00:10:00"}})
        result = await _run({"action": "pause_timer", "entity": "", "parameters": {"timer_duration": "00:05:00"}})
        assert result["success"] is True
        states = {row["logical_name"]: row["state"] for row in await _pending_timers(scheduler)}
        assert states == {"5 minutes timer": "paused", "10 minutes timer": "pending"}

    async def test_timer_duration_selector_queries_only_that_timer(self, scheduler):
        await _run({"action": "start_timer", "entity": "", "parameters": {"duration": "00:05:00"}})
        await _run({"action": "start_timer", "entity": "", "parameters": {"duration": "00:10:00"}})
        result = await _run({"action": "query_timer", "entity": "", "parameters": {"timer_duration": "00:10:00"}})
        assert result["success"] is True
        assert result["speech"].startswith("10 minutes timer has")
        assert "5 minutes timer" not in result["speech"]

    async def test_timer_duration_selector_without_match_is_not_found(self, scheduler):
        await _run({"action": "start_timer", "entity": "", "parameters": {"duration": "00:05:00"}})
        result = await _run({"action": "cancel_timer", "entity": "", "parameters": {"timer_duration": "00:07:00"}})
        assert result["success"] is False
        assert result["speech"] == "No 7 minutes timer is running."
        assert len(await _pending_timers(scheduler)) == 1


# ---------------------------------------------------------------------------
# Alarm dates (FIX-11)
# ---------------------------------------------------------------------------

# Friday 2026-11-06 21:00 Europe/Berlin.
_FRIDAY_EVENING = _epoch(2026, 11, 6, 21, 0)


async def _alarm_at(scheduler, epoch: int, name: str = "alarm") -> str:
    return await scheduler.schedule(logical_name=name, kind="alarm", duration_seconds=epoch - _FRIDAY_EVENING)


class TestCancelAlarmDates:
    @pytest.mark.parametrize(
        ("entity", "parameters"),
        [
            ("alarm", {"time": "07:00:00", "date": "tomorrow"}),
            ("Wecker", {"time": "07:00", "date": "tomorrow"}),
            ("alarm", {"time": "07:00:00", "date": "+1"}),
            ("alarm", {"time": "07:00:00", "date": "SA"}),
        ],
    )
    async def test_relative_date_tokens_are_accepted(self, scheduler, entity, parameters):
        with patch("app.agents.timer_executor._alarms.time.time", return_value=_FRIDAY_EVENING):
            saturday = await _alarm_at(scheduler, _epoch(2026, 11, 7, 7, 0))
            sunday = await _alarm_at(scheduler, _epoch(2026, 11, 8, 7, 0))
            result = await _run({"action": "cancel_alarm", "entity": entity, "parameters": parameters}, timezone=BERLIN)
        assert result["success"] is True, result["speech"]
        assert (await ScheduledTimersRepository.get(saturday))["state"] == "cancelled"
        assert (await ScheduledTimersRepository.get(sunday))["state"] == "pending"

    @pytest.mark.parametrize(
        ("entity", "date"),
        [("alarm", "tomorrow"), ("Wecker", "tomorrow"), ("alarm", "2026-11-07"), ("", "SA")],
    )
    async def test_date_only_selector_cancels_only_that_day(self, scheduler, entity, date):
        with patch("app.agents.timer_executor._alarms.time.time", return_value=_FRIDAY_EVENING):
            sunday = await _alarm_at(scheduler, _epoch(2026, 11, 8, 7, 0))
            saturday = await _alarm_at(scheduler, _epoch(2026, 11, 7, 9, 30))
            result = await _run(
                {"action": "cancel_alarm", "entity": entity, "parameters": {"date": date}}, timezone=BERLIN
            )
        assert result["success"] is True, result["speech"]
        assert (await ScheduledTimersRepository.get(saturday))["state"] == "cancelled"
        assert (await ScheduledTimersRepository.get(sunday))["state"] == "pending"

    @pytest.mark.parametrize("entity", ["alarm", "Wecker"])
    async def test_date_only_selector_without_alarm_that_day_cancels_nothing(self, scheduler, entity):
        with patch("app.agents.timer_executor._alarms.time.time", return_value=_FRIDAY_EVENING):
            sunday = await _alarm_at(scheduler, _epoch(2026, 11, 8, 7, 0))
            result = await _run(
                {"action": "cancel_alarm", "entity": entity, "parameters": {"date": "tomorrow"}}, timezone=BERLIN
            )
        assert result["success"] is False
        assert result["metadata"]["status"] == "not_found"
        assert result["speech"] == "No pending internal alarm on 2026-11-07 was found."
        assert (await ScheduledTimersRepository.get(sunday))["state"] == "pending"

    async def test_date_only_selector_with_two_alarms_that_day_asks(self, scheduler):
        with patch("app.agents.timer_executor._alarms.time.time", return_value=_FRIDAY_EVENING):
            await _alarm_at(scheduler, _epoch(2026, 11, 7, 7, 0))
            await _alarm_at(scheduler, _epoch(2026, 11, 7, 9, 0))
            result = await _run(
                {"action": "cancel_alarm", "entity": "alarm", "parameters": {"date": "tomorrow"}}, timezone=BERLIN
            )
        assert result["success"] is False
        assert result["metadata"]["status"] == "ambiguous"
        assert result["voice_followup"] is True
        assert result["speech"].endswith("Which one should I cancel?")
        assert len(await scheduler.list(kinds={"alarm"})) == 2

    async def test_invalid_cancel_date_token_names_the_accepted_tokens(self, scheduler):
        result = await _run({"action": "cancel_alarm", "entity": "alarm", "parameters": {"date": "someday"}})
        assert result["success"] is False
        assert "tomorrow" in result["speech"]


class TestDateOnlyAlarmAsksForTime:
    @pytest.mark.parametrize(
        ("entity", "date"),
        [("alarm", "tomorrow"), ("Wecker", "tomorrow"), ("alarm", "2026-11-07"), ("Wecker", "SA")],
    )
    async def test_set_datetime_with_a_date_only_asks_for_the_time(self, scheduler, entity, date):
        with patch("app.agents.timer_executor._alarms.time.time", return_value=_FRIDAY_EVENING):
            result = await _run(
                {"action": "set_datetime", "entity": entity, "parameters": {"date": date}}, timezone=BERLIN
            )
        assert result["success"] is False
        assert result["voice_followup"] is True
        assert result["metadata"]["status"] == "needs_time"
        assert result["speech"] == "At what time should the alarm ring on 2026-11-07?"
        assert await scheduler.list(kinds={"alarm"}) == []

    async def test_epoch_helper_never_defaults_a_date_to_midnight(self):
        epoch, error = _parse_alarm_target_epoch({"date": "tomorrow"}, now_ts=_FRIDAY_EVENING, timezone=BERLIN)
        assert epoch is None
        assert "time is required" in error

    async def test_invalid_date_only_token_is_still_rejected(self, scheduler):
        result = await _run({"action": "set_datetime", "entity": "alarm", "parameters": {"date": "someday"}})
        assert result["success"] is False
        assert "Invalid date" in result["speech"]


class TestWeeklyIntervalFirstOccurrence:
    @pytest.mark.parametrize(
        ("now", "expected_day"),
        [
            (_epoch(2026, 11, 4, 10, 0), 9),  # Wednesday
            (_epoch(2026, 11, 5, 10, 0), 9),  # Thursday
            (_epoch(2026, 11, 7, 10, 0), 9),  # Saturday
            (_epoch(2026, 11, 8, 10, 0), 9),  # Sunday
            (_epoch(2026, 11, 9, 5, 0), 9),  # Monday before 07:00
            (_epoch(2026, 11, 9, 8, 0), 16),  # Monday after 07:00
        ],
    )
    async def test_every_other_monday_first_rings_on_the_nearest_monday(self, scheduler, now, expected_day):
        from app.agents.timer_scheduler import _compute_next_recurring_fire_epoch, _load_recurrence

        with patch("app.agents.timer_executor._alarms.time.time", return_value=now):
            result = await _run(
                {
                    "action": "set_datetime",
                    "entity": "garbage",
                    "parameters": {
                        "time": "07:00:00",
                        "recurrence": {"freq": "weekly", "interval": 2, "byweekday": ["MO"]},
                    },
                },
                timezone=BERLIN,
            )
        assert result["success"] is True
        first = result["metadata"]["fires_at"]
        assert _local(first) == datetime(2026, 11, expected_day, 7, 0, tzinfo=ZoneInfo(BERLIN))
        recurrence = result["metadata"]["recurrence"]
        assert recurrence["anchor_week"] == f"2026-11-{expected_day:02d}"
        loaded = _load_recurrence({"recurrence": recurrence})
        following = _compute_next_recurring_fire_epoch({"fires_at": first}, loaded, first)
        assert (_local(following).date() - _local(first).date()).days == 14

    async def test_two_weekdays_every_other_week_start_in_the_current_week(self, scheduler):
        from app.agents.timer_scheduler import _compute_next_recurring_fire_epoch, _load_recurrence

        wednesday = _epoch(2026, 11, 4, 10, 0)
        with patch("app.agents.timer_executor._alarms.time.time", return_value=wednesday):
            result = await _run(
                {
                    "action": "set_datetime",
                    "entity": "alarm",
                    "parameters": {
                        "time": "07:00:00",
                        "recurrence": {"freq": "weekly", "interval": 2, "byweekday": ["MO", "TH"]},
                    },
                },
                timezone=BERLIN,
            )
        first = result["metadata"]["fires_at"]
        assert _local(first) == datetime(2026, 11, 5, 7, 0, tzinfo=ZoneInfo(BERLIN))
        loaded = _load_recurrence({"recurrence": result["metadata"]["recurrence"]})
        following = _compute_next_recurring_fire_epoch({"fires_at": first}, loaded, first)
        assert _local(following).date().isoformat() == "2026-11-16"
