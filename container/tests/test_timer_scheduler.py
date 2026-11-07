"""Tests for ``app.agents.timer_scheduler.TimerScheduler`` (0.26.0).

The scheduler owns every non-native timer. These tests verify
persistence, fire dispatch per kind, cancellation, and restart
recovery. They do NOT exercise any HA ``timer.*`` helper -- the helper
pool is gone.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.agents.background_actions import handle_background_event
from app.agents.timer_scheduler import TimerScheduler
from app.db.repository import ScheduledTimersRepository
from app.ha_client.rest import HARestClient

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _dispatch_event_type(dispatcher_mock):
    """Extract the background event type from the most recent dispatch call."""
    req = dispatcher_mock.dispatch.await_args.args[0]
    return req.params["task"].context.background_event.event_type


def _dispatch_payload(dispatcher_mock):
    """Extract the background event payload from the most recent dispatch call."""
    req = dispatcher_mock.dispatch.await_args.args[0]
    return req.params["task"].context.background_event.payload


def _dispatch_args_list(dispatcher_mock):
    """Return all dispatch call payloads."""
    return [
        call_args.args[0].params["task"].context.background_event.payload
        for call_args in dispatcher_mock.dispatch.call_args_list
    ]


def _make_scheduler(*, gateway=None) -> tuple[TimerScheduler, MagicMock]:
    dispatcher = gateway or MagicMock()
    dispatcher.dispatch = AsyncMock()
    scheduler = TimerScheduler(
        ScheduledTimersRepository,
        dispatcher=dispatcher,
    )
    return scheduler, dispatcher


async def _wait_for_state(timer_id: str, state: str, *, timeout: float = 5.0) -> dict | None:
    """Poll the row in real time until it reaches ``state`` or ``timeout`` passes; return the last row read."""
    deadline = time.monotonic() + timeout
    while True:
        row = await ScheduledTimersRepository.get(timer_id)
        if (row and row["state"] == state) or time.monotonic() >= deadline:
            return row
        await asyncio.sleep(0.01)


class TestSchedulePersistence:
    async def test_schedule_persists_row(self, db_repository):
        sched, _gateway = _make_scheduler()
        try:
            timer_id = await sched.schedule(
                logical_name="egg timer",
                kind="notification",
                duration_seconds=60,
                origin_device_id="dev-1",
                origin_area="kitchen",
                payload={"notification_message": "Eggs done!", "language": "de"},
            )
            assert timer_id
            rows = await ScheduledTimersRepository.list_pending()
            assert len(rows) == 1
            row = rows[0]
            assert row["logical_name"] == "egg timer"
            assert row["kind"] == "notification"
            assert row["origin_area"] == "kitchen"
            assert json.loads(row["payload_json"])["notification_message"] == "Eggs done!"
            assert json.loads(row["payload_json"])["language"] == "de"
            assert row["state"] == "pending"
        finally:
            await sched.stop()


class TestFireOnDeadline:
    async def test_schedule_fires_on_deadline(self, db_repository):
        sched, gateway = _make_scheduler()
        try:
            timer_id = await sched.schedule(
                logical_name="quick",
                kind="notification",
                duration_seconds=0,
                payload={"notification_message": "boom"},
            )
            row = await _wait_for_state(timer_id, "fired")
            assert row["state"] == "fired"
            gateway.dispatch.assert_awaited_once()
            assert _dispatch_event_type(gateway) == "timer_notification"
        finally:
            await sched.stop()


class TestCancellation:
    async def test_cancel_by_id(self, db_repository):
        sched, _gateway = _make_scheduler()
        try:
            timer_id = await sched.schedule(
                logical_name="long",
                kind="notification",
                duration_seconds=3600,
                payload={"notification_message": "x"},
            )
            count = await sched.cancel(id_=timer_id)
            assert count == 1
            row = await ScheduledTimersRepository.get(timer_id)
            assert row["state"] == "cancelled"
        finally:
            await sched.stop()

    async def test_cancel_by_logical_name(self, db_repository):
        sched, _gateway = _make_scheduler()
        try:
            id1 = await sched.schedule(
                logical_name="dupe",
                kind="notification",
                duration_seconds=3600,
                payload={"notification_message": "a"},
            )
            id2 = await sched.schedule(
                logical_name="dupe",
                kind="notification",
                duration_seconds=3600,
                payload={"notification_message": "b"},
            )
            count = await sched.cancel(logical_name="dupe")
            assert count == 2
            for tid in (id1, id2):
                row = await ScheduledTimersRepository.get(tid)
                assert row["state"] == "cancelled"
        finally:
            await sched.stop()


class TestList:
    async def test_list_filters_by_area(self, db_repository):
        sched, _gateway = _make_scheduler()
        try:
            await sched.schedule(
                logical_name="a",
                kind="notification",
                duration_seconds=3600,
                origin_area="kitchen",
                payload={"notification_message": "x"},
            )
            await sched.schedule(
                logical_name="b",
                kind="notification",
                duration_seconds=3600,
                origin_area="bedroom",
                payload={"notification_message": "x"},
            )
            await sched.schedule(
                logical_name="c",
                kind="notification",
                duration_seconds=3600,
                origin_area="kitchen",
                payload={"notification_message": "x"},
            )
            kitchen = await sched.list(area="kitchen")
            assert {r["logical_name"] for r in kitchen} == {"a", "c"}
        finally:
            await sched.stop()

    async def test_list_filters_by_kind(self, db_repository):
        sched, _gateway = _make_scheduler()
        try:
            await sched.schedule(
                logical_name="kitchen alarm",
                kind="alarm",
                duration_seconds=3600,
                origin_area="kitchen",
                payload={"alarm_label": "Kitchen Alarm"},
            )
            await sched.schedule(
                logical_name="kitchen timer",
                kind="notification",
                duration_seconds=3600,
                origin_area="kitchen",
                payload={"notification_message": "done"},
            )

            alarms = await sched.list(area="kitchen", kinds={"alarm"})
            assert len(alarms) == 1
            assert alarms[0]["kind"] == "alarm"
            assert alarms[0]["logical_name"] == "kitchen alarm"
        finally:
            await sched.stop()


class TestReschedule:
    async def test_reschedule_updates_label(self, db_repository):
        sched, _gateway = _make_scheduler()
        try:
            timer_id = await sched.schedule(
                logical_name="original-name",
                kind="notification",
                duration_seconds=3600,
                payload={"notification_message": "x"},
            )

            updated = await sched.reschedule(timer_id, logical_name="updated-name")
            assert updated is True

            rows = await sched.list(logical_name="updated-name")
            assert any(row["id"] == timer_id for row in rows)
        finally:
            await sched.stop()

    async def test_reschedule_updates_fires_at(self, db_repository):
        sched, _gateway = _make_scheduler()
        try:
            timer_id = await sched.schedule(
                logical_name="reschedule-me",
                kind="notification",
                duration_seconds=120,
                payload={"notification_message": "x"},
            )
            before = await ScheduledTimersRepository.get(timer_id)
            assert before is not None

            new_fires_at = int(time.time()) + 7200
            updated = await sched.reschedule(timer_id, new_fires_at=new_fires_at)
            assert updated is True

            after = await ScheduledTimersRepository.get(timer_id)
            assert after is not None
            assert int(after["fires_at"]) == new_fires_at
            assert int(after["fires_at"]) != int(before["fires_at"])
        finally:
            await sched.stop()

    async def test_reschedule_unknown_id_returns_false(self, db_repository):
        sched, _gateway = _make_scheduler()
        try:
            updated = await sched.reschedule("nonexistent-id", logical_name="unused")
            assert updated is False
        finally:
            await sched.stop()

    async def test_reschedule_cancelled_row_returns_false(self, db_repository):
        sched, _gateway = _make_scheduler()
        try:
            timer_id = await sched.schedule(
                logical_name="cancel-then-reschedule",
                kind="notification",
                duration_seconds=3600,
                payload={"notification_message": "x"},
            )
            cancelled = await sched.cancel(id_=timer_id)
            assert cancelled == 1

            updated = await sched.reschedule(timer_id, logical_name="new-name")
            assert updated is False
        finally:
            await sched.stop()

    async def test_reschedule_alarm_updates_recurrence_payload(self, db_repository):
        sched, _gateway = _make_scheduler()
        try:
            timer_id = await sched.schedule(
                logical_name="wake-recur",
                kind="alarm",
                duration_seconds=900,
                briefing=True,
                payload={
                    "alarm_label": "wake-recur",
                    "scheduled_for_epoch": int(time.time()) + 900,
                    "briefing": True,
                    "recurrence": {
                        "freq": "daily",
                        "interval": 1,
                        "anchor_time": "07:30:00",
                        "timezone": "Europe/Berlin",
                    },
                },
            )

            updated = await sched.reschedule(
                timer_id,
                recurrence={
                    "freq": "weekly",
                    "interval": 2,
                    "byweekday": ["MO", "WE", "FR"],
                    "anchor_time": "07:30:00",
                    "timezone": "Europe/Berlin",
                },
            )
            assert updated is True

            row = await ScheduledTimersRepository.get(timer_id)
            assert row is not None
            payload = json.loads(row["payload_json"])
            assert payload["recurrence"]["freq"] == "weekly"
            assert payload["recurrence"]["interval"] == 2
            assert payload["recurrence"]["byweekday"] == ["MO", "WE", "FR"]
        finally:
            await sched.stop()


class TestRestartRecovery:
    async def test_restart_recovery(self, db_repository):
        # Scheduler #1: create a long-pending timer and stop without firing.
        s1, _gateway = _make_scheduler()
        timer_id = await s1.schedule(
            logical_name="rehydrate-me",
            kind="notification",
            duration_seconds=3600,
            payload={"notification_message": "x"},
        )
        await s1.stop()

        # Scheduler #2: must rehydrate the pending row.
        s2, _gateway2 = _make_scheduler()
        try:
            await s2.start()
            assert timer_id in s2._tasks
        finally:
            await s2.stop()

    async def test_overdue_timer_fires_on_recovery(self, db_repository):
        # Insert an already-overdue row directly (mimics a process that
        # crashed before its asyncio task could fire).
        await ScheduledTimersRepository.insert(
            id="overdue-id",
            logical_name="overdue",
            kind="notification",
            created_at=int(time.time()) - 100,
            fires_at=int(time.time()) - 10,
            duration_seconds=10,
            origin_device_id=None,
            origin_area=None,
            payload_json=json.dumps({"notification_message": "late"}),
        )
        sched, gateway = _make_scheduler()
        try:
            await sched.start()
            await sched.wait_for_overdue_processing()
            row = await ScheduledTimersRepository.get("overdue-id")
            assert row["state"] == "fired"
            gateway.dispatch.assert_awaited_once()
            assert _dispatch_event_type(gateway) == "timer_notification"
        finally:
            await sched.stop()

    async def test_startup_recovery_retry_rehydrates_after_transient_load_failure(self, db_repository):
        now = int(time.time())
        row = {
            "id": "retry-rehydrate-id",
            "logical_name": "rehydrate-later",
            "kind": "notification",
            "created_at": now,
            "fires_at": now + 3600,
            "duration_seconds": 3600,
            "origin_device_id": None,
            "origin_area": None,
            "payload_json": json.dumps({"notification_message": "x"}),
            "state": "pending",
        }
        repo = MagicMock()
        repo.list_pending = AsyncMock(side_effect=[RuntimeError("db unavailable"), [row]])
        repo.mark_fired = AsyncMock()

        sched, _gateway = _make_scheduler()
        sched._repo = repo
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("app.agents.timer_scheduler._STARTUP_RECOVERY_RETRY_DELAY_SECONDS", 0.01)
                await sched.start()
                for _ in range(500):
                    if "retry-rehydrate-id" in sched._tasks:
                        break
                    await asyncio.sleep(0.01)

            assert repo.list_pending.await_count >= 2
            assert "retry-rehydrate-id" in sched._tasks
            repo.mark_fired.assert_not_awaited()
        finally:
            await sched.stop()

    async def test_startup_recovery_retry_fires_overdue_pending_orphan(self, db_repository):
        now = int(time.time())
        row = {
            "id": "retry-overdue-id",
            "logical_name": "overdue-retry",
            "kind": "notification",
            "created_at": now - 120,
            "fires_at": now - 30,
            "duration_seconds": 10,
            "origin_device_id": None,
            "origin_area": None,
            "payload_json": json.dumps({"notification_message": "late"}),
            "state": "pending",
        }
        repo = MagicMock()
        repo.list_pending = AsyncMock(side_effect=[RuntimeError("db unavailable"), [row]])
        repo.mark_fired = AsyncMock()

        sched, gateway = _make_scheduler()
        sched._repo = repo
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("app.agents.timer_scheduler._STARTUP_RECOVERY_RETRY_DELAY_SECONDS", 0.01)
                await sched.start()
                for _ in range(500):
                    if repo.mark_fired.await_count >= 1:
                        break
                    await asyncio.sleep(0.01)

            repo.mark_fired.assert_awaited_once()
            fired_args = repo.mark_fired.await_args.args
            assert fired_args[0] == "retry-overdue-id"
            assert isinstance(fired_args[1], int)
            gateway.dispatch.assert_awaited_once()
            assert _dispatch_event_type(gateway) == "timer_notification"
        finally:
            await sched.stop()


class TestKindDispatch:
    async def test_plain_and_notification_dispatch_background_event(self, db_repository):
        sched, gateway = _make_scheduler()
        try:
            tid = await sched.schedule(
                logical_name="x",
                kind="plain",
                duration_seconds=0,
                origin_device_id="device-123",
                origin_area="kitchen",
                payload={"language": "de"},
            )
            await _wait_for_state(tid, "fired")
            gateway.dispatch.assert_awaited_once()
            assert _dispatch_event_type(gateway) == "timer_notification"
            payload = _dispatch_payload(gateway)
            assert payload["origin_device_id"] == "device-123"
            assert payload["origin_area"] == "kitchen"
            assert payload["language"] == "de"
        finally:
            await sched.stop()

    async def test_delayed_action_fires_target_service(self, db_repository):
        sched, gateway = _make_scheduler()
        try:
            tid = await sched.schedule(
                logical_name="lights off",
                kind="delayed_action",
                duration_seconds=0,
                origin_device_id="device-9",
                origin_area="kitchen",
                payload={
                    "target_entity": "light.kitchen",
                    "target_action": "light/turn_off",
                    "agent_id": "timer-agent",
                    "language": "de",
                },
            )
            await _wait_for_state(tid, "fired")
            gateway.dispatch.assert_awaited_once()
            assert _dispatch_event_type(gateway) == "delayed_action"
            payload = _dispatch_payload(gateway)
            assert payload["target_entity"] == "light.kitchen"
            # H-2: fire payload carries agent/origin metadata for the
            # fire-time recheck and failure announcement.
            assert payload["agent_id"] == "timer-agent"
            assert payload["origin_device_id"] == "device-9"
            assert payload["origin_area"] == "kitchen"
            assert payload["language"] == "de"
        finally:
            await sched.stop()

    async def test_sleep_kind_calls_media_stop(self, db_repository):
        sched, gateway = _make_scheduler()
        try:
            tid = await sched.schedule(
                logical_name="bedtime",
                kind="sleep",
                duration_seconds=0,
                origin_device_id="device-5",
                origin_area="bedroom",
                payload={"media_player": "media_player.bedroom", "agent_id": "timer-agent"},
            )
            await _wait_for_state(tid, "fired")
            gateway.dispatch.assert_awaited_once()
            assert _dispatch_event_type(gateway) == "sleep_media_stop"
            payload = _dispatch_payload(gateway)
            assert payload["media_player"] == "media_player.bedroom"
            assert payload["agent_id"] == "timer-agent"
            assert payload["origin_device_id"] == "device-5"
            assert payload["origin_area"] == "bedroom"
        finally:
            await sched.stop()

    async def test_alarm_kind_dispatches_alarm_notification_with_origin_metadata(self, db_repository):
        sched, gateway = _make_scheduler()
        try:
            tid = await sched.schedule(
                logical_name="Morning Alarm",
                kind="alarm",
                duration_seconds=0,
                origin_device_id="device-123",
                origin_area="bedroom",
                payload={
                    "alarm_label": "Morning Alarm",
                    "briefing": True,
                    "language": "de",
                    "media_player": "media_player.bedroom",
                    "scheduled_for_epoch": 123456,
                    "timezone": "Europe/Berlin",
                },
            )
            await _wait_for_state(tid, "fired")

            gateway.dispatch.assert_awaited_once()
            assert _dispatch_event_type(gateway) == "alarm_notification"
            payload = _dispatch_payload(gateway)
            assert payload["alarm_name"] == "Morning Alarm"
            assert payload["entity_id"].startswith("agenthub_alarm:")
            assert payload["origin_device_id"] == "device-123"
            assert payload["origin_area"] == "bedroom"
            assert payload["language"] == "de"
            assert payload["media_player"] == "media_player.bedroom"
            assert payload["briefing"] is True
            assert payload["scheduled_for_epoch"] == 123456
            assert payload["timezone"] == "Europe/Berlin"
        finally:
            await sched.stop()

    async def test_recurring_alarm_fire_schedules_next_occurrence_with_preserved_metadata(self, db_repository):
        sched, gateway = _make_scheduler()
        try:
            now = int(time.time())
            next_fire = now + 600
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.agents.timer_scheduler._compute_next_recurring_fire_epoch", lambda *_args, **_kwargs: next_fire
                )
                tid = await sched.schedule(
                    logical_name="Morning Alarm",
                    kind="alarm",
                    duration_seconds=0,
                    origin_device_id="device-123",
                    origin_area="bedroom",
                    payload={
                        "alarm_label": "Morning Alarm",
                        "briefing": True,
                        "language": "de",
                        "recurrence": {
                            "freq": "daily",
                            "interval": 1,
                            "anchor_time": "07:00:00",
                            "timezone": "Europe/Berlin",
                        },
                    },
                )

                row = await _wait_for_state(tid, "fired")

            row = await ScheduledTimersRepository.get(tid)
            assert row and row["state"] == "fired"
            pending = await ScheduledTimersRepository.list_pending_for(logical_name="Morning Alarm", kinds={"alarm"})
            assert len(pending) == 1
            next_row = pending[0]
            assert next_row["origin_device_id"] == "device-123"
            assert next_row["origin_area"] == "bedroom"
            assert next_row["briefing"] == 1

            next_payload = json.loads(next_row["payload_json"])
            assert next_payload["alarm_label"] == "Morning Alarm"
            assert next_payload["briefing"] is True
            assert next_payload["language"] == "de"
            assert next_payload["recurrence"]["freq"] == "daily"
            assert int(next_payload["scheduled_for_epoch"]) == next_fire

            gateway.dispatch.assert_awaited_once()
            assert _dispatch_event_type(gateway) == "alarm_notification"
        finally:
            await sched.stop()


class TestRecurringRecovery:
    async def test_overdue_recurring_alarm_recovery_schedules_exactly_one_next_pending(self, db_repository):
        now = int(time.time())
        await ScheduledTimersRepository.insert(
            id="overdue-recurring-id",
            logical_name="Recurring Wake",
            kind="alarm",
            created_at=now - 3600,
            fires_at=now - 10,
            duration_seconds=300,
            origin_device_id="device-777",
            origin_area="bedroom",
            payload_json=json.dumps(
                {
                    "alarm_label": "Recurring Wake",
                    "language": "en",
                    "recurrence": {
                        "freq": "daily",
                        "interval": 1,
                        "anchor_time": "07:00:00",
                        "timezone": "UTC",
                    },
                }
            ),
        )

        sched, gateway = _make_scheduler()
        try:
            await sched.start()
            await sched.wait_for_overdue_processing()

            original = await ScheduledTimersRepository.get("overdue-recurring-id")
            assert original and original["state"] == "fired"

            pending = await ScheduledTimersRepository.list_pending_for(logical_name="Recurring Wake", kinds={"alarm"})
            assert len(pending) == 1
            pending_payload = json.loads(pending[0]["payload_json"])
            assert pending_payload["recurrence"]["freq"] == "daily"
            assert pending[0]["origin_device_id"] == "device-777"
            assert pending[0]["origin_area"] == "bedroom"

            gateway.dispatch.assert_awaited_once()
            assert _dispatch_event_type(gateway) == "alarm_notification"
        finally:
            await sched.stop()


class TestConcurrency:
    async def test_cancel_task_during_iteration(self, db_repository):
        """CONT-1.3: _cancel_task must not mutate _by_logical while iterating without a copy."""
        sched, _gateway = _make_scheduler()
        try:
            # Populate multiple logical names so _by_logical has values to iterate
            for i in range(5):
                await sched.schedule(
                    logical_name=f"timer-{i}",
                    kind="notification",
                    duration_seconds=3600,
                    payload={"notification_message": "x"},
                )
            # Cancel one timer; this should not raise RuntimeError for dict size change
            cancelled = await sched.cancel(logical_name="timer-2")
            assert cancelled == 1
        finally:
            await sched.stop()

    async def test_stop_logs_cancellation_errors(self, db_repository):
        """CONT-6.2: stop() must log exceptions from tasks that raise during gather."""
        sched, _gateway = _make_scheduler()

        async def _explode():
            raise RuntimeError("boom")

        # Inject a task that raises RuntimeError (not CancelledError)
        bad_task = asyncio.create_task(_explode())
        sched._tasks["bad-id"] = bad_task
        # Wait for it to finish so stop() sees it as done and does not cancel it
        await asyncio.sleep(0)

        with patch("app.agents.timer_scheduler.logger") as mock_logger:
            await sched.stop()

        # The stop method logs exceptions from gather results
        assert any("raised exception during stop" in str(call) for call in mock_logger.error.call_args_list)


class TestTimerNameDisplay:
    async def test_fire_omits_generic_logical_name_from_timer_name(self, db_repository):
        """Generic logical names like 'Timer' or 'Timer 1' should not prefix the message."""
        sched, gateway = _make_scheduler()
        try:
            for generic_name in ("Timer", "timer", "Timer 1", "TIMER 2", "timer 3"):
                tid = await sched.schedule(
                    logical_name=generic_name,
                    kind="notification",
                    duration_seconds=0,
                    origin_device_id="device-123",
                    origin_area="kitchen",
                    payload={"notification_message": "Eggs done!", "language": "en"},
                )
                await _wait_for_state(tid, "fired")

                payloads = _dispatch_args_list(gateway)
                payload = payloads[-1]
                assert payload["timer_name"] == "Eggs done!"
        finally:
            await sched.stop()

    async def test_fire_preserves_meaningful_logical_name_with_message(self, db_repository):
        """Non-generic logical names should be preserved as 'name: message'."""
        sched, gateway = _make_scheduler()
        try:
            tid = await sched.schedule(
                logical_name="egg timer",
                kind="notification",
                duration_seconds=0,
                origin_device_id="device-123",
                origin_area="kitchen",
                payload={"notification_message": "Eggs done!", "language": "en"},
            )
            await _wait_for_state(tid, "fired")
            gateway.dispatch.assert_awaited_once()
            payload = _dispatch_payload(gateway)
            assert payload["timer_name"] == "egg timer: Eggs done!"
        finally:
            await sched.stop()


class TestSchedulerRegressions:
    async def test_reschedule_then_cancel_removes_task_and_does_not_fire(self, db_repository):
        sched, gateway = _make_scheduler()
        try:
            timer_id = await sched.schedule(
                logical_name="resched-cancel",
                kind="notification",
                duration_seconds=3600,
                payload={"notification_message": "x"},
            )
            updated = await sched.reschedule(timer_id, new_fires_at=int(time.time()) + 3600)
            assert updated is True
            new_task = sched._tasks[timer_id]

            # Let the old task's cancellation (and its finally block) run.
            for _ in range(10):
                await asyncio.sleep(0)

            assert sched._tasks.get(timer_id) is new_task
            assert timer_id in sched._by_logical.get("resched-cancel", [])

            count = await sched.cancel(id_=timer_id)
            assert count == 1
            await asyncio.gather(new_task, return_exceptions=True)

            assert new_task.cancelled()
            assert timer_id not in sched._tasks
            assert timer_id not in sched._by_logical.get("resched-cancel", [])
            gateway.dispatch.assert_not_awaited()
        finally:
            await sched.stop()

    async def test_recovery_fire_failure_marks_row_fired(self, db_repository):
        now = int(time.time())
        await ScheduledTimersRepository.insert(
            id="overdue-fail-id",
            logical_name="overdue-fail",
            kind="notification",
            created_at=now - 100,
            fires_at=now - 10,
            duration_seconds=10,
            origin_device_id=None,
            origin_area=None,
            payload_json=json.dumps({"notification_message": "late"}),
        )
        sched, gateway = _make_scheduler()
        gateway.dispatch.side_effect = ValueError("dispatch exploded")
        try:
            await sched.start()
            await sched.wait_for_overdue_processing()
            row = await ScheduledTimersRepository.get("overdue-fail-id")
            assert row["state"] == "fired"
        finally:
            await sched.stop()

    async def test_recurring_alarm_schedules_next_occurrence_when_dispatch_raises(self, db_repository):
        sched, gateway = _make_scheduler()
        gateway.dispatch.side_effect = ValueError("dispatch exploded")
        try:
            next_fire = int(time.time()) + 600
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.agents.timer_scheduler._compute_next_recurring_fire_epoch", lambda *_args, **_kwargs: next_fire
                )
                tid = await sched.schedule(
                    logical_name="Failing Alarm",
                    kind="alarm",
                    duration_seconds=0,
                    payload={
                        "alarm_label": "Failing Alarm",
                        "recurrence": {
                            "freq": "daily",
                            "interval": 1,
                            "anchor_time": "07:00:00",
                            "timezone": "UTC",
                        },
                    },
                )
                row = await _wait_for_state(tid, "fired")

            row = await ScheduledTimersRepository.get(tid)
            assert row and row["state"] == "fired"
            gateway.dispatch.assert_awaited_once()
            pending = await ScheduledTimersRepository.list_pending_for(logical_name="Failing Alarm", kinds={"alarm"})
            assert len(pending) == 1
            assert int(json.loads(pending[0]["payload_json"])["scheduled_for_epoch"]) == next_fire
        finally:
            await sched.stop()


async def _insert_row(row_id: str, *, kind: str = "plain", name: str = "t", created_at: int | None = None) -> None:
    now = int(time.time())
    await ScheduledTimersRepository.insert(
        id=row_id,
        logical_name=name,
        kind=kind,
        created_at=created_at or now - 100,
        fires_at=now + 100,
        duration_seconds=60,
        origin_device_id=None,
        origin_area="kitchen",
        payload_json="{}",
    )


class TestHistory:
    async def test_history_lists_terminal_rows_newest_first(self, db_repository):
        now = int(time.time())
        await _insert_row("h-fired", name="pasta")
        await _insert_row("h-cancelled", name="laundry")
        await _insert_row("h-expired", kind="alarm", name="wake")
        await _insert_row("h-pending", name="still running")
        await ScheduledTimersRepository.mark_fired("h-fired", now - 300)
        await ScheduledTimersRepository.mark_cancelled("h-cancelled", now - 100)
        await ScheduledTimersRepository.mark_expired("h-expired", now - 200)

        sched, _ = _make_scheduler()
        rows = await sched.history()

        assert [r["id"] for r in rows] == ["h-cancelled", "h-expired", "h-fired"]
        assert [r["state"] for r in rows] == ["cancelled", "expired", "fired"]
        assert rows[0]["ended_at"] == now - 100

    async def test_history_excludes_rows_past_retention(self, db_repository):
        now = int(time.time())
        await _insert_row("h-old")
        await _insert_row("h-recent")
        await ScheduledTimersRepository.mark_fired("h-old", now - 31 * 86400)
        await ScheduledTimersRepository.mark_fired("h-recent", now - 29 * 86400)

        sched, _ = _make_scheduler()
        rows = await sched.history()

        assert [r["id"] for r in rows] == ["h-recent"]

    async def test_history_respects_limit(self, db_repository):
        now = int(time.time())
        for i in range(3):
            await _insert_row(f"h-{i}")
            await ScheduledTimersRepository.mark_fired(f"h-{i}", now - i)

        sched, _ = _make_scheduler()
        rows = await sched.history(limit=2)

        assert [r["id"] for r in rows] == ["h-0", "h-1"]

    async def test_start_purges_rows_past_retention(self, db_repository):
        now = int(time.time())
        await _insert_row("p-old")
        await _insert_row("p-recent")
        await ScheduledTimersRepository.mark_cancelled("p-old", now - 31 * 86400)
        await ScheduledTimersRepository.mark_cancelled("p-recent", now - 86400)

        sched, _ = _make_scheduler()
        try:
            await sched.start()
        finally:
            await sched.stop()

        assert await ScheduledTimersRepository.get("p-old") is None
        assert await ScheduledTimersRepository.get("p-recent") is not None

    async def test_purge_is_throttled_and_fail_soft(self, db_repository):
        repo = MagicMock()
        repo.purge_terminal_older_than = AsyncMock(side_effect=RuntimeError("db down"))
        sched = TimerScheduler(repo, dispatcher=MagicMock())

        await sched._purge_history()
        await sched._purge_history()

        assert repo.purge_terminal_older_than.await_count == 1


# ---------------------------------------------------------------------------
# FIX-22: rings survive a short Home Assistant outage
# ---------------------------------------------------------------------------


class _FakeClock:
    """Stands in for ``time`` inside ``app.agents.timer_scheduler``; tests advance ``now`` explicitly."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()


class _FakeHomeAssistant:
    """httpx mock transport for a real ``HARestClient``.

    While the simulated clock is before ``down_until`` every request is refused before it is sent
    (``httpx.ConnectError``), and each refused attempt advances the clock by one 15 s retry interval.
    ``service_error`` makes service calls fail after the request was sent.
    """

    def __init__(self, clock: _FakeClock, *, down_until: float, service_error: Exception | None = None) -> None:
        self.clock = clock
        self.down_until = down_until
        self.service_error = service_error
        self.refused = 0
        self.service_calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.clock.now < self.down_until:
            self.refused += 1
            self.clock.now += 15
            raise httpx.ConnectError("Connection refused", request=request)
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if request.url.path.startswith("/api/services/"):
            self.service_calls.append((request.url.path, json.loads(request.content or b"{}")))
            if self.service_error is not None:
                raise self.service_error
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    def messages(self) -> list[str]:
        return [str(data.get("message") or "") for _path, data in self.service_calls]


class _BackgroundGateway:
    """A2A dispatcher stand-in that runs background events through the real handler against the fake HA."""

    def __init__(self, ha_client: HARestClient) -> None:
        self.ha_client = ha_client
        self.dispatched: list[str] = []

    async def dispatch(self, request):
        task = request.params["task"]
        event = task.context.background_event
        self.dispatched.append(event.event_type)
        return await handle_background_event(event, context=task.context, ha_client=self.ha_client)


@pytest.fixture
async def ha_outage(db_repository, monkeypatch):
    """Scheduler wired to a fake HA behind a real REST client, with a simulated clock and a fast retry interval."""
    clock = _FakeClock(float(int(time.time())))
    monkeypatch.setattr("app.agents.timer_scheduler.time", clock)
    monkeypatch.setattr("app.agents.timer_scheduler._HA_RETRY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("app.agents.background_actions._generate_tts_message", AsyncMock(return_value=None))
    created: list[tuple[TimerScheduler, HARestClient]] = []

    def _build(*, down_for: float, service_error: Exception | None = None):
        fake_ha = _FakeHomeAssistant(clock, down_until=clock.now + down_for, service_error=service_error)
        ha_client = HARestClient()
        ha_client._client = httpx.AsyncClient(base_url="http://ha.test", transport=httpx.MockTransport(fake_ha.handler))
        gateway = _BackgroundGateway(ha_client)
        sched = TimerScheduler(ScheduledTimersRepository, dispatcher=gateway)
        created.append((sched, ha_client))
        return sched, gateway, fake_ha

    yield clock, _build
    for sched, ha_client in created:
        await sched.stop()
        await ha_client.close()


async def _wait_for_task_end(sched: TimerScheduler, timer_id: str, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while timer_id in sched._tasks and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert timer_id not in sched._tasks


_DAILY = {"freq": "daily", "interval": 1, "anchor_time": "07:00:00", "timezone": "UTC"}


class TestHaOutageRetry:
    async def test_ring_during_short_outage_rings_after_reconnect(self, ha_outage):
        _clock, build = ha_outage
        sched, gateway, fake_ha = build(down_for=90)
        timer_id = await sched.schedule(
            logical_name="pasta", kind="plain", duration_seconds=0, payload={"language": "en"}
        )

        await _wait_for_task_end(sched, timer_id)

        row = await ScheduledTimersRepository.get(timer_id)
        assert row["state"] == "fired"
        assert fake_ha.refused == 6
        assert gateway.dispatched == ["timer_notification"] * 7
        assert fake_ha.messages() == ["Timer pasta has finished"]

    async def test_ring_during_long_outage_is_missed_with_one_notice(self, ha_outage):
        _clock, build = ha_outage
        sched, _gateway, fake_ha = build(down_for=600)
        timer_id = await sched.schedule(
            logical_name="pasta", kind="plain", duration_seconds=0, payload={"language": "en"}
        )

        await _wait_for_task_end(sched, timer_id)

        row = await ScheduledTimersRepository.get(timer_id)
        assert row["state"] == "expired"
        messages = fake_ha.messages()
        assert len(messages) == 1
        assert "missed" in messages[0] and "timer 'pasta'" in messages[0]
        assert await ScheduledTimersRepository.list_pending() == []

    async def test_timeout_after_send_counts_as_delivered(self, ha_outage):
        _clock, build = ha_outage
        sched, gateway, fake_ha = build(down_for=0, service_error=httpx.ReadTimeout("read timed out"))
        timer_id = await sched.schedule(
            logical_name="pasta", kind="plain", duration_seconds=0, payload={"language": "en"}
        )

        await _wait_for_task_end(sched, timer_id)

        assert (await ScheduledTimersRepository.get(timer_id))["state"] == "fired"
        assert gateway.dispatched == ["timer_notification"]
        assert len(fake_ha.service_calls) == 1

    async def test_dispatch_timeout_counts_as_delivered(self, db_repository):
        sched, gateway = _make_scheduler()
        gateway.dispatch.side_effect = TimeoutError()
        try:
            timer_id = await sched.schedule(logical_name="pasta", kind="plain", duration_seconds=0)
            row = await _wait_for_state(timer_id, "fired")
            assert row["state"] == "fired"
            gateway.dispatch.assert_awaited_once()
        finally:
            await sched.stop()

    async def test_non_connection_error_stays_terminal(self, ha_outage):
        _clock, build = ha_outage
        server_error = httpx.HTTPStatusError(
            "500", request=httpx.Request("POST", "http://ha.test"), response=httpx.Response(500)
        )
        sched, gateway, fake_ha = build(down_for=0, service_error=server_error)
        timer_id = await sched.schedule(
            logical_name="pasta", kind="plain", duration_seconds=0, payload={"language": "en"}
        )

        await _wait_for_task_end(sched, timer_id)

        assert (await ScheduledTimersRepository.get(timer_id))["state"] == "fired"
        assert gateway.dispatched == ["timer_notification"]
        assert fake_ha.refused == 0

    @pytest.mark.parametrize(("down_for", "final_state"), [(90, "fired"), (600, "expired")])
    async def test_recurring_alarm_schedules_next_occurrence_once(self, ha_outage, monkeypatch, down_for, final_state):
        clock, build = ha_outage
        next_fire = int(clock.now) + 86400
        monkeypatch.setattr(
            "app.agents.timer_scheduler._compute_next_recurring_fire_epoch", lambda *_args, **_kwargs: next_fire
        )
        sched, _gateway, fake_ha = build(down_for=down_for)
        timer_id = await sched.schedule(
            logical_name="Wake",
            kind="alarm",
            duration_seconds=0,
            origin_area="bedroom",
            payload={"alarm_label": "Wake", "language": "en", "recurrence": dict(_DAILY)},
        )

        await _wait_for_task_end(sched, timer_id)

        assert (await ScheduledTimersRepository.get(timer_id))["state"] == final_state
        pending = await ScheduledTimersRepository.list_pending_for(logical_name="Wake", kinds={"alarm"})
        assert len(pending) == 1
        assert pending[0]["fires_at"] == next_fire
        assert pending[0]["origin_area"] == "bedroom"
        next_payload = json.loads(pending[0]["payload_json"])
        assert next_payload["recurrence"]["freq"] == "daily"
        assert next_payload["scheduled_for_epoch"] == next_fire
        assert len(fake_ha.messages()) == 1

    async def test_cancel_stops_a_retrying_ring(self, ha_outage, monkeypatch):
        _clock, build = ha_outage
        monkeypatch.setattr("app.agents.timer_scheduler._HA_RETRY_INTERVAL_SECONDS", 3600)
        sched, gateway, _fake_ha = build(down_for=90)
        timer_id = await sched.schedule(logical_name="pasta", kind="plain", duration_seconds=0)
        deadline = time.monotonic() + 5
        while not gateway.dispatched and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        task = sched._tasks[timer_id]
        assert (await ScheduledTimersRepository.get(timer_id))["state"] == "pending"

        assert await sched.cancel(id_=timer_id) == 1
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled()
        assert (await ScheduledTimersRepository.get(timer_id))["state"] == "cancelled"
        assert gateway.dispatched == ["timer_notification"]

    async def test_startup_overdue_rows_wait_for_home_assistant(self, ha_outage):
        clock, build = ha_outage
        now = int(clock.now)
        for row_id, fires_at in (("startup-ring", now - 10), ("startup-missed", now - 3600)):
            await ScheduledTimersRepository.insert(
                id=row_id,
                logical_name=row_id,
                kind="plain",
                created_at=now - 7200,
                fires_at=fires_at,
                duration_seconds=60,
                origin_device_id=None,
                origin_area=None,
                payload_json=json.dumps({"language": "en"}),
            )
        sched, _gateway, fake_ha = build(down_for=90)

        await sched.start()
        await sched.wait_for_overdue_processing()
        await _wait_for_task_end(sched, "startup-ring")

        assert (await ScheduledTimersRepository.get("startup-ring"))["state"] == "fired"
        assert (await ScheduledTimersRepository.get("startup-missed"))["state"] == "expired"
        messages = fake_ha.messages()
        assert len(messages) == 2
        assert sum("timer 'startup-missed'" in message for message in messages) == 1
        assert sum(message == "Timer startup-ring has finished" for message in messages) == 1
