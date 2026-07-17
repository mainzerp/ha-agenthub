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

        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        assert done_chunks[0].get("mediated_speech")

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streaming_token_processing_collects_speech(self, mock_complete, mock_track, mock_settings):
        """G14: Stream tokens must be collected into final mediated_speech."""
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

        done_chunks = [c for c in chunks if c.get("done")]
        assert len(done_chunks) == 1
        final = done_chunks[0]
        assert final["mediated_speech"] == "The light is on."
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
                    ("light-agent", "Turn on light", 0.95, []),
                    ("send-agent", "Send it", 0.95, []),
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
