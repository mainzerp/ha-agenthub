"""AgentHub-managed timer scheduler.

Owns every non-native timer (notification, delayed_action, sleep,
snooze, internal plain). Persists state in SQLite via
``ScheduledTimersRepository`` so timers survive container restart, and
runs one ``asyncio.Task`` per pending timer for wall-clock firing
independent of any HA timer.* helper.

Replaces the obsolete HA ``timer.*`` helper-pool model that lived in
``timer_executor/`` and ``delayed_tasks.py`` prior to 0.26.0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.a2a._request import build_send_request
from app.db.repository import ScheduledTimersRepository
from app.models.agent import BackgroundEvent, BackgroundTask, TaskContext

logger = logging.getLogger(__name__)


_VALID_KINDS = frozenset({"plain", "notification", "delayed_action", "sleep", "snooze", "alarm"})
_STARTUP_RECOVERY_RETRY_DELAY_SECONDS = 2.0
# Overdue rows found at startup (container was down when they were due):
# device actions (delayed_action, sleep) still run when at most this late and
# are dropped otherwise; timers and alarms ring normally only within the short
# grace window and are otherwise reported once as missed instead of late.
_OVERDUE_DEVICE_ACTION_MAX_SECONDS = 300
_OVERDUE_RING_GRACE_SECONDS = 60
_DEVICE_ACTION_KINDS = frozenset({"delayed_action", "sleep"})
_RECURRING_WEEKDAY_INDEX: dict[str, int] = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_recurrence(payload: dict[str, Any]) -> dict[str, Any] | None:
    recurrence = payload.get("recurrence")
    if not isinstance(recurrence, dict):
        return None

    freq = str(recurrence.get("freq") or "").strip().casefold()
    if freq not in {"daily", "weekly"}:
        return None

    try:
        interval = int(recurrence.get("interval", 1))
    except (TypeError, ValueError):
        return None
    if interval < 1:
        return None

    anchor_text = str(recurrence.get("anchor_time") or "").strip()
    try:
        anchor_time = datetime.strptime(anchor_text, "%H:%M:%S").time()
    except ValueError:
        return None

    normalized: dict[str, Any] = {
        "freq": freq,
        "interval": interval,
        "anchor_time": anchor_text,
        "_anchor_time_obj": anchor_time,
    }
    anchor_week_text = str(recurrence.get("anchor_week") or "").strip()
    if anchor_week_text:
        try:
            normalized["_anchor_week_obj"] = _week_start(date.fromisoformat(anchor_week_text))
            normalized["anchor_week"] = anchor_week_text
        except ValueError:
            logger.debug("Invalid recurrence anchor_week %r, ignoring", anchor_week_text)

    timezone_name = recurrence.get("timezone")
    if timezone_name:
        timezone_name = str(timezone_name)
        try:
            normalized["_tz"] = ZoneInfo(timezone_name)
            normalized["timezone"] = timezone_name
        except Exception:
            logger.debug("Invalid timezone %s, clearing", timezone_name, exc_info=True)
            normalized["_tz"] = None
    else:
        normalized["_tz"] = None

    if freq == "weekly":
        byweekday = recurrence.get("byweekday")
        if not isinstance(byweekday, list) or not byweekday:
            return None
        weekday_indexes: list[int] = []
        for item in byweekday:
            key = str(item or "").strip().upper()
            if key not in _RECURRING_WEEKDAY_INDEX:
                return None
            idx = _RECURRING_WEEKDAY_INDEX[key]
            if idx not in weekday_indexes:
                weekday_indexes.append(idx)
        if not weekday_indexes:
            return None
        normalized["_weekday_indexes"] = weekday_indexes
        normalized["byweekday"] = [
            str(item or "").strip().upper()
            for item in byweekday
            if str(item or "").strip().upper() in _RECURRING_WEEKDAY_INDEX
        ]

    return normalized


def _week_start(day: date) -> date:
    """Return the Monday of the ISO week containing ``day``."""
    return day - timedelta(days=day.weekday())


def _compute_next_recurring_fire_epoch(
    row: dict[str, Any],
    recurrence: dict[str, Any],
    now_ts: int,
    *,
    include_current_day: bool = False,
) -> int | None:
    """Return the next occurrence after the day of the row's ``fires_at`` (local wall clock).

    ``include_current_day`` also considers the day of ``fires_at`` itself; it
    places the first occurrence of a new recurring alarm on a listed weekday.
    Weekly recurrences with ``interval > 1`` count weeks from a fixed anchor
    week (``anchor_week`` in the payload, else the week of ``fires_at``), so
    several weekdays inside one active week stay on the same interval grid.
    """
    tz = recurrence.get("_tz")
    anchor_time = recurrence.get("_anchor_time_obj")
    freq = recurrence.get("freq")
    interval = int(recurrence.get("interval") or 1)
    if anchor_time is None or freq not in {"daily", "weekly"}:
        return None

    current_local = (
        datetime.fromtimestamp(int(row.get("fires_at") or 0), tz=tz)
        if tz is not None
        else datetime.fromtimestamp(int(row.get("fires_at") or 0))
    )
    now_local = datetime.fromtimestamp(int(now_ts), tz=tz) if tz is not None else datetime.fromtimestamp(int(now_ts))

    if freq == "daily":
        next_date = current_local.date() + timedelta(days=0 if include_current_day else interval)
        next_local = datetime.combine(next_date, anchor_time, tzinfo=tz)
        while next_local <= now_local:
            next_date = next_date + timedelta(days=interval)
            next_local = datetime.combine(next_date, anchor_time, tzinfo=tz)
        return int(next_local.timestamp())

    weekdays: list[int] = list(recurrence.get("_weekday_indexes") or [])
    if not weekdays:
        return None
    base_date = current_local.date()
    anchor_week = recurrence.get("_anchor_week_obj") or _week_start(base_date)
    first_offset = 0 if include_current_day else 1
    for day_offset in range(first_offset, 366 * max(1, interval)):
        candidate_date = base_date + timedelta(days=day_offset)
        weeks_since_anchor = (_week_start(candidate_date) - anchor_week).days // 7
        if weeks_since_anchor % interval != 0:
            continue
        if candidate_date.weekday() not in weekdays:
            continue
        candidate_local = datetime.combine(candidate_date, anchor_time, tzinfo=tz)
        if candidate_local <= now_local:
            continue
        return int(candidate_local.timestamp())
    return None


class TimerScheduler:
    """In-process scheduler with persisted state.

    Each pending timer is backed by a row in ``scheduled_timers`` and an
    ``asyncio.Task`` that sleeps until ``fires_at`` then dispatches the
    kind-specific fire callback.
    """

    def __init__(
        self,
        repo: type[ScheduledTimersRepository] | ScheduledTimersRepository = ScheduledTimersRepository,
        *,
        dispatcher: Any = None,
    ) -> None:
        self._repo = repo
        self._dispatcher = dispatcher
        self._tasks: dict[str, asyncio.Task] = {}
        self._by_logical: dict[str, list[str]] = {}
        self._startup_recovery_task: asyncio.Task | None = None
        self._overdue_tasks: list[asyncio.Task] = []
        self._started = False

    async def _dispatch_background_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        conversation_id: str | None = None,
        context: TaskContext | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        event_context = context.model_copy(deep=True) if context is not None else TaskContext()
        event_context.source = "background"
        event_context.background_event = BackgroundEvent(
            event_type=event_type,
            payload=dict(payload or {}),
        )
        task = BackgroundTask(
            conversation_id=conversation_id,
            context=event_context,
        )
        request = build_send_request(
            "orchestrator",
            task,
            request_id=request_id or task.conversation_id or f"scheduler-{uuid.uuid4().hex}",
        )
        try:
            response = await self._dispatcher.dispatch(request)
        except RuntimeError as exc:
            return {
                "speech": "",
                "error": {
                    "code": "internal",
                    "message": str(exc),
                    "recoverable": True,
                },
            }
        return response or {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Rehydrate pending timers from the DB.

        Future timers get an asyncio task that sleeps until their
        ``fires_at``. Overdue rows are handed to a background task (see
        ``_process_overdue_rows``) so startup never waits on HA calls.
        """
        if self._started:
            return
        self._started = True
        try:
            rows = await self._repo.list_pending()
        except Exception:
            logger.error("TimerScheduler.start: failed to load pending timers", exc_info=True)
            self._schedule_startup_recovery_retry()
            rows = []
        rehydrated, overdue = self._rehydrate_rows(rows)
        logger.info("TimerScheduler started: rehydrated=%d overdue=%d", rehydrated, overdue)

    async def stop(self) -> None:
        """Cancel all in-flight timer tasks. DB rows remain pending."""
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.error("Timer task raised exception during stop", exc_info=(type(result), result, None))
        background = [self._startup_recovery_task, *self._overdue_tasks]
        self._startup_recovery_task = None
        self._overdue_tasks = []
        for bg_task in background:
            if bg_task is None or bg_task.done():
                continue
            bg_task.cancel()
            bg_results = await asyncio.gather(bg_task, return_exceptions=True)
            for result in bg_results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    logger.error(
                        "Startup recovery task raised exception during stop", exc_info=(type(result), result, None)
                    )
        self._tasks.clear()
        self._by_logical.clear()
        self._started = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def schedule(
        self,
        *,
        logical_name: str,
        kind: str,
        duration_seconds: int,
        origin_device_id: str | None = None,
        origin_area: str | None = None,
        briefing: bool = False,
        payload: dict | None = None,
    ) -> str:
        """Persist a new timer row and start its firing task."""
        if kind not in _VALID_KINDS:
            raise ValueError(f"Unknown timer kind: {kind}")
        if duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")
        timer_id = uuid.uuid4().hex
        now = int(time.time())
        fires_at = now + int(duration_seconds)
        payload_dict = dict(payload or {})
        if kind == "alarm":
            briefing = _coerce_bool(payload_dict.get("briefing", briefing))
            payload_dict["briefing"] = briefing
        else:
            briefing = False
        payload_json = json.dumps(payload_dict)
        await self._repo.insert(
            id=timer_id,
            logical_name=logical_name,
            kind=kind,
            created_at=now,
            fires_at=fires_at,
            duration_seconds=int(duration_seconds),
            origin_device_id=origin_device_id,
            origin_area=origin_area,
            briefing=briefing,
            payload_json=payload_json,
        )
        row = {
            "id": timer_id,
            "logical_name": logical_name,
            "kind": kind,
            "created_at": now,
            "fires_at": fires_at,
            "duration_seconds": int(duration_seconds),
            "origin_device_id": origin_device_id,
            "origin_area": origin_area,
            "briefing": 1 if briefing else 0,
            "payload_json": payload_json,
            "state": "pending",
        }
        self._spawn_task(row)
        return timer_id

    async def cancel(
        self,
        *,
        id_: str | None = None,
        logical_name: str | None = None,
        area: str | None = None,
        kinds: set[str] | frozenset[str] | None = None,
    ) -> int:
        """Cancel by id or by logical_name (optionally scoped to area and kinds).

        Paused rows are cancellable too. Returns the number of timers cancelled.
        """
        now = int(time.time())
        if id_:
            row = await self._repo.get(id_)
            if not row or row.get("state") not in ("pending", "paused"):
                return 0
            await self._repo.mark_cancelled(id_, now)
            self._cancel_task(id_)
            return 1
        if not logical_name:
            return 0
        rows = await self._repo.list_pending_for(
            logical_name=logical_name, area=area, kinds=kinds, states={"pending", "paused"}
        )
        if not rows:
            return 0
        count = 0
        for row in rows:
            await self._repo.mark_cancelled(row["id"], now)
            self._cancel_task(row["id"])
            count += 1
        return count

    async def list(
        self,
        *,
        logical_name: str | None = None,
        area: str | None = None,
        kinds: set[str] | frozenset[str] | None = None,
        states: set[str] | frozenset[str] | None = None,
    ) -> list[dict]:
        """Return timers (pending by default), optionally filtered by name, area, kind, and state."""
        return await self._repo.list_pending_for(logical_name=logical_name, area=area, kinds=kinds, states=states)

    async def pause(self, id_: str) -> int | None:
        """Pause a pending timer; returns the remaining seconds, or None when it is not pending.

        The row moves to state ``paused`` with ``paused_remaining_seconds`` in
        its payload and its firing task is cancelled. Paused rows are not
        rehydrated on restart; they wait for ``resume`` or ``cancel``.
        """
        row = await self._repo.get(id_)
        if not row or row.get("state") != "pending":
            return None
        remaining = max(0, int(row.get("fires_at") or 0) - int(time.time()))
        payload = _load_payload(row)
        payload["paused_remaining_seconds"] = remaining
        if not await self._repo.mark_paused(id_, json.dumps(payload)):
            return None
        self._cancel_task(id_)
        return remaining

    async def resume(self, id_: str) -> int | None:
        """Resume a paused timer; returns the remaining seconds, or None when it is not paused."""
        row = await self._repo.get(id_)
        if not row or row.get("state") != "paused":
            return None
        payload = _load_payload(row)
        try:
            remaining = max(0, int(payload.pop("paused_remaining_seconds", 0) or 0))
        except (TypeError, ValueError):
            remaining = 0
        fires_at = int(time.time()) + remaining
        payload_json = json.dumps(payload)
        if not await self._repo.mark_resumed(id_, fires_at=fires_at, payload_json=payload_json):
            return None
        resumed = dict(row)
        resumed.update({"state": "pending", "fires_at": fires_at, "payload_json": payload_json})
        self._spawn_task(resumed)
        return remaining

    async def reschedule(
        self,
        id_: str,
        *,
        logical_name: str | None = None,
        new_fires_at: int | None = None,
        new_duration_seconds: int | None = None,
        briefing: bool | None = None,
        recurrence: dict[str, Any] | None = None,
        clear_recurrence: bool = False,
    ) -> bool:
        """Update a pending timer/alarm in-place and restart its asyncio task.

        Strategy: update the DB row via the repository, then cancel the
        existing asyncio task and spawn a replacement task from the updated
        row. The row ID is preserved (no identity churn).

        Returns ``True`` if the timer was found, was still pending, and was
        updated. Returns ``False`` if no matching pending row exists.
        """
        row = await self._repo.get(id_)
        if not row or row.get("state") != "pending":
            return False

        payload_json: str | None = None
        normalized_briefing = _coerce_bool(briefing) if briefing is not None else None
        if row.get("kind") == "alarm":
            try:
                payload_dict = json.loads(row.get("payload_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                logger.debug("Invalid alarm payload JSON for timer %s", id_, exc_info=True)
                payload_dict = {}
            if logical_name is not None:
                payload_dict["alarm_label"] = logical_name
            if new_fires_at is not None:
                payload_dict["scheduled_for_epoch"] = int(new_fires_at)
            if normalized_briefing is not None:
                payload_dict["briefing"] = normalized_briefing
            if clear_recurrence:
                payload_dict.pop("recurrence", None)
            elif recurrence is not None:
                payload_dict["recurrence"] = dict(recurrence)
            if (
                logical_name is not None
                or new_fires_at is not None
                or normalized_briefing is not None
                or recurrence is not None
                or clear_recurrence
            ):
                payload_json = json.dumps(payload_dict)

        updated = await self._repo.update_scheduled_timer(
            id_,
            logical_name=logical_name,
            fires_at=new_fires_at,
            duration_seconds=new_duration_seconds,
            briefing=normalized_briefing,
            payload_json=payload_json,
        )
        if not updated:
            return False

        updated_row = dict(row)
        if logical_name is not None:
            old_key = (row.get("logical_name") or "").lower()
            new_key = logical_name.lower()
            if old_key != new_key:
                old_list = self._by_logical.get(old_key, [])
                if id_ in old_list:
                    old_list.remove(id_)
                self._by_logical.setdefault(new_key, []).append(id_)
            updated_row["logical_name"] = logical_name
        if new_fires_at is not None:
            updated_row["fires_at"] = int(new_fires_at)
        if new_duration_seconds is not None:
            updated_row["duration_seconds"] = int(new_duration_seconds)
        if normalized_briefing is not None:
            updated_row["briefing"] = 1 if normalized_briefing else 0
        if payload_json is not None:
            updated_row["payload_json"] = payload_json

        # Keep cancellation semantics centralized in _cancel_task.
        self._cancel_task(id_)
        self._spawn_task(updated_row)
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _spawn_task(self, row: dict) -> None:
        timer_id = row["id"]
        if timer_id in self._tasks:
            return
        task = asyncio.create_task(self._run(row), name=f"timer-{timer_id}")
        self._tasks[timer_id] = task
        self._by_logical.setdefault((row["logical_name"] or "").lower(), []).append(timer_id)

    def _schedule_startup_recovery_retry(self) -> None:
        existing = self._startup_recovery_task
        if existing is not None and not existing.done():
            return
        self._startup_recovery_task = asyncio.create_task(
            self._startup_recovery_retry(),
            name="timer-startup-recovery-retry",
        )

    async def _startup_recovery_retry(self) -> None:
        try:
            await asyncio.sleep(_STARTUP_RECOVERY_RETRY_DELAY_SECONDS)
            rows = await self._repo.list_pending()
            rehydrated, overdue = self._rehydrate_rows(rows)
            logger.info(
                "TimerScheduler startup recovery retry: rehydrated=%d overdue=%d",
                rehydrated,
                overdue,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("TimerScheduler startup recovery retry failed", exc_info=True)

    def _rehydrate_rows(self, rows: Sequence[dict]) -> tuple[int, int]:
        """Spawn tasks for future rows and hand overdue rows to a background task.

        Returns ``(rehydrated, overdue)``. Never awaits HA work, so startup is
        not blocked by overdue processing.
        """
        rehydrated = 0
        overdue_rows: list[dict] = []
        now = int(time.time())
        for row in rows:
            timer_id = row.get("id")
            if not timer_id or timer_id in self._tasks:
                continue
            if int(row["fires_at"]) <= now:
                overdue_rows.append(row)
            else:
                self._spawn_task(row)
                rehydrated += 1
        if overdue_rows:
            task = asyncio.create_task(self._process_overdue_rows(overdue_rows), name="timer-overdue-recovery")
            self._overdue_tasks.append(task)
            task.add_done_callback(self._forget_overdue_task)
        return rehydrated, len(overdue_rows)

    def _forget_overdue_task(self, task: asyncio.Task) -> None:
        if task in self._overdue_tasks:
            self._overdue_tasks.remove(task)

    async def wait_for_overdue_processing(self) -> None:
        """Await in-flight overdue processing (used by tests)."""
        while self._overdue_tasks:
            await asyncio.gather(*list(self._overdue_tasks), return_exceptions=True)

    async def _process_overdue_rows(self, rows: Sequence[dict]) -> None:
        """Handle rows that came due while the container was down.

        - ``delayed_action`` / ``sleep``: executed when at most
          ``_OVERDUE_DEVICE_ACTION_MAX_SECONDS`` late, otherwise dropped and logged.
        - ``plain`` / ``notification`` / ``snooze`` / ``alarm``: ring normally within
          ``_OVERDUE_RING_GRACE_SECONDS``; later ones are reported once, together,
          as missed. A missed recurring alarm still schedules its next occurrence.
        """
        now = int(time.time())
        missed: list[dict] = []
        for row in sorted(rows, key=lambda r: (int(r.get("fires_at") or 0), str(r.get("id") or ""))):
            timer_id = str(row.get("id"))
            overdue_by = max(0, now - int(row.get("fires_at") or 0))
            kind = row.get("kind")
            try:
                if kind in _DEVICE_ACTION_KINDS:
                    if overdue_by > _OVERDUE_DEVICE_ACTION_MAX_SECONDS:
                        logger.warning(
                            "Dropping overdue %s timer %s (%r): %ds late after restart (limit %ds)",
                            kind,
                            timer_id,
                            row.get("logical_name"),
                            overdue_by,
                            _OVERDUE_DEVICE_ACTION_MAX_SECONDS,
                        )
                        await self._repo.mark_expired(timer_id, now)
                        continue
                elif overdue_by > _OVERDUE_RING_GRACE_SECONDS:
                    missed.append(row)
                    await self._repo.mark_expired(timer_id, now)
                    if kind == "alarm":
                        await self._schedule_next_recurrence(row, _load_payload(row))
                    continue
                await self._fire(row)
                await self._repo.mark_fired(timer_id, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("TimerScheduler: overdue recovery failed for %s", timer_id, exc_info=True)
                # Match _run: a failed fire is still terminal, so it does not re-fire on every restart.
                try:
                    await self._repo.mark_fired(timer_id, now)
                except Exception:
                    logger.error("Timer %s mark_fired failed on recovery", timer_id, exc_info=True)
        if missed:
            await self._dispatch_missed(missed)

    async def _dispatch_missed(self, rows: Sequence[dict]) -> None:
        """Send one consolidated notification for timers/alarms missed while offline."""
        logger.warning("TimerScheduler: %d timers/alarms were missed while AgentHub was offline", len(rows))
        if self._dispatcher is None:
            return
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = _load_payload(row)
            is_alarm = row.get("kind") == "alarm"
            name = (payload.get("alarm_label") if is_alarm else None) or row.get("logical_name") or ""
            items.append(
                {
                    "name": str(name),
                    "kind": "alarm" if is_alarm else "timer",
                    "due_epoch": int(row.get("fires_at") or 0),
                }
            )
        latest = rows[-1]
        latest_payload = _load_payload(latest)
        try:
            await self._dispatch_background_event(
                "timer_notification",
                {
                    "missed": items,
                    "entity_id": "agenthub_internal:missed",
                    "origin_device_id": latest.get("origin_device_id"),
                    "origin_area": latest.get("origin_area"),
                    "language": latest_payload.get("language"),
                    "timezone": latest_payload.get("timezone"),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("TimerScheduler: missed-timer notification failed", exc_info=True)

    def _cancel_task(self, timer_id: str) -> None:
        task = self._tasks.pop(timer_id, None)
        if task and not task.done():
            task.cancel()
        for ids in list(self._by_logical.values()):
            if timer_id in ids:
                ids.remove(timer_id)

    async def _run(self, row: dict) -> None:
        timer_id = row["id"]
        try:
            delay = max(0.0, float(row["fires_at"]) - time.time())
            if delay > 0:
                await asyncio.sleep(delay)
            await self._fire(row)
            await self._repo.mark_fired(timer_id, int(time.time()))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Timer %s fire failed", timer_id, exc_info=True)
            try:
                await self._repo.mark_fired(timer_id, int(time.time()))
            except Exception:
                logger.error("Timer %s mark_fired failed", timer_id, exc_info=True)
        finally:
            # reschedule() replaces the task under the same id; only drop tracking that still belongs to us.
            if self._tasks.get(timer_id) is asyncio.current_task():
                self._tasks.pop(timer_id, None)
                for ids in list(self._by_logical.values()):
                    if timer_id in ids:
                        ids.remove(timer_id)

    async def _schedule_next_recurrence(self, row: dict, payload: dict) -> None:
        recurrence = _load_recurrence(payload)
        if recurrence is None:
            return

        next_fire_epoch = _compute_next_recurring_fire_epoch(row, recurrence, int(time.time()))
        if next_fire_epoch is None:
            logger.warning("Recurring alarm %s has invalid/unschedulable recurrence payload", row["id"])
            return

        next_payload = dict(payload)
        next_payload["scheduled_for_epoch"] = int(next_fire_epoch)
        await self.schedule(
            logical_name=row.get("logical_name") or "",
            kind="alarm",
            duration_seconds=max(0, int(next_fire_epoch) - int(time.time())),
            origin_device_id=row.get("origin_device_id"),
            origin_area=row.get("origin_area"),
            payload=next_payload,
        )

    async def _fire(self, row: dict) -> None:
        """Dispatch the kind-specific fire action.

        Errors are logged; the timer is still marked fired by the
        caller. Exact-once semantics surfaced to logs.
        """
        kind = row["kind"]
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {}

        logical_name = row.get("logical_name") or ""
        origin_device_id = row.get("origin_device_id")
        origin_area = row.get("origin_area")
        duration_seconds = int(row.get("duration_seconds") or 0)
        duration_str = _seconds_to_hms(duration_seconds)
        language = payload.get("language")
        dispatcher = self._dispatcher
        if dispatcher is None:
            logger.warning("TimerScheduler fire skipped for %s: no dispatcher available", row["id"])
            return

        if kind in ("plain", "notification"):
            message = payload.get("notification_message") if kind == "notification" else None
            synthetic_entity_id = f"agenthub_internal:{row['id']}"
            display_name = logical_name
            if message:
                if logical_name and logical_name.lower() not in ("timer", "timer 1", "timer 2", "timer 3"):
                    display_name = f"{logical_name}: {message}"
                else:
                    display_name = message
            await self._dispatch_background_event(
                "timer_notification",
                {
                    "timer_name": display_name,
                    "entity_id": synthetic_entity_id,
                    "media_player": payload.get("media_player"),
                    "origin_device_id": origin_device_id,
                    "origin_area": origin_area,
                    "duration": duration_str,
                    "language": language,
                },
            )
            return

        if kind == "alarm":
            alarm_name = (payload.get("alarm_label") or logical_name or "alarm").strip() or "alarm"
            synthetic_entity_id = f"agenthub_alarm:{row['id']}"
            briefing = _coerce_bool(payload.get("briefing", False))
            try:
                await self._dispatch_background_event(
                    "alarm_notification",
                    {
                        "alarm_name": alarm_name,
                        "alarm_label": payload.get("alarm_label") or alarm_name,
                        "briefing": briefing,
                        "entity_id": synthetic_entity_id,
                        "media_player": payload.get("media_player"),
                        "origin_device_id": origin_device_id,
                        "origin_area": origin_area,
                        "language": language,
                        "scheduled_for_epoch": int(payload.get("scheduled_for_epoch") or row.get("fires_at") or 0),
                        "timezone": payload.get("timezone"),
                    },
                )
            except asyncio.CancelledError:
                # Cancellation (user cancel, shutdown) must not spawn a new occurrence.
                raise
            except Exception:
                # A failed dispatch must not end a recurring series.
                await self._schedule_next_recurrence(row, payload)
                raise
            await self._schedule_next_recurrence(row, payload)
            return

        if kind == "delayed_action":
            target_entity = payload.get("target_entity") or ""
            target_action = payload.get("target_action") or ""
            if not target_entity or "/" not in target_action:
                logger.error("delayed_action timer %s missing target_entity/target_action", row["id"])
                return
            await self._dispatch_background_event(
                "delayed_action",
                {
                    "target_entity": target_entity,
                    "target_action": target_action,
                    "agent_id": payload.get("agent_id"),
                    "origin_device_id": origin_device_id,
                    "origin_area": origin_area,
                    "language": language,
                },
            )
            return

        if kind == "sleep":
            media_player = payload.get("media_player") or ""
            if not media_player:
                logger.error("sleep timer %s missing media_player", row["id"])
                return
            await self._dispatch_background_event(
                "sleep_media_stop",
                {
                    "media_player": media_player,
                    "agent_id": payload.get("agent_id"),
                    "origin_device_id": origin_device_id,
                    "origin_area": origin_area,
                    "language": language,
                },
            )
            return

        if kind == "snooze":
            # Legacy rows only: new snoozes are scheduled as plain timers. The
            # snooze delay has already elapsed, so ring now instead of
            # scheduling a second timer of the same length.
            await self._dispatch_background_event(
                "timer_notification",
                {
                    "timer_name": logical_name,
                    "entity_id": f"agenthub_internal:{row['id']}",
                    "media_player": payload.get("media_player"),
                    "origin_device_id": origin_device_id,
                    "origin_area": origin_area,
                    "duration": duration_str,
                    "language": language,
                },
            )
            return

        logger.warning("Unknown timer kind for %s: %s", row["id"], kind)


def _load_payload(row: dict) -> dict[str, Any]:
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _seconds_to_hms(total: int) -> str:
    if total <= 0:
        return "00:00:00"
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def get_timer_scheduler(app: Any = None) -> TimerScheduler | None:
    """Return the scheduler stored on ``app.state.timer_scheduler``, or None."""
    if app is None:
        return None
    return getattr(app.state, "timer_scheduler", None)
