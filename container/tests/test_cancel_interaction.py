"""Tests for cancel-interaction routing and LLM-backed acknowledgement."""

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
from app.agents.cancel_speech import cancel_interaction_ack, generate_cancel_speech  # noqa: E402
from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.models.agent import AgentCard, IngressTask, TaskContext  # noqa: E402
from tests.helpers import make_ingress_task  # noqa: E402

CANCEL_AGENT = "cancel-interaction"


def _classify_then_cancel(classify_text: str, cancel_text: str | Exception):
    calls = {"n": 0}

    async def _side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return classify_text
        if isinstance(cancel_text, Exception):
            raise cancel_text
        return cancel_text

    return _side_effect


def _make_orch():
    dispatcher = AsyncMock()
    response_mock = {"speech": "unexpected"}
    dispatcher.dispatch = AsyncMock(return_value=response_mock)
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


class TestCancelInteractionAck:
    def test_english(self):
        assert cancel_interaction_ack("en") == "Okay, got it."
        assert cancel_interaction_ack(None) == "Okay, got it."

    def test_german(self):
        assert cancel_interaction_ack("de") == "Alles klar, verstanden."
        assert cancel_interaction_ack("de-DE") == "Alles klar, verstanden."

    def test_fallbacks_meet_minimum_word_count(self):
        for lang in ("en", "de", None):
            ack = cancel_interaction_ack(lang)
            assert len(ack.split()) >= 3, f"Fallback for {lang!r} too short: {ack!r}"


class TestGenerateCancelSpeech:
    @patch("app.agents.cancel_speech.complete", new_callable=AsyncMock)
    async def test_generate_cancel_speech_uses_llm(self, mock_complete):
        mock_complete.return_value = "Klar, kein Problem."

        result = await generate_cancel_speech("de", "Vergiss es")

        assert result == "Klar, kein Problem."
        mock_complete.assert_awaited_once()
        kwargs = mock_complete.await_args.kwargs
        assert kwargs["agent_id"] == "filler-agent"
        assert kwargs["max_tokens"] == 30
        assert kwargs["temperature"] == 0.6

    @patch("app.agents.cancel_speech.complete", new_callable=AsyncMock)
    async def test_generate_cancel_speech_falls_back_on_short_response(self, mock_complete):
        mock_complete.return_value = "Verstanden."  # only 1 word — rejected

        result = await generate_cancel_speech("de", "Vergiss es")

        assert result == cancel_interaction_ack("de")

    @patch("app.agents.cancel_speech.complete", new_callable=AsyncMock)
    async def test_generate_cancel_speech_falls_back_on_timeout(self, mock_complete):
        mock_complete.side_effect = TimeoutError()

        result = await generate_cancel_speech("de", "Vergiss es")

        assert result == cancel_interaction_ack("de")

    @patch("app.agents.cancel_speech.complete", new_callable=AsyncMock)
    async def test_generate_cancel_speech_falls_back_on_empty(self, mock_complete):
        mock_complete.return_value = "   "

        result = await generate_cancel_speech("en", "never mind")

        assert result == cancel_interaction_ack("en")

    @patch("app.agents.cancel_speech.complete", new_callable=AsyncMock)
    async def test_generate_cancel_speech_falls_back_on_question(self, mock_complete):
        mock_complete.return_value = "Soll ich noch etwas tun?"

        result = await generate_cancel_speech("de", "Vergiss es")

        assert result == cancel_interaction_ack("de")


class TestOrchestratorCancelInteraction:
    @pytest.fixture(autouse=True)
    def _mock_conversation_repo(self):
        with patch("app.agents.conversation_manager.ConversationRepository") as mock_repo:
            mock_repo.insert = AsyncMock(return_value=1)
            yield mock_repo

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.agents.cancel_speech.complete", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_handle_task_cancel_does_not_dispatch(
        self, mock_complete, mock_cancel_complete, mock_track, mock_settings
    ):
        mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: "auto" if k == "language" else d)
        orch, dispatcher = _make_orch()
        mock_complete.side_effect = _classify_then_cancel(
            f"{CANCEL_AGENT} (98%): dismiss interaction",
            "Okay, dismissed.",
        )
        mock_cancel_complete.side_effect = mock_complete.side_effect

        task = make_ingress_task(description="nevermind", context=TaskContext(language="en"))
        task.conversation_id = "c1"
        result = await orch.handle_task(task)

        assert result["speech"]
        assert len(result["speech"]) <= 80
        assert "?" not in result["speech"]
        assert result["routed_to"] == CANCEL_AGENT
        assert mock_complete.await_count == 1
        assert mock_cancel_complete.await_count == 1
        dispatcher.dispatch.assert_not_awaited()
        dispatcher.dispatch_stream.assert_not_awaited()

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.agents.cancel_speech.complete", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_handle_task_cancel_german_ack(self, mock_complete, mock_cancel_complete, mock_track, mock_settings):
        mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: "de" if k == "language" else d)
        orch, dispatcher = _make_orch()
        mock_complete.side_effect = _classify_then_cancel(
            f"{CANCEL_AGENT} (95%): dismiss",
            "Verstanden.",
        )
        mock_cancel_complete.side_effect = mock_complete.side_effect

        task = make_ingress_task(description="Abbrechen", context=TaskContext(language="de"))
        task.conversation_id = "c2"
        result = await orch.handle_task(task)

        assert result["speech"]
        assert len(result["speech"]) <= 80
        assert "?" not in result["speech"]
        assert mock_complete.await_count == 1
        assert mock_cancel_complete.await_count == 1
        dispatcher.dispatch.assert_not_awaited()

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.agents.cancel_speech.complete", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_handle_task_stream_early_return_cancel(
        self, mock_complete, mock_cancel_complete, mock_track, mock_settings
    ):
        mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: "auto" if k == "language" else d)
        orch, dispatcher = _make_orch()
        mock_complete.side_effect = _classify_then_cancel(
            f"{CANCEL_AGENT} (95%): dismiss interaction",
            "Understood.",
        )
        mock_cancel_complete.side_effect = mock_complete.side_effect

        task = IngressTask(
            description="stop",
            conversation_id="c3",
            context=TaskContext(language="en"),
        )

        chunks = []
        async for ch in orch.handle_task_stream(task):
            chunks.append(ch)

        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert chunks[0]["mediated_speech"]
        assert len(chunks[0]["mediated_speech"]) <= 80
        assert "?" not in chunks[0]["mediated_speech"]
        assert mock_complete.await_count == 1
        assert mock_cancel_complete.await_count == 1
        dispatcher.dispatch.assert_not_awaited()
        dispatcher.dispatch_stream.assert_not_awaited()
