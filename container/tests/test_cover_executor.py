"""Tests for app.agents.cover_executor -- execute_cover_action and helpers."""

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

from app.agents.cover_executor import execute_cover_action  # noqa: E402
from tests.helpers import attach_expect_state_shim  # noqa: E402


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )


# ---------------------------------------------------------------------------
# execute_cover_action tests
# ---------------------------------------------------------------------------


class TestExecuteCoverAction:
    """Tests for execute_cover_action() with mocked dependencies."""

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
        client.get_state = AsyncMock(return_value={"state": "open", "attributes": {}})
        return attach_expect_state_shim(client)

    @pytest.fixture()
    def entity_matcher(self):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "cover.living_room_blinds"
        match_result.friendly_name = "Living Room Blinds"
        matcher.match = AsyncMock(return_value=[match_result])
        return matcher

    @pytest.fixture()
    def entity_index(self):
        index = MagicMock()
        entry = MagicMock()
        entry.entity_id = "cover.living_room_blinds"
        entry.friendly_name = "Living Room Blinds"
        index.search = MagicMock(return_value=[(entry, 0.1)])
        return index

    @pytest.mark.asyncio
    async def test_open_cover_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "closed", "attributes": {}},
                {"state": "open", "attributes": {}},
            ]
        )
        action = {"action": "open_cover", "entity": "living room blinds", "parameters": {}}
        result = await execute_cover_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "cover.living_room_blinds"
        assert result["new_state"] == "open"
        ha_client.call_service.assert_awaited_once_with("cover", "open_cover", "cover.living_room_blinds", None)

    @pytest.mark.asyncio
    async def test_close_cover_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "open", "attributes": {}},
                {"state": "closed", "attributes": {}},
            ]
        )
        action = {"action": "close_cover", "entity": "living room blinds", "parameters": {}}
        result = await execute_cover_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "closed"
        ha_client.call_service.assert_awaited_once_with("cover", "close_cover", "cover.living_room_blinds", None)

    @pytest.mark.asyncio
    async def test_set_cover_position_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "open", "attributes": {}},
                {"state": "open", "attributes": {"current_position": 50}},
            ]
        )
        action = {"action": "set_cover_position", "entity": "living room blinds", "parameters": {"position": 50}}
        result = await execute_cover_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once_with(
            "cover", "set_cover_position", "cover.living_room_blinds", {"position": 50}
        )

    @pytest.mark.asyncio
    async def test_stop_cover_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "opening", "attributes": {}},
                {"state": "open", "attributes": {}},
            ]
        )
        action = {"action": "stop_cover", "entity": "living room blinds", "parameters": {}}
        result = await execute_cover_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once_with("cover", "stop_cover", "cover.living_room_blinds", None)

    @pytest.mark.asyncio
    async def test_unknown_action(self, ha_client, entity_matcher, entity_index):
        action = {"action": "fly_away", "entity": "living room blinds", "parameters": {}}
        result = await execute_cover_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Unknown action" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entity_not_found(self, ha_client, entity_index):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        entity_index.search = MagicMock(return_value=[])

        action = {"action": "open_cover", "entity": "nonexistent blinds", "parameters": {}}
        result = await execute_cover_action(action, ha_client, entity_index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_domain_validation_rejects_light(self, ha_client):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "light.kitchen_ceiling"
        match_result.friendly_name = "Kitchen Ceiling"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        action = {"action": "open_cover", "entity": "kitchen", "parameters": {}}
        result = await execute_cover_action(action, ha_client, index, matcher, agent_id="cover-agent")

        assert result["success"] is False
        assert "Could not find" in result["speech"]

    @pytest.mark.asyncio
    async def test_idle_state_position_skip(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "closed", "attributes": {}})
        action = {"action": "close_cover", "entity": "living room blinds", "parameters": {}}
        result = await execute_cover_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "closed"
        assert "already closed" in result["speech"]
        ha_client.call_service.assert_not_awaited()


# ---------------------------------------------------------------------------
# Direct entity_id query tests
# ---------------------------------------------------------------------------


class TestQueryCoverStateDirectEntityId:
    """Tests for query_cover_state with a direct entity_id from the LLM."""

    @pytest.mark.asyncio
    async def test_query_cover_state_with_direct_entity_id(self):
        ha_client = AsyncMock()
        ha_client.get_state = AsyncMock(
            return_value={
                "state": "open",
                "attributes": {"friendly_name": "Living Room Blinds", "current_position": 80},
            }
        )
        action = {"action": "query_cover_state", "entity_id": "cover.living_room_blinds"}
        result = await execute_cover_action(action, ha_client, MagicMock(), MagicMock())

        assert result["success"] is True
        assert result["entity_id"] == "cover.living_room_blinds"
        assert result["metadata"]["resolution_path"] == "llm_entity_id"
        ha_client.get_state.assert_awaited_once_with("cover.living_room_blinds")

    @pytest.mark.asyncio
    async def test_query_cover_state_direct_entity_id_wrong_domain_falls_back(self):
        ha_client = AsyncMock()
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        index = MagicMock()
        index.search = MagicMock(return_value=[])

        action = {"action": "query_cover_state", "entity_id": "light.kitchen"}
        result = await execute_cover_action(action, ha_client, index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        ha_client.get_state.assert_not_awaited()
