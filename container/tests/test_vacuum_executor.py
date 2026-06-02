"""Tests for app.agents.vacuum_executor -- execute_vacuum_action and helpers."""

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

from app.agents.vacuum_executor import execute_vacuum_action  # noqa: E402
from tests.helpers import attach_expect_state_shim  # noqa: E402


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )


# ---------------------------------------------------------------------------
# execute_vacuum_action tests
# ---------------------------------------------------------------------------


class TestExecuteVacuumAction:
    """Tests for execute_vacuum_action() with mocked dependencies."""

    @pytest.fixture(autouse=True)
    def _fast_state_verify(self, monkeypatch):
        """Shrink FLOW-VERIFY-1 timing knobs so tests stay fast."""
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
        client.get_state = AsyncMock(return_value={"state": "idle", "attributes": {}})
        return attach_expect_state_shim(client)

    @pytest.fixture()
    def entity_matcher(self):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "vacuum.xiaomi"
        match_result.friendly_name = "Xiaomi"
        matcher.match = AsyncMock(return_value=[match_result])
        return matcher

    @pytest.fixture()
    def entity_index(self):
        index = MagicMock()
        entry = MagicMock()
        entry.entity_id = "vacuum.xiaomi"
        entry.friendly_name = "Xiaomi"
        index.search = MagicMock(return_value=[(entry, 0.1)])
        return index

    @pytest.mark.asyncio
    async def test_start_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "idle", "attributes": {}},
                {"state": "cleaning", "attributes": {}},
            ]
        )
        action = {"action": "start", "entity": "xiaomi vacuum", "parameters": {}}
        result = await execute_vacuum_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "vacuum.xiaomi"
        ha_client.call_service.assert_awaited_once_with("vacuum", "start", "vacuum.xiaomi", None)

    @pytest.mark.asyncio
    async def test_pause_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "cleaning", "attributes": {}},
                {"state": "paused", "attributes": {}},
            ]
        )
        action = {"action": "pause", "entity": "xiaomi vacuum", "parameters": {}}
        result = await execute_vacuum_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "vacuum.xiaomi"
        ha_client.call_service.assert_awaited_once_with("vacuum", "pause", "vacuum.xiaomi", None)

    @pytest.mark.asyncio
    async def test_return_to_base_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "cleaning", "attributes": {}},
                {"state": "returning", "attributes": {}},
            ]
        )
        action = {"action": "return_to_base", "entity": "xiaomi vacuum", "parameters": {}}
        result = await execute_vacuum_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "vacuum.xiaomi"
        ha_client.call_service.assert_awaited_once_with("vacuum", "return_to_base", "vacuum.xiaomi", None)

    @pytest.mark.asyncio
    async def test_stop_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "cleaning", "attributes": {}},
                {"state": "idle", "attributes": {}},
            ]
        )
        action = {"action": "stop", "entity": "xiaomi vacuum", "parameters": {}}
        result = await execute_vacuum_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "vacuum.xiaomi"
        ha_client.call_service.assert_awaited_once_with("vacuum", "stop", "vacuum.xiaomi", None)

    @pytest.mark.asyncio
    async def test_unknown_action(self, ha_client, entity_matcher, entity_index):
        action = {"action": "hover", "entity": "xiaomi vacuum", "parameters": {}}
        result = await execute_vacuum_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Unknown action" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entity_not_found(self, ha_client, entity_index):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        entity_index.search = MagicMock(return_value=[])

        action = {"action": "start", "entity": "nonexistent vacuum", "parameters": {}}
        result = await execute_vacuum_action(action, ha_client, entity_index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_domain_validation_rejects_light(self, ha_client):
        """Resolved entity in wrong domain should be treated as not found."""
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "light.kitchen_ceiling"
        match_result.friendly_name = "Kitchen Ceiling"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        action = {"action": "start", "entity": "kitchen light", "parameters": {}}
        result = await execute_vacuum_action(action, ha_client, index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]

    @pytest.mark.asyncio
    async def test_idle_state_skip_start_already_cleaning(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "cleaning", "attributes": {}})
        action = {"action": "start", "entity": "xiaomi vacuum", "parameters": {}}
        result = await execute_vacuum_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "cleaning"
        assert "already" in result["speech"]
        ha_client.call_service.assert_not_awaited()
