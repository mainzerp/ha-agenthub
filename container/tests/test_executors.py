"""Tests for app.agents -- all specialized agents, orchestrator, rewrite, and custom loader."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

# Mock litellm before importing any app modules that depend on it
_litellm_mock = MagicMock()


class _AuthenticationError(Exception):
    pass


class _APIError(Exception):
    pass


class _RateLimitError(Exception):
    pass


_litellm_mock.exceptions.AuthenticationError = _AuthenticationError
_litellm_mock.exceptions.APIError = _APIError
_litellm_mock.RateLimitError = _RateLimitError
sys.modules.setdefault("litellm", _litellm_mock)

import app.llm.client  # noqa: E402,F401 -- force module load for patch targets
from app.agents.automation_executor import execute_automation_action  # noqa: E402
from app.agents.climate_executor import execute_climate_action  # noqa: E402
from app.agents.cover_executor import execute_cover_action  # noqa: E402
from app.agents.light_executor import execute_light_action  # noqa: E402
from app.agents.media_executor import execute_media_action  # noqa: E402
from app.agents.music_executor import execute_music_action  # noqa: E402
from app.agents.scene_executor import execute_scene_action  # noqa: E402
from app.agents.security_executor import execute_security_action  # noqa: E402
from app.agents.vacuum_executor import execute_vacuum_action  # noqa: E402
from app.models.agent import (  # noqa: E402
    AgentTask,
    TaskContext,
)
from tests.helpers import make_agent_task, make_entity_index_entry  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    description: str = "turn on kitchen light", user_text: str | None = None, context: TaskContext | None = None
) -> AgentTask:
    return make_agent_task(
        description=description,
        user_text=user_text or description,
        context=context,
    )


# ---------------------------------------------------------------------------
# BaseAgent abstract contract
# ---------------------------------------------------------------------------


class TestClimateExecutor:
    async def test_unknown_action_returns_failure(self):
        result = await execute_climate_action(
            {"action": "unknown", "entity": "thermostat"}, MagicMock(), MagicMock(), MagicMock()
        )
        assert result["success"] is False
        assert "Unknown action" in result["speech"]

    async def test_entity_not_found(self):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        index = MagicMock()
        index.search = MagicMock(return_value=[])
        result = await execute_climate_action(
            {"action": "set_temperature", "entity": "nonexistent", "parameters": {"temperature": 72}},
            MagicMock(),
            index,
            matcher,
        )
        assert result["success"] is False
        assert "Could not find" in result["speech"]

    async def test_set_temperature_calls_service(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="climate.living_room", friendly_name="Living Room")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.call_service = AsyncMock()
        ha.get_state = AsyncMock(return_value={"state": "heat"})
        result = await execute_climate_action(
            {"action": "set_temperature", "entity": "thermostat", "parameters": {"temperature": 72}},
            ha,
            MagicMock(),
            matcher,
        )
        assert result["success"] is True
        ha.call_service.assert_awaited_once_with(
            "climate", "set_temperature", "climate.living_room", {"temperature": 72.0}
        )

    async def test_service_call_failure(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="climate.living_room", friendly_name="Living Room")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.call_service = AsyncMock(side_effect=Exception("Connection refused"))
        result = await execute_climate_action(
            {"action": "turn_off", "entity": "thermostat", "parameters": {}}, ha, MagicMock(), matcher
        )
        assert result["success"] is False
        assert "Failed" in result["speech"]

    async def test_turn_off_skips_when_already_off(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="climate.living_room", friendly_name="Living Room")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.get_state = AsyncMock(return_value={"state": "off"})
        result = await execute_climate_action(
            {"action": "turn_off", "entity": "thermostat", "parameters": {}}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        assert "already off" in result["speech"]
        ha.call_service.assert_not_awaited()

    async def test_turn_on_skips_when_already_heat(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="climate.living_room", friendly_name="Living Room")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.get_state = AsyncMock(return_value={"state": "heat"})
        result = await execute_climate_action(
            {"action": "turn_on", "entity": "thermostat", "parameters": {}}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        assert "already" in result["speech"]
        ha.call_service.assert_not_awaited()


# ---------------------------------------------------------------------------
# Security Executor
# ---------------------------------------------------------------------------


class TestSecurityExecutor:
    async def test_unknown_action_returns_failure(self):
        result = await execute_security_action(
            {"action": "unknown", "entity": "door"}, MagicMock(), MagicMock(), MagicMock()
        )
        assert result["success"] is False

    async def test_lock_calls_correct_service(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="lock.front_door", friendly_name="Front Door")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.call_service = AsyncMock()
        ha.get_state = AsyncMock(return_value={"state": "unlocked"})
        result = await execute_security_action(
            {"action": "lock", "entity": "front door", "parameters": {}}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        ha.call_service.assert_awaited_once_with("lock", "lock", "lock.front_door", None)

    async def test_alarm_arm_away_calls_correct_service(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="alarm_control_panel.home", friendly_name="Home Alarm")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.call_service = AsyncMock()
        ha.get_state = AsyncMock(return_value={"state": "disarmed"})
        result = await execute_security_action(
            {"action": "alarm_arm_away", "entity": "house alarm", "parameters": {}}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        ha.call_service.assert_awaited_once_with(
            "alarm_control_panel", "alarm_arm_away", "alarm_control_panel.home", None
        )

    async def test_unlock_with_code(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="lock.front_door", friendly_name="Front Door")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.call_service = AsyncMock()
        ha.get_state = AsyncMock(return_value={"state": "locked"})
        result = await execute_security_action(
            {"action": "unlock", "entity": "front door", "parameters": {"code": "1234"}}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        ha.call_service.assert_awaited_once_with("lock", "unlock", "lock.front_door", {"code": "1234"})

    async def test_lock_skips_when_already_locked(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="lock.front_door", friendly_name="Front Door")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.get_state = AsyncMock(return_value={"state": "locked"})
        result = await execute_security_action(
            {"action": "lock", "entity": "front door", "parameters": {}}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        assert "already locked" in result["speech"]
        ha.call_service.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cover Executor
# ---------------------------------------------------------------------------


class TestCoverExecutor:
    async def test_open_cover_skips_when_already_open(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="cover.living_room", friendly_name="Living Room Blind")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.get_state = AsyncMock(return_value={"state": "open"})
        result = await execute_cover_action(
            {"action": "open_cover", "entity": "living room blind"}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        assert "already open" in result["speech"]
        ha.call_service.assert_not_awaited()

    async def test_close_cover_skips_when_already_closed(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="cover.living_room", friendly_name="Living Room Blind")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.get_state = AsyncMock(return_value={"state": "closed"})
        result = await execute_cover_action(
            {"action": "close_cover", "entity": "living room blind"}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        assert "already closed" in result["speech"]
        ha.call_service.assert_not_awaited()


# ---------------------------------------------------------------------------
# Status/State Query Tests (all domain executors)
# ---------------------------------------------------------------------------


class TestLightExecutorQueries:
    async def test_query_light_state_on(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"brightness": 128, "color_name": "red", "friendly_name": "Kitchen Light"},
            }
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="light.kitchen", friendly_name="Kitchen Light")])
        result = await execute_light_action(
            {"action": "query_light_state", "entity": "kitchen light"},
            ha,
            None,
            matcher,
            agent_id="light-agent",
        )
        assert result["success"]
        assert "Kitchen Light" in result["speech"]
        assert "on" in result["speech"]
        assert "50%" in result["speech"]  # 128/255 ~= 50%

    async def test_query_light_state_switch(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(return_value={"state": "on", "attributes": {"friendly_name": "Garden Pump"}})
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="switch.garden_pump", friendly_name="Garden Pump")])
        result = await execute_light_action(
            {"action": "query_light_state", "entity": "garden pump"},
            ha,
            None,
            matcher,
            agent_id="light-agent",
        )
        assert result["success"]
        assert "Garden Pump" in result["speech"]
        assert "on" in result["speech"]

    async def test_query_light_state_not_found(self):
        ha = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_light_action(
            {"action": "query_light_state", "entity": "nonexistent light"},
            ha,
            None,
            matcher,
            agent_id="light-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]

    async def test_query_light_state_ha_error(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(side_effect=Exception("HA down"))
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="light.kitchen", friendly_name="Kitchen Light")])
        result = await execute_light_action(
            {"action": "query_light_state", "entity": "kitchen light"},
            ha,
            None,
            matcher,
            agent_id="light-agent",
        )
        assert not result["success"]
        assert "Failed" in result["speech"]

    async def test_list_lights(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(
            return_value=[
                {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}},
                {"entity_id": "light.bedroom", "state": "off", "attributes": {"friendly_name": "Bedroom Light"}},
                {"entity_id": "switch.garden_pump", "state": "on", "attributes": {"friendly_name": "Garden Pump"}},
            ]
        )
        result = await execute_light_action(
            {"action": "list_lights", "entity": ""},
            ha,
            None,
            None,
            agent_id="light-agent",
        )
        assert result["success"]
        assert "Kitchen Light" in result["speech"]
        assert "Garden Pump" in result["speech"]

    async def test_list_lights_empty(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(return_value=[])
        result = await execute_light_action(
            {"action": "list_lights", "entity": ""},
            ha,
            None,
            None,
            agent_id="light-agent",
        )
        assert result["success"]
        assert "No light" in result["speech"]


class TestClimateExecutorQueries:
    async def test_query_climate_state(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "heat",
                "attributes": {
                    "current_temperature": 21.5,
                    "temperature": 23,
                    "current_humidity": 45,
                    "fan_mode": "auto",
                    "friendly_name": "Living Room Thermostat",
                },
            }
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="climate.living_room", friendly_name="Living Room Thermostat")]
        )
        result = await execute_climate_action(
            {"action": "query_climate_state", "entity": "living room thermostat"},
            ha,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"]
        assert "heat" in result["speech"]
        assert "21.5" in result["speech"]

    async def test_query_climate_state_not_found(self):
        ha = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_climate_action(
            {"action": "query_climate_state", "entity": "nonexistent"},
            ha,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]

    async def test_query_climate_state_ha_error(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(side_effect=Exception("HA down"))
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="climate.living_room", friendly_name="Thermostat")])
        result = await execute_climate_action(
            {"action": "query_climate_state", "entity": "thermostat"},
            ha,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert not result["success"]
        assert "Failed" in result["speech"]

    async def test_list_climate(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "climate.living_room",
                    "state": "heat",
                    "attributes": {"friendly_name": "Living Room", "current_temperature": 21.5, "temperature": 23},
                },
                {
                    "entity_id": "sensor.outdoor_temperature",
                    "state": "15.2",
                    "attributes": {"friendly_name": "Outdoor Temp", "unit_of_measurement": "C"},
                },
            ]
        )
        result = await execute_climate_action(
            {"action": "list_climate", "entity": ""},
            ha,
            None,
            None,
            agent_id="climate-agent",
        )
        assert result["success"]
        assert "Living Room" in result["speech"]
        assert "Outdoor Temp" in result["speech"]

    async def test_list_climate_empty(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(return_value=[])
        result = await execute_climate_action(
            {"action": "list_climate", "entity": ""},
            ha,
            None,
            None,
            agent_id="climate-agent",
        )
        assert result["success"]
        assert "No climate" in result["speech"]

    async def test_query_weather_success(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "sunny",
                "attributes": {
                    "friendly_name": "Home",
                    "temperature": 22,
                    "temperature_unit": "C",
                    "humidity": 60,
                    "wind_speed": 10,
                    "wind_speed_unit": "km/h",
                },
            }
        )
        ha.get_states = AsyncMock(
            return_value=[
                {"entity_id": "weather.home", "state": "sunny", "attributes": {"friendly_name": "Home"}},
            ]
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="weather.home", friendly_name="Home")])
        result = await execute_climate_action(
            {"action": "query_weather", "entity": "home"},
            ha,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"]
        assert "sunny" in result["speech"]
        assert "22" in result["speech"]

    async def test_query_weather_auto_discover(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "cloudy",
                "attributes": {"friendly_name": "Home Weather", "temperature": 15},
            }
        )
        ha.get_states = AsyncMock(
            return_value=[
                {"entity_id": "weather.home", "state": "cloudy", "attributes": {"friendly_name": "Home Weather"}},
            ]
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_climate_action(
            {"action": "query_weather", "entity": ""},
            ha,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"]
        assert "cloudy" in result["speech"]

    async def test_query_weather_auto_discover_picks_only_visible_entity(self, monkeypatch):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            side_effect=lambda entity_id: {
                "state": "sunny" if entity_id == "weather.visible" else "stormy",
                "attributes": {
                    "friendly_name": "Visible Weather" if entity_id == "weather.visible" else "Hidden Weather",
                    "temperature": 21 if entity_id == "weather.visible" else 8,
                },
            }
        )
        entity_entries = [
            make_entity_index_entry("weather.hidden", "Hidden Weather", area="roof"),
            make_entity_index_entry("weather.visible", "Visible Weather", area="garden"),
        ]
        entity_index = MagicMock()
        entity_index.list_entries_async = AsyncMock(return_value=entity_entries)
        entity_index.get_by_id = MagicMock(
            side_effect=lambda entity_id: next(
                (entry for entry in entity_entries if entry.entity_id == entity_id), None
            )
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        monkeypatch.setattr(
            "app.entity.visibility.EntityVisibilityRepository.get_rules",
            AsyncMock(return_value=[{"rule_type": "area_exclude", "rule_value": "roof"}]),
        )

        result = await execute_climate_action(
            {"action": "query_weather", "entity": ""},
            ha,
            entity_index,
            matcher,
            agent_id="climate-agent",
        )

        assert result["success"]
        assert result["entity_id"] == "weather.visible"
        ha.get_state.assert_awaited_once_with("weather.visible")

    async def test_query_weather_named_entity_uses_deterministic_resolution(self, monkeypatch):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "rainy",
                "attributes": {"friendly_name": "Garden Weather", "temperature": 14},
            }
        )
        monkeypatch.setattr(
            "app.entity.visibility.EntityVisibilityRepository.get_rules",
            AsyncMock(return_value=[]),
        )
        entity_entries = [
            make_entity_index_entry("weather.garden", "Garden Weather", area="garden"),
            make_entity_index_entry("weather.home", "Home Weather", area="house"),
        ]
        entity_index = MagicMock()
        entity_index.list_entries_async = AsyncMock(return_value=entity_entries)
        entity_index.get_by_id = MagicMock(side_effect=lambda entity_id: None)
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="weather.home", friendly_name="Home Weather")])

        result = await execute_climate_action(
            {"action": "query_weather", "entity": "Garden Weather"},
            ha,
            entity_index,
            matcher,
            agent_id="climate-agent",
        )

        assert result["success"]
        assert result["entity_id"] == "weather.garden"
        matcher.match.assert_not_awaited()

    async def test_query_weather_forecast_success(self):
        ha = AsyncMock()
        ha.call_service = AsyncMock(
            return_value={
                "weather.home": {
                    "forecast": [
                        {"datetime": "2025-01-16T00:00:00", "condition": "rainy", "temperature": 14, "templow": 5},
                    ],
                },
            }
        )
        ha.get_states = AsyncMock(
            return_value=[
                {"entity_id": "weather.home", "state": "sunny", "attributes": {"friendly_name": "Home"}},
            ]
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="weather.home", friendly_name="Home")])
        result = await execute_climate_action(
            {"action": "query_weather_forecast", "entity": "home"},
            ha,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"]
        assert "rainy" in result["speech"]
        ha.call_service.assert_awaited_once_with(
            "weather", "get_forecasts", "weather.home", {"type": "daily"}, return_response=True
        )

    async def test_query_weather_forecast_success_with_websocket_response_shape(self):
        ha = AsyncMock()
        ha.call_service = AsyncMock(
            return_value={
                "weather.home": {
                    "forecast": [
                        {"datetime": "2025-01-16T00:00:00", "condition": "rainy", "temperature": 14, "templow": 5},
                    ],
                },
            }
        )
        ha.get_states = AsyncMock(
            return_value=[
                {"entity_id": "weather.home", "state": "sunny", "attributes": {"friendly_name": "Home"}},
            ]
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="weather.home", friendly_name="Home")])
        result = await execute_climate_action(
            {"action": "query_weather_forecast", "entity": "home"},
            ha,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"]
        assert "rainy" in result["speech"]
        ha.call_service.assert_awaited_once_with(
            "weather", "get_forecasts", "weather.home", {"type": "daily"}, return_response=True
        )

    async def test_query_weather_forecast_hourly(self):
        ha = AsyncMock()
        ha.call_service = AsyncMock(
            return_value={
                "weather.home": {
                    "forecast": [
                        {"datetime": "2025-01-16T01:00:00", "condition": "clear-night", "temperature": 8},
                        {"datetime": "2025-01-16T02:00:00", "condition": "clear-night", "temperature": 7},
                    ],
                },
            }
        )
        ha.get_states = AsyncMock(
            return_value=[
                {"entity_id": "weather.home", "state": "sunny", "attributes": {"friendly_name": "Home"}},
            ]
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="weather.home", friendly_name="Home")])
        result = await execute_climate_action(
            {"action": "query_weather_forecast", "entity": "home", "parameters": {"type": "hourly"}},
            ha,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"]
        assert "clear-night" in result["speech"]
        ha.call_service.assert_awaited_once_with(
            "weather", "get_forecasts", "weather.home", {"type": "hourly"}, return_response=True
        )


class TestAutomationExecutorQueries:
    async def test_query_automation_state(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"last_triggered": "2024-01-15T10:30:00", "friendly_name": "Morning Routine"},
            }
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="automation.morning_routine", friendly_name="Morning Routine")]
        )
        result = await execute_automation_action(
            {"action": "query_automation_state", "entity": "morning routine"},
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"]
        assert "enabled" in result["speech"]
        assert "last triggered" in result["speech"]

    async def test_query_automation_state_not_found(self):
        ha = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_automation_action(
            {"action": "query_automation_state", "entity": "nonexistent"},
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]

    async def test_query_automation_state_ha_error(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(side_effect=Exception("HA down"))
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="automation.morning_routine", friendly_name="Morning Routine")]
        )
        result = await execute_automation_action(
            {"action": "query_automation_state", "entity": "morning routine"},
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert not result["success"]
        assert "Failed" in result["speech"]

    async def test_query_automation_state_with_direct_entity_id(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"last_triggered": "2024-01-15T10:30:00", "friendly_name": "Morning Routine"},
            }
        )
        result = await execute_automation_action(
            {"action": "query_automation_state", "entity_id": "automation.morning_routine"},
            ha,
            None,
            None,
            agent_id="automation-agent",
        )
        assert result["success"]
        assert result["entity_id"] == "automation.morning_routine"
        assert result["metadata"]["resolution_path"] == "llm_entity_id"
        ha.get_state.assert_awaited_once_with("automation.morning_routine")

    async def test_query_automation_state_direct_entity_id_wrong_domain_falls_back(self):
        ha = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_automation_action(
            {"action": "query_automation_state", "entity_id": "light.kitchen"},
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]
        ha.get_state.assert_not_awaited()

    async def test_list_automations(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "automation.morning_routine",
                    "state": "on",
                    "attributes": {"friendly_name": "Morning Routine", "last_triggered": "2024-01-15T10:30:00"},
                },
                {"entity_id": "automation.night_mode", "state": "off", "attributes": {"friendly_name": "Night Mode"}},
            ]
        )
        result = await execute_automation_action(
            {"action": "list_automations", "entity": ""},
            ha,
            None,
            None,
            agent_id="automation-agent",
        )
        assert result["success"]
        assert "Morning Routine" in result["speech"]
        assert "Night Mode" in result["speech"]

    async def test_list_automations_empty(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(return_value=[])
        result = await execute_automation_action(
            {"action": "list_automations", "entity": ""},
            ha,
            None,
            None,
            agent_id="automation-agent",
        )
        assert result["success"]
        assert "No automation" in result["speech"]


class TestAutomationExecutorCrud:
    async def test_create_automation_success(self):
        ha = AsyncMock()
        ha.get_automation_config = AsyncMock(return_value=None)
        ha.save_automation_config = AsyncMock(return_value={"result": "ok"})
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_automation_action(
            {
                "action": "create_automation",
                "entity": "kitchen sunset",
                "parameters": {
                    "config": {
                        "alias": "Kitchen Sunset",
                        "triggers": [{"platform": "sun", "event": "sunset"}],
                        "actions": [{"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}],
                    }
                },
            },
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"] is True
        assert "created" in result["speech"]
        assert result["entity_id"].startswith("ah_")

    async def test_create_automation_without_alias_uses_entity(self):
        ha = AsyncMock()
        ha.get_automation_config = AsyncMock(return_value=None)
        ha.save_automation_config = AsyncMock(return_value={"result": "ok"})
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_automation_action(
            {
                "action": "create_automation",
                "entity": "kitchen sunset",
                "parameters": {
                    "config": {
                        "triggers": [{"platform": "sun", "event": "sunset"}],
                        "actions": [{"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}],
                    }
                },
            },
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"] is True
        assert "created" in result["speech"]
        saved_config = ha.save_automation_config.call_args[0][1]
        assert saved_config.get("alias") == "kitchen sunset"

    async def test_create_automation_id_collision_generates_unique_id(self):
        ha = AsyncMock()
        ha.get_automation_config = AsyncMock(side_effect=[{"existing": True}, None])
        ha.save_automation_config = AsyncMock(return_value={"result": "ok"})
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_automation_action(
            {
                "action": "create_automation",
                "entity": "kitchen sunset",
                "parameters": {
                    "config": {
                        "alias": "Kitchen Sunset",
                        "triggers": [],
                        "actions": [],
                    }
                },
            },
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"] is True
        assert result["entity_id"].endswith("_2")

    async def test_create_automation_ha_error(self):
        ha = AsyncMock()
        ha.get_automation_config = AsyncMock(return_value=None)
        ha.save_automation_config = AsyncMock(side_effect=Exception("Connection refused"))
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_automation_action(
            {
                "action": "create_automation",
                "entity": "kitchen sunset",
                "parameters": {
                    "config": {
                        "alias": "Kitchen Sunset",
                        "triggers": [],
                        "actions": [],
                    }
                },
            },
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"] is False
        assert "Failed" in result["speech"]

    async def test_update_automation_success(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"id": "morning_routine_001", "friendly_name": "Morning Routine"},
            }
        )
        ha.save_automation_config = AsyncMock(return_value={"result": "ok"})
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="automation.morning_routine", friendly_name="Morning Routine")]
        )
        result = await execute_automation_action(
            {
                "action": "update_automation",
                "entity": "morning routine",
                "parameters": {
                    "config": {
                        "alias": "Morning Routine",
                        "triggers": [{"platform": "time", "at": "07:00:00"}],
                        "actions": [{"service": "light.turn_on", "target": {"entity_id": "light.bedroom"}}],
                    }
                },
            },
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"] is True
        assert "updated" in result["speech"]

    async def test_update_automation_missing_config_id(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"friendly_name": "Morning Routine"},
            }
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="automation.morning_routine", friendly_name="Morning Routine")]
        )
        result = await execute_automation_action(
            {
                "action": "update_automation",
                "entity": "morning routine",
                "parameters": {
                    "config": {
                        "alias": "Morning Routine",
                        "triggers": [],
                        "actions": [],
                    }
                },
            },
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"] is False
        assert "editable configuration" in result["speech"]

    async def test_delete_automation_success(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"id": "abc123", "friendly_name": "Vacation Mode"},
            }
        )
        ha.delete_automation_config = AsyncMock(return_value={"result": "ok"})
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="automation.vacation_mode", friendly_name="Vacation Mode")]
        )
        result = await execute_automation_action(
            {
                "action": "delete_automation",
                "entity": "vacation mode",
                "parameters": {},
            },
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"] is True
        assert "deleted" in result["speech"]

    async def test_delete_automation_not_found(self):
        ha = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_automation_action(
            {
                "action": "delete_automation",
                "entity": "nonexistent",
                "parameters": {},
            },
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"] is False
        assert "Could not find" in result["speech"]

    async def test_get_automation_config_success(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"id": "motion_sensor_001", "friendly_name": "Motion Sensor"},
            }
        )
        ha.get_automation_config = AsyncMock(
            return_value={
                "alias": "Motion Sensor",
                "trigger": [{"platform": "state"}, {"platform": "time"}],
                "condition": [{"condition": "state"}],
                "action": [
                    {"service": "light.turn_on"},
                    {"service": "light.turn_off"},
                    {"service": "notify.mobile_app"},
                ],
            }
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="automation.motion_sensor", friendly_name="Motion Sensor")]
        )
        result = await execute_automation_action(
            {
                "action": "get_automation_config",
                "entity": "motion sensor",
                "parameters": {},
            },
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"] is True
        assert "trigger" in result["speech"]
        assert "condition" in result["speech"]
        assert "action" in result["speech"]

    async def test_get_automation_config_missing_config_id(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"friendly_name": "Motion Sensor"},
            }
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="automation.motion_sensor", friendly_name="Motion Sensor")]
        )
        result = await execute_automation_action(
            {
                "action": "get_automation_config",
                "entity": "motion sensor",
                "parameters": {},
            },
            ha,
            None,
            matcher,
            agent_id="automation-agent",
        )
        assert result["success"] is False

    async def test_get_automation_config_with_direct_entity_id(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"id": "motion_sensor_001", "friendly_name": "Motion Sensor"},
            }
        )
        ha.get_automation_config = AsyncMock(
            return_value={
                "alias": "Motion Sensor",
                "trigger": [{"platform": "state"}],
                "condition": [],
                "action": [{"service": "light.turn_on"}],
            }
        )
        result = await execute_automation_action(
            {
                "action": "get_automation_config",
                "entity_id": "automation.motion_sensor",
                "parameters": {},
            },
            ha,
            None,
            None,
            agent_id="automation-agent",
        )
        assert result["success"] is True
        assert result["entity_id"] == "automation.motion_sensor"
        assert result["metadata"]["resolution_path"] == "llm_entity_id"
        ha.get_state.assert_awaited_once_with("automation.motion_sensor")

    async def test_list_automations_shows_agenthub_marker(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "automation.morning_routine",
                    "state": "on",
                    "attributes": {"friendly_name": "Morning Routine", "id": "ah_test"},
                },
                {
                    "entity_id": "automation.night_mode",
                    "state": "off",
                    "attributes": {"friendly_name": "Night Mode"},
                },
            ]
        )
        result = await execute_automation_action(
            {"action": "list_automations", "entity": ""},
            ha,
            None,
            None,
            agent_id="automation-agent",
        )
        assert result["success"] is True
        assert "AgentHub" in result["speech"]


class TestSceneExecutorQueries:
    async def test_query_scene_found(self):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="scene.movie_night", friendly_name="Movie Night")])
        result = await execute_scene_action(
            {"action": "query_scene", "entity": "movie scene"},
            AsyncMock(),
            None,
            matcher,
            agent_id="scene-agent",
        )
        assert result["success"]
        assert "Movie Night" in result["speech"]

    async def test_query_scene_not_found(self):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_scene_action(
            {"action": "query_scene", "entity": "nonexistent scene"},
            AsyncMock(),
            None,
            matcher,
            agent_id="scene-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]

    async def test_query_scene_with_direct_entity_id(self):
        result = await execute_scene_action(
            {"action": "query_scene", "entity_id": "scene.movie_night"},
            AsyncMock(),
            None,
            None,
            agent_id="scene-agent",
        )
        assert result["success"]
        assert result["entity_id"] == "scene.movie_night"
        assert result["metadata"]["resolution_path"] == "llm_entity_id"

    async def test_query_scene_direct_entity_id_wrong_domain_falls_back(self):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_scene_action(
            {"action": "query_scene", "entity_id": "light.kitchen"},
            AsyncMock(),
            None,
            matcher,
            agent_id="scene-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]

    async def test_list_scenes(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(
            return_value=[
                {"entity_id": "scene.movie_night", "state": "scening", "attributes": {"friendly_name": "Movie Night"}},
                {"entity_id": "scene.bedtime", "state": "scening", "attributes": {"friendly_name": "Bedtime"}},
            ]
        )
        result = await execute_scene_action(
            {"action": "list_scenes", "entity": ""},
            ha,
            None,
            None,
            agent_id="scene-agent",
        )
        assert result["success"]
        assert "Movie Night" in result["speech"]
        assert "Bedtime" in result["speech"]

    async def test_list_scenes_empty(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(return_value=[])
        result = await execute_scene_action(
            {"action": "list_scenes", "entity": ""},
            ha,
            None,
            None,
            agent_id="scene-agent",
        )
        assert result["success"]
        assert "No scenes" in result["speech"]


class TestSecurityExecutorQueries:
    async def test_query_security_state_lock(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(return_value={"state": "locked", "attributes": {"friendly_name": "Front Door Lock"}})
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="lock.front_door", friendly_name="Front Door Lock")]
        )
        result = await execute_security_action(
            {"action": "query_security_state", "entity": "front door lock"},
            ha,
            None,
            matcher,
            agent_id="security-agent",
        )
        assert result["success"]
        assert "locked" in result["speech"]

    async def test_query_security_state_binary_sensor_motion(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={"state": "on", "attributes": {"friendly_name": "Backyard Motion", "device_class": "motion"}}
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="binary_sensor.backyard_motion", friendly_name="Backyard Motion")]
        )
        result = await execute_security_action(
            {"action": "query_security_state", "entity": "backyard motion sensor"},
            ha,
            None,
            matcher,
            agent_id="security-agent",
        )
        assert result["success"]
        assert "motion detected" in result["speech"]

    async def test_query_security_state_not_found(self):
        ha = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_security_action(
            {"action": "query_security_state", "entity": "nonexistent"},
            ha,
            None,
            matcher,
            agent_id="security-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]

    async def test_query_security_state_ha_error(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(side_effect=Exception("HA down"))
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="lock.front_door", friendly_name="Front Door Lock")]
        )
        result = await execute_security_action(
            {"action": "query_security_state", "entity": "front door lock"},
            ha,
            None,
            matcher,
            agent_id="security-agent",
        )
        assert not result["success"]
        assert "Failed" in result["speech"]

    async def test_list_security(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(
            return_value=[
                {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
                {
                    "entity_id": "alarm_control_panel.home",
                    "state": "armed_away",
                    "attributes": {"friendly_name": "Home Alarm"},
                },
                {
                    "entity_id": "binary_sensor.hallway_motion",
                    "state": "off",
                    "attributes": {"friendly_name": "Hallway Motion", "device_class": "motion"},
                },
            ]
        )
        result = await execute_security_action(
            {"action": "list_security", "entity": ""},
            ha,
            None,
            None,
            agent_id="security-agent",
        )
        assert result["success"]
        assert "Front Door" in result["speech"]
        assert "Home Alarm" in result["speech"]

    async def test_list_security_empty(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(return_value=[])
        result = await execute_security_action(
            {"action": "list_security", "entity": ""},
            ha,
            None,
            None,
            agent_id="security-agent",
        )
        assert result["success"]
        assert "No security" in result["speech"]


class TestMusicExecutorQueries:
    async def test_query_music_state(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "playing",
                "attributes": {
                    "media_title": "Bohemian Rhapsody",
                    "media_artist": "Queen",
                    "volume_level": 0.5,
                    "friendly_name": "Kitchen Speaker",
                },
            }
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="media_player.kitchen", friendly_name="Kitchen Speaker")]
        )
        result = await execute_music_action(
            {"action": "query_music_state", "entity": "kitchen speaker"},
            ha,
            None,
            matcher,
            agent_id="music-agent",
        )
        assert result["success"]
        assert "playing" in result["speech"]
        assert "Bohemian Rhapsody" in result["speech"]
        assert "Queen" in result["speech"]

    async def test_query_music_state_not_found(self):
        ha = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_music_action(
            {"action": "query_music_state", "entity": "nonexistent"},
            ha,
            None,
            matcher,
            agent_id="music-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]

    async def test_query_music_state_ha_error(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(side_effect=Exception("HA down"))
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="media_player.kitchen", friendly_name="Kitchen Speaker")]
        )
        result = await execute_music_action(
            {"action": "query_music_state", "entity": "kitchen speaker"},
            ha,
            None,
            matcher,
            agent_id="music-agent",
        )
        assert not result["success"]
        assert "Failed" in result["speech"]

    async def test_query_music_state_with_direct_entity_id(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "playing",
                "attributes": {
                    "media_title": "Bohemian Rhapsody",
                    "media_artist": "Queen",
                    "volume_level": 0.5,
                    "friendly_name": "Kitchen Speaker",
                },
            }
        )
        result = await execute_music_action(
            {"action": "query_music_state", "entity_id": "media_player.kitchen"},
            ha,
            None,
            None,
            agent_id="music-agent",
        )
        assert result["success"]
        assert result["entity_id"] == "media_player.kitchen"
        assert result["metadata"]["resolution_path"] == "llm_entity_id"
        ha.get_state.assert_awaited_once_with("media_player.kitchen")

    async def test_query_music_state_direct_entity_id_wrong_domain_falls_back(self):
        ha = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_music_action(
            {"action": "query_music_state", "entity_id": "light.kitchen"},
            ha,
            None,
            matcher,
            agent_id="music-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]
        ha.get_state.assert_not_awaited()

    async def test_list_music_players(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "media_player.kitchen",
                    "state": "playing",
                    "attributes": {"friendly_name": "Kitchen Speaker", "media_title": "Song", "media_artist": "Artist"},
                },
                {
                    "entity_id": "media_player.bedroom",
                    "state": "idle",
                    "attributes": {"friendly_name": "Bedroom Speaker"},
                },
            ]
        )
        result = await execute_music_action(
            {"action": "list_music_players", "entity": ""},
            ha,
            None,
            None,
            agent_id="music-agent",
        )
        assert result["success"]
        assert "Kitchen Speaker" in result["speech"]
        assert "Bedroom Speaker" in result["speech"]

    async def test_list_music_players_empty(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(return_value=[])
        result = await execute_music_action(
            {"action": "list_music_players", "entity": ""},
            ha,
            None,
            None,
            agent_id="music-agent",
        )
        assert result["success"]
        assert "No music" in result["speech"]


class TestMediaExecutor:
    async def test_turn_off_skips_when_already_off(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="media_player.tv", friendly_name="TV")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.get_state = AsyncMock(return_value={"state": "off"})
        result = await execute_media_action(
            {"action": "turn_off", "entity": "tv", "parameters": {}}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        assert "already off" in result["speech"]
        ha.call_service.assert_not_awaited()

    async def test_play_skips_when_already_playing(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="media_player.tv", friendly_name="TV")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.get_state = AsyncMock(return_value={"state": "playing"})
        result = await execute_media_action(
            {"action": "play", "entity": "tv", "parameters": {}}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        assert "already playing" in result["speech"]
        ha.call_service.assert_not_awaited()


class TestVacuumExecutor:
    async def test_start_skips_when_already_cleaning(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="vacuum.robot", friendly_name="Robot")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.get_state = AsyncMock(return_value={"state": "cleaning"})
        result = await execute_vacuum_action(
            {"action": "start", "entity": "robot", "parameters": {}}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        assert "already cleaning" in result["speech"]
        ha.call_service.assert_not_awaited()

    async def test_return_to_base_skips_when_already_returning(self):
        matcher = AsyncMock()
        match_obj = MagicMock(entity_id="vacuum.robot", friendly_name="Robot")
        matcher.match = AsyncMock(return_value=[match_obj])
        ha = AsyncMock()
        ha.expect_state = None
        ha.get_state = AsyncMock(return_value={"state": "returning"})
        result = await execute_vacuum_action(
            {"action": "return_to_base", "entity": "robot", "parameters": {}}, ha, MagicMock(), matcher
        )
        assert result["success"] is True
        assert "already returning" in result["speech"]
        ha.call_service.assert_not_awaited()


class TestMediaExecutorQueries:
    async def test_query_media_state(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "playing",
                "attributes": {
                    "media_title": "Movie",
                    "source": "HDMI 1",
                    "volume_level": 0.6,
                    "friendly_name": "Living Room TV",
                },
            }
        )
        matcher = AsyncMock()
        matcher.match = AsyncMock(
            return_value=[MagicMock(entity_id="media_player.living_room_tv", friendly_name="Living Room TV")]
        )
        result = await execute_media_action(
            {"action": "query_media_state", "entity": "living room TV"},
            ha,
            None,
            matcher,
            agent_id="media-agent",
        )
        assert result["success"]
        assert "playing" in result["speech"]
        assert "Movie" in result["speech"]
        assert "HDMI 1" in result["speech"]

    async def test_query_media_state_not_found(self):
        ha = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_media_action(
            {"action": "query_media_state", "entity": "nonexistent"},
            ha,
            None,
            matcher,
            agent_id="media-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]

    async def test_query_media_state_ha_error(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(side_effect=Exception("HA down"))
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[MagicMock(entity_id="media_player.tv", friendly_name="TV")])
        result = await execute_media_action(
            {"action": "query_media_state", "entity": "TV"},
            ha,
            None,
            matcher,
            agent_id="media-agent",
        )
        assert not result["success"]
        assert "Failed" in result["speech"]

    async def test_query_media_state_with_direct_entity_id(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(
            return_value={
                "state": "playing",
                "attributes": {
                    "media_title": "Movie",
                    "source": "HDMI 1",
                    "volume_level": 0.6,
                    "friendly_name": "Living Room TV",
                },
            }
        )
        result = await execute_media_action(
            {"action": "query_media_state", "entity_id": "media_player.living_room_tv"},
            ha,
            None,
            None,
            agent_id="media-agent",
        )
        assert result["success"]
        assert result["entity_id"] == "media_player.living_room_tv"
        assert result["metadata"]["resolution_path"] == "llm_entity_id"
        ha.get_state.assert_awaited_once_with("media_player.living_room_tv")

    async def test_query_media_state_direct_entity_id_wrong_domain_falls_back(self):
        ha = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_media_action(
            {"action": "query_media_state", "entity_id": "light.kitchen"},
            ha,
            None,
            matcher,
            agent_id="media-agent",
        )
        assert not result["success"]
        assert "Could not find" in result["speech"]
        ha.get_state.assert_not_awaited()

    async def test_list_media_players(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "media_player.living_room_tv",
                    "state": "playing",
                    "attributes": {"friendly_name": "Living Room TV", "source": "HDMI 1", "media_title": "Movie"},
                },
                {"entity_id": "media_player.chromecast", "state": "off", "attributes": {"friendly_name": "Chromecast"}},
            ]
        )
        result = await execute_media_action(
            {"action": "list_media_players", "entity": ""},
            ha,
            None,
            None,
            agent_id="media-agent",
        )
        assert result["success"]
        assert "Living Room TV" in result["speech"]
        assert "Chromecast" in result["speech"]

    async def test_list_media_players_empty(self):
        ha = AsyncMock()
        ha.get_states = AsyncMock(return_value=[])
        result = await execute_media_action(
            {"action": "list_media_players", "entity": ""},
            ha,
            None,
            None,
            agent_id="media-agent",
        )
        assert result["success"]
        assert "No media" in result["speech"]


# ---------------------------------------------------------------------------
# Phase 4.3: Conversation memory eviction tests
# ---------------------------------------------------------------------------
