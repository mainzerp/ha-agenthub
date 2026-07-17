"""Background alarm monitor for input_datetime entities."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime
from typing import Any

from app.a2a._request import build_send_request
from app.models.agent import BackgroundEvent, BackgroundTask, TaskContext

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 30.0  # seconds
_MATCH_WINDOW = 60  # seconds -- alarm matches if within this window


class AlarmMonitor:
    """Polls input_datetime entities and dispatches notifications when alarm time is reached."""

    def __init__(self, entity_index: Any, dispatcher: Any) -> None:
        self._entity_index = entity_index
        self._dispatcher = dispatcher
        self._fired: set[str] = set()
        self._last_reset_date: str = ""
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background monitoring task."""
        self._task = asyncio.create_task(self._run())
        logger.info("AlarmMonitor started (interval=%ss, window=%ss)", _CHECK_INTERVAL, _MATCH_WINDOW)

    async def stop(self) -> None:
        """Stop the background monitoring task."""
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("AlarmMonitor stopped")

    @property
    def fired_today(self) -> list[str]:
        """Return list of entity_ids that have fired today."""
        return list(self._fired)

    async def _run(self) -> None:
        """Main loop: check alarms every _CHECK_INTERVAL seconds."""
        while True:
            try:
                await self._check_alarms()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("AlarmMonitor check failed", exc_info=True)
            await asyncio.sleep(_CHECK_INTERVAL)

    async def _check_alarms(self) -> None:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        # Reset fired set at midnight
        if today_str != self._last_reset_date:
            self._fired.clear()
            self._last_reset_date = today_str

        try:
            entries = await self._entity_index.list_entries_async(domains={"input_datetime"})
        except Exception:
            logger.warning("Failed to read input_datetime entries in AlarmMonitor", exc_info=True)
            return

        for entry in entries:
            entity_id = entry.entity_id
            if not entity_id.startswith("input_datetime."):
                continue
            if not entry.has_time:
                continue

            state_val = entry.state or ""
            if not state_val or state_val == "unknown":
                continue

            alarm_time = self._parse_alarm_time(
                state_val,
                {"has_date": entry.has_date, "has_time": entry.has_time},
                now,
            )
            if alarm_time is None:
                continue

            fire_key = f"{entity_id}:{today_str}"
            delta = abs((now - alarm_time).total_seconds())
            if delta <= _MATCH_WINDOW and fire_key not in self._fired:
                self._fired.add(fire_key)
                friendly_name = entry.friendly_name or entity_id
                logger.info("Alarm triggered: %s (%s)", entity_id, friendly_name)
                await self._fire_notification(entry)

    def _parse_alarm_time(self, state_val: str, attrs: dict, now: datetime) -> datetime | None:
        """Parse input_datetime state into a datetime for comparison."""
        has_date = attrs.get("has_date", False)
        has_time = attrs.get("has_time", False)

        try:
            if has_date and has_time:
                return datetime.strptime(state_val, "%Y-%m-%d %H:%M:%S")
            elif has_time and not has_date:
                time_parts = state_val.split(":")
                return now.replace(
                    hour=int(time_parts[0]),
                    minute=int(time_parts[1]),
                    second=int(time_parts[2]) if len(time_parts) > 2 else 0,
                    microsecond=0,
                )
        except (ValueError, IndexError):
            return None
        return None

    async def _fire_notification(self, entry: Any) -> None:
        """Dispatch alarm notification through the dispatcher."""
        entity_id = entry.entity_id
        friendly_name = entry.friendly_name or entity_id
        try:
            event_context = TaskContext(source="background")
            event_context.background_event = BackgroundEvent(
                event_type="alarm_notification",
                payload={
                    "alarm_name": friendly_name,
                    "briefing": False,
                    "entity_id": entity_id,
                    "media_player": getattr(entry, "media_player", None),
                    "origin_device_id": getattr(entry, "origin_device_id", None),
                    "origin_area": getattr(entry, "area", None),
                    "language": getattr(entry, "language", None),
                },
            )
            task = BackgroundTask(context=event_context)
            request = build_send_request(
                "orchestrator",
                task,
                request_id=str(uuid.uuid4()),
            )
            await self._dispatcher.dispatch(request)
        except Exception:
            logger.error("Alarm notification dispatch failed for %s", entity_id, exc_info=True)
