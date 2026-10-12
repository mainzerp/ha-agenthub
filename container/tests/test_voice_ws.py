"""``/ws/voice`` protocol: one socket per Assist voice turn (speaker identification)."""

from __future__ import annotations

import asyncio
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from app.api.routes import voice as voice_routes
from app.voice import service as voice_service
from app.voice.profiles import ProfileCentroid, l2_normalize
from app.voice.service import SpeakerIdService, VoiceConfig

_MODEL = "test-model"


class _FakeEngine:
    model_id = _MODEL
    status = "ready"
    last_error = None

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.pcm_lengths: list[int] = []

    async def ensure_ready(self, num_threads: int = 2) -> bool:
        return True

    async def embed(self, pcm: bytes, sample_rate: int, *, num_threads: int = 2) -> np.ndarray:
        self.pcm_lengths.append(len(pcm))
        return np.array(self.vector, dtype=np.float32)


class _FakeCentroids:
    def __init__(self) -> None:
        self.centroids = [
            ProfileCentroid(1, "Anna", "user-anna", l2_normalize(np.array([1, 0, 0], dtype=np.float32))),
            ProfileCentroid(2, "Ben", None, l2_normalize(np.array([0, 1, 0], dtype=np.float32))),
        ]

    async def get(self, model: str) -> list[ProfileCentroid]:
        return self.centroids

    def invalidate(self) -> None:
        pass


def _config(enabled: bool = True) -> VoiceConfig:
    return VoiceConfig(
        enabled=enabled,
        threshold=0.55,
        margin=0.05,
        min_duration_s=1.5,
        min_enroll_duration_s=2.0,
        wait_ms=500,
        num_threads=2,
    )


def _text(payload: dict) -> dict:
    return {"type": "websocket.receive", "text": json.dumps(payload)}


def _binary(data: bytes) -> dict:
    return {"type": "websocket.receive", "bytes": data}


_DISCONNECT = {"type": "websocket.disconnect", "code": 1000}


def _start(conversation_id: str | None = "conv-1", **overrides) -> dict:
    payload = {
        "type": "start",
        "conversation_id": conversation_id,
        "language": "en",
        "sample_rate": 16000,
        "bit_rate": 16,
        "channels": 1,
        "codec": "pcm",
        "format": "wav",
    }
    payload.update(overrides)
    return _text(payload)


def _wav_header() -> bytes:
    fmt = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", 0)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _pcm(seconds: float) -> bytes:
    return b"\x10\x00" * int(16000 * seconds)


def _make_ws(messages: list[dict], ip: str = "10.7.0.1") -> MagicMock:
    ws = MagicMock()
    ws.headers = {}
    ws.client.host = ip
    ws.app.state.allowed_ws_origins = set()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    ws.receive = AsyncMock(side_effect=[*messages, _DISCONNECT])
    return ws


def _results(ws: MagicMock) -> list[dict]:
    return [call.args[0] for call in ws.send_json.await_args_list]


@pytest.fixture()
def service():
    svc = SpeakerIdService(engine=_FakeEngine([0.98, 0.05, 0.0]), centroids=_FakeCentroids())
    voice_service.set_speaker_service(svc)
    voice_routes.reset_active_voice_connections()
    yield svc
    voice_service.set_speaker_service(None)
    voice_routes.reset_active_voice_connections()


@pytest.fixture()
def enabled():
    with patch.object(voice_routes, "load_voice_config", AsyncMock(return_value=_config(True))):
        yield


@pytest.fixture()
def disabled():
    with patch.object(voice_routes, "load_voice_config", AsyncMock(return_value=_config(False))):
        yield


class TestVoiceWsProtocol:
    async def test_happy_path_identifies_and_stores_result(self, service, enabled):
        ws = _make_ws(
            [
                _start(),
                _binary(_wav_header() + _pcm(1.0)),
                _binary(_pcm(1.0)),
                _text({"type": "audio_end"}),
                _binary(_pcm(1.0)),  # ignored after audio_end
                _text({"type": "end", "transcript_chars": 17}),
            ]
        )
        await voice_routes.ws_voice(ws)

        ws.accept.assert_awaited_once()
        results = _results(ws)
        assert len(results) == 1
        result = results[0]
        assert result["type"] == "result"
        assert result["status"] == "identified"
        assert result["profile_id"] == 1 and result["name"] == "Anna" and result["ha_user_id"] == "user-anna"
        assert result["duration_s"] == pytest.approx(2.0)
        assert set(result) == {
            "type",
            "status",
            "profile_id",
            "name",
            "ha_user_id",
            "score",
            "margin",
            "duration_s",
            "reason",
        }
        # WAV header stripped, audio after audio_end ignored.
        assert service.engine.pcm_lengths == [64000]
        ws.close.assert_awaited_with(code=1000)

        lookup = await service.store.pop("conv-1", 0)
        assert lookup.found and lookup.result["status"] == "identified"
        assert voice_routes._active_connections == {}

    async def test_disabled_replies_immediately_and_stores_nothing(self, service, disabled):
        ws = _make_ws([_start(), _binary(_pcm(2.0)), _text({"type": "end"})])
        await voice_routes.ws_voice(ws)
        assert [r["status"] for r in _results(ws)] == ["disabled"]
        assert not service.store.has("conv-1")
        assert service.engine.pcm_lengths == []
        # A disabled feature is not a diagnosis event: the ring buffer stays empty.
        assert service.store.recent() == []

    async def test_unsupported_audio(self, service, enabled):
        ws = _make_ws([_start(sample_rate=8000), _binary(_pcm(2.0)), _text({"type": "end"})])
        await voice_routes.ws_voice(ws)
        assert [r["status"] for r in _results(ws)] == ["unsupported_audio"]
        assert not service.store.has("conv-1")
        # Recorded so the dashboard shows the pipeline sends an unusable format.
        assert [r["status"] for r in service.store.recent()] == ["unsupported_audio"]
        assert service.engine.pcm_lengths == []

    @pytest.mark.parametrize("overrides", [{"channels": 2}, {"bit_rate": 32}, {"codec": "opus"}, {"format": "ogg"}])
    async def test_other_unsupported_formats(self, service, enabled, overrides):
        ws = _make_ws([_start(**overrides), _text({"type": "end"})])
        await voice_routes.ws_voice(ws)
        assert _results(ws)[0]["status"] == "unsupported_audio"

    async def test_null_conversation_id(self, service, enabled):
        ws = _make_ws([_start(None), _binary(_pcm(2.0)), _text({"type": "end"})])
        await voice_routes.ws_voice(ws)
        results = _results(ws)
        assert results[0]["status"] == "error" and results[0]["reason"] == "no_conversation_id"
        assert service.engine.pcm_lengths == []

    async def test_overlong_conversation_id(self, service, enabled):
        ws = _make_ws([_start("x" * 65), _text({"type": "end"})])
        await voice_routes.ws_voice(ws)
        assert _results(ws)[0]["reason"] == "invalid_conversation_id"

    async def test_too_short(self, service, enabled):
        ws = _make_ws([_start(), _binary(_pcm(1.0)), _text({"type": "end"})])
        await voice_routes.ws_voice(ws)
        assert _results(ws)[0]["status"] == "too_short"
        assert (await service.store.pop("conv-1", 0)).result["status"] == "too_short"

    async def test_abort_discards_entry(self, service, enabled):
        ws = _make_ws([_start(), _binary(_pcm(2.0)), _text({"type": "abort"})])
        await voice_routes.ws_voice(ws)
        results = _results(ws)
        assert results[0]["status"] == "error" and results[0]["reason"] == "aborted"
        assert not service.store.has("conv-1")
        assert service.engine.pcm_lengths == []

    async def test_disconnect_without_end_uses_received_audio(self, service, enabled):
        ws = _make_ws([_start(), _binary(_pcm(2.0))])
        await voice_routes.ws_voice(ws)
        assert _results(ws) == []
        lookup = await service.store.pop("conv-1", 0)
        assert lookup.found and lookup.result["status"] == "identified"

    async def test_disconnect_without_audio_discards(self, service, enabled):
        ws = _make_ws([_start()])
        await voice_routes.ws_voice(ws)
        assert not service.store.has("conv-1")

    async def test_binary_before_start_is_protocol_error(self, service, enabled):
        ws = _make_ws([_binary(_pcm(1.0))])
        await voice_routes.ws_voice(ws)
        assert _results(ws)[0]["reason"] == "start_required"
        ws.close.assert_awaited_with(code=1008)

    async def test_invalid_json_is_protocol_error(self, service, enabled):
        ws = _make_ws([_start(), {"type": "websocket.receive", "text": "{not json"}])
        await voice_routes.ws_voice(ws)
        assert _results(ws)[0]["reason"] == "invalid_message"
        assert not service.store.has("conv-1")

    async def test_idle_timeout_finalises_received_audio(self, service, enabled, monkeypatch):
        monkeypatch.setattr(voice_routes, "_IDLE_TIMEOUT_S", 0.05)
        messages = [_start(), _binary(_pcm(2.0))]

        async def _receive():
            if messages:
                return messages.pop(0)
            await asyncio.sleep(10)

        ws = _make_ws([])
        ws.receive = _receive
        await voice_routes.ws_voice(ws)
        assert _results(ws)[0]["status"] == "identified"
        assert (await service.store.pop("conv-1", 0)).result["status"] == "identified"

    async def test_connection_limit_rejects_before_accept(self, service, enabled):
        ip = "10.7.0.9"
        voice_routes._active_connections[ip] = voice_routes._MAX_WS_CONNECTIONS_PER_IP
        ws = _make_ws([], ip=ip)
        await voice_routes.ws_voice(ws)
        ws.accept.assert_not_awaited()
        ws.close.assert_awaited_once_with(code=1008, reason="Connection limit exceeded")

    async def test_disallowed_origin_rejected(self, service, enabled):
        ws = _make_ws([_start()])
        ws.headers = {"origin": "https://evil.example"}
        ws.app.state.allowed_ws_origins = {"https://ha.local"}
        await voice_routes.ws_voice(ws)
        ws.close.assert_awaited_with(code=1008, reason="Origin not allowed")
        assert not service.store.has("conv-1")

    async def test_conversation_waits_for_result_in_flight(self, service, enabled):
        queue: asyncio.Queue[dict] = asyncio.Queue()
        ws = _make_ws([])
        ws.receive = queue.get
        session = asyncio.create_task(voice_routes.ws_voice(ws))
        await queue.put(_start())
        await queue.put(_binary(_pcm(2.0)))
        for _ in range(20):
            await asyncio.sleep(0)
            if service.store.has("conv-1"):
                break
        assert service.store.has("conv-1")
        waiter = asyncio.create_task(service.store.pop("conv-1", 2.0))
        await asyncio.sleep(0.01)
        await queue.put(_text({"type": "end"}))
        lookup = await asyncio.wait_for(waiter, 2.0)
        await asyncio.wait_for(session, 2.0)
        assert lookup.result is not None and lookup.result["status"] == "identified"
        assert lookup.waited_ms > 0

    async def test_legacy_transcript_field_is_tolerated(self, service, enabled):
        ws = _make_ws([_start(), _binary(_pcm(2.0)), _text({"type": "end", "transcript": "turn on the light"})])
        await voice_routes.ws_voice(ws)
        assert _results(ws)[0]["status"] == "identified"

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"type": "end", "transcript_chars": 12}, 12),
            ({"type": "end", "transcript": "hello"}, 5),
            ({"type": "end", "transcript_chars": 3, "transcript": "hello"}, 3),
            ({"type": "end", "transcript_chars": -1}, None),
            ({"type": "end", "transcript_chars": True}, None),
            ({"type": "end", "transcript_chars": "12"}, None),
            ({"type": "end"}, None),
        ],
    )
    def test_transcript_chars_parsing(self, payload, expected):
        assert voice_routes._transcript_chars(payload) == expected


async def _wait_for(predicate, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


class TestVoiceWsEnrollmentConfirmation:
    """While enrollment is armed, a sample is stored only after ``end`` confirms the turn."""

    async def _run_until_processed(self, service, messages: list[dict]):
        queue: asyncio.Queue[dict] = asyncio.Queue()
        ws = _make_ws([])
        ws.receive = queue.get
        session = asyncio.create_task(voice_routes.ws_voice(ws))
        for message in messages:
            await queue.put(message)
        await _wait_for(lambda: service.engine.pcm_lengths)
        await _wait_for(lambda: service.store.has("conv-1") and service.store._entries["conv-1"].event.is_set())
        return ws, queue, session

    async def test_sample_stored_only_after_end(self, service, enabled):
        service.enrollment.arm(1, "Anna", samples=2, timeout_s=60)
        add_sample = AsyncMock()
        with patch.object(voice_service.VoiceProfileRepository, "add_sample", add_sample):
            ws, queue, session = await self._run_until_processed(
                service, [_start(), _binary(_pcm(2.5)), _text({"type": "audio_end"})]
            )
            # Embedding computed at audio_end, but nothing persisted yet.
            add_sample.assert_not_awaited()
            assert service.enrollment.current()["remaining"] == 2
            assert service.store.recent() == []

            await queue.put(_text({"type": "end", "transcript_chars": 9}))
            await asyncio.wait_for(session, 2.0)

        add_sample.assert_awaited_once()
        assert add_sample.await_args.args[0] == 1
        assert service.enrollment.current()["remaining"] == 1
        result = _results(ws)[0]
        assert result["status"] == "enrolled_sample" and result["profile_id"] == 1
        assert [r["status"] for r in service.store.recent()] == ["enrolled_sample"]

    async def test_abort_discards_pending_sample(self, service, enabled):
        service.enrollment.arm(1, "Anna", samples=2, timeout_s=60)
        add_sample = AsyncMock()
        with patch.object(voice_service.VoiceProfileRepository, "add_sample", add_sample):
            ws, queue, session = await self._run_until_processed(
                service, [_start(), _binary(_pcm(2.5)), _text({"type": "audio_end"})]
            )
            await queue.put(_text({"type": "abort"}))
            await asyncio.wait_for(session, 2.0)

        add_sample.assert_not_awaited()
        assert service.enrollment.current()["remaining"] == 2
        assert _results(ws)[0]["reason"] == "aborted"

    async def test_disconnect_without_end_discards_pending_sample(self, service, enabled):
        service.enrollment.arm(1, "Anna", samples=2, timeout_s=60)
        add_sample = AsyncMock()
        with patch.object(voice_service.VoiceProfileRepository, "add_sample", add_sample):
            ws, queue, session = await self._run_until_processed(
                service, [_start(), _binary(_pcm(2.5)), _text({"type": "audio_end"})]
            )
            await queue.put(_DISCONNECT)
            await asyncio.wait_for(session, 2.0)

        add_sample.assert_not_awaited()
        assert service.enrollment.current()["remaining"] == 2
        assert _results(ws) == []

    async def test_idle_timeout_discards_pending_sample(self, service, enabled, monkeypatch):
        monkeypatch.setattr(voice_routes, "_IDLE_TIMEOUT_S", 0.05)
        service.enrollment.arm(1, "Anna", samples=2, timeout_s=60)
        messages = [_start(), _binary(_pcm(2.5)), _text({"type": "audio_end"})]

        async def _receive():
            if messages:
                return messages.pop(0)
            await asyncio.sleep(10)

        ws = _make_ws([])
        ws.receive = _receive
        add_sample = AsyncMock()
        with patch.object(voice_service.VoiceProfileRepository, "add_sample", add_sample):
            await voice_routes.ws_voice(ws)

        add_sample.assert_not_awaited()
        assert service.enrollment.current()["remaining"] == 2
        result = _results(ws)[0]
        assert result["status"] == "error" and result["reason"] == "enrollment_unconfirmed"

    async def test_enrollment_disarmed_before_end_stores_nothing(self, service, enabled):
        service.enrollment.arm(1, "Anna", samples=2, timeout_s=60)
        add_sample = AsyncMock()
        with patch.object(voice_service.VoiceProfileRepository, "add_sample", add_sample):
            ws, queue, session = await self._run_until_processed(
                service, [_start(), _binary(_pcm(2.5)), _text({"type": "audio_end"})]
            )
            service.enrollment.cancel()
            await queue.put(_text({"type": "end", "transcript_chars": 9}))
            await asyncio.wait_for(session, 2.0)

        add_sample.assert_not_awaited()
        result = _results(ws)[0]
        assert result["status"] == "error" and result["reason"] == "enrollment_not_armed"


def test_real_websocket_round_trip(service, enabled):
    """Route registration + real ASGI frames (text and binary) through Starlette."""
    from starlette.testclient import TestClient

    from app.security.auth import require_api_key_ws
    from tests.conftest import build_integration_test_app

    app = build_integration_test_app()
    app.dependency_overrides[require_api_key_ws] = lambda: "test-api-key"
    with TestClient(app) as client, client.websocket_connect("/ws/voice") as ws:
        ws.send_text(_start()["text"])
        ws.send_bytes(_wav_header() + _pcm(2.0))
        ws.send_text(json.dumps({"type": "end", "transcript": None}))
        result = ws.receive_json()
    assert result["type"] == "result"
    assert result["status"] == "identified"
    assert result["name"] == "Anna"
