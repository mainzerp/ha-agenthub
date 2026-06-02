from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.wake_briefing import compose_wake_briefing
from tests.helpers import make_entity_index_entry

pytestmark = pytest.mark.asyncio


class _SettingsRepo:
    def __init__(self, overrides: dict[str, object] | None = None) -> None:
        defaults: dict[str, object] = {
            "wake_briefing.enabled": "true",
            "wake_briefing.sources.weather": "true",
            "wake_briefing.sources.date": "true",
            "wake_briefing.sources.news": "true",
            "wake_briefing.sources.calendar": "true",
            "wake_briefing.sources.sensors": "false",
            "wake_briefing.news_query": "top news today",
            "wake_briefing.news_count": "3",
            "wake_briefing.timeout_seconds": "10",
            "wake_briefing.sensor_entities": "[]",
            "wake_briefing.composer_prompt": "Compose a short wake briefing.",
        }
        if overrides:
            defaults.update(overrides)
        self._values = defaults

    async def get_value(self, key: str, default=None):
        return self._values.get(key, default)


class _HaClient:
    def __init__(self) -> None:
        self.get_calendar_events = AsyncMock(return_value=[])
        self.get_state = AsyncMock(return_value=None)


def _alarm_payload(**overrides):
    payload = {
        "alarm_name": "Morning Alarm",
        "alarm_label": "Morning Alarm",
        "language": "en",
        "scheduled_for_epoch": int(datetime(2026, 4, 27, 7, 0, 0, tzinfo=UTC).timestamp()),
        "timezone": "UTC",
        "origin_device_id": "device-bedroom",
        "origin_area": "bedroom",
    }
    payload.update(overrides)
    return payload


async def test_compose_wake_briefing_happy_path_uses_gateway_and_calendar_facts() -> None:
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(side_effect=[{"speech": "18 C and sunny."}, {"speech": "Headline one."}])
    ha_client = _HaClient()
    ha_client.get_calendar_events.return_value = [{"summary": "Standup", "start": "2026-04-27T09:00:00+00:00"}]
    entity_index = MagicMock()
    entity_index.list_entries_async = AsyncMock(
        return_value=[make_entity_index_entry(entity_id="calendar.work", domain="calendar", area="office")]
    )

    with (
        patch("app.entity.visibility.EntityVisibilityRepository.get_rules", new=AsyncMock(return_value=[])),
        patch(
            "app.db.repository.CalendarEntitySettingsRepository.get_enabled_entity_ids",
            new=AsyncMock(return_value=["calendar.work"]),
        ),
        patch(
            "app.db.repository.CalendarEntitySettingsRepository.get_universal_entity_ids",
            new=AsyncMock(return_value=[]),
        ),
        patch("app.agents.wake_briefing.complete", new=AsyncMock(return_value="Good morning.")) as complete_mock,
    ):
        result = await compose_wake_briefing(
            dispatcher,
            _alarm_payload(),
            ha_client=ha_client,
            entity_index=entity_index,
            settings_repo=_SettingsRepo(),
        )

    assert result == "Good morning."
    assert dispatcher.dispatch.call_count == 2
    second_call = dispatcher.dispatch.call_args_list[1]
    second_task = second_call.args[0].params["task"]
    assert second_task.description == "top news today. Return 3 concise headlines."
    facts = json.loads(complete_mock.await_args.kwargs["messages"][1]["content"])
    assert facts["date"] == "2026-04-27"
    assert facts["weekday"] == "Monday"
    assert facts["time"] == "07:00"
    assert facts["weather"] == "18 C and sunny."
    assert facts["news"] == "Headline one."
    assert facts["calendar"][0]["entity_id"] == "calendar.work"


async def test_compose_wake_briefing_omits_timed_out_source() -> None:
    async def _dispatch(request):
        task = request.params["task"]
        if task.description.startswith("top news today"):
            raise TimeoutError()
        return {"speech": "Clear and cool."}

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(side_effect=_dispatch)

    settings_repo = _SettingsRepo(
        {
            "wake_briefing.sources.calendar": "false",
            "wake_briefing.sources.sensors": "false",
        }
    )

    with (
        patch("app.entity.visibility.EntityVisibilityRepository.get_rules", new=AsyncMock(return_value=[])),
        patch("app.agents.wake_briefing.complete", new=AsyncMock(return_value="Briefing")) as complete_mock,
    ):
        result = await compose_wake_briefing(
            dispatcher,
            _alarm_payload(),
            ha_client=_HaClient(),
            entity_index=MagicMock(),
            settings_repo=settings_repo,
        )

    assert result == "Briefing"
    facts = json.loads(complete_mock.await_args.kwargs["messages"][1]["content"])
    assert facts["weather"] == "Clear and cool."
    assert "news" not in facts


async def test_compose_wake_briefing_top_level_timeout_falls_back_to_alarm_text() -> None:
    async def _slow_complete(**kwargs):
        raise TimeoutError()

    settings_repo = _SettingsRepo(
        {
            "wake_briefing.timeout_seconds": "1",
            "wake_briefing.sources.news": "false",
            "wake_briefing.sources.calendar": "false",
        }
    )

    with (
        patch("app.entity.visibility.EntityVisibilityRepository.get_rules", new=AsyncMock(return_value=[])),
        patch("app.agents.wake_briefing.complete", new=AsyncMock(side_effect=_slow_complete)),
    ):
        result = await compose_wake_briefing(
            MagicMock(dispatch_text=AsyncMock(return_value={"speech": "Sunny"})),
            _alarm_payload(alarm_label="Gentle Wake"),
            ha_client=_HaClient(),
            entity_index=MagicMock(),
            settings_repo=settings_repo,
        )

    assert result == "Alarm 'Gentle Wake' has triggered."


async def test_compose_wake_briefing_skips_hidden_calendar_entities() -> None:
    visible = make_entity_index_entry(entity_id="calendar.work", domain="calendar", area="office")
    hidden = make_entity_index_entry(entity_id="calendar.private", domain="calendar", area="bedroom")
    entity_index = MagicMock()
    entity_index.list_entries_async = AsyncMock(return_value=[visible, hidden])
    entity_index.get_by_id = MagicMock(
        side_effect=lambda entity_id: {visible.entity_id: visible, hidden.entity_id: hidden}.get(entity_id)
    )

    ha_client = _HaClient()
    ha_client.get_calendar_events = AsyncMock(
        return_value=[{"summary": "Visible Event", "start": "2026-04-27T10:00:00+00:00"}]
    )
    settings_repo = _SettingsRepo(
        {
            "wake_briefing.sources.weather": "false",
            "wake_briefing.sources.news": "false",
            "wake_briefing.sources.sensors": "false",
        }
    )

    with (
        patch(
            "app.entity.visibility.EntityVisibilityRepository.get_rules",
            new=AsyncMock(return_value=[{"rule_type": "area_include", "rule_value": "office"}]),
        ),
        patch(
            "app.db.repository.CalendarEntitySettingsRepository.get_enabled_entity_ids",
            new=AsyncMock(return_value=["calendar.work", "calendar.private"]),
        ),
        patch(
            "app.db.repository.CalendarEntitySettingsRepository.get_universal_entity_ids",
            new=AsyncMock(return_value=[]),
        ),
        patch("app.agents.wake_briefing.complete", new=AsyncMock(return_value="Calendar only")) as complete_mock,
    ):
        result = await compose_wake_briefing(
            MagicMock(dispatch_text=AsyncMock()),
            _alarm_payload(),
            ha_client=ha_client,
            entity_index=entity_index,
            settings_repo=settings_repo,
        )

    assert result == "Calendar only"
    ha_client.get_calendar_events.assert_awaited_once()
    assert ha_client.get_calendar_events.await_args.args[0] == "calendar.work"
    facts = json.loads(complete_mock.await_args.kwargs["messages"][1]["content"])
    assert [item["entity_id"] for item in facts["calendar"]] == ["calendar.work"]


async def test_wake_briefing_module_uses_gateway_boundary_only() -> None:
    source = (Path(__file__).resolve().parents[1] / "app" / "agents" / "wake_briefing.py").read_text(encoding="utf-8")

    assert "from app.a2a.protocol import JsonRpcRequest" in source
    assert "await dispatcher.dispatch(" in source
    assert "from app.agents." not in source
