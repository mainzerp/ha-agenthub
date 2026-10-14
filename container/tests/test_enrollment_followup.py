"""Voice follow-ups are suppressed while a voice-profile enrollment is armed.

A follow-up turn reopens the microphone without a wake word right after TTS,
so during enrollment it would record the assistant's own voice tail as a
sample. Covers the shared decision helper, the blocking, streaming and
multi-agent finalization paths, the organic follow-up hint, and the
disabled/unavailable speaker-service fallbacks.
"""

from __future__ import annotations

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
import app.voice.service as voice_service  # noqa: E402
from app.agents.followup import (  # noqa: E402
    FOLLOWUP_SUPPRESSED_ATTR,
    FOLLOWUP_SUPPRESSED_ENROLLMENT,
    organic_followup_allowed,
    resolve_voice_followup,
)
from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.agents.pipeline_strategies import DefaultFinalizationStrategy  # noqa: E402
from app.agents.task_pipeline import DispatchResult  # noqa: E402
from app.analytics.tracer import SpanCollector  # noqa: E402
from app.models.agent import AgentCard, IngressTask, TaskContext  # noqa: E402
from app.voice.service import SpeakerIdService, enrollment_active  # noqa: E402

pytestmark = pytest.mark.asyncio

_AGENTS = ("light-agent", "general-agent", "music-agent")


@pytest.fixture()
def speaker_service():
    svc = SpeakerIdService(engine=MagicMock(model_id="m", status="ready"))
    voice_service.set_speaker_service(svc)
    yield svc
    voice_service.set_speaker_service(None)


@pytest.fixture()
def speaker_id_enabled():
    with patch.object(voice_service, "is_speaker_id_enabled", AsyncMock(return_value=True)) as mock_enabled:
        yield mock_enabled


@pytest.fixture()
def armed(speaker_service, speaker_id_enabled):
    speaker_service.enrollment.arm(7, "Anna", samples=10, timeout_s=600)
    return speaker_service


def _settings(**extra: str):
    values = {"language": "en", "personality.prompt": "", "orchestrator.organic_followup_enabled": "false", **extra}

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
    orch._create_trace = AsyncMock()
    return orch, dispatcher


def _task(text: str, conversation_id: str, source: str = "ha") -> IngressTask:
    task = IngressTask(
        description=text,
        conversation_id=conversation_id,
        context=TaskContext(language="en", source=source),
    )
    task.span_collector = SpanCollector(f"trace-{conversation_id}", source="ha")
    return task


def _return_span(task: IngressTask) -> dict:
    spans = [s for s in task.span_collector.get_spans() if s.get("span_name") == "return"]
    assert len(spans) == 1
    return spans[0]["metadata"]


# ---------------------------------------------------------------------------
# Enrollment state check and the shared decision helper
# ---------------------------------------------------------------------------


class TestEnrollmentActive:
    async def test_no_service_is_inactive(self):
        voice_service.set_speaker_service(None)
        assert await enrollment_active() is False

    async def test_not_armed_is_inactive(self, speaker_service, speaker_id_enabled):
        assert await enrollment_active() is False
        speaker_id_enabled.assert_not_awaited()

    async def test_armed_and_enabled_is_active(self, armed):
        assert await enrollment_active() is True

    async def test_armed_but_disabled_is_inactive(self, speaker_service):
        speaker_service.enrollment.arm(7, "Anna")
        with patch.object(voice_service, "is_speaker_id_enabled", AsyncMock(return_value=False)):
            assert await enrollment_active() is False

    async def test_state_failure_is_inactive(self, speaker_service, speaker_id_enabled):
        speaker_service.enrollment.current = MagicMock(side_effect=RuntimeError("boom"))
        assert await enrollment_active() is False

    async def test_cancelled_enrollment_is_inactive(self, armed):
        armed.enrollment.cancel()
        assert await enrollment_active() is False


class TestResolveVoiceFollowup:
    async def test_armed_suppresses_and_records_trace(self, armed):
        collector = SpanCollector("trace-helper", source="ha")
        metadata: dict = {}
        assert await resolve_voice_followup(True, "ha", span_collector=collector, span_metadata=metadata) is False
        assert metadata[FOLLOWUP_SUPPRESSED_ATTR] == FOLLOWUP_SUPPRESSED_ENROLLMENT
        assert collector.request_attributes[FOLLOWUP_SUPPRESSED_ATTR] == FOLLOWUP_SUPPRESSED_ENROLLMENT

    async def test_armed_without_followup_records_nothing(self, armed):
        metadata: dict = {}
        assert await resolve_voice_followup(False, "ha", span_metadata=metadata) is False
        assert metadata == {}

    async def test_text_source_never_follows_up(self, armed):
        metadata: dict = {}
        assert await resolve_voice_followup(True, "openai", span_metadata=metadata) is False
        assert metadata == {}

    async def test_not_armed_keeps_followup(self, speaker_service, speaker_id_enabled):
        metadata: dict = {}
        assert await resolve_voice_followup(True, "ha", span_metadata=metadata) is True
        assert metadata == {}

    async def test_no_service_keeps_followup(self):
        voice_service.set_speaker_service(None)
        assert await resolve_voice_followup(True, "ha") is True

    async def test_organic_followup_blocked_while_armed(self, armed):
        collector = SpanCollector("trace-organic", source="ha")
        assert await organic_followup_allowed(collector) is False
        assert collector.request_attributes[FOLLOWUP_SUPPRESSED_ATTR] == FOLLOWUP_SUPPRESSED_ENROLLMENT

    async def test_organic_followup_allowed_without_enrollment(self, speaker_service, speaker_id_enabled):
        assert await organic_followup_allowed(None) is True


# ---------------------------------------------------------------------------
# Blocking path
# ---------------------------------------------------------------------------


class TestBlockingPath:
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_agent_clarification_does_not_reopen_mic_while_armed(self, mock_complete, mock_settings, armed):
        """The clarifying question is still spoken and kept as pending question,
        but the result requests no voice follow-up; the trace records why."""
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): turn on the lamp"
        orch, dispatcher = _make_orchestrator()
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Which lamp do you mean?", "voice_followup": True})
        task = _task("turn on the lamp", "conv-enroll-block")

        result = await orch.handle_task(task)

        assert result["speech"] == "Which lamp do you mean?"
        assert result["voice_followup"] is False
        assert orch._create_trace.await_args.kwargs["voice_followup"] is False
        ret = _return_span(task)
        assert ret["voice_followup"] is False
        assert ret[FOLLOWUP_SUPPRESSED_ATTR] == FOLLOWUP_SUPPRESSED_ENROLLMENT
        assert task.span_collector.request_attributes[FOLLOWUP_SUPPRESSED_ATTR] == FOLLOWUP_SUPPRESSED_ENROLLMENT
        assert orch._conversation_manager.pop_pending_question("conv-enroll-block") is not None

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_agent_clarification_reopens_mic_without_enrollment(
        self, mock_complete, mock_settings, speaker_service, speaker_id_enabled
    ):
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): turn on the lamp"
        orch, dispatcher = _make_orchestrator()
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Which lamp do you mean?", "voice_followup": True})
        task = _task("turn on the lamp", "conv-no-enroll-block")

        result = await orch.handle_task(task)

        assert result["voice_followup"] is True
        assert FOLLOWUP_SUPPRESSED_ATTR not in _return_span(task)
        assert FOLLOWUP_SUPPRESSED_ATTR not in task.span_collector.request_attributes

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_speaker_service_unavailable_keeps_followup(self, mock_complete, mock_settings):
        voice_service.set_speaker_service(None)
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): turn on the lamp"
        orch, dispatcher = _make_orchestrator()
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Which lamp do you mean?", "voice_followup": True})

        result = await orch.handle_task(_task("turn on the lamp", "conv-no-service"))

        assert result["voice_followup"] is True

    @patch("app.agents.orchestrator.random.random", return_value=0.01)
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_mediation_gets_no_organic_followup_while_armed(self, mock_complete, mock_settings, _random, armed):
        """An organic follow-up roll that would win is dropped, and a mediated
        [FOLLOWUP] question does not reopen the mic."""
        mock_settings.get_value = AsyncMock(
            side_effect=_settings(
                **{
                    "personality.prompt": "You are friendly.",
                    "orchestrator.organic_followup_enabled": "true",
                    "orchestrator.organic_followup_probability": "0.5",
                }
            )
        )
        mock_complete.return_value = "light-agent (95%): turn on the lamp"
        orch, dispatcher = _make_orchestrator()
        dispatcher.dispatch = AsyncMock(return_value={"speech": "The lamp is on."})
        orch._mediate_response = AsyncMock(return_value=("The lamp is on. Anything else?", True))
        task = _task("turn on the lamp", "conv-enroll-organic")

        result = await orch.handle_task(task)

        assert orch._mediate_response.await_args.kwargs["allow_organic_followup"] is False
        assert result["voice_followup"] is False
        assert _return_span(task)[FOLLOWUP_SUPPRESSED_ATTR] == FOLLOWUP_SUPPRESSED_ENROLLMENT

    @patch("app.agents.orchestrator.random.random", return_value=0.01)
    @patch("app.agents.orchestrator.SettingsRepository")
    async def test_organic_roll_unchanged_without_enrollment(
        self, mock_settings, _random, speaker_service, speaker_id_enabled
    ):
        mock_settings.get_value = AsyncMock(
            side_effect=_settings(
                **{"orchestrator.organic_followup_enabled": "true", "orchestrator.organic_followup_probability": "0.5"}
            )
        )
        orch, _dispatcher = _make_orchestrator()
        _reminder, allow = await orch._prepare_mediation_inputs(
            _task("hi", "conv-organic-free"), has_error=False, language="en"
        )
        assert allow is True


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------


class TestStreamingPath:
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streamed_mediation_followup_suppressed_while_armed(
        self, mock_complete, mock_track, mock_settings, armed
    ):
        mock_complete.return_value = "light-agent (95%): turn on light"
        mock_settings.get_value = AsyncMock(side_effect=_settings(**{"personality.prompt": "You are friendly."}))
        orch, dispatcher = _make_orchestrator()

        async def _stream(_request):
            yield {"token": "Light is on.", "done": True}

        dispatcher.dispatch_stream = _stream

        async def _fake_mediation_stream(**kwargs):
            yield "Light is on. Want more?"
            yield "[FOLLOWUP]"

        orch._mediate_response_stream = _fake_mediation_stream
        task = _task("turn on light", "conv-enroll-stream")

        chunks = [c async for c in orch.handle_task_stream(task)]

        done = [c for c in chunks if c.get("done")]
        assert len(done) == 1
        assert not done[0].get("voice_followup")
        spoken = "".join(c.get("token", "") for c in chunks)
        assert "FOLLOWUP" not in spoken
        assert orch._create_trace.await_args.kwargs["voice_followup"] is False
        # The streamed-mediation finalization has no return span: the root span carries it.
        assert task.span_collector.request_attributes[FOLLOWUP_SUPPRESSED_ATTR] == FOLLOWUP_SUPPRESSED_ENROLLMENT
        assert orch._conversation_manager.pop_pending_question("conv-enroll-stream") is not None

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streamed_agent_clarification_suppressed_while_armed(
        self, mock_complete, mock_track, mock_settings, armed
    ):
        mock_complete.return_value = "light-agent (95%): turn on the lamp"
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, dispatcher = _make_orchestrator()

        async def _stream(_request):
            yield {"token": "Which lamp do you mean?", "done": True, "voice_followup": True}

        dispatcher.dispatch_stream = _stream
        task = _task("turn on the lamp", "conv-enroll-stream-agent")

        chunks = [c async for c in orch.handle_task_stream(task)]

        done = [c for c in chunks if c.get("done")]
        assert len(done) == 1
        assert not done[0].get("voice_followup")
        assert _return_span(task)[FOLLOWUP_SUPPRESSED_ATTR] == FOLLOWUP_SUPPRESSED_ENROLLMENT

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streamed_agent_clarification_kept_without_enrollment(
        self, mock_complete, mock_track, mock_settings, speaker_service, speaker_id_enabled
    ):
        mock_complete.return_value = "light-agent (95%): turn on the lamp"
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, dispatcher = _make_orchestrator()

        async def _stream(_request):
            yield {"token": "Which lamp do you mean?", "done": True, "voice_followup": True}

        dispatcher.dispatch_stream = _stream
        task = _task("turn on the lamp", "conv-free-stream-agent")

        chunks = [c async for c in orch.handle_task_stream(task)]

        done = [c for c in chunks if c.get("done")]
        assert done[0].get("voice_followup") is True
        assert FOLLOWUP_SUPPRESSED_ATTR not in _return_span(task)


# ---------------------------------------------------------------------------
# Multi-agent merge path
# ---------------------------------------------------------------------------


class TestMultiAgentPath:
    async def test_merged_followup_suppressed_while_armed(self, armed):
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
        task = _task("light and music", "conv-enroll-multi")
        response = await strategy.execute(
            task,
            dispatch_result,
            "light and music",
            "en",
            "conv-enroll-multi",
            [],
            task.span_collector,
            dispatch_result.classifications,
            False,
            False,
        )
        assert response["voice_followup"] is False
        conv.set_pending_question.assert_called_once()
        assert _return_span(task)[FOLLOWUP_SUPPRESSED_ATTR] == FOLLOWUP_SUPPRESSED_ENROLLMENT
