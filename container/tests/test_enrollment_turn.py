"""Voice-enrollment turns are answered with a fixed reply instead of agent processing.

A satellite voice turn whose ``/ws/voice`` session started while an
enrollment was armed is only a sample: the conversation turn returns "OK"
(or "Not saved" for a rejected sample) right away, with no cache access, no
turn store, no classification, no dispatch, no filler and no trace rows.
Typed turns (no voice session entry) are processed normally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
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

import app.llm.client  # noqa: E402,F401 -- force module load for patch targets
import app.voice.service as voice_service  # noqa: E402
from app.agents.enrollment_turn import enrollment_reply  # noqa: E402
from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.analytics.tracer import SpanCollector, discard_trace  # noqa: E402
from app.api.routes import voice as voice_routes  # noqa: E402
from app.models.agent import AgentCard, IngressTask, TaskContext  # noqa: E402
from app.voice.profiles import ProfileCentroid, l2_normalize  # noqa: E402
from app.voice.service import (  # noqa: E402
    ENROLLMENT_OUTCOME_ACCEPTED,
    ENROLLMENT_OUTCOME_REJECTED,
    ENROLLMENT_OUTCOME_UNKNOWN,
    SpeakerIdService,
    VoiceConfig,
    consume_enrollment_turn,
    is_enrollment_turn,
    make_result,
)
from app.voice.turn_store import SpeakerTurnStore  # noqa: E402

pytestmark = pytest.mark.asyncio

_AGENTS = ("light-agent", "general-agent")


@pytest.fixture()
def speaker_service():
    svc = SpeakerIdService(engine=MagicMock(model_id="m", status="ready"))
    voice_service.set_speaker_service(svc)
    yield svc
    voice_service.set_speaker_service(None)


@pytest.fixture()
def armed(speaker_service):
    with patch.object(voice_service, "is_speaker_id_enabled", AsyncMock(return_value=True)):
        speaker_service.enrollment.arm(7, "Anna", samples=10, timeout_s=600)
        yield speaker_service


def _settings():
    values = {"language": "en", "personality.prompt": "", "orchestrator.organic_followup_enabled": "false"}

    async def _get_value(key, default=None):
        return values.get(key, default if default is not None else "")

    return _get_value


def _make_orchestrator() -> tuple[OrchestratorAgent, AsyncMock, MagicMock]:
    dispatcher = AsyncMock()
    registry = AsyncMock()
    cache_manager = MagicMock()
    cache_manager.apply_rewrite = AsyncMock()
    cache_manager.try_replay_action = AsyncMock(return_value=None)
    cache_manager.try_routing_skip = AsyncMock(return_value=None)
    cache_manager.store_action_async = AsyncMock()
    cache_manager.store_routing_async = AsyncMock()
    cache_manager.process = AsyncMock(return_value=None)
    registry.list_agents = AsyncMock(
        return_value=[AgentCard(agent_id=a, name=a, description="", skills=[]) for a in _AGENTS]
    )
    orch = OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=cache_manager)
    orch._should_send_filler = AsyncMock(return_value=False)
    orch._create_trace = AsyncMock()
    orch._store_turn = AsyncMock()
    return orch, dispatcher, cache_manager


def _task(conversation_id: str, language: str = "en", text: str = "the quick brown fox") -> IngressTask:
    task = IngressTask(
        description=text,
        conversation_id=conversation_id,
        context=TaskContext(language=language, source="ha"),
    )
    task.span_collector = SpanCollector(f"trace-{conversation_id}", source="ha")
    return task


def _voice_turn(svc: SpeakerIdService, conversation_id: str, result: dict | None, *, enrollment: bool = True) -> None:
    token = svc.store.begin(conversation_id, enrollment=enrollment)
    if result is not None:
        svc.store.resolve(conversation_id, token, result)


_ACCEPTED = make_result("enrolled_sample", duration_s=3.0, profile_id=7, name="Anna", score=0.8, compute_ms=20.0)
_REJECTED = make_result(
    "rejected_sample", duration_s=3.0, profile_id=7, name="Anna", reason="rejected_other_speaker", compute_ms=20.0
)


def _assert_untouched(orch: OrchestratorAgent, dispatcher: AsyncMock, cache_manager: MagicMock) -> None:
    cache_manager.try_replay_action.assert_not_awaited()
    cache_manager.try_routing_skip.assert_not_awaited()
    cache_manager.store_action_async.assert_not_called()
    cache_manager.store_routing_async.assert_not_called()
    cache_manager.process.assert_not_called()
    cache_manager.apply_rewrite.assert_not_called()
    orch._store_turn.assert_not_awaited()
    orch._create_trace.assert_not_awaited()
    orch._should_send_filler.assert_not_awaited()
    dispatcher.dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Turn store marking and the service helpers
# ---------------------------------------------------------------------------


class TestTurnStoreMarking:
    async def test_begin_marks_enrollment_turn(self):
        store = SpeakerTurnStore()
        store.begin("c1", enrollment=True)
        store.begin("c2")
        assert store.is_enrollment("c1") is True
        assert store.is_enrollment("c2") is False
        assert store.is_enrollment("missing") is False
        assert store.is_enrollment(None) is False

    async def test_newer_session_replaces_marking(self):
        store = SpeakerTurnStore()
        store.begin("c1", enrollment=True)
        store.begin("c1")
        assert store.is_enrollment("c1") is False

    async def test_consumed_entry_is_no_longer_marked(self):
        store = SpeakerTurnStore()
        store.begin("c1", enrollment=True)
        await store.pop("c1", 0)
        assert store.is_enrollment("c1") is False

    async def test_is_enrollment_turn_without_service(self):
        voice_service.set_speaker_service(None)
        assert is_enrollment_turn("c1") is False
        outcome = await consume_enrollment_turn("c1", 0)
        assert outcome.outcome == ENROLLMENT_OUTCOME_UNKNOWN

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (_ACCEPTED, ENROLLMENT_OUTCOME_ACCEPTED),
            (_REJECTED, ENROLLMENT_OUTCOME_REJECTED),
            (make_result("too_short", duration_s=0.5, reason="rejected_too_short"), ENROLLMENT_OUTCOME_REJECTED),
            (make_result("unavailable", reason="engine_not_ready"), ENROLLMENT_OUTCOME_REJECTED),
        ],
    )
    async def test_consume_classifies_outcome(self, speaker_service, result, expected):
        _voice_turn(speaker_service, "c1", result)
        outcome = await consume_enrollment_turn("c1", 0.5)
        assert outcome.outcome == expected
        assert outcome.status == result["status"]
        assert not speaker_service.store.has("c1")

    async def test_consume_timeout_is_unknown_and_removes_entry(self, speaker_service):
        _voice_turn(speaker_service, "c1", None)
        outcome = await consume_enrollment_turn("c1", 0.01)
        assert outcome.outcome == ENROLLMENT_OUTCOME_UNKNOWN
        assert not speaker_service.store.has("c1")

    @pytest.mark.parametrize(
        ("outcome", "language", "expected"),
        [
            (ENROLLMENT_OUTCOME_ACCEPTED, "en", "OK"),
            (ENROLLMENT_OUTCOME_ACCEPTED, "de", "OK"),
            (ENROLLMENT_OUTCOME_UNKNOWN, "de-DE", "OK"),
            (ENROLLMENT_OUTCOME_REJECTED, "de", "Nicht gespeichert"),
            (ENROLLMENT_OUTCOME_REJECTED, "de_AT", "Nicht gespeichert"),
            (ENROLLMENT_OUTCOME_REJECTED, "en-US", "Not saved"),
            (ENROLLMENT_OUTCOME_REJECTED, "fr", "Not saved"),
            (ENROLLMENT_OUTCOME_REJECTED, None, "Not saved"),
        ],
    )
    async def test_reply_table(self, outcome, language, expected):
        assert enrollment_reply(outcome, language) == expected


# ---------------------------------------------------------------------------
# Span suppression
# ---------------------------------------------------------------------------


class TestSpanDiscard:
    async def test_discard_drops_spans_and_flush_writes_nothing(self):
        collector = SpanCollector("trace-x", source="ha")
        async with collector.start_span("early"):
            pass
        collector.set_request_attribute("user_id", "u1")
        discard_trace(collector)
        async with collector.start_span("late"):
            pass
        collector.add_root_span({"span_id": "root", "trace_id": "trace-x", "span_name": "ws_turn"})
        assert collector.get_spans() == []
        with (
            patch("app.analytics.tracer.TraceSpanRepository.insert_batch", new_callable=AsyncMock) as insert,
            patch("app.analytics.tracer.TraceSummaryRepository.update_duration", new_callable=AsyncMock) as update,
        ):
            await collector.flush()
        insert.assert_not_awaited()
        update.assert_not_awaited()

    async def test_discard_trace_tolerates_plain_objects(self):
        discard_trace(None)
        discard_trace(object())


# ---------------------------------------------------------------------------
# /ws/voice marks the turn at start
# ---------------------------------------------------------------------------


def _ws_config() -> VoiceConfig:
    return VoiceConfig(
        enabled=True,
        threshold=0.55,
        margin=0.05,
        min_duration_s=1.5,
        min_enroll_duration_s=2.0,
        wait_ms=500,
        num_threads=2,
    )


def _ws_start(conversation_id: str) -> dict:
    return {
        "type": "start",
        "conversation_id": conversation_id,
        "language": "en",
        "sample_rate": 16000,
        "bit_rate": 16,
        "channels": 1,
        "codec": "pcm",
        "format": "wav",
    }


class TestVoiceSessionMarking:
    async def _start_session(self, svc: SpeakerIdService, conversation_id: str) -> bool:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        ws = MagicMock()
        ws.headers = {}
        ws.client.host = "10.7.0.9"
        ws.app.state.allowed_ws_origins = set()
        ws.accept = AsyncMock()
        ws.close = AsyncMock()
        ws.send_json = AsyncMock()
        ws.receive = queue.get
        voice_routes.reset_active_voice_connections()
        with patch.object(voice_routes, "load_voice_config", AsyncMock(return_value=_ws_config())):
            session = asyncio.create_task(voice_routes.ws_voice(ws))
            await queue.put({"type": "websocket.receive", "text": json.dumps(_ws_start(conversation_id))})
            for _ in range(200):
                if svc.store.has(conversation_id):
                    break
                await asyncio.sleep(0)
            assert svc.store.has(conversation_id)
            marked = svc.store.is_enrollment(conversation_id)
            await queue.put({"type": "websocket.receive", "text": json.dumps({"type": "abort"})})
            await asyncio.wait_for(session, 2.0)
        voice_routes.reset_active_voice_connections()
        return marked

    async def test_start_while_armed_marks_entry(self, speaker_service):
        speaker_service.enrollment.arm(7, "Anna", samples=3, timeout_s=60)
        assert await self._start_session(speaker_service, "conv-ws-armed") is True

    async def test_start_without_enrollment_does_not_mark(self, speaker_service):
        assert await self._start_session(speaker_service, "conv-ws-free") is False


# ---------------------------------------------------------------------------
# Orchestrator: blocking path
# ---------------------------------------------------------------------------


class TestBlockingPath:
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_accepted_sample_replies_ok_without_processing(self, mock_complete, mock_settings, armed, caplog):
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, dispatcher, cache_manager = _make_orchestrator()
        _voice_turn(armed, "conv-enroll-ok", _ACCEPTED)
        task = _task("conv-enroll-ok")

        with (
            caplog.at_level(logging.INFO, logger="app.agents.enrollment_turn"),
            patch("app.analytics.tracer.TraceSpanRepository.insert_batch", new_callable=AsyncMock) as insert,
        ):
            result = await orch.handle_task(task)
            await task.span_collector.flush()

        assert result["speech"] == "OK"
        assert result["routed_to"] == "orchestrator"
        assert result["voice_followup"] is False
        assert result["conversation_id"] == "conv-enroll-ok"
        _assert_untouched(orch, dispatcher, cache_manager)
        mock_complete.assert_not_awaited()
        assert task.span_collector.discarded is True
        assert task.span_collector.get_spans() == []
        insert.assert_not_awaited()
        assert not armed.store.has("conv-enroll-ok")
        assert "outcome=accepted" in caplog.text and "conv-enroll-ok" in caplog.text

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_rejected_sample_replies_not_saved_in_german(self, mock_complete, mock_settings, armed):
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, dispatcher, cache_manager = _make_orchestrator()
        _voice_turn(armed, "conv-enroll-de", _REJECTED)

        result = await orch.handle_task(_task("conv-enroll-de", language="de"))

        assert result["speech"] == "Nicht gespeichert"
        _assert_untouched(orch, dispatcher, cache_manager)

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_timeout_replies_ok(self, mock_complete, mock_settings, armed, monkeypatch):
        monkeypatch.setattr(voice_service, "ENROLLMENT_RESULT_WAIT_S", 0.01)
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, dispatcher, cache_manager = _make_orchestrator()
        _voice_turn(armed, "conv-enroll-slow", None)

        result = await orch.handle_task(_task("conv-enroll-slow", language="de"))

        assert result["speech"] == "OK"
        _assert_untouched(orch, dispatcher, cache_manager)
        assert not armed.store.has("conv-enroll-slow")

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_marked_turn_short_circuits_after_final_commit_disarmed(self, mock_complete, mock_settings, armed):
        """The final sample's commit disarms the enrollment before the
        conversation turn arrives: the turn is still an enrollment turn."""
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, dispatcher, cache_manager = _make_orchestrator()
        _voice_turn(armed, "conv-enroll-last", _ACCEPTED)
        armed.enrollment.cancel()
        assert armed.enrollment.current() is None

        result = await orch.handle_task(_task("conv-enroll-last"))

        assert result["speech"] == "OK"
        _assert_untouched(orch, dispatcher, cache_manager)

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_typed_turn_while_armed_is_processed_normally(self, mock_complete, mock_settings, armed):
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): turn on the lamp"
        orch, dispatcher, cache_manager = _make_orchestrator()
        dispatcher.dispatch = AsyncMock(return_value={"speech": "The lamp is on."})
        task = _task("conv-typed", text="turn on the lamp")

        result = await orch.handle_task(task)

        assert result["speech"] == "The lamp is on."
        mock_complete.assert_awaited()
        dispatcher.dispatch.assert_awaited()
        cache_manager.try_replay_action.assert_awaited()
        orch._create_trace.assert_awaited()
        assert task.span_collector.discarded is False

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_unmarked_voice_turn_is_processed_normally(self, mock_complete, mock_settings, speaker_service):
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        mock_complete.return_value = "light-agent (95%): turn on the lamp"
        orch, dispatcher, _cache_manager = _make_orchestrator()
        dispatcher.dispatch = AsyncMock(return_value={"speech": "The lamp is on."})
        _voice_turn(speaker_service, "conv-voice", make_result("unknown", duration_s=2.0), enrollment=False)

        await orch.handle_task(_task("conv-voice", text="turn on the lamp"))

        dispatcher.dispatch.assert_awaited()
        orch._create_trace.assert_awaited()


# ---------------------------------------------------------------------------
# Orchestrator: streaming path
# ---------------------------------------------------------------------------


class TestStreamingPath:
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streamed_enrollment_turn_replies_ok(self, mock_complete, mock_track, mock_settings, armed):
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, dispatcher, cache_manager = _make_orchestrator()
        dispatcher.dispatch_stream = MagicMock(side_effect=AssertionError("dispatch must not run"))
        _voice_turn(armed, "conv-enroll-stream", _ACCEPTED)
        task = _task("conv-enroll-stream")

        chunks = [c async for c in orch.handle_task_stream(task)]

        assert len(chunks) == 1
        done = chunks[0]
        assert done["done"] is True
        assert done["token"] == "OK" and done["mediated_speech"] == "OK"
        assert done["routed_to"] == "orchestrator"
        assert not done.get("voice_followup")
        assert not done.get("action_executed")
        _assert_untouched(orch, dispatcher, cache_manager)
        mock_complete.assert_not_awaited()
        mock_track.assert_not_awaited()
        assert task.span_collector.discarded is True

    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streamed_rejected_sample_in_german(self, mock_complete, mock_track, mock_settings, armed):
        mock_settings.get_value = AsyncMock(side_effect=_settings())
        orch, dispatcher, cache_manager = _make_orchestrator()
        _voice_turn(armed, "conv-enroll-stream-de", _REJECTED)

        chunks = [c async for c in orch.handle_task_stream(_task("conv-enroll-stream-de", language="de-DE"))]

        assert chunks[-1]["mediated_speech"] == "Nicht gespeichert"
        _assert_untouched(orch, dispatcher, cache_manager)


# ---------------------------------------------------------------------------
# Rejection log carries the guard scores
# ---------------------------------------------------------------------------


class _FakeEngine:
    model_id = "test-model"
    status = "ready"

    async def embed(self, pcm: bytes, sample_rate: int, *, num_threads: int = 2) -> np.ndarray:
        return np.array([0.98, 0.05, 0.0], dtype=np.float32)


class _FakeCentroids:
    async def get(self, model: str) -> list[ProfileCentroid]:
        return [
            ProfileCentroid(1, "Anna", None, l2_normalize(np.array([1, 0, 0], dtype=np.float32))),
            ProfileCentroid(2, "Ben", None, l2_normalize(np.array([0, 1, 0], dtype=np.float32))),
        ]

    def invalidate(self) -> None:
        pass


async def test_rejection_log_includes_guard_scores(caplog):
    svc = SpeakerIdService(engine=_FakeEngine(), centroids=_FakeCentroids())
    svc.enrollment.arm(2, "Ben", samples=3, timeout_s=60)
    with caplog.at_level(logging.INFO, logger="app.voice.service"):
        analysis = await svc.analyze_turn(b"\x10\x00" * 16000 * 3, 16000, _ws_config())
    assert analysis.rejection is not None
    line = next(r.getMessage() for r in caplog.records if "Rejected voice enrollment sample" in r.getMessage())
    assert "rejected_other_speaker" in line
    for field in ("own=", "best_other=", "best_other_profile=1", "lead=", "speech=", "threshold=0.55", "margin=0.05"):
        assert field in line
