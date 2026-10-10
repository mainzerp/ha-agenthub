"""Regression tests for the orchestrator follow-up review (#132, theme T2).

Covers clarification pinning, streaming recovery/timeout finalization, the
HA-action double-execution guard, filler races, single prelude per turn,
sequential send with several content legs, system-line localization and
the cancel-wins sanitizer rule.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

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
from app.agents.classification_engine import ClassificationEngine  # noqa: E402
from app.agents.dispatch_manager import (  # noqa: E402
    _CANNED_ACTION_UNCONFIRMED_SPEECH,
    _CANNED_TIMEOUT_SPEECH,
)
from app.agents.mediation import MediationService  # noqa: E402
from app.agents.orchestrator import OrchestratorAgent, PipelinePreludeResult  # noqa: E402
from app.agents.pipeline_strategies import DefaultFinalizationStrategy  # noqa: E402
from app.agents.task_pipeline import DispatchResult  # noqa: E402
from app.ha_client.action_marker import note_ha_action_started  # noqa: E402
from app.models.agent import AgentCard, IngressTask, TaskContext  # noqa: E402

pytestmark = pytest.mark.asyncio

_AGENTS = ("light-agent", "general-agent", "automation-agent", "music-agent", "send-agent")


def _settings(language: str = "en", **extra: str):
    values = {"language": language, "personality.prompt": "", **extra}

    async def _get_value(key, default=None):
        return values.get(key, default if default is not None else "")

    return _get_value


def _make_orchestrator() -> tuple[OrchestratorAgent, AsyncMock]:
    dispatcher = AsyncMock()
    registry = AsyncMock()
    cache_manager = MagicMock()
    cache_manager.apply_rewrite = AsyncMock()
    cache_manager.try_replay_action = AsyncMock(return_value=None)
    cache_manager.try_routing_skip = AsyncMock(return_value=None)
    cache_manager.store_action_async = AsyncMock()
    cache_manager.store_routing_async = AsyncMock()
    registry.list_agents = AsyncMock(
        return_value=[AgentCard(agent_id=a, name=a, description="", skills=[]) for a in _AGENTS]
    )
    orch = OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=cache_manager)
    orch._should_send_filler = AsyncMock(return_value=False)
    return orch, dispatcher


def _task(text: str, conversation_id: str | None = "conv-t2", **ctx) -> IngressTask:
    return IngressTask(
        description=text,
        conversation_id=conversation_id,
        context=TaskContext(language="en", **ctx),
    )


def _prelude(task: IngressTask, classifications, **overrides) -> PipelinePreludeResult:
    fields = {
        "conversation_id": task.conversation_id or "conv-t2",
        "detected_language": "en",
        "lang_turns": [],
        "span_collector": task.span_collector,
        "classifications": classifications,
        "routing_cached": False,
        "target_agent": classifications[0][0],
        "condensed_task": classifications[0][1],
        "confidence": classifications[0][2],
        "used_origin_context": False,
    }
    fields.update(overrides)
    return PipelinePreludeResult(**fields)


# ---------------------------------------------------------------------------
# Item 2: clarification answers are pinned to the asking agent
# ---------------------------------------------------------------------------


class TestFollowupPinning:
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_answer_marked_by_classifier_is_pinned_to_asking_agent(self, mock_complete, mock_settings):
        """A yes/no answer reaches the asking agent with pending_question set,
        even when the classifier line names another agent."""
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, dispatcher = _make_orchestrator()
        orch._conversation_manager.set_pending_question("conv-pin", "Shall I save the automation?", "automation-agent")
        mock_complete.return_value = "[ANSWER] general-agent (80%): yes, save the automation"
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Saved."})

        result = await orch.handle_task(_task("yes", conversation_id="conv-pin"))

        request = dispatcher.dispatch.await_args.args[0]
        assert request.params["agent_id"] == "automation-agent"
        dispatched = request.params["task"]
        assert dispatched.context.pending_question == "Shall I save the automation?"
        assert dispatched.context.is_followup is True
        assert dispatched.description == "yes, save the automation"
        assert result["routed_to"] == "automation-agent"
        # The classifier saw the asking agent and the marker contract.
        system_prompt = mock_complete.await_args.args[1][0]["content"]
        assert "asked by automation-agent" in system_prompt
        assert "[ANSWER]" in system_prompt

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_new_request_is_classified_normally_and_drops_followup(self, mock_complete, mock_settings):
        """Without the marker the turn classifies normally; another agent does
        not receive the stale pending question."""
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, dispatcher = _make_orchestrator()
        orch._conversation_manager.set_pending_question("conv-new", "Shall I save the automation?", "automation-agent")
        mock_complete.return_value = "light-agent (95%): turn on the kitchen light"
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Light on."})

        await orch.handle_task(_task("turn on the kitchen light", conversation_id="conv-new"))

        request = dispatcher.dispatch.await_args.args[0]
        assert request.params["agent_id"] == "light-agent"
        assert request.params["task"].context.pending_question is None
        assert request.params["task"].context.is_followup is False

    async def test_unpinnable_pending_agent_is_ignored(self):
        """Comma-joined multi-agent tags and pseudo agents are never pinned."""
        registry = AsyncMock()
        registry.get_known_agents = AsyncMock(return_value=set(_AGENTS))
        engine = ClassificationEngine(
            agent_registry=registry, get_pending_agent=lambda _cid: "light-agent, music-agent"
        )
        assert await engine._resolve_pending_agent("c") is None
        engine._get_pending_agent = lambda _cid: "send-agent"
        assert await engine._resolve_pending_agent("c") is None
        engine._get_pending_agent = lambda _cid: "automation-agent"
        assert await engine._resolve_pending_agent("c") == "automation-agent"


# ---------------------------------------------------------------------------
# Item 3: multi-agent finalization records the pending question
# ---------------------------------------------------------------------------


class TestMultiAgentPendingQuestion:
    async def test_merged_followup_sets_pending_question(self):
        conv = MagicMock()
        conv.store_turn = AsyncMock()
        strategy = DefaultFinalizationStrategy(
            conversation_manager=conv,
            merge_responses=AsyncMock(return_value=("Light on. Shall I also start music?", True)),
            create_trace=AsyncMock(),
        )
        dispatch_result = DispatchResult(
            classifications=[("light-agent", "a", 0.9), ("music-agent", "b", 0.9)],
            target_agent="light-agent",
            routed_to="light-agent, music-agent",
            speech="",
            action_executed=None,
            has_error=False,
            agent_responses=[("light-agent", "Light on.", True), ("music-agent", "Ready.", False)],
        )
        task = _task("light and music", conversation_id="conv-multi", source="ha")
        response = await strategy.execute(
            task,
            dispatch_result,
            "light and music",
            "en",
            "conv-multi",
            [],
            None,
            dispatch_result.classifications,
            False,
            False,
        )
        conv.set_pending_question.assert_called_once_with(
            "conv-multi", "Light on. Shall I also start music?", "light-agent, music-agent"
        )
        assert response["voice_followup"] is True


# ---------------------------------------------------------------------------
# Items 1, 4, 9, 10a, 13: streaming recovery and timeouts
# ---------------------------------------------------------------------------


class TestStreamingRecovery:
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_error_frame_without_tokens_uses_fallback_agent(self, mock_complete, mock_settings):
        """Item 1: an agent error frame with no text re-dispatches to the
        fallback agent instead of speaking a canned line."""
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): turn on light"
        orch, dispatcher = _make_orchestrator()

        async def _failing_stream(_request):
            yield {"token": "", "done": True, "error": "light-agent: internal error"}

        dispatcher.dispatch_stream = _failing_stream
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Fallback answer."})

        chunks = [c async for c in orch.handle_task_stream(_task("turn on light"))]
        final = chunks[-1]
        assert final["done"] is True
        assert final["mediated_speech"] == "Fallback answer."
        assert final["routed_to"] == "general-agent"
        assert "error" not in final
        assert dispatcher.dispatch.await_args.args[0].params["agent_id"] == "general-agent"

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_stream_timeout_finalizes_turn_and_restores_pending_question(self, mock_complete, mock_settings):
        """Item 4: a timed-out stream stores the turn, writes the trace and
        re-arms the popped clarifying question when only canned speech went out."""
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): kitchen"
        orch, dispatcher = _make_orchestrator()
        orch._dispatch_manager.resolve_dispatch_timeout = AsyncMock(return_value=0.05)
        orch._store_turn = AsyncMock()
        orch._create_trace = AsyncMock()
        orch._conversation_manager.set_pending_question("conv-to", "Kitchen or hallway?", "light-agent")

        async def _hanging(_request):
            await asyncio.sleep(30)
            yield {"token": "never", "done": True}

        dispatcher.dispatch_stream = _hanging
        dispatcher.dispatch = AsyncMock(side_effect=RuntimeError("fallback down"))

        task = _task("kitchen", conversation_id="conv-to")
        task.span_collector = MagicMock()
        chunks = [c async for c in orch.handle_task_stream(task)]

        final = chunks[-1]
        assert final["mediated_speech"] == _CANNED_TIMEOUT_SPEECH
        assert "timed out" in final["error"]
        orch._store_turn.assert_awaited_once()
        orch._create_trace.assert_awaited_once()
        assert orch._conversation_manager.has_pending_question("conv-to")

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_timeout_after_relayed_tokens_appends_no_fallback(self, mock_complete, mock_settings):
        """Item 4: when tokens were already relayed, no fallback text follows
        the partial answer and no fallback agent is dispatched."""
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): turn on light"
        orch, dispatcher = _make_orchestrator()
        orch._dispatch_manager.resolve_dispatch_timeout = AsyncMock(return_value=0.1)

        async def _partial_then_hang(_request):
            yield {"token": "The light", "done": False}
            await asyncio.sleep(30)
            yield {"token": " is on.", "done": True}

        dispatcher.dispatch_stream = _partial_then_hang
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Fallback answer."})

        chunks = [c async for c in orch.handle_task_stream(_task("turn on light"))]
        relayed = [c["token"] for c in chunks if not c.get("done") and c.get("token")]
        assert relayed == ["The light"]
        final = chunks[-1]
        assert "mediated_speech" not in final
        assert "timed out" in final["error"]
        dispatcher.dispatch.assert_not_called()

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_stream_timeout_after_ha_action_does_not_redispatch(self, mock_complete, mock_settings):
        """Item 9: the streaming timeout never re-dispatches once the agent's
        HA service call started."""
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): turn on light"
        orch, dispatcher = _make_orchestrator()
        orch._dispatch_manager.resolve_dispatch_timeout = AsyncMock(return_value=0.05)

        async def _ha_call_then_hang(_request):
            note_ha_action_started()
            await asyncio.sleep(30)
            yield {"token": "never", "done": True}

        dispatcher.dispatch_stream = _ha_call_then_hang
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Fallback answer."})

        chunks = [c async for c in orch.handle_task_stream(_task("turn on light"))]
        final = chunks[-1]
        dispatcher.dispatch.assert_not_called()
        assert final["mediated_speech"] == _CANNED_ACTION_UNCONFIRMED_SPEECH
        assert final["routed_to"] == "light-agent"

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_empty_stream_before_filler_threshold_returns_immediately(self, mock_complete, mock_settings):
        """Item 10a: a stream that ends without chunks before the filler
        threshold must not wait for the dispatch timeout."""
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): turn on light"
        orch, dispatcher = _make_orchestrator()
        orch._should_send_filler = AsyncMock(return_value=True)
        orch._get_filler_threshold_ms = AsyncMock(return_value=5000)
        orch._invoke_filler_agent = AsyncMock(return_value="One moment.")
        orch._dispatch_manager.resolve_dispatch_timeout = AsyncMock(return_value=3.0)

        async def _empty(_request):
            return
            yield  # pragma: no cover

        dispatcher.dispatch_stream = _empty
        task = _task("turn on light")
        task.span_collector = None
        t0 = time.perf_counter()
        chunks = [c async for c in orch.handle_task_stream(task)]
        assert time.perf_counter() - t0 < 1.5
        assert chunks[-1]["done"] is True
        assert "timed out" not in (chunks[-1].get("error") or "")

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_slow_consumer_is_not_cancelled_by_dispatch_timeout(self, mock_complete, mock_settings):
        """Item 13: the dispatch timeout is enforced on the queue reads, not
        around the ``yield`` -- a consumer busy between frames is never hit
        by a cancellation."""
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): turn on light"
        orch, dispatcher = _make_orchestrator()
        orch._dispatch_manager.resolve_dispatch_timeout = AsyncMock(return_value=0.15)

        async def _token_then_hang(_request):
            yield {"token": "Working", "done": False}
            await asyncio.sleep(30)
            yield {"token": "", "done": True}

        dispatcher.dispatch_stream = _token_then_hang

        chunks = []
        async for chunk in orch.handle_task_stream(_task("turn on light")):
            chunks.append(chunk)
            if chunk.get("token") == "Working":
                # Consumer work that outlasts the dispatch budget.
                await asyncio.sleep(0.3)
        assert chunks[-1]["done"] is True
        assert "timed out" in chunks[-1]["error"]


# ---------------------------------------------------------------------------
# Item 8: one prelude per turn, stable fallback conversation id
# ---------------------------------------------------------------------------


class TestSinglePrelude:
    @patch("app.agents.orchestrator.SettingsRepository")
    async def test_streaming_multi_agent_runs_prelude_once(self, mock_settings):
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, _dispatcher = _make_orchestrator()
        prelude_calls = 0

        async def _prelude_spy(task, **_kwargs):
            nonlocal prelude_calls
            prelude_calls += 1
            return _prelude(task, [("light-agent", "a", 0.9), ("music-agent", "b", 0.8)])

        orch._run_pipeline_prelude = _prelude_spy
        orch._dispatch_and_finalize = AsyncMock(return_value={"speech": "Both done.", "routed_to": "x"})

        chunks = [c async for c in orch.handle_task_stream(_task("light and music"))]
        assert prelude_calls == 1
        assert chunks[-1]["mediated_speech"] == "Both done."
        orch._dispatch_and_finalize.assert_awaited_once()

    async def test_fallback_conversation_id_is_written_back(self):
        orch, _ = _make_orchestrator()
        task = _task("hello", conversation_id=None)
        first, _ = orch._pipeline_resolve_conversation_id(task)
        second, _ = orch._pipeline_resolve_conversation_id(task)
        assert first == second == task.conversation_id


# ---------------------------------------------------------------------------
# Item 5: sequential send runs every content leg
# ---------------------------------------------------------------------------


class TestSequentialSendContentLegs:
    async def test_all_content_legs_are_dispatched_and_joined(self):
        orch, _ = _make_orchestrator()
        calls = []

        async def _dispatch_single(agent_id, task_text, *args, **kwargs):
            calls.append((agent_id, task_text))
            if agent_id == "send-agent":
                return "send-agent", "Sent.", {"action_executed": {"success": True}}
            return agent_id, f"content from {agent_id}", {"action_executed": None}

        orch._dispatch_single = AsyncMock(side_effect=_dispatch_single)
        routed_to, speech, _result = await orch._handle_sequential_send(
            [("general-agent", "weather", 0.9), ("light-agent", "light states", 0.8), ("send-agent", "send", 0.9)],
            "send me the weather and the light states",
            "conv-seq",
            [],
            None,
            TaskContext(language="en"),
        )
        content_calls = [c for c in calls if c[0] != "send-agent"]
        assert sorted(content_calls) == [("general-agent", "weather"), ("light-agent", "light states")]
        send_task = next(t for a, t in calls if a == "send-agent")
        assert "content from general-agent" in send_task
        assert "content from light-agent" in send_task
        assert routed_to == "general-agent, light-agent, send-agent"
        assert speech == "Sent."

    async def test_failed_content_leg_blocks_delivery(self):
        orch, _ = _make_orchestrator()

        async def _dispatch_single(agent_id, task_text, *args, **kwargs):
            if agent_id == "light-agent":
                return agent_id, "x", {"speech": "x", "error": {"code": "timeout", "canned": True}}
            if agent_id == "send-agent":
                raise AssertionError("must not send partial content")
            return agent_id, "content", {}

        orch._dispatch_single = AsyncMock(side_effect=_dispatch_single)
        routed_to, _speech, result = await orch._handle_sequential_send(
            [("general-agent", "weather", 0.9), ("light-agent", "light states", 0.8), ("send-agent", "send", 0.9)],
            "send me the weather and the light states",
            "conv-seq-fail",
            [],
            None,
            TaskContext(language="en"),
        )
        assert routed_to == "send-agent"
        assert result["error"]["code"] == "content_unavailable"


# ---------------------------------------------------------------------------
# Item 7: system-line localization through mediation
# ---------------------------------------------------------------------------


class TestLocalization:
    def _service(self, llm_result=None, llm_error=None):
        orch = MagicMock()
        orch._load_prompt_async = AsyncMock(return_value="Translate into {language}.")
        orch._mediation_temperature = 0.3
        orch._mediation_model = None
        orch._call_llm = AsyncMock(return_value=llm_result, side_effect=llm_error)
        return MediationService(orch), orch

    async def test_english_needs_no_llm_call(self):
        service, orch = self._service("unused")
        assert await service.localize_message("I couldn't do that.", "en") == "I couldn't do that."
        orch._call_llm.assert_not_called()

    async def test_other_language_goes_through_llm(self):
        service, orch = self._service("Das ging nicht.")
        assert await service.localize_message("I couldn't do that.", "de") == "Das ging nicht."
        system_prompt = orch._call_llm.await_args.args[0][0]["content"]
        assert "German" in system_prompt

    async def test_llm_failure_falls_back_to_english(self):
        service, _ = self._service(llm_error=RuntimeError("llm down"))
        assert await service.localize_message("I couldn't do that.", "de") == "I couldn't do that."

    async def test_all_agents_failed_line_is_localized(self):
        service, _ = self._service("Leider sind alle Agenten fehlgeschlagen.")
        speech, followup = await service.merge_responses([], "mach alles", failed_agents=["light-agent"], language="de")
        assert speech == "Leider sind alle Agenten fehlgeschlagen."
        assert followup is False

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_non_streaming_canned_error_is_localized(self, mock_complete, mock_settings):
        """Non-streaming error turns are no longer exempt from mediation: the
        canned line reaches the user in the turn language."""
        mock_settings.get_value = AsyncMock(side_effect=_settings(language="de"))
        mock_complete.side_effect = [
            "light-agent (95%): Licht an",
            "Das konnte ich gerade nicht verarbeiten.",
        ]
        orch, dispatcher = _make_orchestrator()
        dispatcher.dispatch = AsyncMock(side_effect=[RuntimeError("down"), RuntimeError("fallback down")])
        result = await orch.handle_task(_task("Licht an", conversation_id="conv-de"))
        assert result["speech"] == "Das konnte ich gerade nicht verarbeiten."
        assert result["error"] == "agent_error"


# ---------------------------------------------------------------------------
# Items 11, 12, 15, 17
# ---------------------------------------------------------------------------


class TestMisc:
    @patch("app.agents.orchestrator.SettingsRepository")
    async def test_language_detection_runs_off_the_event_loop(self, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="auto")
        orch, _ = _make_orchestrator()
        loop_thread = threading.get_ident()
        seen = []

        def _detect(text, fallback="en"):
            seen.append(threading.get_ident())
            return "de"

        with patch("app.agents.orchestrator.detect_user_language", side_effect=_detect):
            assert await orch._resolve_language("Schalte bitte das Licht ein", "en") == "de"
        assert seen and all(ident != loop_thread for ident in seen)

    def test_cancel_description_has_no_hardcoded_german(self):
        line = ClassificationEngine.cancel_interaction_description_line()
        for phrase in ("abbrechen", "egal", "schon gut"):
            assert phrase not in line

    async def test_cancel_mixed_with_actions_wins(self):
        engine = ClassificationEngine(agent_registry=AsyncMock())
        result, repaired = await engine.sanitize_or_repair_classifications(
            [("light-agent", "turn on the light", 0.9), ("cancel-interaction", "dismiss", 0.8)],
            user_text="turn on the light, no, forget it",
            conversation_id=None,
        )
        assert result == [("cancel-interaction", "dismiss", 0.8)]
        assert repaired is False

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    async def test_streaming_cancel_turn_stores_user_language_source(self, mock_track, mock_settings):
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, _ = _make_orchestrator()
        orch._store_turn = AsyncMock()

        async def _prelude_cancel(task, **_kwargs):
            return _prelude(task, [("cancel-interaction", "dismiss", 1.0)], detected_language="de")

        orch._run_pipeline_prelude = _prelude_cancel
        task = _task("vergiss es", user_id="user-1", source="ha")
        with patch("app.agents.orchestrator.generate_cancel_speech", AsyncMock(return_value="Okay.")):
            _ = [c async for c in orch.handle_task_stream(task)]
        kwargs = orch._store_turn.await_args.kwargs
        assert kwargs["user_id"] == "user-1"
        assert kwargs["language"] == "de"
        assert kwargs["source"] == "ha"
