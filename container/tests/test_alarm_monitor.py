from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.alarm_monitor import AlarmMonitor

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _visible_by_default():
    with patch("app.agents.alarm_monitor.entity_is_visible", new=AsyncMock(return_value=True)) as visible:
        yield visible


def _settings(label: str | None = "agenthub_alarm") -> SimpleNamespace:
    return SimpleNamespace(get_value=AsyncMock(return_value=label))


def _labeling_client(*labeled: str) -> MagicMock:
    """HA client stub answering the label_entities() template with ``labeled``."""
    client = MagicMock(spec=["render_template"])
    client.render_template = AsyncMock(return_value=",".join(labeled) or None)
    return client


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
    ha_client = _labeling_client("input_datetime.morning_alarm")
    monitor = AlarmMonitor(entity_index, dispatcher, ha_client=ha_client, settings_repo=_settings())

    fake_datetime = MagicMock(wraps=datetime)
    fake_datetime.now.return_value = datetime(2026, 4, 24, 8, 30, 0)

    with patch("app.agents.alarm_monitor.datetime", fake_datetime):
        await monitor._check_alarms()
        await monitor._check_alarms()

    assert ha_client.render_template.await_args.args[0] == "{{ label_entities('agenthub_alarm') | join(',') }}"
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


def _berlin_monitor(state: str = "07:00:00") -> tuple[AlarmMonitor, MagicMock]:
    entry = SimpleNamespace(
        entity_id="input_datetime.wake_up",
        friendly_name="Wake Up",
        state=state,
        has_date=False,
        has_time=True,
    )
    entity_index = MagicMock()
    entity_index.list_entries_async = AsyncMock(return_value=[entry])
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    ha_client = MagicMock()
    ha_client.get_config = AsyncMock(return_value={"time_zone": "Europe/Berlin"})
    ha_client.render_template = AsyncMock(return_value="input_datetime.wake_up")
    return AlarmMonitor(entity_index, dispatcher, ha_client=ha_client, settings_repo=_settings()), dispatcher


def _utc_clock(utc_now: datetime) -> MagicMock:
    """Patch target whose now(tz) behaves like a real clock fixed at ``utc_now`` (aware, UTC)."""
    fake_datetime = MagicMock(wraps=datetime)
    fake_datetime.now.side_effect = lambda tz=None: (
        utc_now.astimezone(tz) if tz is not None else utc_now.replace(tzinfo=None)
    )
    return fake_datetime


@pytest.fixture
def _fresh_home_context():
    from app.ha_client.home_context import HomeContextProvider

    with patch("app.ha_client.home_context.home_context_provider", HomeContextProvider()):
        yield


@pytest.mark.usefixtures("_fresh_home_context")
async def test_alarm_monitor_fires_at_ha_local_time_not_utc() -> None:
    # 07:00 Europe/Berlin (CEST, UTC+2) == 05:00 UTC.
    monitor, dispatcher = _berlin_monitor("07:00:00")
    with patch("app.agents.alarm_monitor.datetime", _utc_clock(datetime(2026, 6, 15, 7, 0, 10, tzinfo=UTC))):
        await monitor._check_alarms()
    dispatcher.dispatch.assert_not_awaited()

    monitor, dispatcher = _berlin_monitor("07:00:00")
    with patch("app.agents.alarm_monitor.datetime", _utc_clock(datetime(2026, 6, 15, 5, 0, 10, tzinfo=UTC))):
        await monitor._check_alarms()
    dispatcher.dispatch.assert_awaited_once()


@pytest.mark.usefixtures("_fresh_home_context")
async def test_alarm_monitor_does_not_fire_early() -> None:
    monitor, dispatcher = _berlin_monitor("07:00:00")
    # 30 s before 07:00 Berlin local.
    with patch("app.agents.alarm_monitor.datetime", _utc_clock(datetime(2026, 6, 15, 4, 59, 30, tzinfo=UTC))):
        await monitor._check_alarms()
    dispatcher.dispatch.assert_not_awaited()
    assert monitor.fired_today == []


@pytest.mark.usefixtures("_fresh_home_context")
async def test_alarm_monitor_fires_within_window_after_and_only_once() -> None:
    monitor, dispatcher = _berlin_monitor("07:00:00")
    with patch("app.agents.alarm_monitor.datetime", _utc_clock(datetime(2026, 6, 15, 5, 0, 45, tzinfo=UTC))):
        await monitor._check_alarms()
        await monitor._check_alarms()
    dispatcher.dispatch.assert_awaited_once()

    # Past the window: a fresh monitor must not fire.
    monitor, dispatcher = _berlin_monitor("07:00:00")
    with patch("app.agents.alarm_monitor.datetime", _utc_clock(datetime(2026, 6, 15, 5, 1, 30, tzinfo=UTC))):
        await monitor._check_alarms()
    dispatcher.dispatch.assert_not_awaited()


def _due_helper_monitor(*, labeled: tuple[str, ...], label: str | None = "agenthub_alarm"):
    entry = SimpleNamespace(
        entity_id="input_datetime.dishwasher_start",
        friendly_name="Dishwasher Start",
        state="08:30:00",
        has_date=False,
        has_time=True,
    )
    entity_index = MagicMock()
    entity_index.list_entries_async = AsyncMock(return_value=[entry])
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    ha_client = _labeling_client(*labeled)
    monitor = AlarmMonitor(entity_index, dispatcher, ha_client=ha_client, settings_repo=_settings(label))
    clock = MagicMock(wraps=datetime)
    clock.now.return_value = datetime(2026, 4, 24, 8, 30, 10)
    return monitor, dispatcher, ha_client, clock


async def test_unlabeled_datetime_helper_does_not_ring_and_warns_once(caplog) -> None:
    monitor, dispatcher, _client, clock = _due_helper_monitor(labeled=())
    with patch("app.agents.alarm_monitor.datetime", clock), caplog.at_level("WARNING"):
        await monitor._check_alarms()
        await monitor._check_alarms()
    dispatcher.dispatch.assert_not_awaited()
    warnings = [r for r in caplog.records if "none carries the HA label" in r.getMessage()]
    assert len(warnings) == 1


async def test_helper_with_another_label_set_does_not_ring() -> None:
    monitor, dispatcher, _client, clock = _due_helper_monitor(labeled=("input_datetime.wake_up",))
    with patch("app.agents.alarm_monitor.datetime", clock):
        await monitor._check_alarms()
    dispatcher.dispatch.assert_not_awaited()


async def test_labeled_helper_invisible_to_timer_agent_does_not_ring(_visible_by_default) -> None:
    _visible_by_default.return_value = False
    monitor, dispatcher, _client, clock = _due_helper_monitor(labeled=("input_datetime.dishwasher_start",))
    with patch("app.agents.alarm_monitor.datetime", clock):
        await monitor._check_alarms()
    dispatcher.dispatch.assert_not_awaited()
    assert _visible_by_default.await_args.args[:2] == ("timer-agent", "input_datetime.dishwasher_start")


async def test_labeled_visible_helper_rings() -> None:
    monitor, dispatcher, _client, clock = _due_helper_monitor(labeled=("input_datetime.dishwasher_start",))
    with patch("app.agents.alarm_monitor.datetime", clock):
        await monitor._check_alarms()
    dispatcher.dispatch.assert_awaited_once()


async def test_empty_label_setting_disables_helper_alarms() -> None:
    monitor, dispatcher, client, clock = _due_helper_monitor(labeled=("input_datetime.dishwasher_start",), label="")
    with patch("app.agents.alarm_monitor.datetime", clock):
        await monitor._check_alarms()
    dispatcher.dispatch.assert_not_awaited()
    client.render_template.assert_not_awaited()


async def test_unsafe_label_is_never_rendered_into_a_template() -> None:
    monitor, dispatcher, client, clock = _due_helper_monitor(
        labeled=("input_datetime.dishwasher_start",), label="x') }}{{ states"
    )
    with patch("app.agents.alarm_monitor.datetime", clock):
        await monitor._check_alarms()
    dispatcher.dispatch.assert_not_awaited()
    client.render_template.assert_not_awaited()
