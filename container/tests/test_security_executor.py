"""Tests for app.agents.security_executor -- execute_security_action and helpers."""

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

from app.agents.security_executor import execute_security_action  # noqa: E402
from tests.helpers import attach_expect_state_shim  # noqa: E402


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )


# ---------------------------------------------------------------------------
# execute_security_action tests
# ---------------------------------------------------------------------------


class TestExecuteSecurityAction:
    """Tests for execute_security_action() with mocked dependencies."""

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
        client.get_state = AsyncMock(
            return_value={"state": "locked", "attributes": {"friendly_name": "Front Door Lock"}}
        )
        return attach_expect_state_shim(client)

    @pytest.fixture()
    def entity_matcher(self):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "lock.front_door"
        match_result.friendly_name = "Front Door Lock"
        matcher.match = AsyncMock(return_value=[match_result])
        return matcher

    @pytest.fixture()
    def entity_index(self):
        index = MagicMock()
        entry = MagicMock()
        entry.entity_id = "lock.front_door"
        entry.friendly_name = "Front Door Lock"
        index.search = MagicMock(return_value=[(entry, 0.1)])
        return index

    @pytest.mark.asyncio
    async def test_lock_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "unlocked", "attributes": {"friendly_name": "Front Door Lock"}},
                {"state": "locked", "attributes": {"friendly_name": "Front Door Lock"}},
            ]
        )
        action = {"action": "lock", "entity": "front door", "parameters": {}}
        result = await execute_security_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "lock.front_door"
        assert result["new_state"] == "locked"
        assert "locked" in result["speech"]
        ha_client.call_service.assert_awaited_once_with("lock", "lock", "lock.front_door", None)

    @pytest.mark.asyncio
    async def test_unlock_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "locked", "attributes": {"friendly_name": "Front Door Lock"}},
                {"state": "unlocked", "attributes": {"friendly_name": "Front Door Lock"}},
            ]
        )
        action = {"action": "unlock", "entity": "front door", "parameters": {}}
        result = await execute_security_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "unlocked"
        ha_client.call_service.assert_awaited_once_with("lock", "unlock", "lock.front_door", None)

    @pytest.mark.asyncio
    async def test_alarm_arm_home_with_code_success(self, ha_client):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "alarm_control_panel.main"
        match_result.friendly_name = "Main Alarm"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "disarmed", "attributes": {"friendly_name": "Main Alarm"}},
                {"state": "armed_home", "attributes": {"friendly_name": "Main Alarm"}},
            ]
        )
        action = {"action": "alarm_arm_home", "entity": "main alarm", "parameters": {"code": "1234"}}
        result = await execute_security_action(action, ha_client, index, matcher)

        assert result["success"] is True
        assert result["entity_id"] == "alarm_control_panel.main"
        assert result["new_state"] == "armed_home"
        ha_client.call_service.assert_awaited_once_with(
            "alarm_control_panel", "alarm_arm_home", "alarm_control_panel.main", {"code": "1234"}
        )

    @pytest.mark.asyncio
    async def test_alarm_disarm_success(self, ha_client):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "alarm_control_panel.main"
        match_result.friendly_name = "Main Alarm"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "armed_home", "attributes": {"friendly_name": "Main Alarm"}},
                {"state": "disarmed", "attributes": {"friendly_name": "Main Alarm"}},
            ]
        )
        action = {"action": "alarm_disarm", "entity": "main alarm", "parameters": {"code": "5678"}}
        result = await execute_security_action(action, ha_client, index, matcher)

        assert result["success"] is True
        assert result["entity_id"] == "alarm_control_panel.main"
        assert result["new_state"] == "disarmed"
        ha_client.call_service.assert_awaited_once_with(
            "alarm_control_panel", "alarm_disarm", "alarm_control_panel.main", {"code": "5678"}
        )

    @pytest.mark.asyncio
    async def test_per_action_domain_validation_lock_rejects_camera(self, ha_client):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "camera.front_door"
        match_result.friendly_name = "Front Door Camera"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        action = {"action": "lock", "entity": "front door", "parameters": {}}
        result = await execute_security_action(action, ha_client, index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]

    @pytest.mark.asyncio
    async def test_unknown_action(self, ha_client, entity_matcher, entity_index):
        action = {"action": "explode", "entity": "front door", "parameters": {}}
        result = await execute_security_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Unknown action" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entity_not_found(self, ha_client, entity_index):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        index = MagicMock()

        action = {"action": "lock", "entity": "nonexistent door", "parameters": {}}
        result = await execute_security_action(action, ha_client, index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idle_state_lock_already_locked(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            return_value={"state": "locked", "attributes": {"friendly_name": "Front Door Lock"}}
        )
        action = {"action": "lock", "entity": "front door", "parameters": {}}
        result = await execute_security_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "locked"
        assert "already" in result["speech"]
        ha_client.call_service.assert_not_awaited()


# ---------------------------------------------------------------------------
# Direct entity_id query tests
# ---------------------------------------------------------------------------


class TestQuerySecurityStateDirectEntityId:
    """Tests for query_security_state with a direct entity_id from the LLM."""

    @pytest.mark.asyncio
    async def test_query_security_state_with_direct_entity_id(self):
        ha_client = AsyncMock()
        ha_client.get_state = AsyncMock(
            return_value={"state": "locked", "attributes": {"friendly_name": "Front Door Lock"}}
        )
        action = {"action": "query_security_state", "entity_id": "lock.front_door"}
        result = await execute_security_action(action, ha_client, MagicMock(), MagicMock())

        assert result["success"] is True
        assert result["entity_id"] == "lock.front_door"
        assert result["metadata"]["resolution_path"] == "llm_entity_id"
        ha_client.get_state.assert_awaited_once_with("lock.front_door")

    @pytest.mark.asyncio
    async def test_query_security_state_direct_entity_id_wrong_domain_falls_back(self):
        ha_client = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        index = MagicMock()
        index.search = MagicMock(return_value=[])

        action = {"action": "query_security_state", "entity_id": "light.kitchen"}
        result = await execute_security_action(action, ha_client, index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        ha_client.get_state.assert_not_awaited()
