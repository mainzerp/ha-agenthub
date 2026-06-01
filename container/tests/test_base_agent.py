"""Tests for app.agents -- all specialized agents, orchestrator, rewrite, and custom loader."""

from __future__ import annotations

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

import app.llm.client  # noqa: E402,F401 -- force module load for patch targets
from app.agents.actionable import (  # noqa: E402
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
from app.agents.base import BaseAgent, _prompt_cache, preload_prompt_cache  # noqa: E402
from app.agents.general import GeneralAgent  # noqa: E402
from app.agents.lists import ListsAgent  # noqa: E402
from app.agents.rewrite import RewriteAgent  # noqa: E402
from app.agents.timer import TimerAgent  # noqa: E402
from app.models.agent import (  # noqa: E402
    AgentCard,
    AgentTask,
    TaskContext,
)
from tests.helpers import make_agent_task  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    description: str = "turn on kitchen light", user_text: str | None = None, context: TaskContext | None = None
) -> AgentTask:
    return make_agent_task(
        description=description,
        user_text=user_text or description,
        context=context,
    )


# ---------------------------------------------------------------------------
# BaseAgent abstract contract
# ---------------------------------------------------------------------------


class TestBaseAgent:
    """Tests for the BaseAgent abstract base class."""

    def test_base_agent_is_abstract(self):
        with pytest.raises(TypeError):
            BaseAgent()  # type: ignore[abstract]

    def test_base_agent_stores_ha_client_and_entity_index(self):
        ha = MagicMock()
        ei = MagicMock()
        agent = LightAgent(ha_client=ha, entity_index=ei)
        assert agent._ha_client is ha
        assert agent._entity_index is ei

    def test_base_agent_defaults_to_none_dependencies(self):
        agent = LightAgent()
        assert agent._ha_client is None
        assert agent._entity_index is None

    def test_build_time_location_context_with_full_context(self):
        ctx = TaskContext(
            timezone="Europe/Berlin",
            location_name="Berlin",
            local_time="2025-01-15 14:30",
        )
        result = BaseAgent._build_time_location_context(ctx)
        assert "Current local time: 2025-01-15 14:30" in result
        assert "Timezone: Europe/Berlin" in result
        assert "Home location: Berlin" in result

    def test_build_time_location_context_empty_when_no_local_time(self):
        ctx = TaskContext(timezone="Europe/Berlin", location_name="Berlin")
        result = BaseAgent._build_time_location_context(ctx)
        assert result == ""

    def test_build_time_location_context_none_context(self):
        result = BaseAgent._build_time_location_context(None)
        assert result == ""

    def test_build_time_location_context_utc_timezone_omitted(self):
        ctx = TaskContext(
            timezone="UTC",
            location_name="",
            local_time="2025-01-15 14:30",
        )
        result = BaseAgent._build_time_location_context(ctx)
        assert "Current local time: 2025-01-15 14:30" in result
        assert "Timezone" not in result
        assert "Home location" not in result

    @patch("app.llm.client.complete", new_callable=AsyncMock, return_value="Turned on the kitchen light.")
    async def test_preloaded_prompt_cache_avoids_disk_reads_during_repeated_async_turns(self, mock_complete):
        _prompt_cache.clear()
        preload_prompt_cache(["light"])
        agent = LightAgent()

        try:
            with patch("app.agents.base.Path.read_text", side_effect=AssertionError("unexpected prompt disk read")):
                await agent.handle_task(_make_task("turn on the kitchen light"))
                await agent.handle_task(_make_task("turn on the bedroom light"))
        finally:
            _prompt_cache.clear()

        assert mock_complete.await_count == 2

    @patch("app.llm.client.complete", new_callable=AsyncMock, return_value="Turned on the kitchen light.")
    async def test_prompt_cache_miss_uses_to_thread(self, mock_complete):
        _prompt_cache.clear()
        agent = LightAgent()
        calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        try:
            with patch(
                "app.agents.base.asyncio.to_thread", new=AsyncMock(side_effect=fake_to_thread)
            ) as mock_to_thread:
                await agent.handle_task(_make_task("turn on the kitchen light"))
        finally:
            _prompt_cache.clear()

        assert mock_to_thread.await_count == 1
        assert calls[0][0].__name__ == "_load_prompt_path"
        assert calls[0][1][0].name == "light.txt"
        mock_complete.assert_awaited_once()


# ---------------------------------------------------------------------------
# Agent card validation (all agents)
# ---------------------------------------------------------------------------


class TestAgentCards:
    """Each agent must expose a valid AgentCard."""

    @pytest.mark.parametrize(
        "agent_cls,expected_id",
        [
            (LightAgent, "light-agent"),
            (MusicAgent, "music-agent"),
            (ClimateAgent, "climate-agent"),
            (CoverAgent, "cover-agent"),
            (MediaAgent, "media-agent"),
            (TimerAgent, "timer-agent"),
            (SceneAgent, "scene-agent"),
            (AutomationAgent, "automation-agent"),
            (SecurityAgent, "security-agent"),
            (GeneralAgent, "general-agent"),
            (RewriteAgent, "rewrite-agent"),
            (ListsAgent, "lists-agent"),
            (VacuumAgent, "vacuum-agent"),
        ],
    )
    def test_agent_card_has_correct_id(self, agent_cls, expected_id):
        agent = agent_cls()
        card = agent.agent_card
        assert isinstance(card, AgentCard)
        assert card.agent_id == expected_id

    @pytest.mark.parametrize(
        "agent_cls",
        [
            LightAgent,
            MusicAgent,
            ClimateAgent,
            CoverAgent,
            MediaAgent,
            TimerAgent,
            SceneAgent,
            AutomationAgent,
            SecurityAgent,
            GeneralAgent,
            RewriteAgent,
            ListsAgent,
            VacuumAgent,
        ],
    )
    def test_agent_card_has_endpoint(self, agent_cls):
        agent = agent_cls()
        card = agent.agent_card
        assert card.endpoint.startswith("local://")


# ---------------------------------------------------------------------------
# Specialized agents: handle_task via mocked LLM
# ---------------------------------------------------------------------------


class TestBaseAgentStream:
    @patch("app.llm.client.complete", new_callable=AsyncMock, return_value="All done.")
    async def test_handle_task_stream_default_wraps_handle_task(self, mock_complete):
        agent = LightAgent()
        task = _make_task("turn on kitchen light")
        chunks = []
        async for chunk in agent.handle_task_stream(task):
            chunks.append(chunk)
        assert len(chunks) == 1
        assert chunks[0]["token"] == "All done."
        assert chunks[0]["done"] is True

    async def test_handle_task_stream_includes_action_executed(self):
        agent = LightAgent()
        agent.handle_task = AsyncMock(
            return_value={
                "speech": "Light is on.",
                "action_executed": {"action": "turn_on", "entity_id": "light.kitchen", "success": True},
            }
        )
        task = _make_task("turn on kitchen light")
        chunks = [c async for c in agent.handle_task_stream(task)]
        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert chunks[0]["action_executed"]["entity_id"] == "light.kitchen"

    async def test_handle_task_stream_omits_action_executed_when_absent(self):
        agent = LightAgent()
        agent.handle_task = AsyncMock(
            return_value={
                "speech": "No action needed.",
            }
        )
        task = _make_task("what lights are on")
        chunks = [c async for c in agent.handle_task_stream(task)]
        assert len(chunks) == 1
        assert "action_executed" not in chunks[0]

    async def test_handle_task_stream_includes_voice_followup(self):
        agent = LightAgent()
        agent.handle_task = AsyncMock(
            return_value={
                "speech": "Which lamp?",
                "voice_followup": True,
            }
        )
        task = _make_task("turn on the light")
        chunks = [c async for c in agent.handle_task_stream(task)]
        assert len(chunks) == 1
        assert chunks[0]["voice_followup"] is True

    async def test_handle_task_stream_includes_directive_and_reason(self):
        agent = LightAgent()
        agent.handle_task = AsyncMock(
            return_value={
                "speech": "",
                "directive": "test_directive",
                "reason": "test_reason",
            }
        )
        task = _make_task("set a timer for 5 minutes")
        chunks = [c async for c in agent.handle_task_stream(task)]
        assert len(chunks) == 1
        assert chunks[0]["directive"] == "test_directive"
        assert chunks[0]["reason"] == "test_reason"

    async def test_handle_task_stream_converts_handle_task_exception(self):
        """Default stream must not propagate exceptions to InProcessTransport (generic error chunk)."""
        agent = LightAgent()
        agent.handle_task = AsyncMock(side_effect=RuntimeError("simulated failure"))
        task = _make_task("turn on light")
        chunks = [c async for c in agent.handle_task_stream(task)]
        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert "error" not in chunks[0]
        assert "Sorry, something went wrong" in chunks[0]["token"]


# ---------------------------------------------------------------------------
# RewriteAgent
# ---------------------------------------------------------------------------


class TestAgentConfigDefaultTemperature:
    def test_default_temperature_is_0_2(self):
        from app.models.agent import AgentConfig

        config = AgentConfig(agent_id="test-agent")
        assert config.temperature == 0.2


class TestAgentConfigReasoningEffort:
    def test_default_reasoning_effort_is_none(self):
        from app.models.agent import AgentConfig

        config = AgentConfig(agent_id="test-agent")
        assert config.reasoning_effort is None

    def test_reasoning_effort_accepted(self):
        from app.models.agent import AgentConfig

        config = AgentConfig(agent_id="test-agent", reasoning_effort="low")
        assert config.reasoning_effort == "low"


# ---------------------------------------------------------------------------
# Climate Executor
# ---------------------------------------------------------------------------
