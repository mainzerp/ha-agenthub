"""Bootstrap: AlarmMonitor and TimerScheduler initialization."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.entity.index import EntityIndex

logger = logging.getLogger(__name__)


async def setup_monitors(
    app: FastAPI,
    source: str,
    entity_index: EntityIndex,
    dispatcher,
) -> None:
    """Create and start AlarmMonitor and TimerScheduler.

    Stores ``alarm_monitor`` and ``timer_scheduler`` on ``app.state``.
    """
    alarm_monitor = getattr(app.state, "alarm_monitor", None)
    if alarm_monitor is None:
        from app.agents.alarm_monitor import AlarmMonitor

        alarm_monitor = AlarmMonitor(entity_index, dispatcher)
        await alarm_monitor.start()
        app.state.alarm_monitor = alarm_monitor

    timer_scheduler = getattr(app.state, "timer_scheduler", None)
    if timer_scheduler is None:
        from app.agents.timer_scheduler import TimerScheduler
        from app.db.repository import ScheduledTimersRepository

        timer_scheduler = TimerScheduler(
            ScheduledTimersRepository,
            dispatcher=dispatcher,
        )
        await timer_scheduler.start()
        app.state.timer_scheduler = timer_scheduler
