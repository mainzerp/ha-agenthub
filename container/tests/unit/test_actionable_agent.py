"""Tests for ActionableAgent base class: state injection, entity resolution, and subclasses."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

from tests.helpers import make_dispatch_task  # noqa: E402

from app.agents.actionable import (  # noqa: E402
    ActionableAgent,
    AutomationAgent,
    ClimateAgent,
    CoverAgent,
    LightAgent,
    MediaAgent,
    MusicAgent,
    SceneAgent,
    SecurityAgent,
    VacuumAgent,
)
from app.agents.lists import ListsAgent  # noqa: E402
from app.agents.timer import TimerAgent  # noqa: E402
from app.models.agent import AgentCard, AgentErrorCode, TaskContext, TaskResult  # noqa: E402

# ---------------------------------------------------------------------------
# _resolve_relevant_entities
# ---------------------------------------------------------------------------


class DummyEntry:
    def __init__(self, entity_id, friendly_name, state=None):
        self.entity_id = entity_id
        self.friendly_name = friendly_name
        self.state = state


class TestResolveRelevantEntities:
    @pytest.mark.asyncio
    async def test_returns_correct_entities(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")
        task.verbatim_terms = ["kitchen light"]

        with patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={"entity_id": "light.kitchen_ceiling", "friendly_name": "Kitchen Ceiling"},
        ) as mock_resolve:
            result = await agent._resolve_relevant_entities(task)

        assert len(result) == 1
        assert result[0] == ("light.kitchen_ceiling", "Kitchen Ceiling")
        mock_resolve.assert_awaited_once()
        call_kwargs = mock_resolve.call_args.kwargs
        assert call_kwargs.get("allowed_domains") == frozenset({"light", "switch", "sensor"})
        assert mock_resolve.call_args.args[0] == "kitchen light"

    @pytest.mark.asyncio
    async def test_limits_to_three(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on light one, light two, light three, light four")
        task.verbatim_terms = ["light one", "light two", "light three", "light four"]

        async def _fake_resolve(query, *args, **kwargs):
            return {"entity_id": f"light.{query.replace(' ', '_')}", "friendly_name": query}

        with patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            side_effect=_fake_resolve,
        ):
            result = await agent._resolve_relevant_entities(task)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_deduplicates_by_entity_id(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light and kitchen light")
        task.verbatim_terms = ["kitchen light", "kitchen light"]

        with patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={"entity_id": "light.kitchen_ceiling", "friendly_name": "Kitchen Ceiling"},
        ):
            result = await agent._resolve_relevant_entities(task)

        assert len(result) == 1
        assert result[0] == ("light.kitchen_ceiling", "Kitchen Ceiling")

    @pytest.mark.asyncio
    async def test_graceful_when_resolution_fails(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the unknown thing")
        task.verbatim_terms = ["unknown thing"]

        with patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            result = await agent._resolve_relevant_entities(task)

        assert result == []

    @pytest.mark.asyncio
    async def test_skips_unresolved_mentions(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")
        task.verbatim_terms = ["kitchen light"]

        with patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={"entity_id": None, "friendly_name": None},
        ):
            result = await agent._resolve_relevant_entities(task)

        assert result == []

    @pytest.mark.asyncio
    async def test_reuses_visible_entries_across_mentions(self):
        """Cross-mention visible_entries caching: only the first call lists the index."""
        agent = LightAgent()
        task = make_dispatch_task(
            description="turn on the kitchen light and the bedroom light and the living room light"
        )
        task.verbatim_terms = ["kitchen light", "bedroom light", "living room light"]

        call_count = 0

        async def _fake_resolve(query, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                assert kwargs.get("visible_entries") is None
            else:
                assert kwargs.get("visible_entries") is not None
            return {
                "entity_id": f"light.{query.replace(' ', '_')}",
                "friendly_name": query,
                "_visible_entries": ["mock_entry"],
            }

        with patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            side_effect=_fake_resolve,
        ):
            result = await agent._resolve_relevant_entities(task)

        assert len(result) == 3
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_conditional_clauses_resolve_both_target_and_condition_entity(self):
        agent = LightAgent()
        task = make_dispatch_task(description="if outdoor brightness is dark, turn on the kitchen light")
        task.verbatim_terms = ["outdoor brightness", "kitchen light"]

        call_count = 0

        async def _fake_resolve(query, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if "outdoor" in query.lower() or "brightness" in query.lower():
                return {"entity_id": "sensor.outdoor_brightness", "friendly_name": "Outdoor Brightness"}
            return {"entity_id": "light.kitchen_ceiling", "friendly_name": "Kitchen Ceiling"}

        with patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            side_effect=_fake_resolve,
        ):
            result = await agent._resolve_relevant_entities(task)

        entity_ids = [r[0] for r in result]
        assert "sensor.outdoor_brightness" in entity_ids
        assert "light.kitchen_ceiling" in entity_ids

    @pytest.mark.asyncio
    async def test_uses_description_fallback_when_verbatim_terms_empty(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")
        task.verbatim_terms = []

        with patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={"entity_id": "light.kitchen_ceiling", "friendly_name": "Kitchen Ceiling"},
        ) as mock_resolve:
            result = await agent._resolve_relevant_entities(task)

        assert len(result) == 1
        mock_resolve.assert_awaited_once()
        assert mock_resolve.call_args.args[0] == "turn on the kitchen light"


# ---------------------------------------------------------------------------
# _build_relevant_entity_state_context
# ---------------------------------------------------------------------------


class TestBuildRelevantEntityStateContext:
    @pytest.mark.asyncio
    async def test_formats_from_entity_index(self):
        agent = LightAgent()
        index = AsyncMock()
        index.get_by_id_async = AsyncMock(
            return_value=DummyEntry("light.kitchen_ceiling", "Kitchen Ceiling", state="on")
        )
        agent._entity_index = index
        agent._ha_client = None

        result = await agent._build_relevant_entity_state_context([("light.kitchen_ceiling", "Kitchen Ceiling")])

        assert result is not None
        assert result == "Kitchen Ceiling (light.kitchen_ceiling): on"

    @pytest.mark.asyncio
    async def test_fallback_to_ha_client_when_index_lacks_state(self):
        agent = LightAgent()
        index = AsyncMock()
        index.get_by_id_async = AsyncMock(
            return_value=DummyEntry("light.kitchen_ceiling", "Kitchen Ceiling", state=None)
        )
        agent._entity_index = index

        ha_client = AsyncMock()
        ha_client.get_state = AsyncMock(return_value={"entity_id": "light.kitchen_ceiling", "state": "off"})
        agent._ha_client = ha_client

        result = await agent._build_relevant_entity_state_context([("light.kitchen_ceiling", "Kitchen Ceiling")])

        assert result is not None
        assert result == "Kitchen Ceiling (light.kitchen_ceiling): off"
        ha_client.get_state.assert_awaited_once_with("light.kitchen_ceiling")

    @pytest.mark.asyncio
    async def test_fallback_to_ha_client_when_index_is_none(self):
        agent = LightAgent()
        agent._entity_index = None

        ha_client = AsyncMock()
        ha_client.get_state = AsyncMock(return_value={"entity_id": "light.kitchen_ceiling", "state": "on"})
        agent._ha_client = ha_client

        result = await agent._build_relevant_entity_state_context([("light.kitchen_ceiling", "Kitchen Ceiling")])

        assert result is not None
        assert result == "Kitchen Ceiling (light.kitchen_ceiling): on"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_states_available(self):
        agent = LightAgent()
        index = AsyncMock()
        index.get_by_id_async = AsyncMock(return_value=None)
        agent._entity_index = index

        ha_client = AsyncMock()
        ha_client.get_state = AsyncMock(side_effect=Exception("no state"))
        agent._ha_client = ha_client

        result = await agent._build_relevant_entity_state_context([("light.unknown", "Unknown Light")])

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_input(self):
        agent = LightAgent()
        result = await agent._build_relevant_entity_state_context([])
        assert result is None

    @pytest.mark.asyncio
    async def test_multiple_entities(self):
        agent = LightAgent()
        index = AsyncMock()

        async def _get_by_id(entity_id):
            if entity_id == "light.kitchen_ceiling":
                return DummyEntry(entity_id, "Kitchen Ceiling", state="on")
            return DummyEntry(entity_id, "Living Room Lamp", state="off")

        index.get_by_id_async = AsyncMock(side_effect=_get_by_id)
        agent._entity_index = index
        agent._ha_client = None

        result = await agent._build_relevant_entity_state_context(
            [
                ("light.kitchen_ceiling", "Kitchen Ceiling"),
                ("light.living_room_lamp", "Living Room Lamp"),
            ]
        )

        assert result is not None
        assert result == "Kitchen Ceiling (light.kitchen_ceiling): on, Living Room Lamp (light.living_room_lamp): off"


# ---------------------------------------------------------------------------
# Graceful degradation when entity_index is None
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_resolve_relevant_entities_with_none_index_and_matcher(self):
        agent = LightAgent(entity_index=None, entity_matcher=None)
        task = make_dispatch_task(description="turn on the kitchen light")

        with patch(
            "app.agents.actionable.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={"entity_id": None, "friendly_name": None},
        ):
            result = await agent._resolve_relevant_entities(task)

        assert result == []

    @pytest.mark.asyncio
    async def test_build_context_with_none_index_and_ha_client(self):
        agent = LightAgent(entity_index=None, entity_matcher=None)
        agent._ha_client = None

        result = await agent._build_relevant_entity_state_context([("light.kitchen_ceiling", "Kitchen Ceiling")])

        assert result is None


# ---------------------------------------------------------------------------
# _allowed_domains on all actionable subclasses
# ---------------------------------------------------------------------------


_ACTIONABLE_AGENT_CLASSES = [
    LightAgent,
    ClimateAgent,
    SecurityAgent,
    CoverAgent,
    VacuumAgent,
    MediaAgent,
    SceneAgent,
    AutomationAgent,
    ListsAgent,
    MusicAgent,
    TimerAgent,
]


class TestAllowedDomains:
    @pytest.mark.parametrize("agent_cls", _ACTIONABLE_AGENT_CLASSES)
    def test_all_actionable_subclasses_define_allowed_domains(self, agent_cls):
        """Every actionable agent subclass must explicitly set _allowed_domains."""
        assert hasattr(agent_cls, "_allowed_domains")
        assert agent_cls._allowed_domains is not None
        assert isinstance(agent_cls._allowed_domains, frozenset)

    def test_base_class_allows_none(self):
        assert ActionableAgent._allowed_domains is None

    @pytest.mark.parametrize(
        "agent_cls,expected_domains",
        [
            (LightAgent, {"light", "switch", "sensor"}),
            (ClimateAgent, {"climate", "weather", "sensor"}),
            (SecurityAgent, {"lock", "binary_sensor", "alarm_control_panel"}),
            (CoverAgent, {"cover"}),
            (VacuumAgent, {"vacuum"}),
            (MediaAgent, {"media_player"}),
            (SceneAgent, {"scene"}),
            (AutomationAgent, {"automation", "script"}),
            (ListsAgent, {"todo", "shopping_list"}),
            (MusicAgent, {"media_player"}),
            (TimerAgent, {"timer", "input_datetime", "input_boolean"}),
        ],
    )
    def test_expected_domain_values(self, agent_cls, expected_domains):
        assert agent_cls._allowed_domains == frozenset(expected_domains)


# ---------------------------------------------------------------------------
# Integration: _handle_task_inner injects state context into prompt
# ---------------------------------------------------------------------------


class TestHandleTaskInnerInjection:
    @pytest.mark.asyncio
    async def test_prompt_includes_entity_states(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch("app.agents.actionable.resolve_entity_deterministic_first", new_callable=AsyncMock) as mock_resolve,
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent, "_call_llm", new_callable=AsyncMock, return_value='{"action": "turn_on", "entity": "kitchen"}'
            ) as mock_llm,
        ):
            mock_resolve.return_value = {"entity_id": "light.kitchen_ceiling", "friendly_name": "Kitchen Ceiling"}

            index = AsyncMock()
            index.get_by_id_async = AsyncMock(
                return_value=DummyEntry("light.kitchen_ceiling", "Kitchen Ceiling", state="off")
            )
            agent._entity_index = index
            agent._ha_client = None
            agent._entity_matcher = None

            await agent.handle_task(task)

        assert mock_llm.await_count == 1
        system_msg = mock_llm.call_args.args[0][0]["content"]
        assert "Context: Kitchen Ceiling (light.kitchen_ceiling): off" in system_msg
        assert "Output rules:" in system_msg
        assert "Conditional actions:" in system_msg
        # Output rules must appear BEFORE the context line
        assert system_msg.index("Output rules:") < system_msg.index("Context:")

    @pytest.mark.asyncio
    async def test_prompt_includes_state_aware_block_even_without_entities(self):
        agent = LightAgent()
        task = make_dispatch_task(description="hello")

        with (
            patch(
                "app.agents.actionable.resolve_entity_deterministic_first",
                new_callable=AsyncMock,
                return_value={"entity_id": None, "friendly_name": None},
            ),
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(agent, "_call_llm", new_callable=AsyncMock, return_value="Hello there!") as mock_llm,
        ):
            agent._entity_index = None
            agent._ha_client = None
            agent._entity_matcher = None

            await agent.handle_task(task)

        system_msg = mock_llm.call_args.args[0][0]["content"]
        assert "Output rules:" in system_msg
        assert "Conditional actions:" in system_msg

    @pytest.mark.asyncio
    async def test_injection_gracefully_degrades_on_exception(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(
                agent,
                "_resolve_relevant_entities",
                new_callable=AsyncMock,
                side_effect=RuntimeError("injection failed"),
            ),
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(agent, "_call_llm", new_callable=AsyncMock, return_value="Hello there!") as mock_llm,
        ):
            agent._entity_index = None
            agent._ha_client = None
            agent._entity_matcher = None

            await agent.handle_task(task)

        system_msg = mock_llm.call_args.args[0][0]["content"]
        assert "Output rules:" in system_msg

    @pytest.mark.asyncio
    async def test_query_candidate_context_injected_into_system_prompt(self):
        agent = LightAgent()
        task = make_dispatch_task(description="is the kitchen light on?")
        task.verbatim_terms = ["kitchen light"]

        matcher = AsyncMock()
        match_result = MagicMock()
        match_result.entity_id = "light.kitchen_ceiling"
        match_result.friendly_name = "Kitchen Ceiling"
        match_result.score = 0.95
        matcher.match = AsyncMock(return_value=[match_result])

        index = AsyncMock()
        index.get_by_id_async = AsyncMock(return_value=None)

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent,
                "_call_llm",
                new_callable=AsyncMock,
                return_value='{"action": "query_light_state", "entity_id": "light.kitchen_ceiling"}',
            ) as mock_llm,
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
            patch.object(agent, "_do_execute", new_callable=AsyncMock, return_value={"speech": "OK", "success": True}),
        ):
            agent._entity_index = index
            agent._entity_matcher = matcher
            agent._ha_client = AsyncMock()

            await agent.handle_task(task)

        system_msg = mock_llm.call_args.args[0][0]["content"]
        assert "Candidate entities for 'kitchen light'" in system_msg
        assert "Kitchen Ceiling (light.kitchen_ceiling)" in system_msg
        matcher.match.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_query_candidate_context_skipped_when_no_candidates(self):
        agent = LightAgent()
        task = make_dispatch_task(description="is the kitchen light on?")
        task.verbatim_terms = ["kitchen light"]

        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])

        index = AsyncMock()

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent,
                "_call_llm",
                new_callable=AsyncMock,
                return_value='{"action": "query_light_state", "entity_id": "light.kitchen_ceiling"}',
            ) as mock_llm,
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
            patch.object(agent, "_do_execute", new_callable=AsyncMock, return_value={"speech": "OK", "success": True}),
        ):
            agent._entity_index = index
            agent._entity_matcher = matcher
            agent._ha_client = AsyncMock()

            await agent.handle_task(task)

        system_msg = mock_llm.call_args.args[0][0]["content"]
        assert "Candidate entities for" not in system_msg
        matcher.match.assert_awaited_once()


# ---------------------------------------------------------------------------
# handle_task execution paths
# ---------------------------------------------------------------------------


class TestHandleTaskExecution:
    @pytest.mark.asyncio
    async def test_success_path_returns_action_executed(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent,
                "_call_llm",
                new_callable=AsyncMock,
                return_value='{"action": "turn_on", "entity": "kitchen light"}',
            ),
            patch.object(
                agent,
                "_do_execute",
                new_callable=AsyncMock,
                return_value={
                    "speech": "Kitchen light is on",
                    "entity_id": "light.kitchen_ceiling",
                    "success": True,
                    "new_state": "on",
                },
            ),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            result = await agent.handle_task(task)

        assert result.speech == "Kitchen light is on"
        assert result.action_executed is not None
        assert result.action_executed.action == "turn_on"
        assert result.action_executed.entity_id == "light.kitchen_ceiling"
        assert result.action_executed.success is True
        assert result.error is None

    @pytest.mark.asyncio
    async def test_success_path_service_data_from_parameters(self):
        agent = LightAgent()
        task = make_dispatch_task(description="set brightness")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent,
                "_call_llm",
                new_callable=AsyncMock,
                return_value='{"action": "turn_on", "entity": "kitchen light", "parameters": {"brightness": 128}}',
            ),
            patch.object(
                agent,
                "_do_execute",
                new_callable=AsyncMock,
                return_value={"speech": "OK", "entity_id": "light.a", "success": True},
            ),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            result = await agent.handle_task(task)

        assert result.action_executed is not None
        assert result.action_executed.service_data == {"brightness": 128}
        assert result.error is None

    @pytest.mark.asyncio
    async def test_directive_from_executor(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent,
                "_call_llm",
                new_callable=AsyncMock,
                return_value='{"action": "turn_on", "entity": "kitchen light"}',
            ),
            patch.object(
                agent,
                "_do_execute",
                new_callable=AsyncMock,
                return_value={
                    "speech": "Delegating",
                    "directive": "timer:create",
                    "reason": "native timer",
                },
            ),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            result = await agent.handle_task(task)

        assert result.directive == "timer:create"
        assert result.reason == "native timer"
        assert result.action_executed is None
        assert result.error is None

    @pytest.mark.asyncio
    async def test_executor_returns_explicit_error(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent,
                "_call_llm",
                new_callable=AsyncMock,
                return_value='{"action": "turn_on", "entity": "kitchen light"}',
            ),
            patch.object(
                agent,
                "_do_execute",
                new_callable=AsyncMock,
                return_value={
                    "speech": "Entity not found",
                    "error": {"code": "entity_not_found", "message": "not found"},
                },
            ),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            result = await agent.handle_task(task)

        assert result.error is not None
        assert result.error.code == AgentErrorCode.ENTITY_NOT_FOUND
        assert result.error.recoverable is True

    @pytest.mark.asyncio
    async def test_executor_raises_action_failed(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent,
                "_call_llm",
                new_callable=AsyncMock,
                return_value='{"action": "turn_on", "entity": "kitchen light"}',
            ),
            patch.object(agent, "_do_execute", new_callable=AsyncMock, side_effect=RuntimeError("HA timeout")),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            result = await agent.handle_task(task)

        assert result.error is not None
        assert result.error.code == AgentErrorCode.ACTION_FAILED
        assert "kitchen light" in result.speech.lower()
        assert result.action_executed is None

    @pytest.mark.asyncio
    async def test_llm_call_raises_exception(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(agent, "_call_llm", new_callable=AsyncMock, side_effect=RuntimeError("LLM down")),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            with patch.object(agent, "_do_execute", new_callable=AsyncMock) as mock_exec:
                result = await agent.handle_task(task)

        assert result.error is not None
        assert result.error.code == AgentErrorCode.LLM_ERROR
        assert result.speech
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_returns_empty_response(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(agent, "_call_llm", new_callable=AsyncMock, return_value=""),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            with patch.object(agent, "_do_execute", new_callable=AsyncMock) as mock_exec:
                result = await agent.handle_task(task)

        assert result.error is not None
        assert result.error.code == AgentErrorCode.LLM_EMPTY_RESPONSE
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_cancelled_error_propagates(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(agent, "_call_llm", new_callable=AsyncMock, side_effect=asyncio.CancelledError()),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            with pytest.raises(asyncio.CancelledError):
                await agent.handle_task(task)

    @pytest.mark.asyncio
    async def test_parse_miss_returns_fallback_speech(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(agent, "_call_llm", new_callable=AsyncMock, return_value="Hello! How can I help?"),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            result = await agent.handle_task(task)

        assert "Hello" in result.speech
        assert result.error is None
        assert result.action_executed is None

    @pytest.mark.asyncio
    async def test_no_ha_client_returns_ha_unavailable(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent,
                "_call_llm",
                new_callable=AsyncMock,
                return_value='{"action": "turn_on", "entity": "kitchen light"}',
            ),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = None
            agent._entity_index = None
            agent._entity_matcher = None

            with patch.object(agent, "_do_execute", new_callable=AsyncMock) as mock_exec:
                result = await agent.handle_task(task)

        assert result.error is not None
        assert result.error.code == AgentErrorCode.HA_UNAVAILABLE
        assert result.error.recoverable is False
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_outer_handle_task_exception_returns_internal(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(
                agent, "_load_prompt_async", new_callable=AsyncMock, side_effect=TypeError("injection failed")
            ),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            result = await agent.handle_task(task)

        assert result.error is not None
        assert result.error.code == AgentErrorCode.INTERNAL
        assert "something went wrong" in result.speech.lower()

    @pytest.mark.asyncio
    async def test_entity_not_found_clarification_calls_llm(self):
        agent = LightAgent()
        task = make_dispatch_task(description="turn on the kitchen light")

        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent,
                "_call_llm",
                new_callable=AsyncMock,
                return_value='{"action": "turn_on", "entity": "kitchen light"}',
            ),
            patch.object(
                agent,
                "_do_execute",
                new_callable=AsyncMock,
                return_value={"success": False, "entity_id": None, "error": None, "speech": "not found"},
            ),
            patch.object(
                agent,
                "_generate_not_found_speech",
                new_callable=AsyncMock,
                return_value="Which light?",
            ),
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            result = await agent.handle_task(task)

        assert result.speech == "Which light?"
        assert result.action_executed is not None
        assert result.action_executed.success is False
        assert result.error is None

    @pytest.mark.asyncio
    async def test_ambiguous_resolution_not_overwritten_by_llm_clarification(self):
        """LOW-15: when the resolver already produced a targeted disambiguation
        speech (resolution_path ending in '_ambiguous'), the generic LLM
        not-found clarification must NOT overwrite it."""
        agent = LightAgent()
        task = make_dispatch_task(description="turn on Keller")

        ambiguity_speech = "Multiple entities match 'Keller'. Please be more specific."
        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(
                agent,
                "_call_llm",
                new_callable=AsyncMock,
                return_value='{"action": "turn_on", "entity": "Keller"}',
            ),
            patch.object(
                agent,
                "_do_execute",
                new_callable=AsyncMock,
                return_value={
                    "success": False,
                    "entity_id": None,
                    "error": None,
                    "speech": ambiguity_speech,
                    "metadata": {"resolution_path": "exact_friendly_name_ambiguous"},
                },
            ),
            patch.object(
                agent,
                "_generate_not_found_speech",
                new_callable=AsyncMock,
                return_value="Which light did you mean?",
            ) as mock_not_found_speech,
            patch.object(agent, "_resolve_relevant_entities", new_callable=AsyncMock, return_value=[]),
        ):
            agent._ha_client = AsyncMock()
            agent._entity_index = None
            agent._entity_matcher = None

            result = await agent.handle_task(task)

        mock_not_found_speech.assert_not_awaited()
        assert result.speech == ambiguity_speech
        assert result.action_executed is not None
        assert result.action_executed.success is False
        assert result.error is None


# ---------------------------------------------------------------------------
# CORE-H3: per-request state isolation on singleton agents
# ---------------------------------------------------------------------------


class TestConcurrentHandleTaskContextIsolation:
    """Regression: concurrent handle_task calls on one agent instance must
    each see their own task/context (ContextVar-backed per-request state)."""

    @pytest.mark.asyncio
    async def test_concurrent_handle_task_calls_see_own_context(self):
        class _ProbeAgent(ActionableAgent):
            def __init__(self) -> None:
                super().__init__()
                self.seen: dict[str, tuple[object, object]] = {}

            @property
            def agent_card(self):
                return AgentCard(agent_id="probe-agent", name="Probe", description="", skills=[])

            async def _handle_task_inner(self, task):
                # Yield control so the sibling request interleaves between
                # the ContextVar set in handle_task and this read.
                await asyncio.sleep(0)
                self.seen[task.description] = (self._get_current_task_context(), self._get_current_task())
                return TaskResult(speech="ok")

        agent = _ProbeAgent()
        ctx_a = TaskContext(area_id="area-a", language="en")
        ctx_b = TaskContext(area_id="area-b", language="de")
        task_a = make_dispatch_task(description="req-a", context=ctx_a)
        task_b = make_dispatch_task(description="req-b", context=ctx_b)

        await asyncio.gather(agent.handle_task(task_a), agent.handle_task(task_b))

        seen_ctx_a, seen_task_a = agent.seen["req-a"]
        seen_ctx_b, seen_task_b = agent.seen["req-b"]
        assert seen_ctx_a is ctx_a
        assert seen_task_a is task_a
        assert seen_ctx_b is ctx_b
        assert seen_task_b is task_b

    @pytest.mark.asyncio
    async def test_context_reset_after_handle_task(self):
        class _ProbeAgent(ActionableAgent):
            @property
            def agent_card(self):
                return AgentCard(agent_id="probe-agent", name="Probe", description="", skills=[])

            async def _handle_task_inner(self, task):
                return TaskResult(speech="ok")

        agent = _ProbeAgent()
        await agent.handle_task(make_dispatch_task(description="req", context=TaskContext(language="en")))

        assert agent._get_current_task_context() is None
        assert agent._get_current_task() is None
