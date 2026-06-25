"""Tests for ActionCondition and _evaluate_condition in action_executor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.action_executor import (
    ActionCondition,
    _evaluate_condition,
)


class TestActionConditionValidation:
    """Tests for ActionCondition Pydantic validation."""

    def test_valid_full_condition(self):
        cond = ActionCondition(entity="kitchen light", state="on", attribute="brightness", operator="eq")
        assert cond.entity == "kitchen light"
        assert cond.state == "on"
        assert cond.attribute == "brightness"
        assert cond.operator == "eq"

    def test_valid_minimal_condition(self):
        cond = ActionCondition(entity="kitchen light")
        assert cond.entity == "kitchen light"
        assert cond.state is None
        assert cond.attribute is None
        assert cond.operator == "eq"

    def test_invalid_empty_entity(self):
        with pytest.raises(ValueError):  # pydantic ValidationError
            ActionCondition(entity="")

    def test_extra_fields_allowed(self):
        cond = ActionCondition(entity="kitchen light", custom_field="extra")
        assert cond.entity == "kitchen light"
        assert getattr(cond, "custom_field", None) == "extra"


class TestEvaluateCondition:
    """Tests for _evaluate_condition with mocked HA client."""

    @pytest.fixture()
    def ha_client(self):
        client = AsyncMock()
        client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
        return client

    @pytest.fixture()
    def entity_index(self):
        return MagicMock()

    @pytest.fixture()
    def entity_matcher(self):
        return MagicMock()

    @pytest.mark.asyncio
    async def test_eq_match_passes(self, ha_client, entity_index, entity_matcher):
        with patch(
            "app.agents.action_executor.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"}),
        ):
            cond = ActionCondition(entity="kitchen light", state="on", operator="eq")
            passed, observed, entity_id, error = await _evaluate_condition(
                cond, ha_client, entity_index, entity_matcher
            )
        assert passed is True
        assert observed == "on"
        assert entity_id == "light.kitchen"
        assert error is None

    @pytest.mark.asyncio
    async def test_eq_mismatch_fails(self, ha_client, entity_index, entity_matcher):
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
        with patch(
            "app.agents.action_executor.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"}),
        ):
            cond = ActionCondition(entity="kitchen light", state="on", operator="eq")
            passed, observed, entity_id, error = await _evaluate_condition(
                cond, ha_client, entity_index, entity_matcher
            )
        assert passed is False
        assert observed == "off"
        assert entity_id == "light.kitchen"
        assert error is None

    @pytest.mark.asyncio
    async def test_neq_mismatch_passes(self, ha_client, entity_index, entity_matcher):
        ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
        with patch(
            "app.agents.action_executor.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"}),
        ):
            cond = ActionCondition(entity="kitchen light", state="on", operator="neq")
            passed, observed, entity_id, error = await _evaluate_condition(
                cond, ha_client, entity_index, entity_matcher
            )
        assert passed is True
        assert observed == "off"
        assert entity_id == "light.kitchen"
        assert error is None

    @pytest.mark.asyncio
    async def test_attribute_check(self, ha_client, entity_index, entity_matcher):
        ha_client.get_state = AsyncMock(return_value={"state": "on", "attributes": {"brightness": 128}})
        with patch(
            "app.agents.action_executor.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"}),
        ):
            cond = ActionCondition(entity="kitchen light", attribute="brightness", state="128", operator="eq")
            passed, observed, entity_id, error = await _evaluate_condition(
                cond, ha_client, entity_index, entity_matcher
            )
        assert passed is True
        assert observed == "128"
        assert entity_id == "light.kitchen"
        assert error is None

    @pytest.mark.asyncio
    async def test_entity_resolution_failure_returns_error(self, ha_client, entity_index, entity_matcher):
        with patch(
            "app.agents.action_executor.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": None, "friendly_name": "kitchen light", "speech": "Not found"}),
        ):
            cond = ActionCondition(entity="kitchen light", state="on", operator="eq")
            passed, observed, entity_id, error = await _evaluate_condition(
                cond, ha_client, entity_index, entity_matcher
            )
        assert passed is False
        assert observed is None
        assert entity_id is None
        assert error is not None

    @pytest.mark.asyncio
    async def test_state_lookup_failure_returns_error(self, ha_client, entity_index, entity_matcher):
        ha_client.get_state = AsyncMock(side_effect=Exception("Connection lost"))
        with patch(
            "app.agents.action_executor.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"}),
        ):
            cond = ActionCondition(entity="kitchen light", state="on", operator="eq")
            passed, observed, entity_id, error = await _evaluate_condition(
                cond, ha_client, entity_index, entity_matcher
            )
        assert passed is False
        assert observed is None
        assert entity_id == "light.kitchen"
        assert error is not None

    @pytest.mark.asyncio
    async def test_missing_state_returns_error(self, ha_client, entity_index, entity_matcher):
        ha_client.get_state = AsyncMock(return_value={"state": None, "attributes": {}})
        with patch(
            "app.agents.action_executor.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"}),
        ):
            cond = ActionCondition(entity="kitchen light", state="on", operator="eq")
            passed, observed, entity_id, error = await _evaluate_condition(
                cond, ha_client, entity_index, entity_matcher
            )
        assert passed is False
        assert observed is None
        assert entity_id == "light.kitchen"
        assert error is not None

    @pytest.mark.asyncio
    async def test_case_insensitive_eq(self, ha_client, entity_index, entity_matcher):
        ha_client.get_state = AsyncMock(return_value={"state": "ON", "attributes": {}})
        with patch(
            "app.agents.action_executor.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"}),
        ):
            cond = ActionCondition(entity="kitchen light", state="on", operator="eq")
            passed, observed, entity_id, error = await _evaluate_condition(
                cond, ha_client, entity_index, entity_matcher
            )
        assert passed is True
        assert observed == "ON"
        assert entity_id == "light.kitchen"
        assert error is None

    @pytest.mark.asyncio
    async def test_condition_passes_agent_id_and_allowed_domains(self, ha_client, entity_index, entity_matcher):
        with patch(
            "app.agents.action_executor.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": "light.kitchen", "friendly_name": "Kitchen Light"}),
        ) as mock_resolve:
            cond = ActionCondition(entity="kitchen light", state="on", operator="eq")
            passed, _observed, _entity_id, _error = await _evaluate_condition(
                cond,
                ha_client,
                entity_index,
                entity_matcher,
                agent_id="light-agent",
                allowed_domains=frozenset({"light", "switch"}),
            )
        assert passed is True
        mock_resolve.assert_awaited_once()
        kwargs = mock_resolve.await_args.kwargs
        assert kwargs.get("agent_id") == "light-agent"
        assert kwargs.get("allowed_domains") == frozenset({"light", "switch"})

    @pytest.mark.asyncio
    async def test_hidden_condition_entity_fails_condition(self, ha_client, entity_index, entity_matcher):
        with patch(
            "app.agents.action_executor.resolve_entity_deterministic_first",
            new=AsyncMock(return_value={"entity_id": None, "friendly_name": "kitchen light", "speech": "Not visible"}),
        ):
            cond = ActionCondition(entity="kitchen light", state="on", operator="eq")
            passed, observed, entity_id, error = await _evaluate_condition(
                cond,
                ha_client,
                entity_index,
                entity_matcher,
                agent_id="restricted-light-agent",
                allowed_domains=frozenset({"light"}),
            )
        assert passed is False
        assert observed is None
        assert entity_id is None
        assert error is not None
