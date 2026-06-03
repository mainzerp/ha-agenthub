"""Scenario-level integration tests for the orchestrator pipeline.

Complements the YAML-driven real-scenario suite (test_real_scenarios.py)
with focused tests for scenario types that are hard to express in YAML
or need additional assertion coverage.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

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

from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.models.agent import AgentCard  # noqa: E402
from tests.helpers import make_agent_task  # noqa: E402


class TestScenarioTypes:
    """Focused scenario tests for the 4 high-priority orchestrator flows."""

    def _make_orchestrator(self, dispatch_result=None):
        dispatcher = AsyncMock()
        registry = AsyncMock()
        cache_manager = MagicMock()
        cache_manager.process = AsyncMock(return_value=MagicMock(hit_type="miss", agent_id=None, similarity=0.5))
        cache_manager.apply_rewrite = AsyncMock()
        cache_manager.try_replay_action = AsyncMock(return_value=None)
        cache_manager.try_routing_skip = AsyncMock(return_value=None)
        cache_manager.store_response = MagicMock()

        async def _store_routing_async(*args, **kwargs):
            return cache_manager.store_routing(*args, **kwargs)

        async def _store_action_async(entry):
            return cache_manager.store_response(entry)

        cache_manager.store_routing_async = _store_routing_async
        cache_manager.store_action_async = _store_action_async

        response_mock = dispatch_result or {"speech": "Done!"}
        dispatcher.dispatch = AsyncMock(return_value=response_mock)

        registry.list_agents = AsyncMock(
            return_value=[
                AgentCard(agent_id="light-agent", name="Light Agent", description="", skills=["light"]),
                AgentCard(agent_id="general-agent", name="General Agent", description="", skills=["general"]),
                AgentCard(agent_id="send-agent", name="Send Agent", description="", skills=["send"]),
                AgentCard(agent_id="custom-1", name="Custom Agent", description="", skills=["custom"]),
            ]
        )

        orchestrator = OrchestratorAgent(
            dispatcher=dispatcher,
            registry=registry,
            cache_manager=cache_manager,
        )
        return orchestrator, dispatcher, registry, cache_manager

    # ------------------------------------------------------------------
    # Scenario: Sequential send
    # ------------------------------------------------------------------

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_sequential_send_content_then_deliver(self, mock_complete, mock_track, mock_settings):
        """Sequential send: content agent produces output, then send-agent delivers it."""
        mock_settings.get_value = AsyncMock(
            side_effect=lambda k, d=None: {
                "language": "auto",
                "personality.prompt": "",
                "rewrite.model": "groq/llama-3.1-8b-instant",
                "rewrite.temperature": "0.3",
            }.get(k, d)
        )
        orch, _dispatcher, *_ = self._make_orchestrator()
        mock_complete.side_effect = [
            "general-agent (90%): compose reminder\nsend-agent (95%): Laura Phone",
            "Sent to Laura Phone.",
        ]
        orch._dispatch_single = AsyncMock(
            side_effect=[
                (
                    "general-agent",
                    "Reminder: dental appointment tomorrow at 9am.",
                    {"speech": "Reminder: dental appointment tomorrow at 9am."},
                ),
                ("send-agent", "Sent to Laura Phone.", {"speech": "Sent to Laura Phone."}),
            ]
        )

        task = make_agent_task(
            description="send Laura a reminder about the dental appointment",
            user_text="send Laura a reminder about the dental appointment",
        )
        task.conversation_id = "conv-seq"
        result = await orch.handle_task(task)

        assert "general-agent" in result["routed_to"]
        assert "send-agent" in result["routed_to"]
        calls = orch._dispatch_single.call_args_list
        assert len(calls) == 2
        # First call is content generation
        assert calls[0][0][0] == "general-agent"
        # Second call is send delivery
        assert calls[1][0][0] == "send-agent"

    # ------------------------------------------------------------------
    # Scenario: Custom agent routing
    # ------------------------------------------------------------------

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_custom_agent_routing_scenario(self, mock_complete, mock_track, mock_settings):
        """Custom agent routing: orchestrator classifies and routes to a custom agent."""
        mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: "auto" if k == "language" else d)
        orch, dispatcher, *_ = self._make_orchestrator()
        mock_complete.return_value = "custom-1 (95%): Do custom thing"

        task = make_agent_task(description="do a custom thing", user_text="do a custom thing")
        task.conversation_id = "conv-custom"
        result = await orch.handle_task(task)

        assert result["routed_to"] == "custom-1"
        dispatcher.dispatch.assert_awaited_once()
        # Verify the dispatched task went to the custom agent
        # dispatcher.dispatch receives a JsonRpcRequest; agent_id is in params["agent_id"]
        rpc_request = dispatcher.dispatch.call_args[0][0]
        assert rpc_request.params["agent_id"] == "custom-1"

    # ------------------------------------------------------------------
    # Scenario: Cache hit short-circuit
    # ------------------------------------------------------------------

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    async def test_cache_hit_short_circuit_bypasses_llm(self, mock_track, mock_settings):
        """Cache hit short-circuit: routing cache bypasses LLM classification."""
        from app.cache.cache_manager import RoutingSkipOutcome

        mock_settings.get_value = AsyncMock(return_value="")
        orch, dispatcher, _, cache_manager = self._make_orchestrator()
        cache_manager.try_replay_action = AsyncMock(return_value=None)
        cache_manager.try_routing_skip = AsyncMock(
            return_value=RoutingSkipOutcome(
                kind="routing_hit",
                entry_id="routing-1",
                agent_id="light-agent",
                condensed_task="Turn on light",
                similarity=0.96,
            )
        )
        dispatcher.dispatch.return_value = {
            "speech": "Light is on!",
            "action_executed": {"success": True, "entity_id": "light.kitchen", "action": "turn_on"},
        }

        task = make_agent_task(description="turn on the light", user_text="turn on the light")
        task.conversation_id = "conv-cache"
        result = await orch.handle_task(task)

        assert result["routed_to"] == "light-agent"
        assert result["speech"] == "Light is on!"
        # LLM should NOT have been called because routing cache hit
        # (mock_complete is not patched here, so any call would fail)

    # ------------------------------------------------------------------
    # Scenario: Agent timeout with fallback
    # ------------------------------------------------------------------

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.agents.dispatch_manager.track_agent_timeout", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_agent_timeout_with_fallback_scenario(self, mock_complete, mock_timeout, mock_track, mock_settings):
        """Agent timeout with fallback: agent times out, general-agent fallback succeeds."""
        mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: "auto" if k == "language" else d)
        orch, dispatcher, *_ = self._make_orchestrator()
        mock_complete.return_value = "light-agent (95%): turn on kitchen light"
        orch._default_timeout = 0.001  # very short timeout

        fallback_response = {"speech": "Fallback response."}
        dispatcher.dispatch = AsyncMock(side_effect=[TimeoutError(), fallback_response])

        task = make_agent_task(description="turn on kitchen light", user_text="turn on kitchen light")
        task.conversation_id = "conv-timeout"
        result = await orch.handle_task(task)

        assert result["speech"] == "Fallback response."
        # Two dispatches: first light-agent times out, then general-agent fallback
        assert dispatcher.dispatch.await_count == 2


class TestScenarioLoader:
    """Tests for the scenario loader infrastructure."""

    def test_loader_finds_all_orchestrator_scenarios(self):
        from tests.scenarios.loader import list_scenario_files

        paths = list_scenario_files()
        orchestrator_paths = [p for p in paths if "orchestrator" in p.as_posix()]
        ids = {p.stem for p in orchestrator_paths}
        assert "sequential_send_general_then_send" in ids
        assert "cancel_interaction" in ids
        assert "routing_cache_hit_skip_classify" in ids
        assert "cache_hit_response_replay" in ids
        assert "dispatch_timeout_light" in ids

    def test_loader_parses_scenario_fields(self):
        from tests.scenarios.loader import load_scenario, scenario_root

        path = scenario_root() / "orchestrator" / "cancel_interaction.yaml"
        scenario = load_scenario(path)
        assert scenario.id == "orchestrator.cancel_interaction"
        assert scenario.request_text == "never mind"
        assert scenario.expected.routed_agent == "cancel-interaction"
        assert scenario.expected.service_calls == []

    def test_loader_parses_follow_up_turns(self):
        from tests.scenarios.loader import load_scenario, scenario_root

        path = scenario_root() / "orchestrator" / "sequential_send_general_then_send.yaml"
        scenario = load_scenario(path)
        assert scenario.llm.classify is not None
        assert "general-agent" in scenario.llm.agents
        assert "send-agent" in scenario.llm.agents
