"""Tests for app.agents.climate_executor -- execute_climate_action and helpers."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mock litellm before importing app modules
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

from app.agents.climate_executor import execute_climate_action  # noqa: E402
from tests.helpers import attach_expect_state_shim  # noqa: E402


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )


# ---------------------------------------------------------------------------
# execute_climate_action tests
# ---------------------------------------------------------------------------


class TestExecuteClimateAction:
    """Tests for execute_climate_action() with mocked dependencies."""

    @pytest.fixture(autouse=True)
    def _fast_state_verify(self, monkeypatch):
        from app.agents import action_executor as _ae

        async def _fast(key, *, default):
            return {
                "state_verify.ws_timeout_sec": 0.05,
                "state_verify.poll_interval_sec": 0.01,
                "state_verify.poll_max_sec": 0.05,
            }.get(key, default)

        monkeypatch.setattr(_ae, "_settings_float", _fast)

    @pytest.fixture()
    def ha_client(self):
        client = AsyncMock()
        client.call_service = AsyncMock(return_value=[])
        client.get_state = AsyncMock(return_value={"state": "heat", "attributes": {}})
        return attach_expect_state_shim(client)

    @pytest.fixture()
    def entity_matcher(self):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "climate.living_room"
        match_result.friendly_name = "Living Room"
        matcher.match = AsyncMock(return_value=[match_result])
        return matcher

    @pytest.fixture()
    def entity_index(self):
        index = MagicMock()
        entry = MagicMock()
        entry.entity_id = "climate.living_room"
        entry.friendly_name = "Living Room"
        index.search = MagicMock(return_value=[(entry, 0.1)])
        return index

    @pytest.mark.asyncio
    async def test_set_temperature_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "heat", "attributes": {}},
                {"state": "heat", "attributes": {"temperature": 22.5}},
            ]
        )
        action = {"action": "set_temperature", "entity": "living room", "parameters": {"temperature": 22.5}}
        result = await execute_climate_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "climate.living_room"
        ha_client.call_service.assert_awaited_once_with(
            "climate", "set_temperature", "climate.living_room", {"temperature": 22.5}
        )

    @pytest.mark.asyncio
    async def test_set_hvac_mode_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "off", "attributes": {}},
                {"state": "heat", "attributes": {}},
            ]
        )
        action = {"action": "set_hvac_mode", "entity": "living room", "parameters": {"hvac_mode": "heat"}}
        result = await execute_climate_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once_with(
            "climate", "set_hvac_mode", "climate.living_room", {"hvac_mode": "heat"}
        )

    @pytest.mark.asyncio
    async def test_turn_on_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "off", "attributes": {}},
                {"state": "heat", "attributes": {}},
            ]
        )
        action = {"action": "turn_on", "entity": "living room", "parameters": {}}
        result = await execute_climate_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "climate.living_room"
        ha_client.call_service.assert_awaited_once_with("climate", "turn_on", "climate.living_room", None)

    @pytest.mark.asyncio
    async def test_turn_off_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "heat", "attributes": {}},
                {"state": "off", "attributes": {}},
            ]
        )
        action = {"action": "turn_off", "entity": "living room", "parameters": {}}
        result = await execute_climate_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once_with("climate", "turn_off", "climate.living_room", None)

    @pytest.mark.asyncio
    async def test_fan_entity_routing(self, ha_client, entity_index):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "fan.ceiling"
        match_result.friendly_name = "Ceiling Fan"
        matcher.match = AsyncMock(return_value=[match_result])

        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "on", "attributes": {}},
                {"state": "on", "attributes": {"preset_mode": "auto"}},
            ]
        )
        action = {"action": "set_fan_preset_mode", "entity": "ceiling fan", "parameters": {"preset_mode": "auto"}}
        result = await execute_climate_action(action, ha_client, entity_index, matcher)

        assert result["success"] is True
        assert result["entity_id"] == "fan.ceiling"
        ha_client.call_service.assert_awaited_once_with(
            "fan", "set_preset_mode", "fan.ceiling", {"preset_mode": "auto"}
        )

    @pytest.mark.asyncio
    async def test_unknown_action(self, ha_client, entity_matcher, entity_index):
        action = {"action": "make_blizzard", "entity": "living room", "parameters": {}}
        result = await execute_climate_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Unknown action" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entity_not_found(self, ha_client, entity_index):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        entity_index.search = MagicMock(return_value=[])

        action = {"action": "set_temperature", "entity": "nonexistent device", "parameters": {"temperature": 22}}
        result = await execute_climate_action(action, ha_client, entity_index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_service_failure(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "heat", "attributes": {}})
        ha_client.call_service = AsyncMock(side_effect=Exception("Connection refused"))
        action = {"action": "set_temperature", "entity": "living room", "parameters": {"temperature": 22}}
        result = await execute_climate_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Failed to execute" in result["speech"]

    @pytest.mark.asyncio
    async def test_domain_validation_rejects_media(self, ha_client):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "media_player.living_room_tv"
        match_result.friendly_name = "Living Room TV"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        action = {"action": "set_temperature", "entity": "living room", "parameters": {"temperature": 22}}
        result = await execute_climate_action(action, ha_client, index, matcher, agent_id="climate-agent")

        assert result["success"] is False
        assert "Could not find" in result["speech"]

    @pytest.mark.asyncio
    async def test_idle_state_skip(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
        action = {"action": "turn_off", "entity": "living room", "parameters": {}}
        result = await execute_climate_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "off"
        assert "already off" in result["speech"]
        ha_client.call_service.assert_not_awaited()
