"""Scheduler regressions: overdue recovery, snooze, pause/resume, weekly grid, bootstrap order."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.timer_scheduler import TimerScheduler, _compute_next_recurring_fire_epoch, _load_recurrence
from app.db.repository import ScheduledTimersRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _make_scheduler() -> tuple[TimerScheduler, MagicMock]:
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value={})
    return TimerScheduler(ScheduledTimersRepository, dispatcher=dispatcher), dispatcher


def _event(dispatcher: MagicMock, index: int = -1):
    request = dispatcher.dispatch.await_args_list[index].args[0]
    return request.params["task"].context.background_event


async def _insert_row(row_id: str, *, kind: str, fires_at: int, payload: dict, name: str | None = None) -> None:
    await ScheduledTimersRepository.insert(
        id=row_id,
        logical_name=name or row_id,
        kind=kind,
        created_at=int(time.time()) - 7200,
        fires_at=fires_at,
        duration_seconds=60,
        origin_device_id="device-1",
        origin_area="kitchen",
        payload_json=json.dumps(payload),
    )


class TestOverdueRecoveryPolicy:
    async def test_start_does_not_wait_for_overdue_processing(self, db_repository):
        now = int(time.time())
        await _insert_row("overdue-quick", kind="notification", fires_at=now - 5, payload={"notification_message": "x"})
        release = asyncio.Event()

        async def _slow_dispatch(_request):
            await release.wait()
            return {}

        sched, dispatcher = _make_scheduler()
        dispatcher.dispatch = AsyncMock(side_effect=_slow_dispatch)
        try:
            await asyncio.wait_for(sched.start(), timeout=1.0)
            assert (await ScheduledTimersRepository.get("overdue-quick"))["state"] == "pending"
            release.set()
            await sched.wait_for_overdue_processing()
            assert (await ScheduledTimersRepository.get("overdue-quick"))["state"] == "fired"
        finally:
            release.set()
            await sched.stop()

    async def test_long_overdue_timers_and_alarms_are_reported_once_as_missed(self, db_repository):
        now = int(time.time())
        await _insert_row("missed-1", kind="plain", fires_at=now - 3600, payload={"language": "de"}, name="pasta")
        await _insert_row(
            "missed-2", kind="notification", fires_at=now - 1800, payload={"notification_message": "x"}, name="oven"
        )
        await _insert_row(
            "missed-3",
            kind="alarm",
            fires_at=now - 600,
            payload={"alarm_label": "Wake", "language": "fr", "timezone": "Europe/Paris"},
            name="Wake",
        )
        sched, dispatcher = _make_scheduler()
        try:
            await sched.start()
            await sched.wait_for_overdue_processing()
            dispatcher.dispatch.assert_awaited_once()
            event = _event(dispatcher)
            assert event.event_type == "timer_notification"
            assert [item["name"] for item in event.payload["missed"]] == ["pasta", "oven", "Wake"]
            assert [item["kind"] for item in event.payload["missed"]] == ["timer", "timer", "alarm"]
            # The most recent missed row provides origin and language.
            assert event.payload["language"] == "fr"
            assert event.payload["origin_area"] == "kitchen"
            for row_id in ("missed-1", "missed-2", "missed-3"):
                assert (await ScheduledTimersRepository.get(row_id))["state"] == "expired"
        finally:
            await sched.stop()

    async def test_missed_recurring_alarm_keeps_its_series(self, db_repository):
        now = int(time.time())
        await _insert_row(
            "missed-recurring",
            kind="alarm",
            fires_at=now - 7200,
            payload={
                "alarm_label": "Daily",
                "recurrence": {"freq": "daily", "interval": 1, "anchor_time": "07:00:00", "timezone": "UTC"},
            },
            name="Daily",
        )
        sched, dispatcher = _make_scheduler()
        try:
            await sched.start()
            await sched.wait_for_overdue_processing()
            assert (await ScheduledTimersRepository.get("missed-recurring"))["state"] == "expired"
            pending = await ScheduledTimersRepository.list_pending_for(logical_name="Daily", kinds={"alarm"})
            assert len(pending) == 1
            assert pending[0]["fires_at"] > now
            assert _event(dispatcher).payload["missed"][0]["kind"] == "alarm"
        finally:
            await sched.stop()

    async def test_device_action_more_than_five_minutes_overdue_is_dropped(self, db_repository):
        now = int(time.time())
        await _insert_row(
            "late-action",
            kind="delayed_action",
            fires_at=now - 600,
            payload={"target_entity": "light.kitchen", "target_action": "light/turn_off"},
        )
        await _insert_row(
            "recent-action",
            kind="delayed_action",
            fires_at=now - 120,
            payload={"target_entity": "light.hall", "target_action": "light/turn_off"},
        )
        sched, dispatcher = _make_scheduler()
        try:
            await sched.start()
            await sched.wait_for_overdue_processing()
            assert (await ScheduledTimersRepository.get("late-action"))["state"] == "expired"
            assert (await ScheduledTimersRepository.get("recent-action"))["state"] == "fired"
            dispatcher.dispatch.assert_awaited_once()
            event = _event(dispatcher)
            assert event.event_type == "delayed_action"
            assert event.payload["target_entity"] == "light.hall"
        finally:
            await sched.stop()


class TestSnoozeAndPause:
    async def test_legacy_snooze_row_rings_without_scheduling_a_second_timer(self, db_repository):
        sched, dispatcher = _make_scheduler()
        try:
            timer_id = await sched.schedule(
                logical_name="nap", kind="snooze", duration_seconds=0, payload={"snooze_seconds": 300}
            )
            for _ in range(50):
                await asyncio.sleep(0)
                row = await ScheduledTimersRepository.get(timer_id)
                if row and row["state"] == "fired":
                    break
            dispatcher.dispatch.assert_awaited_once()
            assert _event(dispatcher).event_type == "timer_notification"
            assert await ScheduledTimersRepository.list_pending() == []
        finally:
            await sched.stop()

    async def test_pause_keeps_remaining_time_and_resume_restarts(self, db_repository):
        sched, _dispatcher = _make_scheduler()
        try:
            timer_id = await sched.schedule(logical_name="tea", kind="plain", duration_seconds=600)
            remaining = await sched.pause(timer_id)
            assert remaining is not None and 595 <= remaining <= 600
            assert timer_id not in sched._tasks
            row = await ScheduledTimersRepository.get(timer_id)
            assert row["state"] == "paused"
            assert json.loads(row["payload_json"])["paused_remaining_seconds"] == remaining
            assert await sched.pause(timer_id) is None

            # A paused timer is not rehydrated on restart.
            await sched.stop()
            await sched.start()
            assert timer_id not in sched._tasks

            assert await sched.resume(timer_id) == remaining
            row = await ScheduledTimersRepository.get(timer_id)
            assert row["state"] == "pending"
            assert "paused_remaining_seconds" not in json.loads(row["payload_json"])
            assert abs(row["fires_at"] - (int(time.time()) + remaining)) <= 2
            assert timer_id in sched._tasks
        finally:
            await sched.stop()

    async def test_cancel_by_name_respects_kinds(self, db_repository):
        sched, _dispatcher = _make_scheduler()
        try:
            alarm_id = await sched.schedule(logical_name="wake", kind="alarm", duration_seconds=3600)
            plain_id = await sched.schedule(logical_name="wake", kind="plain", duration_seconds=3600)
            assert await sched.cancel(logical_name="wake", kinds={"plain", "notification"}) == 1
            assert (await ScheduledTimersRepository.get(alarm_id))["state"] == "pending"
            assert (await ScheduledTimersRepository.get(plain_id))["state"] == "cancelled"
        finally:
            await sched.stop()


def _weekly_mo_we_every_two_weeks(**extra) -> dict:
    recurrence = {
        "freq": "weekly",
        "interval": 2,
        "byweekday": ["MO", "WE"],
        "anchor_time": "07:00:00",
        "timezone": "UTC",
        **extra,
    }
    loaded = _load_recurrence({"recurrence": recurrence})
    assert loaded is not None
    return loaded


class TestWeeklyRecurrenceGrid:
    async def test_interval_two_with_several_weekdays_skips_the_off_week(self):
        recurrence = _weekly_mo_we_every_two_weeks(anchor_week="2026-10-05")
        monday = int(datetime(2026, 10, 5, 7, 0, tzinfo=UTC).timestamp())
        wednesday = int(datetime(2026, 10, 7, 7, 0, tzinfo=UTC).timestamp())
        assert _compute_next_recurring_fire_epoch({"fires_at": monday}, recurrence, monday + 60) == wednesday
        # From Wednesday the next occurrence is the Monday two weeks after the anchor, not the next Monday.
        expected = int(datetime(2026, 10, 19, 7, 0, tzinfo=UTC).timestamp())
        assert _compute_next_recurring_fire_epoch({"fires_at": wednesday}, recurrence, wednesday + 60) == expected

    async def test_legacy_payload_without_anchor_week_uses_the_current_week(self):
        recurrence = _weekly_mo_we_every_two_weeks()
        wednesday = int(datetime(2026, 10, 7, 7, 0, tzinfo=UTC).timestamp())
        expected = int(datetime(2026, 10, 19, 7, 0, tzinfo=UTC).timestamp())
        assert _compute_next_recurring_fire_epoch({"fires_at": wednesday}, recurrence, wednesday + 60) == expected


class TestBootstrapOrder:
    async def test_scheduler_is_published_before_start(self):
        from app.bootstrap._monitors import setup_monitors

        app = SimpleNamespace(state=SimpleNamespace(alarm_monitor=object()))
        seen: dict[str, bool] = {}

        async def _fake_start(self):
            seen["published"] = getattr(app.state, "timer_scheduler", None) is self

        with patch("app.agents.timer_scheduler.TimerScheduler.start", _fake_start):
            await setup_monitors(app, "test", MagicMock(), MagicMock())
        assert seen["published"] is True
