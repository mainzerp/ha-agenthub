"""Tests for app.agents.action_executor -- parse_action and execute_action."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest


def _attach_expect_state_shim(client):
    """Install an ``expect_state`` async context manager on a mocked client.

    The shim mimics :meth:`HARestClient.expect_state` in "no WS observer"
    mode: it yields a mutable dict to the ``with`` body and, on exit, fills
    ``new_state`` from a single call to ``client.get_state`` (or leaves it
    ``None`` if ``get_state`` raises). That keeps ``execute_action`` tests
    deterministic without pulling the real REST client into the unit test.
    """

    @asynccontextmanager
    async def _expect_state(
        entity_id,
        *,
        expected=None,
        timeout=0.05,
        poll_interval=0.01,
        poll_max=0.05,
    ):
        result = {"new_state": None}
        yield result
        try:
            state_resp = await client.get_state(entity_id)
        except Exception:
            return
        if isinstance(state_resp, dict):
            state = state_resp.get("state")
            if expected is None or state == expected:
                result["new_state"] = state
            else:
                result["new_state"] = state

    client.expect_state = _expect_state
    client.set_state_observer = MagicMock()
    return client


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

from app.agents.action_executor import execute_action, filter_matches_by_domain, parse_action  # noqa: E402
from app.entity.index import EntityIndex  # noqa: E402
from app.entity.matcher import EntityMatcher, MatchResult  # noqa: E402
from tests.helpers import make_entity_index_entry  # noqa: E402


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )


def _make_listable_entity_index(*entries):
    entry_list = list(entries)
    index = MagicMock(spec=EntityIndex)
    index.list_entries_async = AsyncMock(return_value=entry_list)
    index.list_entries = MagicMock(return_value=entry_list)
    index.get_by_id = MagicMock(
        side_effect=lambda entity_id: next((e for e in entry_list if e.entity_id == entity_id), None)
    )
    index.search = MagicMock()
    index.search_async = AsyncMock()
    return index


# ---------------------------------------------------------------------------
# parse_action tests
# ---------------------------------------------------------------------------


class TestParseAction:
    """Tests for parse_action()."""

    def test_fenced_json(self):
        response = (
            "Sure, I'll turn on the kitchen light.\n"
            '```json\n{"action": "turn_on", "entity": "kitchen light", "parameters": {}}\n```\n'
            "Turning on the kitchen light."
        )
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "turn_on"
        assert result["entity"] == "kitchen light"
        assert result["parameters"] == {}

    def test_raw_json(self):
        response = 'Here you go: {"action": "turn_off", "entity": "bedroom lamp", "parameters": {}} Done.'
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "turn_off"
        assert result["entity"] == "bedroom lamp"

    def test_no_json(self):
        response = "The kitchen light is currently on at 80% brightness."
        result = parse_action(response)
        assert result is None

    def test_malformed_json(self):
        response = '```json\n{"action": "turn_on", "entity": }\n```'
        result = parse_action(response)
        assert result is None

    def test_json_without_action_key(self):
        response = '```json\n{"command": "turn_on", "target": "lamp"}\n```'
        result = parse_action(response)
        assert result is None

    def test_brightness_action(self):
        response = (
            '```json\n{"action": "set_brightness", "entity": "living room", "parameters": {"brightness": 128}}\n```'
        )
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "set_brightness"
        assert result["parameters"]["brightness"] == 128

    def test_color_action(self):
        response = '```json\n{"action": "set_color", "entity": "desk lamp", "parameters": {"color_name": "red"}}\n```'
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "set_color"
        assert result["parameters"]["color_name"] == "red"

    def test_toggle_action(self):
        response = '{"action": "toggle", "entity": "hallway light", "parameters": {}}'
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "toggle"

    def test_parse_action_accepts_plain_fence(self):
        """FLOW-LOW-1: unlabelled ``` fences are parsed too."""
        response = 'Sure:\n```\n{"action": "turn_on", "entity": "kitchen light", "parameters": {}}\n```\n'
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "turn_on"
        assert result["entity"] == "kitchen light"

    def test_parse_action_prefers_json_fence_over_plain_fence(self):
        """FLOW-LOW-1: when both labelled and plain fences exist, the
        labelled one wins so a prose example in a plain fence cannot
        override the real action block."""
        response = (
            "Example of the format:\n"
            '```\n{"action": "turn_off", "entity": "bogus"}\n```\n'
            "Actual command:\n"
            '```json\n{"action": "turn_on", "entity": "kitchen light", "parameters": {}}\n```\n'
        )
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "turn_on"
        assert result["entity"] == "kitchen light"

    def test_brace_in_string_literal_does_not_break_parsing(self):
        """COR-10: braces inside string literals must not confuse the
        balanced-brace scanner. The first ``{`` in the description must
        not be treated as a JSON object start."""
        response = (
            'Sure thing. {"action": "turn_on", "entity": "kitchen light", '
            '"parameters": {"note": "use {placeholder} value"}}'
        )
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "turn_on"
        assert result["entity"] == "kitchen light"
        assert result["parameters"]["note"] == "use {placeholder} value"

    def test_parse_action_rejects_missing_entity(self):
        """P2-6 (FLOW-PARSE-1): a state-changing action without an
        entity / entity_id is treated as a parse miss so the caller can
        fall through to the next regex path or fail loudly instead of
        executing a bad service call."""
        response = '```json\n{"action": "turn_on", "parameters": {}}\n```'
        result = parse_action(response)
        assert result is None

    def test_parse_action_rejects_entityless_start_timer(self):
        """Timer writes stay aligned with the shared entity-required contract."""
        response = '```json\n{"action": "start_timer", "parameters": {"duration": "00:05:00"}}\n```'
        result = parse_action(response)
        assert result is None

    def test_parse_action_rejects_empty_action_string(self):
        """P2-6 (FLOW-PARSE-1): the schema requires ``action`` to be a
        non-empty string."""
        response = '```json\n{"action": "", "entity": "kitchen"}\n```'
        result = parse_action(response)
        assert result is None

    def test_parse_action_accepts_entity_id_as_synonym(self):
        """P2-6 (FLOW-PARSE-1): ``entity_id`` is accepted as a synonym
        for ``entity`` to keep callers that already speak HA-native
        ids working without a schema-validation rejection."""
        response = '```json\n{"action": "turn_on", "entity_id": "light.kitchen"}\n```'
        result = parse_action(response)
        assert result is not None
        assert result["entity_id"] == "light.kitchen"

    def test_parse_action_allows_entityless_list_actions(self):
        """P2-6 (FLOW-PARSE-1): aggregation actions (``list_lights``,
        ``list_timers``, ``list_lists`` etc.) legitimately omit an entity; they must
        still parse."""
        response = '```json\n{"action": "list_lights", "parameters": {}}\n```'
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "list_lights"

        response = '```json\n{"action": "list_lists", "parameters": {}}\n```'
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "list_lists"

    def test_parse_action_allows_entityless_weather_query(self):
        response = '```json\n{"action": "query_weather"}\n```'
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "query_weather"

    def test_parse_action_allows_weather_forecast_days_payload(self):
        response = '```json\n{"action": "query_weather_forecast", "parameters": {"days": 3}}\n```'
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "query_weather_forecast"
        assert result["parameters"] == {"days": 3}

    def test_parse_action_allows_weather_forecast_without_days(self):
        response = '```json\n{"action": "query_weather_forecast"}\n```'
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "query_weather_forecast"

    def test_parse_action_accepts_set_datetime_with_briefing(self):
        response = (
            "```json\n"
            '{"action": "set_datetime", "entity": "alarm", '
            '"parameters": {"time": "07:00:00", "briefing": true}}\n'
            "```"
        )
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "set_datetime"
        assert result["parameters"]["briefing"] is True

    def test_parse_action_falls_through_when_first_fence_invalid(self):
        """P2-6 (FLOW-PARSE-1): if a labelled ```json fence contains a
        malformed-but-decodable action stub (no entity), the parser
        must keep scanning subsequent fences instead of returning the
        bad payload."""
        response = (
            "Skeleton:\n"
            '```json\n{"action": "turn_on"}\n```\n'
            "Real call:\n"
            '```\n{"action": "turn_off", "entity": "bedroom lamp"}\n```\n'
        )
        result = parse_action(response)
        assert result is not None
        assert result["action"] == "turn_off"
        assert result["entity"] == "bedroom lamp"


# ---------------------------------------------------------------------------
# execute_action tests
# ---------------------------------------------------------------------------


class TestExecuteAction:
    """Tests for execute_action() with mocked dependencies."""

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
        client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
        return _attach_expect_state_shim(client)

    @pytest.fixture()
    def entity_matcher(self):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "light.kitchen_ceiling"
        match_result.friendly_name = "Kitchen Ceiling"
        matcher.match = AsyncMock(return_value=[match_result])
        return matcher

    @pytest.fixture()
    def entity_index(self):
        index = MagicMock()
        entry = MagicMock()
        entry.entity_id = "light.kitchen_ceiling"
        entry.friendly_name = "Kitchen Ceiling"
        index.search = MagicMock(return_value=[(entry, 0.1)])
        return index

    @pytest.mark.asyncio
    async def test_turn_on_success(self, ha_client, entity_matcher, entity_index):
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "light.kitchen_ceiling"
        assert result["new_state"] == "on"
        assert "Kitchen Ceiling" in result["speech"]
        ha_client.call_service.assert_awaited_once_with("light", "turn_on", "light.kitchen_ceiling", None)

    @pytest.mark.asyncio
    async def test_turn_off_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
        action = {"action": "turn_off", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "off"
        ha_client.call_service.assert_awaited_once_with("light", "turn_off", "light.kitchen_ceiling", None)

    @pytest.mark.asyncio
    async def test_set_brightness(self, ha_client, entity_matcher, entity_index):
        action = {"action": "set_brightness", "entity": "kitchen light", "parameters": {"brightness": 128}}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once_with(
            "light", "turn_on", "light.kitchen_ceiling", {"brightness": 128}
        )

    @pytest.mark.asyncio
    async def test_unknown_action(self, ha_client, entity_matcher, entity_index):
        action = {"action": "explode", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Unknown action" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entity_not_found(self, ha_client, entity_index):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        entity_index.search = MagicMock(return_value=[])

        action = {"action": "turn_on", "entity": "nonexistent lamp", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_service_call_failure(self, ha_client, entity_matcher, entity_index):
        ha_client.call_service = AsyncMock(side_effect=Exception("Connection refused"))
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Failed to execute" in result["speech"]

    @pytest.mark.asyncio
    async def test_no_fallback_to_entity_index(self, ha_client, entity_index):
        """When entity_matcher returns no results, should NOT fall back to unfiltered entity_index."""
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])

        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        entity_index.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_matcher_no_fallback(self, ha_client, entity_index):
        """When entity_matcher is None, should NOT fall back to unfiltered entity_index."""
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, None)

        assert result["success"] is False
        assert "Could not find" in result["speech"]

    @pytest.mark.asyncio
    async def test_state_verification_failure(self, ha_client, entity_matcher, entity_index):
        """State verification failure should not affect success status."""
        ha_client.get_state = AsyncMock(side_effect=Exception("timeout"))
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] is None

    @pytest.mark.asyncio
    async def test_execute_action_passes_agent_id(self, ha_client, entity_matcher, entity_index):
        """Verify that entity_matcher.match is called with agent_id kwarg."""
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, entity_matcher, agent_id="light-agent")

        assert result["success"] is True
        entity_matcher.match.assert_awaited_once()
        call = entity_matcher.match.await_args
        assert call.args == ("kitchen light",)
        assert call.kwargs == {
            "agent_id": "light-agent",
            "preferred_domains": ("light", "switch"),
            "verbatim_terms": None,
        }

    @pytest.mark.asyncio
    async def test_domain_validation_rejects_wrong_domain(self, ha_client):
        """Resolved entity in wrong domain should be treated as not found."""
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "media_player.living_room_tv"
        match_result.friendly_name = "Living Room TV"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        action = {"action": "turn_on", "entity": "living room", "parameters": {}}
        result = await execute_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert result["success"] is False
        assert "Could not find" in result["speech"]

    @pytest.mark.asyncio
    async def test_domain_validation_accepts_light_domain(self, ha_client, entity_matcher, entity_index):
        """Entity in light domain should pass domain validation."""
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "light.kitchen_ceiling"

    @pytest.mark.asyncio
    async def test_domain_validation_accepts_switch_domain(self, ha_client):
        """Entity in switch domain should pass domain validation."""
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "switch.kitchen_outlet"
        match_result.friendly_name = "Kitchen Outlet"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        ha_client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
        action = {"action": "turn_on", "entity": "kitchen outlet", "parameters": {}}
        result = await execute_action(action, ha_client, index, matcher)

        assert result["success"] is True
        assert result["entity_id"] == "switch.kitchen_outlet"

    @pytest.mark.asyncio
    async def test_exact_friendly_name_resolves_without_hybrid_match(self, ha_client):
        matcher = MagicMock(spec=EntityMatcher)
        matcher.match = AsyncMock(return_value=[])
        matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
        index = MagicMock(spec=EntityIndex)
        index.get_by_id.return_value = None
        index.list_entries_async = AsyncMock(
            return_value=[make_entity_index_entry("light.keller", "Keller", area="Basement")]
        )

        action = {"action": "turn_on", "entity": "Keller", "parameters": {}}
        result = await execute_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert result["success"] is True
        assert result["entity_id"] == "light.keller"
        matcher.match.assert_not_awaited()
        ha_client.call_service.assert_awaited_once_with("light", "turn_on", "light.keller", None)

    @pytest.mark.asyncio
    async def test_trailing_device_noun_resolves_exact_name(self, ha_client):
        matcher = MagicMock(spec=EntityMatcher)
        matcher.match = AsyncMock(return_value=[])
        matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
        index = MagicMock(spec=EntityIndex)
        index.get_by_id.return_value = None
        index.list_entries_async = AsyncMock(
            return_value=[make_entity_index_entry("light.keller", "Keller", area="Basement")]
        )

        action = {"action": "turn_on", "entity": "Keller light", "parameters": {}}
        result = await execute_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert result["success"] is True
        assert result["entity_id"] == "light.keller"
        matcher.match.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exact_entity_id_query_resolves_directly(self, ha_client):
        matcher = MagicMock(spec=EntityMatcher)
        matcher.match = AsyncMock(return_value=[])
        matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
        index = MagicMock(spec=EntityIndex)
        index.get_by_id_async = AsyncMock(
            return_value=make_entity_index_entry("light.keller", "Keller", area="Basement")
        )
        index.list_entries_async = AsyncMock(return_value=[])

        action = {"action": "turn_on", "entity": "light.keller", "parameters": {}}
        result = await execute_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert result["success"] is True
        assert result["entity_id"] == "light.keller"
        index.get_by_id_async.assert_awaited_once_with("light.keller")
        matcher.match.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_area_fallback_returns_ambiguity_for_multiple_lights(self, ha_client):
        matcher = MagicMock(spec=EntityMatcher)
        matcher.match = AsyncMock(return_value=[])
        matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
        index = MagicMock(spec=EntityIndex)
        index.get_by_id.return_value = None
        index.list_entries_async = AsyncMock(
            return_value=[
                make_entity_index_entry("light.keller_main", "Deckenlicht", area="Keller"),
                make_entity_index_entry("light.keller_side", "Wandlicht", area="Keller"),
            ]
        )

        action = {"action": "turn_on", "entity": "Keller", "parameters": {}}
        result = await execute_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert result["success"] is False
        assert "Multiple entities match 'Keller'" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exact_friendly_name_ambiguity_falls_back_to_hybrid_matcher(self, ha_client):
        """When multiple entities share the same friendly name, the hybrid matcher should break the tie."""
        match_result = MagicMock()
        match_result.entity_id = "light.keller_main"
        match_result.friendly_name = "Keller"
        match_result.score = 0.95
        matcher = MagicMock(spec=EntityMatcher)
        matcher.match = AsyncMock(return_value=[match_result])
        matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
        index = MagicMock(spec=EntityIndex)
        index.get_by_id.return_value = None
        index.list_entries_async = AsyncMock(
            return_value=[
                make_entity_index_entry("light.keller_main", "Keller", area="Keller"),
                make_entity_index_entry("light.keller_side", "Keller", area="Keller"),
            ]
        )

        action = {"action": "turn_on", "entity": "Keller", "parameters": {}}
        result = await execute_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert result["success"] is True
        assert result["entity_id"] == "light.keller_main"
        ha_client.call_service.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ambiguity_returns_voice_followup(self, ha_client):
        """When entity resolution is ambiguous, voice_followup should be True so the user can reply."""
        matcher = MagicMock(spec=EntityMatcher)
        matcher.match = AsyncMock(return_value=[])
        matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
        index = MagicMock(spec=EntityIndex)
        index.get_by_id.return_value = None
        index.list_entries_async = AsyncMock(
            return_value=[
                make_entity_index_entry("light.keller_main", "Keller", area="Keller"),
                make_entity_index_entry("light.keller_side", "Keller", area="Keller"),
            ]
        )

        action = {"action": "turn_on", "entity": "Keller", "parameters": {}}
        result = await execute_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert result["success"] is False
        assert "Multiple entities match 'Keller'" in result["speech"]
        assert result.get("voice_followup") is True
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_area_fallback_uses_refreshed_index(self, ha_client):
        matcher = MagicMock(spec=EntityMatcher)
        matcher.match = AsyncMock(return_value=[])
        matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
        index = MagicMock(spec=EntityIndex)
        index.get_by_id.return_value = None
        index.list_entries_async = AsyncMock(
            side_effect=[
                [],
                [make_entity_index_entry("light.keller", "Deckenlicht", area="Keller")],
            ]
        )

        action = {"action": "turn_on", "entity": "Keller", "parameters": {}}
        first_result = await execute_action(action, ha_client, index, matcher, agent_id="light-agent")
        second_result = await execute_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert first_result["success"] is False
        assert second_result["success"] is True
        assert second_result["entity_id"] == "light.keller"

    @pytest.mark.asyncio
    async def test_query_light_state_uses_deterministic_resolution(self, ha_client):
        matcher = MagicMock(spec=EntityMatcher)
        matcher.match = AsyncMock(return_value=[])
        matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
        index = MagicMock(spec=EntityIndex)
        index.get_by_id.return_value = None
        index.list_entries_async = AsyncMock(
            return_value=[make_entity_index_entry("light.keller", "Keller", area="Basement")]
        )

        action = {"action": "query_light_state", "entity": "Keller"}
        result = await execute_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert result["success"] is True
        assert result["entity_id"] == "light.keller"
        matcher.match.assert_not_awaited()

    # ------------------------------------------------------------------
    # FLOW-VERIFY-1: post-action state verification and speech tests
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_uses_call_service_response_state(self, ha_client, entity_matcher, entity_index):
        """call_service returning a state list is authoritative over get_state."""
        ha_client.call_service = AsyncMock(return_value=[{"entity_id": "light.kitchen_ceiling", "state": "off"}])
        ha_client.get_state = AsyncMock(
            return_value={"state": "on", "attributes": {}},
        )

        action = {"action": "turn_off", "entity": "kitchen light"}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "off"
        assert "Kitchen Ceiling is now off" in result["speech"]

    @pytest.mark.asyncio
    async def test_uses_ws_waiter_when_available(self, ha_client, entity_matcher, entity_index):
        """If a WS observer resolves the waiter, get_state must not be consulted."""
        ha_client.call_service = AsyncMock(return_value=None)
        ha_client.get_state = AsyncMock(
            return_value={"state": "on", "attributes": {}},
        )

        @asynccontextmanager
        async def _ws_expect_state(entity_id, *, expected=None, **_kw):
            result = {"new_state": None}
            yield result
            result["new_state"] = expected or "off"

        ha_client.expect_state = _ws_expect_state

        action = {"action": "turn_off", "entity": "kitchen light"}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "off"
        ha_client.get_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_intent_speech_when_verified_state_is_stale(self, ha_client, entity_matcher, entity_index):
        """turn_off + observed 'on' must not speak 'is now on'."""
        ha_client.call_service = AsyncMock(return_value=None)
        ha_client.get_state = AsyncMock(
            return_value={"state": "on", "attributes": {}},
        )

        action = {"action": "turn_off", "entity": "kitchen light"}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert "is now on" not in result["speech"]
        assert "turned off Kitchen Ceiling" in result["speech"]

    @pytest.mark.asyncio
    async def test_toggle_uses_observed_state(self, ha_client, entity_matcher, entity_index):
        """toggle has no expected state; observed state drives the speech."""
        ha_client.call_service = AsyncMock(return_value=None)
        ha_client.get_state = AsyncMock(
            return_value={"state": "off", "attributes": {}},
        )

        action = {"action": "toggle", "entity": "kitchen light"}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "off"
        assert "Kitchen Ceiling is now off" in result["speech"]


# ---------------------------------------------------------------------------
# Climate executor domain validation tests
# ---------------------------------------------------------------------------

from app.agents.climate_executor import execute_climate_action  # noqa: E402


class TestClimateExecutorDomainValidation:
    """Tests for climate executor domain validation."""

    @pytest.fixture()
    def ha_client(self):
        client = AsyncMock()
        client.call_service = AsyncMock(return_value={})
        client.get_state = AsyncMock(
            return_value={
                "state": "heat",
                "attributes": {"friendly_name": "Living Room Climate", "current_temperature": 21.5},
            }
        )
        return client

    @pytest.mark.asyncio
    async def test_rejects_media_player_entity(self, ha_client):
        """Climate executor should reject media_player entities."""
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "media_player.wohnzimmer_tv"
        match_result.friendly_name = "TV Wohnzimmer-TV"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        action = {"action": "query_climate_state", "entity": "Wohnzimmer", "parameters": {}}
        result = await execute_climate_action(action, ha_client, index, matcher, agent_id="climate-agent")

        assert result["success"] is False
        assert "Could not find" in result["speech"]

    @pytest.mark.asyncio
    async def test_accepts_climate_entity(self, ha_client):
        """Climate executor should accept climate domain entities."""
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "climate.living_room"
        match_result.friendly_name = "Living Room Climate"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        action = {"action": "query_climate_state", "entity": "living room", "parameters": {}}
        result = await execute_climate_action(action, ha_client, index, matcher, agent_id="climate-agent")

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_accepts_sensor_entity(self, ha_client):
        """Climate executor should accept sensor domain entities."""
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "sensor.living_room_temperature"
        match_result.friendly_name = "Living Room Temperature"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        ha_client.get_state = AsyncMock(
            return_value={
                "state": "21.5",
                "attributes": {"friendly_name": "Living Room Temperature", "unit_of_measurement": "C"},
            }
        )
        action = {"action": "query_climate_state", "entity": "living room temp", "parameters": {}}
        result = await execute_climate_action(action, ha_client, index, matcher, agent_id="climate-agent")

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_no_fallback_to_unfiltered_index(self, ha_client):
        """When matcher returns empty, should NOT fall back to entity_index.search()."""
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        index = MagicMock()

        action = {"action": "query_climate_state", "entity": "Wohnzimmer", "parameters": {}}
        result = await execute_climate_action(action, ha_client, index, matcher, agent_id="climate-agent")

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        index.search.assert_not_called()


# ---------------------------------------------------------------------------
# Media executor domain validation tests
# ---------------------------------------------------------------------------

from app.agents.media_executor import execute_media_action  # noqa: E402


class TestMediaExecutorDomainValidation:
    """Tests for media executor domain validation."""

    @pytest.fixture()
    def ha_client(self):
        client = AsyncMock()
        client.call_service = AsyncMock(return_value={})
        client.get_state = AsyncMock(return_value={"state": "playing", "attributes": {}})
        return client

    @pytest.mark.asyncio
    async def test_rejects_light_entity(self, ha_client):
        """Media executor should reject light entities."""
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "light.living_room"
        match_result.friendly_name = "Living Room Light"
        matcher.match = AsyncMock(return_value=[match_result])
        index = MagicMock()

        action = {"action": "play", "entity": "living room", "parameters": {}}
        result = await execute_media_action(action, ha_client, index, matcher, agent_id="media-agent")

        assert result["success"] is False
        assert "Could not find" in result["speech"]


from app.agents.automation_executor import execute_automation_action  # noqa: E402
from app.agents.scene_executor import execute_scene_action  # noqa: E402
from app.agents.security_executor import execute_security_action  # noqa: E402


class TestSharedDeterministicResolution:
    """Deterministic-first exact resolution should short-circuit hybrid fallback."""

    @pytest.mark.asyncio
    async def test_climate_exact_friendly_name_skips_hybrid_matcher(self):
        ha_client = AsyncMock()
        ha_client.get_state = AsyncMock(
            return_value={
                "state": "heat",
                "attributes": {"friendly_name": "Living Room Climate", "current_temperature": 21.5},
            }
        )
        matcher = AsyncMock(spec=EntityMatcher)
        matcher.match = AsyncMock()
        index = _make_listable_entity_index(make_entity_index_entry("climate.living_room", "Living Room Climate"))

        result = await execute_climate_action(
            {"action": "query_climate_state", "entity": "Living Room Climate", "parameters": {}},
            ha_client,
            index,
            matcher,
            agent_id="climate-agent",
        )

        assert result["success"] is True
        matcher.match.assert_not_awaited()
        index.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_media_exact_friendly_name_skips_hybrid_matcher(self):
        ha_client = AsyncMock()
        ha_client.get_state = AsyncMock(
            return_value={
                "state": "playing",
                "attributes": {"friendly_name": "Kitchen Speaker", "media_title": "Blue in Green"},
            }
        )
        matcher = AsyncMock(spec=EntityMatcher)
        matcher.match = AsyncMock()
        index = _make_listable_entity_index(make_entity_index_entry("media_player.kitchen", "Kitchen Speaker"))

        result = await execute_media_action(
            {"action": "query_media_state", "entity": "Kitchen Speaker", "parameters": {}},
            ha_client,
            index,
            matcher,
            agent_id="media-agent",
        )

        assert result["success"] is True
        matcher.match.assert_not_awaited()
        index.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_automation_exact_friendly_name_skips_hybrid_matcher(self):
        ha_client = AsyncMock()
        ha_client.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"friendly_name": "Morning Routine", "last_triggered": "2024-01-15T10:30:00"},
            }
        )
        matcher = AsyncMock(spec=EntityMatcher)
        matcher.match = AsyncMock()
        index = _make_listable_entity_index(make_entity_index_entry("automation.morning_routine", "Morning Routine"))

        result = await execute_automation_action(
            {"action": "query_automation_state", "entity": "Morning Routine", "parameters": {}},
            ha_client,
            index,
            matcher,
            agent_id="automation-agent",
        )

        assert result["success"] is True
        matcher.match.assert_not_awaited()
        index.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_scene_exact_friendly_name_skips_hybrid_matcher(self):
        matcher = AsyncMock(spec=EntityMatcher)
        matcher.match = AsyncMock()
        index = _make_listable_entity_index(make_entity_index_entry("scene.movie_night", "Movie Night"))

        result = await execute_scene_action(
            {"action": "query_scene", "entity": "Movie Night", "parameters": {}},
            AsyncMock(),
            index,
            matcher,
            agent_id="scene-agent",
        )

        assert result["success"] is True
        matcher.match.assert_not_awaited()
        index.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_security_exact_friendly_name_skips_hybrid_matcher(self):
        ha_client = AsyncMock()
        ha_client.get_state = AsyncMock(
            return_value={
                "state": "locked",
                "attributes": {"friendly_name": "Front Door Lock"},
            }
        )
        matcher = AsyncMock(spec=EntityMatcher)
        matcher.match = AsyncMock()
        index = _make_listable_entity_index(make_entity_index_entry("lock.front_door", "Front Door Lock"))

        result = await execute_security_action(
            {"action": "query_security_state", "entity": "Front Door Lock", "parameters": {}},
            ha_client,
            index,
            matcher,
            agent_id="security-agent",
        )

        assert result["success"] is True
        matcher.match.assert_not_awaited()
        index.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_calendar_hidden_calendar_does_not_fall_back_to_raw_search(self, monkeypatch):
        from app.agents.calendar_executor import execute_calendar_action

        monkeypatch.setattr(
            "app.entity.visibility.EntityVisibilityRepository.get_rules",
            AsyncMock(return_value=[{"rule_type": "domain_include", "rule_value": "input_datetime"}]),
        )
        ha_client = AsyncMock()
        ha_client.call_service = AsyncMock(return_value={})
        entry = make_entity_index_entry("calendar.family", "Family Calendar", area=None)
        index = _make_listable_entity_index(entry)
        index.search_async = AsyncMock(return_value=[(entry, 0.08)])

        result = await execute_calendar_action(
            {
                "action": "create_event",
                "entity": "Family Calendar",
                "parameters": {
                    "summary": "Take medication",
                    "start_date_time": "2026-04-27 08:00:00",
                },
            },
            ha_client,
            index,
            None,
            agent_id="calendar-agent",
        )

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        index.list_entries_async.assert_awaited_once_with(domains=frozenset({"calendar"}))
        index.search_async.assert_not_awaited()
        ha_client.call_service.assert_not_awaited()


# ---------------------------------------------------------------------------
# execute_music_action tests
# ---------------------------------------------------------------------------

from app.agents.music_executor import execute_music_action  # noqa: E402


class TestMusicExecutor:
    """Tests for execute_music_action() with mocked dependencies."""

    @pytest.fixture()
    def ha_client(self):
        client = AsyncMock()
        client.call_service = AsyncMock(return_value={})
        client.get_state = AsyncMock(return_value={"state": "playing", "attributes": {}})
        return _attach_expect_state_shim(client)

    @pytest.fixture()
    def entity_matcher(self):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "media_player.ma_kitchen"
        match_result.friendly_name = "Kitchen Speaker"
        matcher.match = AsyncMock(return_value=[match_result])
        return matcher

    @pytest.fixture()
    def entity_index(self):
        index = MagicMock()
        entry = MagicMock()
        entry.entity_id = "media_player.ma_kitchen"
        entry.friendly_name = "Kitchen Speaker"
        index.search = MagicMock(return_value=[(entry, 0.1)])
        return index

    @pytest.mark.asyncio
    async def test_execute_play_media(self, ha_client, entity_matcher, entity_index):
        action = {
            "action": "play_media",
            "entity": "kitchen speaker",
            "parameters": {"media_id": "jazz", "media_type": "track", "enqueue": "play"},
        }
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "media_player.ma_kitchen"
        ha_client.call_service.assert_awaited_once_with(
            "music_assistant",
            "play_media",
            "media_player.ma_kitchen",
            {"media_id": "jazz", "media_type": "track", "enqueue": "play"},
        )

    @pytest.mark.asyncio
    async def test_execute_play_media_with_artist_and_album(self, ha_client, entity_matcher, entity_index):
        action = {
            "action": "play_media",
            "entity": "kitchen speaker",
            "parameters": {
                "media_id": "jazz",
                "media_type": "track",
                "enqueue": "play",
                "artist": "Dave Brubeck",
                "album": "Time Out",
            },
        }
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "media_player.ma_kitchen"
        ha_client.call_service.assert_awaited_once_with(
            "music_assistant",
            "play_media",
            "media_player.ma_kitchen",
            {
                "media_id": "jazz",
                "media_type": "track",
                "enqueue": "play",
                "artist": "Dave Brubeck",
                "album": "Time Out",
            },
        )

    @pytest.mark.asyncio
    async def test_execute_play_media_with_radio_mode(self, ha_client, entity_matcher, entity_index):
        action = {
            "action": "play_media",
            "entity": "kitchen speaker",
            "parameters": {
                "media_id": "jazz",
                "media_type": "radio",
                "radio_mode": 1,
            },
        }
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "media_player.ma_kitchen"
        ha_client.call_service.assert_awaited_once_with(
            "music_assistant",
            "play_media",
            "media_player.ma_kitchen",
            {"media_id": "jazz", "media_type": "radio", "radio_mode": True},
        )

    @pytest.mark.asyncio
    async def test_execute_volume_set(self, ha_client, entity_matcher, entity_index):
        action = {"action": "volume_set", "entity": "kitchen speaker", "parameters": {"volume_level": 0.5}}
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once_with(
            "media_player",
            "volume_set",
            "media_player.ma_kitchen",
            {"volume_level": 0.5},
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action_name", ["media_play", "media_pause", "media_next_track", "media_previous_track"])
    async def test_execute_transport_controls(self, action_name, ha_client, entity_matcher, entity_index):
        action = {"action": action_name, "entity": "kitchen speaker", "parameters": {}}
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once_with(
            "media_player",
            action_name,
            "media_player.ma_kitchen",
            None,
        )

    @pytest.mark.asyncio
    async def test_execute_shuffle_set(self, ha_client, entity_matcher, entity_index):
        action = {"action": "shuffle_set", "entity": "kitchen speaker", "parameters": {"shuffle": True}}
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once_with(
            "media_player",
            "shuffle_set",
            "media_player.ma_kitchen",
            {"shuffle": True},
        )

    @pytest.mark.asyncio
    async def test_execute_repeat_set(self, ha_client, entity_matcher, entity_index):
        action = {"action": "repeat_set", "entity": "kitchen speaker", "parameters": {"repeat": "all"}}
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once_with(
            "media_player",
            "repeat_set",
            "media_player.ma_kitchen",
            {"repeat": "all"},
        )

    @pytest.mark.asyncio
    async def test_execute_search_returns_speech(self, ha_client, entity_matcher, entity_index):
        ha_client.call_service = AsyncMock(
            return_value=[
                {"name": "Jazz Suite", "artist": "Dave Brubeck"},
                {"name": "Blue Train", "artist": "John Coltrane"},
            ]
        )
        action = {
            "action": "search",
            "entity": "kitchen speaker",
            "parameters": {"name": "jazz", "media_type": "track"},
        }
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] is None
        assert "Jazz Suite" in result["speech"]
        assert "Dave Brubeck" in result["speech"]
        ha_client.call_service.assert_awaited_once_with(
            "music_assistant",
            "search",
            "media_player.ma_kitchen",
            {"name": "jazz", "media_type": "track"},
        )

    @pytest.mark.asyncio
    async def test_execute_search_with_library_only(self, ha_client, entity_matcher, entity_index):
        ha_client.call_service = AsyncMock(return_value=[])
        action = {
            "action": "search",
            "entity": "kitchen speaker",
            "parameters": {"name": "jazz", "media_type": "track", "library_only": 1},
        }
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] is None
        ha_client.call_service.assert_awaited_once_with(
            "music_assistant",
            "search",
            "media_player.ma_kitchen",
            {"name": "jazz", "media_type": "track", "library_only": True},
        )

    @pytest.mark.asyncio
    async def test_execute_unknown_action(self, ha_client, entity_matcher, entity_index):
        action = {"action": "nonexistent", "entity": "kitchen speaker", "parameters": {}}
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Unknown action" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_entity_not_found(self, ha_client):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        index = MagicMock()
        index.search = MagicMock(return_value=[])

        action = {"action": "play_media", "entity": "nonexistent speaker", "parameters": {"media_id": "jazz"}}
        result = await execute_music_action(action, ha_client, index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_service_call_failure(self, ha_client, entity_matcher, entity_index):
        ha_client.call_service = AsyncMock(side_effect=Exception("Connection refused"))
        action = {"action": "media_play", "entity": "kitchen speaker", "parameters": {}}
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Failed" in result["speech"]

    @pytest.mark.asyncio
    async def test_entity_resolution_prefers_matcher(self, ha_client, entity_matcher, entity_index):
        action = {"action": "media_play", "entity": "kitchen speaker", "parameters": {}}
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        entity_matcher.match.assert_awaited_once()
        call = entity_matcher.match.await_args
        assert call.args == ("kitchen speaker",)
        assert call.kwargs == {"agent_id": None, "verbatim_terms": None, "preferred_domains": ("media_player",)}
        entity_index.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_music_action_passes_agent_id(self, ha_client, entity_matcher, entity_index):
        """Verify that entity_matcher.match is called with agent_id kwarg."""
        action = {"action": "media_play", "entity": "kitchen speaker", "parameters": {}}
        result = await execute_music_action(action, ha_client, entity_index, entity_matcher, agent_id="music-agent")

        assert result["success"] is True
        entity_matcher.match.assert_awaited_once()
        call = entity_matcher.match.await_args
        assert call.args == ("kitchen speaker",)
        assert call.kwargs == {
            "agent_id": "music-agent",
            "verbatim_terms": None,
            "preferred_domains": ("media_player",),
        }


# ---------------------------------------------------------------------------
# Entity match span verification tests
# ---------------------------------------------------------------------------

from app.analytics.tracer import SpanCollector  # noqa: E402


class TestEntityMatchSpan:
    """Tests that entity_match spans are recorded with correct metadata."""

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
        client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
        return _attach_expect_state_shim(client)

    @pytest.fixture()
    def entity_matcher(self):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "light.kitchen_ceiling"
        match_result.friendly_name = "Kitchen Ceiling"
        match_result.score = 0.95
        match_result.signal_scores = {"levenshtein": 0.9, "embedding": 0.8}
        matcher.match = AsyncMock(return_value=[match_result])
        return matcher

    @pytest.fixture()
    def entity_index(self):
        return MagicMock()

    @pytest.mark.asyncio
    async def test_entity_match_span_recorded(self, ha_client, entity_matcher, entity_index):
        """Verify entity_match span is recorded with correct metadata when SpanCollector is passed."""
        span_collector = SpanCollector(trace_id="test-trace-123")
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}

        result = await execute_action(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id="light-agent",
            span_collector=span_collector,
        )

        assert result["success"] is True

        # Find the entity_match span
        em_spans = [s for s in span_collector._spans if s["span_name"] == "entity_match"]
        assert len(em_spans) == 1
        span = em_spans[0]

        assert span["agent_id"] == "light-agent"
        assert span["status"] == "ok"
        assert span["metadata"]["query"] == "kitchen light"
        assert span["metadata"]["match_count"] == 1
        assert span["metadata"]["top_entity_id"] == "light.kitchen_ceiling"
        assert span["metadata"]["top_friendly_name"] == "Kitchen Ceiling"
        assert span["metadata"]["top_score"] == 0.95
        assert span["metadata"]["signal_scores"] == {"levenshtein": 0.9, "embedding": 0.8}

    @pytest.mark.asyncio
    async def test_entity_match_span_no_match(self, ha_client, entity_index):
        """Verify entity_match span records zero matches correctly."""
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        span_collector = SpanCollector(trace_id="test-trace-456")
        action = {"action": "turn_on", "entity": "nonexistent", "parameters": {}}

        result = await execute_action(
            action,
            ha_client,
            entity_index,
            matcher,
            agent_id="light-agent",
            span_collector=span_collector,
        )

        assert result["success"] is False
        em_spans = [s for s in span_collector._spans if s["span_name"] == "entity_match"]
        assert len(em_spans) == 1
        assert em_spans[0]["metadata"]["match_count"] == 0

    @pytest.mark.asyncio
    async def test_no_span_without_collector(self, ha_client, entity_matcher, entity_index):
        """Verify execute_action works without span_collector (backward compatible)."""
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "light.kitchen_ceiling"

    @pytest.mark.asyncio
    async def test_entity_match_span_records_exact_resolution_path(self, ha_client):
        matcher = MagicMock(spec=EntityMatcher)
        matcher.match = AsyncMock(return_value=[])
        matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
        index = MagicMock(spec=EntityIndex)
        index.get_by_id.return_value = None
        index.list_entries_async = AsyncMock(
            return_value=[make_entity_index_entry("light.keller", "Keller", area="Basement")]
        )
        span_collector = SpanCollector(trace_id="test-trace-deterministic")

        result = await execute_action(
            {"action": "turn_on", "entity": "Keller", "parameters": {}},
            ha_client,
            index,
            matcher,
            agent_id="light-agent",
            span_collector=span_collector,
        )

        assert result["success"] is True
        em_spans = [s for s in span_collector._spans if s["span_name"] == "entity_match"]
        assert len(em_spans) == 1
        assert em_spans[0]["metadata"]["resolution_path"] == "exact_friendly_name"
        assert em_spans[0]["metadata"]["top_entity_id"] == "light.keller"


# ---------------------------------------------------------------------------
# Read-only actions return cacheable=False
# ---------------------------------------------------------------------------


class TestReadActionCacheable:
    """Tests that read-only executor actions return cacheable=False."""

    @pytest.fixture()
    def ha_client(self):
        client = AsyncMock()
        client.get_state = AsyncMock(
            return_value={
                "state": "on",
                "attributes": {"friendly_name": "Kitchen Light", "brightness": 200},
            }
        )
        client.get_states = AsyncMock(
            return_value=[
                {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}},
            ]
        )
        return client

    @pytest.fixture()
    def entity_matcher(self):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "light.kitchen"
        match_result.friendly_name = "Kitchen Light"
        match_result.score = 0.95
        match_result.signal_scores = {}
        matcher.match = AsyncMock(return_value=[match_result])
        return matcher

    @pytest.fixture()
    def entity_index(self):
        return MagicMock()

    @pytest.mark.asyncio
    async def test_query_light_state_cacheable_false(self, ha_client, entity_matcher, entity_index):
        action = {"action": "query_light_state", "entity": "kitchen light"}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)
        assert result["cacheable"] is False

    @pytest.mark.asyncio
    async def test_list_lights_cacheable_false(self, ha_client, entity_matcher, entity_index):
        action = {"action": "list_lights", "entity": ""}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)
        assert result["cacheable"] is False


# ---------------------------------------------------------------------------
# Climate executor weather action tests
# ---------------------------------------------------------------------------


class TestClimateExecutorWeatherActions:
    """Tests for climate executor weather query actions."""

    @pytest.fixture()
    def ha_client(self):
        client = AsyncMock()
        client.call_service = AsyncMock(return_value={})
        client.get_state = AsyncMock(
            return_value={
                "state": "sunny",
                "attributes": {
                    "friendly_name": "Home",
                    "temperature": 22.5,
                    "temperature_unit": "C",
                    "humidity": 55,
                    "wind_speed": 12.3,
                    "wind_speed_unit": "km/h",
                    "pressure": 1013,
                    "pressure_unit": "hPa",
                },
            }
        )
        client.get_states = AsyncMock(
            return_value=[
                {"entity_id": "weather.home", "state": "sunny", "attributes": {"friendly_name": "Home"}},
            ]
        )
        return client

    @pytest.mark.asyncio
    async def test_weather_domain_validation_accepts(self):
        from app.agents.climate_executor import _validate_domain

        assert _validate_domain("weather.home") is True

    @pytest.mark.asyncio
    async def test_query_weather_with_entity_match(self, ha_client):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "weather.home"
        match_result.friendly_name = "Home"
        match_result.score = 0.9
        match_result.signal_scores = {}
        matcher.match = AsyncMock(return_value=[match_result])

        result = await execute_climate_action(
            {"action": "query_weather", "entity": "home weather"},
            ha_client,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"] is True
        assert "sunny" in result["speech"]
        assert "22.5" in result["speech"]
        assert "55%" in result["speech"]

    @pytest.mark.asyncio
    async def test_query_weather_auto_discover(self, ha_client):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])

        result = await execute_climate_action(
            {"action": "query_weather", "entity": ""},
            ha_client,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"] is True
        assert "sunny" in result["speech"]

    @pytest.mark.asyncio
    async def test_query_weather_no_entity_found(self, ha_client):
        ha_client.get_states = AsyncMock(return_value=[])
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])

        result = await execute_climate_action(
            {"action": "query_weather", "entity": ""},
            ha_client,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"] is False
        assert "No weather entities" in result["speech"]

    @pytest.mark.asyncio
    async def test_query_weather_forecast_service_call(self, ha_client):
        ha_client.call_service = AsyncMock(
            return_value={
                "weather.home": {
                    "forecast": [
                        {
                            "datetime": "2025-01-16T00:00:00",
                            "condition": "cloudy",
                            "temperature": 18,
                            "templow": 8,
                            "precipitation": 2.5,
                            "wind_speed": 15,
                        },
                        {
                            "datetime": "2025-01-17T00:00:00",
                            "condition": "rainy",
                            "temperature": 15,
                            "templow": 6,
                            "precipitation": 10,
                            "wind_speed": 20,
                        },
                    ],
                },
            }
        )
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "weather.home"
        match_result.friendly_name = "Home"
        match_result.score = 0.9
        match_result.signal_scores = {}
        matcher.match = AsyncMock(return_value=[match_result])

        result = await execute_climate_action(
            {"action": "query_weather_forecast", "entity": "home"},
            ha_client,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"] is True
        assert "cloudy" in result["speech"]
        assert "rainy" in result["speech"]
        ha_client.call_service.assert_awaited_once_with(
            "weather", "get_forecasts", "weather.home", {"type": "daily"}, return_response=True
        )

    @pytest.mark.asyncio
    async def test_query_weather_forecast_fallback_to_state(self, ha_client):
        ha_client.call_service = AsyncMock(side_effect=Exception("Service not found"))
        ha_client.get_state = AsyncMock(
            return_value={
                "state": "sunny",
                "attributes": {
                    "friendly_name": "Home",
                    "forecast": [
                        {
                            "datetime": "2025-01-16T00:00:00",
                            "condition": "partly_cloudy",
                            "temperature": 20,
                            "templow": 10,
                        },
                    ],
                },
            }
        )
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "weather.home"
        match_result.friendly_name = "Home"
        match_result.score = 0.9
        match_result.signal_scores = {}
        matcher.match = AsyncMock(return_value=[match_result])

        result = await execute_climate_action(
            {"action": "query_weather_forecast", "entity": "home"},
            ha_client,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"] is True
        assert "partly_cloudy" in result["speech"]

    @pytest.mark.asyncio
    async def test_query_weather_forecast_no_data(self, ha_client):
        ha_client.call_service = AsyncMock(side_effect=Exception("Service not found"))
        ha_client.get_state = AsyncMock(
            return_value={
                "state": "sunny",
                "attributes": {"friendly_name": "Home"},
            }
        )
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "weather.home"
        match_result.friendly_name = "Home"
        match_result.score = 0.9
        match_result.signal_scores = {}
        matcher.match = AsyncMock(return_value=[match_result])

        result = await execute_climate_action(
            {"action": "query_weather_forecast", "entity": "home"},
            ha_client,
            None,
            matcher,
            agent_id="climate-agent",
        )
        assert result["success"] is False
        assert "not available" in result["speech"]


# ---------------------------------------------------------------------------
# filter_matches_by_domain helper tests (FLOW-DOMAIN-1)
# ---------------------------------------------------------------------------


class TestFilterMatchesByDomain:
    def test_drops_wrong_domain(self):
        matches = [
            MatchResult(entity_id="lock.front_door", friendly_name="Front Door Lock", score=0.9),
            MatchResult(entity_id="camera.front_door", friendly_name="Front Door Camera", score=0.85),
        ]
        out = filter_matches_by_domain(matches, frozenset({"camera"}))
        assert [m.entity_id for m in out] == ["camera.front_door"]

    def test_empty_when_no_match_no_fallback(self):
        matches = [MatchResult(entity_id="lock.front_door", friendly_name="x", score=0.9)]
        assert filter_matches_by_domain(matches, frozenset({"camera"})) == []

    def test_fallback_returns_copy_of_original(self):
        matches = [MatchResult(entity_id="lock.front_door", friendly_name="x", score=0.9)]
        out = filter_matches_by_domain(matches, frozenset({"camera"}), fallback_to_unfiltered=True)
        assert out is not matches
        assert [m.entity_id for m in out] == ["lock.front_door"]

    def test_preserves_order(self):
        matches = [
            MatchResult(entity_id="switch.a", friendly_name="A", score=0.9),
            MatchResult(entity_id="light.a", friendly_name="A", score=0.85),
            MatchResult(entity_id="light.b", friendly_name="B", score=0.8),
        ]
        out = filter_matches_by_domain(matches, frozenset({"light"}))
        assert [m.entity_id for m in out] == ["light.a", "light.b"]


# ---------------------------------------------------------------------------
# Conditional action tests
# ---------------------------------------------------------------------------


class TestExecuteActionCondition:
    """Tests for execute_action with pre-action conditions."""

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
        client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
        return _attach_expect_state_shim(client)

    @pytest.fixture()
    def entity_matcher(self):
        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "light.kitchen_ceiling"
        match_result.friendly_name = "Kitchen Ceiling"
        matcher.match = AsyncMock(return_value=[match_result])
        return matcher

    @pytest.fixture()
    def entity_index(self):
        index = MagicMock()
        entry = MagicMock()
        entry.entity_id = "light.kitchen_ceiling"
        entry.friendly_name = "Kitchen Ceiling"
        index.search = MagicMock(return_value=[(entry, 0.1)])
        return index

    @pytest.mark.asyncio
    async def test_passing_condition_calls_service_and_sets_cacheable_false(
        self, ha_client, entity_matcher, entity_index
    ):
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
        action = {
            "action": "turn_on",
            "entity": "kitchen light",
            "parameters": {},
            "condition": {"entity": "kitchen light", "state": "off", "operator": "eq"},
        }
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once()
        assert result.get("cacheable") is False

    @pytest.mark.asyncio
    async def test_failing_condition_skips_service_and_sets_cacheable_false(
        self, ha_client, entity_matcher, entity_index
    ):
        ha_client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
        action = {
            "action": "turn_on",
            "entity": "kitchen light",
            "parameters": {},
            "condition": {"entity": "kitchen light", "state": "off", "operator": "eq"},
        }
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert "Skipped" in result["speech"]
        ha_client.call_service.assert_not_awaited()
        assert result.get("cacheable") is False

    @pytest.mark.asyncio
    async def test_no_condition_regression_cacheable_true(self, ha_client, entity_matcher, entity_index):
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once()
        # Without a condition the result should not carry cacheable=False.
        assert result.get("cacheable", True) is True
