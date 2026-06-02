"""Tests for app.agents.scene_executor -- execute_scene_action and helpers."""

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

from app.agents.scene_executor import execute_scene_action  # noqa: E402
from tests.helpers import attach_expect_state_shim  # noqa: E402


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )


# ---------------------------------------------------------------------------
# execute_scene_action tests
# ---------------------------------------------------------------------------


class TestExecuteSceneAction:
    """Tests for execute_scene_action() with mocked dependencies."""

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
        client.get_state = AsyncMock(return_value={"state": "2025-01-01T00:00:00", "attributes": {}})
        return attach_expect_state_shim(client)

    @pytest.fixture()
    def entity_matcher(self):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "scene.movie_night"
        match_result.friendly_name = "Movie Night"
        matcher.match = AsyncMock(return_value=[match_result])
        return matcher

    @pytest.fixture()
    def entity_index(self):
        index = MagicMock()
        entry = MagicMock()
        entry.entity_id = "scene.movie_night"
        entry.friendly_name = "Movie Night"
        index.search = MagicMock(return_value=[(entry, 0.1)])
        return index

    @pytest.mark.asyncio
    async def test_activate_scene_success(self, ha_client, entity_matcher, entity_index):
        action = {"action": "activate_scene", "entity": "movie night", "parameters": {}}
        result = await execute_scene_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "scene.movie_night"
        assert "activated" in result["speech"]
        ha_client.call_service.assert_awaited_once_with("scene", "turn_on", "scene.movie_night", None)

    @pytest.mark.asyncio
    async def test_activate_scene_with_transition(self, ha_client, entity_matcher, entity_index):
        action = {
            "action": "activate_scene",
            "entity": "movie night",
            "parameters": {"transition": 5.0},
        }
        result = await execute_scene_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "scene.movie_night"
        ha_client.call_service.assert_awaited_once_with("scene", "turn_on", "scene.movie_night", {"transition": 5.0})

    @pytest.mark.asyncio
    async def test_query_scene_read_action(self, ha_client, entity_matcher, entity_index):
        action = {"action": "query_scene", "entity": "movie night"}
        result = await execute_scene_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "scene.movie_night"
        assert "Scene found" in result["speech"]
        assert result.get("cacheable") is False

    @pytest.mark.asyncio
    async def test_entity_not_found(self, ha_client, entity_index):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        entity_index.search = MagicMock(return_value=[])

        action = {"action": "activate_scene", "entity": "nonexistent scene", "parameters": {}}
        result = await execute_scene_action(action, ha_client, entity_index, matcher)

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

        action = {"action": "activate_scene", "entity": "kitchen light", "parameters": {}}
        result = await execute_scene_action(action, ha_client, index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]

    @pytest.mark.asyncio
    async def test_scene_always_calls_service_no_skip(self, ha_client, entity_matcher, entity_index):
        """Scene executor has no idle/skip check; always calls the service."""
        ha_client.get_state = AsyncMock(return_value={"state": "2025-01-01T00:00:00", "attributes": {}})
        action = {"action": "activate_scene", "entity": "movie night", "parameters": {}}
        result = await execute_scene_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once()
        assert "already" not in result["speech"].lower()
