"""Tests for orchestrator streaming gaps: cancel-interaction and streaming dispatch."""

from __future__ import annotations

import asyncio
import sys
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
from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.models.agent import AgentCard, IngressTask, TaskContext  # noqa: E402


def _make_task(text: str = "turn on light", conversation_id: str = "conv-stream") -> IngressTask:
    return IngressTask(
        description=text,
        conversation_id=conversation_id,
        context=TaskContext(language="en"),
    )


def _make_orchestrator() -> tuple[OrchestratorAgent, AsyncMock]:
    dispatcher = AsyncMock()
    registry = AsyncMock()
    cache_manager = MagicMock()
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
            AgentCard(agent_id="general-agent", name="General Agent", description="", skills=["general"]),
        ]
    )
    orch = OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=cache_manager)
    return orch, dispatcher


# ---------------------------------------------------------------------------
# G6: Cancel-interaction in streaming mode
# ---------------------------------------------------------------------------


class TestCancelInteractionStreaming:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    async def test_cancel_interaction_streaming_yields_done_chunk(self, mock_track, mock_settings):
        """G6: Streaming with cancel-interaction classification must yield a done chunk with mediated speech."""
        mock_settings.get_value = AsyncMock(return_value="")
        orch, dispatcher = _make_orchestrator()
        mock_track.return_value = None

        # Mock the pipeline prelude to return cancel-interaction classification
        async def _mock_prelude(task, **kwargs):
            from app.agents.orchestrator import PipelinePreludeResult

            return PipelinePreludeResult(
                conversation_id=task.conversation_id or "conv-cancel",
                detected_language="en",
                lang_turns=[],
                span_collector=task.span_collector,
                classifications=[("cancel-interaction", "cancel", 1.0)],
                routing_cached=False,
                target_agent="cancel-interaction",
                condensed_task="cancel",
                confidence=1.0,
                used_origin_context=False,
            )

        orch._run_pipeline_prelude = _mock_prelude

        task = _make_task("never mind", conversation_id="conv-cancel")
        chunks = [c async for c in orch.handle_task_stream(task)]

        assert len(chunks) == 1
        assert chunks[0]["done"] is True
        assert "mediated_speech" in chunks[0]
        assert chunks[0].get("routed_to") == "cancel-interaction"
        # dispatch_stream should NOT be called for cancel-interaction
        dispatcher.dispatch_stream.assert_not_called()


# ---------------------------------------------------------------------------
# M-11: streaming dispatch timeout + fallback
# ---------------------------------------------------------------------------


class TestStreamingDispatchTimeout:
    def _patch_settings(self, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="")

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_hanging_stream_falls_back_to_general_agent(self, mock_complete, mock_track, mock_settings):
        """M-11: a streaming agent that never yields hits the per-agent timeout
        and the terminal chunk carries the fallback agent's answer."""
        self._patch_settings(mock_settings)
        mock_complete.return_value = "light-agent (95%): Turn on light"
        orch, dispatcher = _make_orchestrator()
        orch._dispatch_manager.resolve_dispatch_timeout = AsyncMock(return_value=0.05)
        orch._should_send_filler = AsyncMock(return_value=False)

        async def _hanging_stream(_request):
            await asyncio.sleep(30)
            yield {"token": "never", "done": True}

        dispatcher.dispatch_stream = _hanging_stream
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Fallback answer."})

        task = _make_task("turn on light", conversation_id="conv-stream-timeout")
        chunks = [c async for c in orch.handle_task_stream(task)]

        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        final = done_chunks[0]
        assert final["routed_to"] == "general-agent"
        assert final["mediated_speech"] == "Fallback answer."
        assert isinstance(final["error"], str)
        assert "timed out" in final["error"]

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_hanging_stream_failing_fallback_yields_graceful_error(
        self, mock_complete, mock_track, mock_settings
    ):
        """M-11: when the fallback also fails, the terminal chunk carries the
        canned timeout speech and a string error (no crash)."""
        self._patch_settings(mock_settings)
        mock_complete.return_value = "light-agent (95%): Turn on light"
        orch, dispatcher = _make_orchestrator()
        orch._dispatch_manager.resolve_dispatch_timeout = AsyncMock(return_value=0.05)
        orch._should_send_filler = AsyncMock(return_value=False)

        async def _hanging_stream(_request):
            await asyncio.sleep(30)
            yield {"token": "never", "done": True}

        dispatcher.dispatch_stream = _hanging_stream
        dispatcher.dispatch = AsyncMock(side_effect=RuntimeError("fallback down"))

        task = _make_task("turn on light", conversation_id="conv-stream-timeout-fb-fail")
        chunks = [c async for c in orch.handle_task_stream(task)]

        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        final = done_chunks[0]
        assert final["mediated_speech"] == "I couldn't process that request in time."
        assert isinstance(final["error"], str)

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_fallback_agent_timeout_skips_fallback_dispatch(self, mock_complete, mock_track, mock_settings):
        """M-11: when the target IS the fallback agent, no fallback dispatch is
        attempted -- the terminal chunk carries the canned speech directly."""
        self._patch_settings(mock_settings)
        mock_complete.return_value = "general-agent (95%): chat"
        orch, dispatcher = _make_orchestrator()
        orch._dispatch_manager.resolve_dispatch_timeout = AsyncMock(return_value=0.05)
        orch._should_send_filler = AsyncMock(return_value=False)

        async def _hanging_stream(_request):
            await asyncio.sleep(30)
            yield {"token": "never", "done": True}

        dispatcher.dispatch_stream = _hanging_stream
        dispatcher.dispatch = AsyncMock(return_value={"speech": "unused"})

        task = _make_task("hello", conversation_id="conv-stream-timeout-general")
        chunks = [c async for c in orch.handle_task_stream(task)]

        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        final = done_chunks[0]
        assert final["routed_to"] == "general-agent"
        assert final["mediated_speech"] == "I couldn't process that request in time."
        dispatcher.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# M-12: streaming directive turns persist turn + trace
# ---------------------------------------------------------------------------


class TestStreamingDirectiveTurn:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streaming_directive_turn_stores_turn_and_trace(self, mock_complete, mock_track, mock_settings):
        """M-12: a directive terminal chunk in streaming mode persists the turn
        and creates a trace before the chunk is yielded."""
        from app.analytics.tracer import SpanCollector

        mock_settings.get_value = AsyncMock(return_value="")
        mock_complete.return_value = "light-agent (95%): Set a timer"
        orch, dispatcher = _make_orchestrator()
        orch._should_send_filler = AsyncMock(return_value=False)

        async def _directive_stream(_request):
            yield {
                "token": "Timer set for 5 minutes.",
                "done": True,
                "directive": "start_timer",
                "reason": "timer_created",
            }

        dispatcher.dispatch_stream = _directive_stream
        orch._store_turn = AsyncMock()
        orch._create_trace = AsyncMock()

        task = _make_task("set a 5 minute timer", conversation_id="conv-directive-stream")
        task.span_collector = SpanCollector("trace-directive-stream")
        chunks = [c async for c in orch.handle_task_stream(task)]

        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        final = done_chunks[0]
        assert final["directive"] == "start_timer"
        assert final["reason"] == "timer_created"
        orch._store_turn.assert_awaited_once()
        assert orch._store_turn.await_args.args[2] == "Timer set for 5 minutes."
        orch._create_trace.assert_awaited_once()


# ---------------------------------------------------------------------------
# M-9 / M-10: streaming mediation correctness
# ---------------------------------------------------------------------------


class TestStreamingMediation:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_empty_personality_reminder_keeps_agent_answer(self, mock_complete, mock_track, mock_settings):
        """M-9: mediation-streaming enabled + empty personality + active
        reminder: the user receives the agent's answer WITH the reminder
        appended (blocking path), never the reminder alone."""
        mock_complete.return_value = "light-agent (95%): Turn on light"
        mock_settings.get_value = AsyncMock(
            side_effect=lambda k, d=None: {
                "orchestrator.mediation_streaming_enabled": "true",
                "personality.prompt": "",
                "orchestrator.organic_followup_enabled": "false",
            }.get(k, d)
        )
        orch, dispatcher = _make_orchestrator()
        orch._calendar_injector = MagicMock()
        orch._calendar_injector.inject_reminders = AsyncMock(return_value="Meeting at 3pm.")

        async def _stream(_request):
            yield {"token": "Light is on.", "done": True}

        dispatcher.dispatch_stream = _stream
        orch._should_send_filler = AsyncMock(return_value=False)

        task = _make_task("turn on light", conversation_id="conv-m9")
        chunks = [c async for c in orch.handle_task_stream(task)]

        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        assert done_chunks[0]["mediated_speech"] == "Light is on. Meeting at 3pm."
        # No lone-reminder token was streamed before the terminal chunk.
        token_chunks = [c for c in chunks if not c.get("done") and c.get("token")]
        assert token_chunks == []

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_mediation_stream_failure_before_first_token_falls_back(
        self, mock_complete, mock_track, mock_settings
    ):
        """M-10: mediation LLM failing before the first token falls back to the
        blocking path -- the full (mediated) answer is delivered and stored.

        Mediation is active because a personality is configured and a
        calendar reminder applies."""
        from app.agents.mediation import MediationStreamError

        mock_complete.return_value = "light-agent (95%): Turn on light"
        mock_settings.get_value = AsyncMock(
            side_effect=lambda k, d=None: {
                "orchestrator.mediation_streaming_enabled": "true",
                "personality.prompt": "You are friendly.",
                "orchestrator.organic_followup_enabled": "false",
            }.get(k, d)
        )
        orch, dispatcher = _make_orchestrator()
        orch._calendar_injector = MagicMock()
        orch._calendar_injector.inject_reminders = AsyncMock(return_value="Meeting at 3pm.")

        async def _stream(_request):
            yield {"token": "Light is on.", "done": True}

        dispatcher.dispatch_stream = _stream
        orch._should_send_filler = AsyncMock(return_value=False)

        async def _failing_mediation_stream(**kwargs):
            raise MediationStreamError("LLM stream broke")
            yield  # pragma: no cover -- async generator shape

        orch._mediate_response_stream = _failing_mediation_stream
        orch._mediate_response = AsyncMock(return_value=("Friendly full answer.", False))
        orch._store_turn = AsyncMock()

        task = _make_task("turn on light", conversation_id="conv-m10-zero")
        chunks = [c async for c in orch.handle_task_stream(task)]

        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        assert done_chunks[0]["mediated_speech"] == "Friendly full answer."
        orch._store_turn.assert_awaited_once()
        assert orch._store_turn.await_args.args[2] == "Friendly full answer."

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_mediation_stream_failure_mid_stream_stores_original(self, mock_complete, mock_track, mock_settings):
        """M-10: mediation LLM failing mid-stream keeps the spoken partial
        output but stores the ORIGINAL full agent answer (no truncation).

        Mediation is active because a personality is configured and a
        calendar reminder applies."""
        from app.agents.mediation import MediationStreamError

        mock_complete.return_value = "light-agent (95%): Turn on light"
        mock_settings.get_value = AsyncMock(
            side_effect=lambda k, d=None: {
                "orchestrator.mediation_streaming_enabled": "true",
                "personality.prompt": "You are friendly.",
                "orchestrator.organic_followup_enabled": "false",
            }.get(k, d)
        )
        orch, dispatcher = _make_orchestrator()
        orch._calendar_injector = MagicMock()
        orch._calendar_injector.inject_reminders = AsyncMock(return_value="Meeting at 3pm.")

        async def _stream(_request):
            yield {"token": "Full agent answer.", "done": True}

        dispatcher.dispatch_stream = _stream
        orch._should_send_filler = AsyncMock(return_value=False)

        async def _partial_mediation_stream(**kwargs):
            yield "Partial mediated "
            raise MediationStreamError("LLM stream broke mid-way")

        orch._mediate_response_stream = _partial_mediation_stream
        orch._store_turn = AsyncMock()

        task = _make_task("turn on light", conversation_id="conv-m10-partial")
        chunks = [c async for c in orch.handle_task_stream(task)]

        # The partial mediated token was already spoken to the client.
        token_chunks = [c for c in chunks if not c.get("done") and c.get("token")]
        assert [c["token"] for c in token_chunks] == ["Partial mediated "]
        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        # mediated_speech suppressed (tokens were streamed) and the stored
        # turn holds the ORIGINAL full agent answer.
        assert done_chunks[0].get("mediated_speech") is None
        orch._store_turn.assert_awaited_once()
        assert orch._store_turn.await_args.args[2] == "Full agent answer."


# ---------------------------------------------------------------------------
# H-1: recoverable classification failure -> terminal chunk carries string error
# ---------------------------------------------------------------------------


class TestClassificationErrorStreaming:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    async def test_recoverable_classification_failure_yields_string_error_chunk(self, mock_track, mock_settings):
        """H-1: recoverable classification failure yields one terminal chunk with
        a string ``error`` (never a dict) plus ``mediated_speech``."""
        mock_settings.get_value = AsyncMock(return_value="")
        orch, _dispatcher = _make_orchestrator()
        from app.agents.classification_engine import _RecoverableClassificationError

        orch._pipeline_director.run_classification = AsyncMock(
            side_effect=_RecoverableClassificationError("Classification service is unavailable.", code="llm_error")
        )

        task = _make_task("turn on light", conversation_id="conv-clf-error")
        chunks = [c async for c in orch.handle_task_stream(task)]

        assert len(chunks) == 1
        final = chunks[0]
        assert final["done"] is True
        assert final["error"] == "Classification service is unavailable."
        assert isinstance(final["error"], str)
        assert final["mediated_speech"] == "Classification service is unavailable."


# ---------------------------------------------------------------------------
# G14: Streaming dispatch: filler generation, queue reader, token processing
# ---------------------------------------------------------------------------


class TestStreamingDispatchInternals:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streaming_filler_generation_and_queue_reader(self, mock_complete, mock_track, mock_settings):
        """G14: Filler threshold exceeded must trigger filler generation and queue-based consumption."""
        mock_settings.get_value = AsyncMock(return_value="")
        mock_complete.return_value = "light-agent (95%): Turn on light"
        orch, dispatcher = _make_orchestrator()

        async def _slow_stream(_request):
            await asyncio.sleep(0.06)
            yield {"token": "Light ", "done": False}
            yield {"token": "is on.", "done": True}

        dispatcher.dispatch_stream = _slow_stream
        task = _make_task("turn on light", conversation_id="conv-filler")

        orch._should_send_filler = AsyncMock(return_value=True)
        orch._get_filler_threshold_ms = AsyncMock(return_value=50)
        orch._invoke_filler_agent = AsyncMock(return_value="One moment please.")

        chunks = []
        async for chunk in orch.handle_task_stream(task):
            chunks.append(chunk)

        filler_chunks = [c for c in chunks if c.get("filler_push")]
        assert len(filler_chunks) >= 1
        assert filler_chunks[0]["filler_push"] == "One moment please."

        # P0: with mediation inactive (no personality, no reminder) the
        # agent tokens relay immediately; the terminal chunk carries no
        # mediated_speech duplicate.
        relayed = [c for c in chunks if not c.get("done") and c.get("token")]
        assert [c["token"] for c in relayed] == ["Light ", "is on."]
        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        assert done_chunks[0].get("mediated_speech") is None

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streaming_token_processing_collects_speech(self, mock_complete, mock_track, mock_settings):
        """G14: Stream tokens must be collected; P0 relays them immediately
        when mediation is inactive, so the terminal chunk carries the action
        metadata but no mediated_speech duplicate."""
        mock_settings.get_value = AsyncMock(return_value="")
        mock_complete.return_value = "light-agent (95%): Turn on light"
        orch, dispatcher = _make_orchestrator()

        async def _token_stream(_request):
            yield {"token": "The ", "done": False}
            yield {"token": "light ", "done": False}
            yield {"token": "is on.", "done": True, "action_executed": {"service": "light/turn_on"}}

        dispatcher.dispatch_stream = _token_stream
        task = _make_task("turn on light", conversation_id="conv-tokens")
        orch._should_send_filler = AsyncMock(return_value=False)

        chunks = []
        async for chunk in orch.handle_task_stream(task):
            chunks.append(chunk)

        relayed = [c for c in chunks if not c.get("done") and c.get("token")]
        assert [c["token"] for c in relayed] == ["The ", "light ", "is on."]
        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        final = done_chunks[0]
        assert final.get("mediated_speech") is None
        assert final.get("action_executed") == {"service": "light/turn_on"}

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streaming_queue_reader_cancels_on_exception(self, mock_complete, mock_track, mock_settings):
        """G14: Queue reader task must be cancelled cleanly when stream raises."""
        mock_settings.get_value = AsyncMock(return_value="")
        mock_complete.return_value = "light-agent (95%): Turn on light"
        orch, dispatcher = _make_orchestrator()

        async def _broken_stream(_request):
            yield {"token": "The ", "done": False}
            raise RuntimeError("stream broke")

        # Set dispatch_stream on the underlying mock to return an async generator
        dispatcher.dispatch_stream = _broken_stream
        # Also need to bypass AsyncMock wrapping for the dispatch_stream attribute
        type(dispatcher).dispatch_stream = property(lambda self: _broken_stream)

        task = _make_task("turn on light", conversation_id="conv-broken")
        orch._should_send_filler = AsyncMock(return_value=True)
        orch._get_filler_threshold_ms = AsyncMock(return_value=50)
        orch._invoke_filler_agent = AsyncMock(return_value="One moment please.")

        chunks = []
        async for chunk in orch.handle_task_stream(task):
            chunks.append(chunk)

        # Should still yield a terminal chunk; filler may or may not be sent
        # depending on race, but the pipeline must not crash.
        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streaming_filler_cancelled_when_agent_answers_first(self, mock_complete, mock_track, mock_settings):
        """P1: the t=0 filler task is cancelled when the first agent chunk
        beats the threshold (no dangling filler LLM call, no filler_push)."""
        mock_settings.get_value = AsyncMock(return_value="")
        mock_complete.return_value = "light-agent (95%): Turn on light"
        orch, dispatcher = _make_orchestrator()

        async def _fast_stream(_request):
            yield {"token": "Light is on.", "done": True}

        dispatcher.dispatch_stream = _fast_stream
        task = _make_task("turn on light", conversation_id="conv-filler-cancel")

        filler_cancelled = asyncio.Event()

        async def _hanging_filler(user_text, agent_id, language):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                filler_cancelled.set()
                raise

        orch._should_send_filler = AsyncMock(return_value=True)
        orch._get_filler_threshold_ms = AsyncMock(return_value=5000)
        orch._invoke_filler_agent = AsyncMock(side_effect=_hanging_filler)

        chunks = []
        async for chunk in orch.handle_task_stream(task):
            chunks.append(chunk)

        filler_chunks = [c for c in chunks if c.get("filler_push")]
        assert len(filler_chunks) == 0
        assert filler_cancelled.is_set()
        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1


# ---------------------------------------------------------------------------
# M-8: sequential-send terminal chunk carries the bridge metadata
# ---------------------------------------------------------------------------


class TestSequentialSendStreamingMetadata:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    async def test_sequential_send_done_chunk_carries_bridge_metadata(self, mock_track, mock_settings):
        """M-8: sequential-send terminal chunk carries routed_to + action_executed."""
        mock_settings.get_value = AsyncMock(return_value="")
        orch, _dispatcher = _make_orchestrator()

        async def _mock_prelude(task, **kwargs):
            from app.agents.orchestrator import PipelinePreludeResult

            return PipelinePreludeResult(
                conversation_id=task.conversation_id or "conv-seq-meta",
                detected_language="en",
                lang_turns=[],
                span_collector=task.span_collector,
                classifications=[
                    ("general-agent", "Summarize", 0.95),
                    ("send-agent", "Send it", 0.95),
                ],
                routing_cached=False,
                target_agent="general-agent",
                condensed_task="Summarize",
                confidence=0.95,
                used_origin_context=False,
            )

        orch._run_pipeline_prelude = _mock_prelude
        orch._should_send_filler = AsyncMock(return_value=False)
        orch.handle_task = AsyncMock(
            return_value={
                "speech": "Sent your summary.",
                "routed_to": "general-agent, send-agent",
                "action_executed": {"action": "notify", "entity_id": "notify.telegram", "success": True},
                "voice_followup": False,
            }
        )

        task = _make_task("summarize and send", conversation_id="conv-seq-meta")
        chunks = [c async for c in orch.handle_task_stream(task)]

        assert chunks[0].get("status") == "sequential_send"
        done = chunks[-1]
        assert done["done"] is True
        assert done["routed_to"] == "general-agent, send-agent"
        assert done["action_executed"] == {"action": "notify", "entity_id": "notify.telegram", "success": True}


# ---------------------------------------------------------------------------
# CORE-M4: sequential-send filler race must not abandon the handle_task future
# ---------------------------------------------------------------------------


class TestSequentialSendFillerRace:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    async def test_cancel_mid_filler_cancels_detached_handle_task(self, mock_track, mock_settings):
        """CORE-M4: cancelling the stream mid-filler must cancel and await the
        detached handle_task future instead of abandoning it."""
        mock_settings.get_value = AsyncMock(return_value="")
        orch, _dispatcher = _make_orchestrator()

        async def _mock_prelude(task, **kwargs):
            from app.agents.orchestrator import PipelinePreludeResult

            return PipelinePreludeResult(
                conversation_id=task.conversation_id or "conv-seq-cancel",
                detected_language="en",
                lang_turns=[],
                span_collector=task.span_collector,
                classifications=[
                    ("light-agent", "Turn on light", 0.95),
                    ("send-agent", "Send it", 0.95),
                ],
                routing_cached=False,
                target_agent="light-agent",
                condensed_task="Turn on light",
                confidence=0.95,
                used_origin_context=False,
            )

        orch._run_pipeline_prelude = _mock_prelude
        orch._should_send_filler = AsyncMock(return_value=True)
        orch._get_filler_threshold_ms = AsyncMock(return_value=0)

        handle_task_cancelled = asyncio.Event()

        async def _hanging_handle_task(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                handle_task_cancelled.set()
                raise
            raise AssertionError("handle_task should have been cancelled")

        async def _hanging_filler(*args, **kwargs):
            await asyncio.Event().wait()
            return ""

        orch.handle_task = _hanging_handle_task
        orch._invoke_filler_agent = _hanging_filler

        task = _make_task("turn on light and send it", conversation_id="conv-seq-cancel")
        agen = orch.handle_task_stream(task)
        first = await agen.__anext__()
        assert first.get("status") == "sequential_send"

        consumer = asyncio.create_task(agen.__anext__())
        await asyncio.sleep(0.05)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

        assert handle_task_cancelled.is_set()
        await agen.aclose()


# ---------------------------------------------------------------------------
# P0: first-frame latency — relay when mediation is inactive, streaming default
# ---------------------------------------------------------------------------


class TestFirstFrameLatency:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streaming_mediation_is_default_on(self, mock_complete, mock_track, mock_settings):
        """P0: with no explicit setting, streaming mediation defaults to ON --
        a personality+reminder turn streams mediated tokens instead of using
        the blocking mediation call."""
        mock_complete.return_value = "light-agent (95%): Turn on light"
        mock_settings.get_value = AsyncMock(
            side_effect=lambda k, d=None: {
                # orchestrator.mediation_streaming_enabled deliberately absent:
                # the new default ("true") must kick in.
                "personality.prompt": "You are friendly.",
                "orchestrator.organic_followup_enabled": "false",
            }.get(k, d)
        )
        orch, dispatcher = _make_orchestrator()
        orch._calendar_injector = MagicMock()
        orch._calendar_injector.inject_reminders = AsyncMock(return_value="Meeting at 3pm.")
        orch._should_send_filler = AsyncMock(return_value=False)

        async def _stream(_request):
            yield {"token": "Light is on.", "done": True}

        dispatcher.dispatch_stream = _stream

        async def _fake_mediation_stream(**kwargs):
            yield "Friendly answer with reminder."

        orch._mediate_response_stream = _fake_mediation_stream
        orch._mediate_response = AsyncMock()

        task = _make_task("turn on light", conversation_id="conv-p0-default-on")
        chunks = [c async for c in orch.handle_task_stream(task)]

        token_chunks = [c for c in chunks if not c.get("done") and c.get("token")]
        assert [c["token"] for c in token_chunks] == ["Friendly answer with reminder."]
        # The blocking mediation path was NOT used.
        orch._mediate_response.assert_not_called()
        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        assert done_chunks[0].get("mediated_speech") is None

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_mediation_skipped_when_personality_and_reminder_empty(
        self, mock_complete, mock_track, mock_settings
    ):
        """P0: no personality and no reminder -> no mediation LLM call at all;
        the executor confirmation relays as an immediate token."""
        mock_complete.return_value = "light-agent (95%): Turn on light"
        mock_settings.get_value = AsyncMock(return_value="")
        orch, dispatcher = _make_orchestrator()
        orch._should_send_filler = AsyncMock(return_value=False)
        orch._mediate_response = AsyncMock()

        async def _fail_mediation_stream(**kwargs):
            raise AssertionError("mediation stream must not run on a mediation-inactive turn")
            yield  # pragma: no cover -- async generator shape

        orch._mediate_response_stream = _fail_mediation_stream

        async def _stream(_request):
            yield {"token": "Light is on.", "done": True}

        dispatcher.dispatch_stream = _stream

        task = _make_task("turn on light", conversation_id="conv-p0-no-mediation")
        chunks = [c async for c in orch.handle_task_stream(task)]

        token_chunks = [c for c in chunks if not c.get("done") and c.get("token")]
        assert [c["token"] for c in token_chunks] == ["Light is on."]
        orch._mediate_response.assert_not_called()
        # Only the classification LLM call happened (no mediation round-trip).
        assert mock_complete.await_count == 1
        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        assert done_chunks[0].get("mediated_speech") is None

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_personality_only_turn_streams_mediated_tokens(self, mock_complete, mock_track, mock_settings):
        """Personality WITHOUT reminder triggers mediation again (personality
        applies to all system responses): the raw agent tokens stay buffered
        and the streamed mediation output is relayed instead (streaming
        mediation defaults to ON)."""
        mock_complete.return_value = "light-agent (95%): Turn on light"
        mock_settings.get_value = AsyncMock(
            side_effect=lambda k, d=None: {
                "personality.prompt": "You are friendly.",
                "orchestrator.organic_followup_enabled": "false",
            }.get(k, d)
        )
        orch, dispatcher = _make_orchestrator()
        orch._should_send_filler = AsyncMock(return_value=False)
        orch._mediate_response = AsyncMock()

        async def _stream(request):
            yield {"token": "Light ", "done": False}
            yield {"token": "is on.", "done": True}

        dispatcher.dispatch_stream = _stream

        async def _fake_mediation_stream(**kwargs):
            yield "Friendly: light is on."

        orch._mediate_response_stream = _fake_mediation_stream

        task = _make_task("turn on light", conversation_id="conv-personality-only")
        chunks = [c async for c in orch.handle_task_stream(task)]

        # No raw agent tokens leaked downstream; the mediated tokens were
        # streamed instead.
        token_chunks = [c for c in chunks if not c.get("done") and c.get("token")]
        assert [c["token"] for c in token_chunks] == ["Friendly: light is on."]
        # The blocking mediation path was NOT used.
        orch._mediate_response.assert_not_called()
        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        assert done_chunks[0].get("mediated_speech") is None

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_reminder_only_mediation_still_weaves_followup(self, mock_complete, mock_track, mock_settings):
        """The mediation path still runs the LLM (reminder weaving) and still
        detects the [FOLLOWUP] tag -- here with streaming mediation disabled
        so the blocking path mediates."""
        mock_complete.side_effect = [
            "light-agent (95%): Turn on light",  # classify
            "Light is on. Meeting at 3pm. Should I dim it?[FOLLOWUP]",  # mediation
        ]
        mock_settings.get_value = AsyncMock(
            side_effect=lambda k, d=None: {
                "personality.prompt": "You are friendly.",
                "orchestrator.mediation_streaming_enabled": "false",
                "orchestrator.organic_followup_enabled": "false",
            }.get(k, d)
        )
        orch, dispatcher = _make_orchestrator()
        orch._calendar_injector = MagicMock()
        orch._calendar_injector.inject_reminders = AsyncMock(return_value="Meeting at 3pm.")
        orch._should_send_filler = AsyncMock(return_value=False)

        async def _stream(_request):
            yield {"token": "Light is on.", "done": True}

        dispatcher.dispatch_stream = _stream

        task = _make_task("turn on light", conversation_id="conv-p0-followup")
        chunks = [c async for c in orch.handle_task_stream(task)]

        # Mediation active: nothing relayed, the terminal frame carries the
        # mediated speech without the tag and asks for the followup.
        token_chunks = [c for c in chunks if not c.get("done") and c.get("token")]
        assert token_chunks == []
        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        final = done_chunks[0]
        assert final["mediated_speech"] == "Light is on. Meeting at 3pm. Should I dim it?"
        assert final.get("voice_followup") is True
        # The mediation LLM call received the reminder to weave in.
        mediation_messages = mock_complete.await_args_list[1].args[1]
        assert any("Meeting at 3pm." in m.get("content", "") for m in mediation_messages)

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_filler_precedes_relayed_tokens(self, mock_complete, mock_track, mock_settings):
        """P0 interplay: when the filler fires on a slow agent and mediation
        is inactive, the filler push is emitted BEFORE the relayed tokens."""
        mock_settings.get_value = AsyncMock(return_value="")
        mock_complete.return_value = "general-agent (95%): search the web"
        orch, dispatcher = _make_orchestrator()

        async def _slow_stream(_request):
            await asyncio.sleep(0.06)
            yield {"token": "Real ", "done": False}
            yield {"token": "answer.", "done": True}

        dispatcher.dispatch_stream = _slow_stream
        orch._should_send_filler = AsyncMock(return_value=True)
        orch._get_filler_threshold_ms = AsyncMock(return_value=50)
        orch._invoke_filler_agent = AsyncMock(return_value="One moment please.")

        task = _make_task("search something", conversation_id="conv-p0-filler-order")
        chunks = [c async for c in orch.handle_task_stream(task)]

        filler_idx = next(i for i, c in enumerate(chunks) if c.get("filler_push"))
        first_token_idx = next(i for i, c in enumerate(chunks) if not c.get("done") and c.get("token"))
        assert filler_idx < first_token_idx
        token_chunks = [c for c in chunks if not c.get("done") and c.get("token")]
        assert [c["token"] for c in token_chunks] == ["Real ", "answer."]
