"""Tests for pipeline strategy override injection and DefaultDispatchStrategy."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mock litellm before importing any app modules that depend on it.
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

import app.llm.client  # noqa: E402,F401
from app.agents.dispatch_manager import _CANNED_TIMEOUT_SPEECH  # noqa: E402
from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.agents.pipeline_strategies import (  # noqa: E402
    DefaultCacheReplayStrategy,
    DefaultClassificationStrategy,
    DefaultDispatchStrategy,
    DefaultFinalizationStrategy,
)
from app.models.agent import AgentCard, AgentTask, TaskContext  # noqa: E402


def _make_task(text: str = "turn on light", conversation_id: str = "conv-1") -> AgentTask:
    return AgentTask(
        description=text,
        user_text=text,
        conversation_id=conversation_id,
        context=TaskContext(language="en"),
    )


# ---------------------------------------------------------------------------
# G1: PipelineDirector strategy override injection
# ---------------------------------------------------------------------------


class TestPipelineStrategyOverride:
    def test_apply_pipeline_strategies_sets_all_four_phases(self):
        """G1: apply_pipeline_strategies must inject overrides into PipelineDirector."""
        dispatcher = AsyncMock()
        registry = AsyncMock()
        cache_manager = MagicMock()
        cache_manager.process = AsyncMock(return_value=MagicMock(hit_type="miss"))
        cache_manager.apply_rewrite = AsyncMock()
        cache_manager.try_replay_action = AsyncMock(return_value=None)
        cache_manager.try_routing_skip = AsyncMock(return_value=None)
        cache_manager.store_action_async = AsyncMock()

        async def _store_routing_async(*args, **kwargs):
            return cache_manager.store_routing(*args, **kwargs)

        cache_manager.store_routing_async = _store_routing_async

        registry.list_agents = AsyncMock(
            return_value=[
                AgentCard(agent_id="light-agent", name="Light Agent", description="", skills=["light"]),
            ]
        )

        orch = OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=cache_manager)

        mock_cache_replay = MagicMock(spec=DefaultCacheReplayStrategy)
        mock_classification = MagicMock(spec=DefaultClassificationStrategy)
        mock_dispatch = MagicMock(spec=DefaultDispatchStrategy)
        mock_finalization = MagicMock(spec=DefaultFinalizationStrategy)

        strategies = {
            "cache_replay": mock_cache_replay,
            "classification": mock_classification,
            "dispatch": mock_dispatch,
            "finalization": mock_finalization,
        }

        orch.apply_pipeline_strategies(strategies)

        assert orch._pipeline_director._cache_replay_strategy is mock_cache_replay
        assert orch._pipeline_director._classification_strategy is mock_classification
        assert orch._pipeline_director._dispatch_strategy is mock_dispatch
        assert orch._pipeline_director._finalization_strategy is mock_finalization

    def test_apply_pipeline_strategies_warns_on_unknown_phase(self, caplog):
        """G1: Unknown strategy phases should be logged as a warning."""
        dispatcher = AsyncMock()
        registry = AsyncMock()
        cache_manager = MagicMock()
        cache_manager.process = AsyncMock(return_value=MagicMock(hit_type="miss"))
        cache_manager.apply_rewrite = AsyncMock()
        cache_manager.try_replay_action = AsyncMock(return_value=None)
        cache_manager.try_routing_skip = AsyncMock(return_value=None)
        cache_manager.store_action_async = AsyncMock()

        async def _store_routing_async(*args, **kwargs):
            return cache_manager.store_routing(*args, **kwargs)

        cache_manager.store_routing_async = _store_routing_async

        registry.list_agents = AsyncMock(
            return_value=[
                AgentCard(agent_id="light-agent", name="Light Agent", description="", skills=["light"]),
            ]
        )

        orch = OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=cache_manager)

        with caplog.at_level("WARNING"):
            orch.apply_pipeline_strategies({"unknown_phase": MagicMock()})

        assert "Unknown pipeline strategy phase" in caplog.text


# ---------------------------------------------------------------------------
# G12: DefaultDispatchStrategy multi-agent asyncio.gather, failure handling, merge
# ---------------------------------------------------------------------------


class TestDefaultDispatchStrategyMultiAgent:
    def _make_strategy(self, dispatch_side_effect=None):
        dispatch_manager = AsyncMock()
        if dispatch_side_effect is not None:
            dispatch_manager.dispatch_single = AsyncMock(side_effect=dispatch_side_effect)
        handle_sequential_send = AsyncMock(return_value=("send-agent", "Sent.", {"action_executed": None}))
        strategy = DefaultDispatchStrategy(
            dispatch_manager=dispatch_manager,
            handle_sequential_send=handle_sequential_send,
        )
        return strategy, dispatch_manager, handle_sequential_send

    @pytest.mark.asyncio
    async def test_multi_agent_gather_returns_all_results(self):
        """G12: Multi-agent dispatch must use asyncio.gather and return merged responses."""
        strategy, dm, _ = self._make_strategy(
            dispatch_side_effect=[
                ("light-agent", "Light is on.", {"action_executed": {"service": "light/turn_on"}}),
                ("music-agent", "Playing jazz.", {"action_executed": None}),
            ]
        )
        classifications = [
            ("light-agent", "turn on light", 0.95),
            ("music-agent", "play jazz", 0.90),
        ]
        task = _make_task("turn on light and play jazz")
        result = await strategy.execute(
            task, classifications, "turn on light and play jazz", "conv-1", [], None, "en", TaskContext()
        )

        assert result.target_agent == "light-agent"
        assert result.routed_to == "light-agent, music-agent"
        assert result.has_error is False
        assert len(result.agent_responses) == 2
        assert result.agent_responses[0] == ("light-agent", "Light is on.", True)
        assert result.agent_responses[1] == ("music-agent", "Playing jazz.", False)
        assert result.action_executed == {"service": "light/turn_on"}
        assert result.agent_voice_followup is False
        # asyncio.gather should have been invoked
        assert dm.dispatch_single.await_count == 2

    @pytest.mark.asyncio
    async def test_multi_agent_gather_with_exception_in_one(self):
        """G12: When one agent raises, it should be recorded in failed_agents."""
        strategy, _dm, _ = self._make_strategy(
            dispatch_side_effect=[
                RuntimeError("light-agent crashed"),
                ("music-agent", "Playing jazz.", {"action_executed": None}),
            ]
        )
        classifications = [
            ("light-agent", "turn on light", 0.95),
            ("music-agent", "play jazz", 0.90),
        ]
        task = _make_task("turn on light and play jazz")
        result = await strategy.execute(
            task, classifications, "turn on light and play jazz", "conv-1", [], None, "en", TaskContext()
        )

        assert result.has_error is True
        assert len(result.failed_agents) == 1
        assert result.failed_agents[0][0] == "light-agent"
        assert "light-agent crashed" in result.failed_agents[0][1]
        assert len(result.agent_responses) == 1
        assert result.agent_responses[0] == ("music-agent", "Playing jazz.", False)

    @pytest.mark.asyncio
    async def test_multi_agent_all_fail_fallback_to_general(self):
        """G12: When all agents fail, target should fallback to general-agent."""
        strategy, _dm, _ = self._make_strategy(
            dispatch_side_effect=[
                TimeoutError("light-agent timed out"),
                RuntimeError("music-agent crashed"),
            ]
        )
        classifications = [
            ("light-agent", "turn on light", 0.95),
            ("music-agent", "play jazz", 0.90),
        ]
        task = _make_task("turn on light and play jazz")
        result = await strategy.execute(
            task, classifications, "turn on light and play jazz", "conv-1", [], None, "en", TaskContext()
        )

        assert result.target_agent == "general-agent"
        assert result.routed_to == "general-agent"
        assert result.has_error is True
        assert len(result.agent_responses) == 0
        assert len(result.failed_agents) == 2

    @pytest.mark.asyncio
    async def test_multi_agent_canned_speech_treated_as_error(self):
        """G12: Canned timeout/error speech should be treated as failure."""
        strategy, _dm, _ = self._make_strategy(
            dispatch_side_effect=[
                ("light-agent", _CANNED_TIMEOUT_SPEECH, {"action_executed": None}),
                ("music-agent", "Playing jazz.", {"action_executed": None}),
            ]
        )
        classifications = [
            ("light-agent", "turn on light", 0.95),
            ("music-agent", "play jazz", 0.90),
        ]
        task = _make_task("turn on light and play jazz")
        result = await strategy.execute(
            task, classifications, "turn on light and play jazz", "conv-1", [], None, "en", TaskContext()
        )

        assert result.has_error is True
        assert len(result.failed_agents) == 1
        assert result.failed_agents[0][0] == "light-agent"
        assert result.failed_agents[0][1] == "canned_speech"
        assert len(result.agent_responses) == 1

    @pytest.mark.asyncio
    async def test_multi_agent_none_result_treated_as_timeout(self):
        """G12: None result from dispatch_single should be treated as timeout."""
        strategy, _dm, _ = self._make_strategy(
            dispatch_side_effect=[
                ("light-agent", "Light is on.", {"action_executed": None}),
                ("music-agent", "Playing jazz.", None),
            ]
        )
        classifications = [
            ("light-agent", "turn on light", 0.95),
            ("music-agent", "play jazz", 0.90),
        ]
        task = _make_task("turn on light and play jazz")
        result = await strategy.execute(
            task, classifications, "turn on light and play jazz", "conv-1", [], None, "en", TaskContext()
        )

        assert result.has_error is True
        assert len(result.failed_agents) == 1
        assert result.failed_agents[0][0] == "music-agent"
        assert result.failed_agents[0][1] == "timeout"
        assert len(result.agent_responses) == 1

    @pytest.mark.asyncio
    async def test_multi_agent_voice_followup_propagates(self):
        """G12: voice_followup from any agent should propagate to result."""
        strategy, _dm, _ = self._make_strategy(
            dispatch_side_effect=[
                ("light-agent", "Light is on.", {"action_executed": None, "voice_followup": True}),
                ("music-agent", "Playing jazz.", {"action_executed": None}),
            ]
        )
        classifications = [
            ("light-agent", "turn on light", 0.95),
            ("music-agent", "play jazz", 0.90),
        ]
        task = _make_task("turn on light and play jazz")
        result = await strategy.execute(
            task, classifications, "turn on light and play jazz", "conv-1", [], None, "en", TaskContext()
        )

        assert result.agent_voice_followup is True
