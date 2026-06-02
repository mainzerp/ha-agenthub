from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.alarm_monitor import AlarmMonitor

pytestmark = pytest.mark.asyncio


async def test_alarm_monitor_reads_entity_index_and_dispatches_gateway() -> None:
    entry = SimpleNamespace(
        entity_id="input_datetime.morning_alarm",
        friendly_name="Morning Alarm",
        state="08:30:00",
        has_date=False,
        has_time=True,
        area="bedroom",
        origin_device_id="device-bedroom",
        media_player="media_player.bedroom",
        language="de",
    )
    entity_index = MagicMock()
    entity_index.list_entries_async = AsyncMock(return_value=[entry])
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    monitor = AlarmMonitor(entity_index, dispatcher)

    fake_datetime = MagicMock(wraps=datetime)
    fake_datetime.now.return_value = datetime(2026, 4, 24, 8, 30, 0)

    with patch("app.agents.alarm_monitor.datetime", fake_datetime):
        await monitor._check_alarms()
        await monitor._check_alarms()

    entity_index.list_entries_async.assert_awaited()
    dispatcher.dispatch.assert_awaited_once()
    call_args = dispatcher.dispatch.await_args.args[0]
    assert call_args.method == "message/send"
    task_params = call_args.params["task"]
    assert task_params.context.background_event.payload["entity_id"] == "input_datetime.morning_alarm"
    assert task_params.context.background_event.payload["briefing"] is False
    assert task_params.context.background_event.payload["origin_area"] == "bedroom"
    assert task_params.context.background_event.payload["origin_device_id"] == "device-bedroom"
    assert task_params.context.background_event.payload["media_player"] == "media_player.bedroom"
    assert task_params.context.background_event.payload["language"] == "de"


async def test_alarm_monitor_resets_fired_set_on_new_day() -> None:
    entity_index = MagicMock()
    entity_index.list_entries_async = AsyncMock(return_value=[])
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    monitor = AlarmMonitor(entity_index, dispatcher)
    monitor._fired = {"input_datetime.old:2026-04-23"}
    monitor._last_reset_date = "2026-04-23"

    fake_datetime = MagicMock(wraps=datetime)
    fake_datetime.now.return_value = datetime(2026, 4, 24, 0, 1, 0)

    with patch("app.agents.alarm_monitor.datetime", fake_datetime):
        await monitor._check_alarms()

    assert monitor.fired_today == []
