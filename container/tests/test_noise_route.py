"""Tests for the noise route: silent drop, pending-question restore."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock litellm before importing app modules; force-load llm client so
# ``@patch("app.llm.client.complete")`` resolves (matches test_agents.py).
_litellm_mock = MagicMock()
_litellm_mock.exceptions.AuthenticationError = type("AuthenticationError", (Exception,), {})
_litellm_mock.exceptions.APIError = type("APIError", (Exception,), {})
_litellm_mock.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _litellm_mock)

import app.llm.client  # noqa: E402,F401
from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.models.agent import NOISE_AGENT, AgentCard, IngressTask, TaskContext  # noqa: E402
from tests.helpers import make_ingress_task  # noqa: E402


def _make_orch():
    dispatcher = AsyncMock()
    dispatcher.dispatch = AsyncMock(return_value={"speech": "unexpected"})
    dispatcher.dispatch_stream = AsyncMock()

    registry = AsyncMock()
    registry.list_agents = AsyncMock(
        return_value=[
            AgentCard(agent_id="light-agent", name="Light Agent", description="", skills=["light"]),
            AgentCard(agent_id="general-agent", name="General Agent", description="", skills=["general"]),
        ]
    )

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

    return OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=cache_manager), dispatcher


class TestNoiseRoute:
    @pytest.fixture(autouse=True)
    def _mock_conversation_repo(self):
        with patch("app.agents.conversation_manager.ConversationRepository") as mock_repo:
            mock_repo.insert = AsyncMock(return_value=1)
            yield mock_repo

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_handle_task_noise_is_silent_and_dispatches_nothing(self, mock_complete, mock_track, mock_settings):
        mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: "auto" if k == "language" else d)
        orch, dispatcher = _make_orch()
        mock_complete.return_value = f"{NOISE_AGENT} (92%): background chatter"

        task = make_ingress_task(description="Und ich, eine Mann...", context=TaskContext(language="de"))
        task.conversation_id = "n1"
        result = await orch.handle_task(task)

        assert result["speech"] == ""
        assert result["routed_to"] == NOISE_AGENT
        assert result["action_executed"] is None
        assert result["voice_followup"] is False
        assert mock_complete.await_count == 1  # classify only -- no speech LLM call
        dispatcher.dispatch.assert_not_awaited()
        dispatcher.dispatch_stream.assert_not_awaited()
        mock_track.assert_awaited_once()
        assert mock_track.await_args.args[0] == NOISE_AGENT
        # Noise turns stay out of the conversation history.
        assert "n1" not in orch._conversation_manager._conversations

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_handle_task_stream_noise_yields_silent_done(self, mock_complete, mock_track, mock_settings):
        mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: "auto" if k == "language" else d)
        orch, dispatcher = _make_orch()
        mock_complete.return_value = f"{NOISE_AGENT} (92%): tv bleed-through"

        task = IngressTask(
            description="Ich bin eine Betrügerin.",
            conversation_id="n2",
            context=TaskContext(language="de"),
        )

        chunks = []
        async for ch in orch.handle_task_stream(task):
            chunks.append(ch)

        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert chunks[0]["mediated_speech"] == ""
        assert chunks[0]["routed_to"] == NOISE_AGENT
        assert mock_complete.await_count == 1
        dispatcher.dispatch.assert_not_awaited()
        dispatcher.dispatch_stream.assert_not_awaited()

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_noise_restores_pending_question(self, mock_complete, mock_track, mock_settings):
        mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: "auto" if k == "language" else d)
        orch, _dispatcher = _make_orch()
        orch._conversation_manager.set_pending_question("n3", "Küche oder Küchenzeile?", "light-agent")
        mock_complete.return_value = f"{NOISE_AGENT} (88%): unrelated fragment"

        task = make_ingress_task(description="...halt deine Klappe!", context=TaskContext(language="de"))
        task.conversation_id = "n3"
        result = await orch.handle_task(task)

        assert result["speech"] == ""
        assert result["routed_to"] == NOISE_AGENT
        # The unrelated fragment must not consume the open question.
        assert orch._conversation_manager.has_pending_question("n3")

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_pending_answer_consumes_question_normally(self, mock_complete, mock_track, mock_settings):
        mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: "auto" if k == "language" else d)
        orch, dispatcher = _make_orch()
        orch._conversation_manager.set_pending_question("n4", "Küche oder Küchenzeile?", "light-agent")
        mock_complete.return_value = "light-agent (95%): Licht Küche einschalten"
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Licht in der Küche ist an."})

        task = make_ingress_task(description="Küche.", context=TaskContext(language="de"))
        task.conversation_id = "n4"
        result = await orch.handle_task(task)

        assert result["routed_to"] == "light-agent"
        dispatcher.dispatch.assert_awaited()
        # A real answer consumes the pending question -- no restore.
        assert not orch._conversation_manager.has_pending_question("n4")

        # A noise turn AFTER a legitimately consumed question must not
        # resurrect the stale stash entry.
        mock_complete.reset_mock()
        mock_complete.return_value = f"{NOISE_AGENT} (80%): background chatter"
        task2 = make_ingress_task(description="...irrelevant...", context=TaskContext(language="de"))
        task2.conversation_id = "n4"
        result2 = await orch.handle_task(task2)
        assert result2["routed_to"] == NOISE_AGENT
        assert not orch._conversation_manager.has_pending_question("n4")


class TestNoiseSanitize:
    async def test_mixed_classification_drops_noise(self):
        orch, _dispatcher = _make_orch()
        engine = orch._classification_engine
        sanitized, repaired = await engine.sanitize_or_repair_classifications(
            [("noise", "background", 0.9), ("light-agent", "Licht Küche ein", 0.9)],
            user_text="Licht Küche ein",
            conversation_id=None,
        )
        assert repaired is False
        assert [c[0] for c in sanitized] == ["light-agent"]

    async def test_sole_noise_survives_sanitize(self):
        orch, _dispatcher = _make_orch()
        engine = orch._classification_engine
        sanitized, repaired = await engine.sanitize_or_repair_classifications(
            [("noise", "background", 0.9)],
            user_text="Und ich, eine Mann...",
            conversation_id=None,
        )
        assert repaired is False
        assert sanitized == [("noise", "background", 0.9)]

    async def test_noise_is_a_known_agent(self):
        orch, _dispatcher = _make_orch()
        known = await orch._agent_registry.get_known_agents()
        assert NOISE_AGENT in known
        assert "cancel-interaction" in known
