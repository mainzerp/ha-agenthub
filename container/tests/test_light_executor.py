"""Tests for app.agents.light_executor -- execute_light_action and helpers."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
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

from app.agents.light_executor import execute_light_action  # noqa: E402
from app.entity.index import EntityIndex  # noqa: E402
from app.entity.matcher import EntityMatcher  # noqa: E402
from tests.helpers import attach_expect_state_shim, make_entity_index_entry  # noqa: E402


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )


# ---------------------------------------------------------------------------
# execute_light_action tests
# ---------------------------------------------------------------------------


class TestExecuteLightAction:
    """Tests for execute_light_action() with mocked dependencies."""

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
        return attach_expect_state_shim(client)

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
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "off", "attributes": {}},
                {"state": "on", "attributes": {}},
            ]
        )
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["entity_id"] == "light.kitchen_ceiling"
        assert result["new_state"] == "on"
        assert "Kitchen Ceiling" in result["speech"]
        ha_client.call_service.assert_awaited_once_with("light", "turn_on", "light.kitchen_ceiling", None)

    @pytest.mark.asyncio
    async def test_turn_off_success(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(
            side_effect=[
                {"state": "on", "attributes": {}},
                {"state": "off", "attributes": {}},
            ]
        )
        action = {"action": "turn_off", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "off"
        ha_client.call_service.assert_awaited_once_with("light", "turn_off", "light.kitchen_ceiling", None)

    @pytest.mark.asyncio
    async def test_set_brightness(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
        action = {"action": "set_brightness", "entity": "kitchen light", "parameters": {"brightness": 128}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once_with(
            "light", "turn_on", "light.kitchen_ceiling", {"brightness": 128}
        )

    @pytest.mark.asyncio
    async def test_unknown_action(self, ha_client, entity_matcher, entity_index):
        action = {"action": "explode", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Unknown action" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entity_not_found(self, ha_client, entity_index):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        entity_index.search = MagicMock(return_value=[])

        action = {"action": "turn_on", "entity": "nonexistent lamp", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_service_call_failure(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
        ha_client.call_service = AsyncMock(side_effect=Exception("Connection refused"))
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is False
        assert "Failed to execute" in result["speech"]

    @pytest.mark.asyncio
    async def test_no_fallback_to_entity_index(self, ha_client, entity_index):
        """When entity_matcher returns no results, should NOT fall back to unfiltered entity_index."""
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])

        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, matcher)

        assert result["success"] is False
        assert "Could not find" in result["speech"]
        entity_index.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_matcher_no_fallback(self, ha_client, entity_index):
        """When entity_matcher is None, should NOT fall back to unfiltered entity_index."""
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, None)

        assert result["success"] is False
        assert "Could not find" in result["speech"]

    @pytest.mark.asyncio
    async def test_state_verification_failure(self, ha_client, entity_matcher, entity_index):
        """State verification failure should not affect success status."""
        ha_client.get_state = AsyncMock(side_effect=Exception("timeout"))
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] is None

    @pytest.mark.asyncio
    async def test_execute_light_action_passes_agent_id(self, ha_client, entity_matcher, entity_index):
        """Verify that entity_matcher.match is called with agent_id kwarg."""
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher, agent_id="light-agent")

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
        result = await execute_light_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert result["success"] is False
        assert "Could not find" in result["speech"]

    @pytest.mark.asyncio
    async def test_domain_validation_accepts_light_domain(self, ha_client, entity_matcher, entity_index):
        """Entity in light domain should pass domain validation."""
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

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
        result = await execute_light_action(action, ha_client, index, matcher)

        assert result["success"] is True
        assert result["entity_id"] == "switch.kitchen_outlet"

    @pytest.mark.asyncio
    async def test_exact_friendly_name_resolves_without_hybrid_match(self, ha_client):
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
        matcher = MagicMock(spec=EntityMatcher)
        matcher.match = AsyncMock(return_value=[])
        matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
        index = MagicMock(spec=EntityIndex)
        index.get_by_id.return_value = None
        index.list_entries_async = AsyncMock(
            return_value=[make_entity_index_entry("light.keller", "Keller", area="Basement")]
        )

        action = {"action": "turn_on", "entity": "Keller", "parameters": {}}
        result = await execute_light_action(action, ha_client, index, matcher, agent_id="light-agent")

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
        result = await execute_light_action(action, ha_client, index, matcher, agent_id="light-agent")

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
        result = await execute_light_action(action, ha_client, index, matcher, agent_id="light-agent")

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
        result = await execute_light_action(action, ha_client, index, matcher, agent_id="light-agent")

        assert result["success"] is False
        assert "Multiple entities match 'Keller'" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exact_friendly_name_ambiguity_falls_back_to_hybrid_matcher(self, ha_client):
        """When multiple entities share the same friendly name, the hybrid matcher should break the tie."""
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
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
        result = await execute_light_action(action, ha_client, index, matcher, agent_id="light-agent")

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
        result = await execute_light_action(action, ha_client, index, matcher, agent_id="light-agent")

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
        first_result = await execute_light_action(action, ha_client, index, matcher, agent_id="light-agent")
        second_result = await execute_light_action(action, ha_client, index, matcher, agent_id="light-agent")

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
        result = await execute_light_action(action, ha_client, index, matcher, agent_id="light-agent")

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
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "off"
        assert "Kitchen Ceiling is now off" in result["speech"]

    @pytest.mark.asyncio
    async def test_uses_ws_waiter_when_available(self, ha_client, entity_matcher, entity_index):
        """If a WS observer resolves the waiter, get_state must not be consulted for verification."""
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
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "off"
        ha_client.get_state.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_intent_speech_when_verified_state_is_stale(self, ha_client, entity_matcher, entity_index):
        """turn_off + observed 'on' must not speak 'is now on'."""
        ha_client.call_service = AsyncMock(return_value=None)
        ha_client.get_state = AsyncMock(
            return_value={"state": "on", "attributes": {}},
        )

        action = {"action": "turn_off", "entity": "kitchen light"}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

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
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "off"
        assert "Kitchen Ceiling is now off" in result["speech"]

    @pytest.mark.asyncio
    async def test_turn_on_skips_when_already_on(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "on"
        assert "already on" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_turn_off_skips_when_already_off(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
        action = {"action": "turn_off", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert result["new_state"] == "off"
        assert "already off" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_toggle_never_skips(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
        action = {"action": "toggle", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skip_check_failure_falls_through_to_service_call(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(side_effect=Exception("HA timeout"))
        action = {"action": "turn_off", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once()


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
        return attach_expect_state_shim(client)

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

        result = await execute_light_action(
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

        result = await execute_light_action(
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
        """Verify execute_light_action works without span_collector (backward compatible)."""
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

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

        result = await execute_light_action(
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
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)
        assert result["cacheable"] is False

    @pytest.mark.asyncio
    async def test_list_lights_cacheable_false(self, ha_client, entity_matcher, entity_index):
        action = {"action": "list_lights", "entity": ""}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)
        assert result["cacheable"] is False


# ---------------------------------------------------------------------------
# Conditional action tests
# ---------------------------------------------------------------------------


class TestExecuteLightActionCondition:
    """Tests for execute_light_action with pre-action conditions."""

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
        return attach_expect_state_shim(client)

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
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once()
        assert result.get("cacheable") is False

    @pytest.mark.asyncio
    async def test_failing_condition_skips_service_and_sets_cacheable_false(
        self, ha_client, entity_matcher, entity_index
    ):
        ha_client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
        action = {
            "action": "turn_off",
            "entity": "kitchen light",
            "parameters": {},
            "condition": {"entity": "kitchen light", "state": "off", "operator": "eq"},
        }
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        assert "Skipped" in result["speech"]
        ha_client.call_service.assert_not_awaited()
        assert result.get("cacheable") is False

    @pytest.mark.asyncio
    async def test_no_condition_regression_cacheable_true(self, ha_client, entity_matcher, entity_index):
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
        action = {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
        result = await execute_light_action(action, ha_client, entity_index, entity_matcher)

        assert result["success"] is True
        ha_client.call_service.assert_awaited_once()
        # Without a condition the result should not carry cacheable=False.
        assert result.get("cacheable", True) is True
