"""WebSocket ingress for Assist voice-turn audio (speaker identification).

One connection per voice turn. Protocol (JSON text frames + binary frames):

- client -> ``{"type": "start", "conversation_id": str | null, "language": str,
  "sample_rate": 16000, "bit_rate": 16, "channels": 1, "codec": "pcm", "format": "wav"}``
- client -> binary frames: raw PCM s16le (a leading RIFF/WAV header is skipped)
- client -> ``{"type": "audio_end"}`` (optional: start embedding now)
- client -> ``{"type": "end", "transcript_chars": int | null}`` confirms the
  turn (the STT produced a non-empty transcript), or ``{"type": "abort"}``
  discards it. A legacy ``transcript`` string is tolerated, never required.
- server -> ``{"type": "result", "status": ..., "profile_id", "name", "ha_user_id",
  "score", "margin", "duration_s", "speech_s", "vad", "scores", "second_name",
  "second_score", "reason", "compute_ms", "latency_ms"}`` exactly once, then a
  normal close. ``compute_ms`` (voice-activity trimming, embedding and
  matching) and ``latency_ms`` (``audio_end`` -- or the frame/disconnect that
  triggered the analysis -- until the result is available to the
  conversation) are null when nothing was computed. ``speech_s`` is the
  speech the VAD kept (``vad`` false: the whole turn was embedded),
  ``scores`` every profile's score (best first). ``name`` is null for
  ``unknown``.

A status known at ``start`` (``disabled``, ``unsupported_audio``, or
``error`` with reason ``no_conversation_id`` / ``invalid_conversation_id``)
is sent immediately; the server then drains frames until ``end``/``abort``/
disconnect and closes. The result is CONTEXT ONLY for the conversation turn
with the same ``conversation_id`` -- it never authenticates a user.

While an enrollment is armed, the embedding may be computed at
``audio_end``, but the sample is stored -- or a rejected sample counted --
only after ``end``; ``abort``, disconnect or idle timeout before ``end``
discard it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.middleware.rate_limit import get_client_ip_from_headers
from app.security.auth import require_api_key_ws
from app.voice.audio import (
    SUPPORTED_BIT_RATE,
    SUPPORTED_CHANNELS,
    SUPPORTED_CODEC,
    SUPPORTED_SAMPLE_RATE,
    PcmBuffer,
)
from app.voice.service import (
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_UNSUPPORTED_AUDIO,
    PendingEnrollment,
    SpeakerIdService,
    VoiceConfig,
    get_speaker_service,
    load_voice_config,
    make_result,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])

# Per-IP concurrent voice-turn sockets (one socket per turn; several
# satellites may stream at once).
_MAX_WS_CONNECTIONS_PER_IP = 8
_MAX_WS_TRACKED_IPS = 10_000
# Control frames are tiny JSON objects.
_MAX_TEXT_FRAME_BYTES = 4096
# A socket without any frame for this long is finalised and closed.
_IDLE_TIMEOUT_S = 30.0
_MAX_CONVERSATION_ID_LENGTH = 64
_ACCEPTED_FORMATS = frozenset({"wav", "raw", "pcm"})

_active_connections: dict[str, int] = {}
_active_lock: asyncio.Lock | None = None
_active_lock_loop_id: int | None = None


def _get_active_lock() -> asyncio.Lock:
    global _active_lock, _active_lock_loop_id
    loop = asyncio.get_running_loop()
    if _active_lock is None or _active_lock_loop_id != id(loop):
        _active_lock = asyncio.Lock()
        _active_lock_loop_id = id(loop)
    return _active_lock


def reset_active_voice_connections() -> None:
    """Reset the per-IP voice socket counter (startup and tests)."""
    _active_connections.clear()


def _client_ip(websocket: WebSocket) -> str:
    direct = websocket.client.host if websocket.client else "unknown"
    return get_client_ip_from_headers(websocket.headers, direct)


def _audio_format_supported(data: dict[str, Any]) -> bool:
    try:
        sample_rate = int(data.get("sample_rate", SUPPORTED_SAMPLE_RATE))
        bit_rate = int(data.get("bit_rate", SUPPORTED_BIT_RATE))
        channels = int(data.get("channels", SUPPORTED_CHANNELS))
    except (TypeError, ValueError):
        return False
    codec = str(data.get("codec") or SUPPORTED_CODEC).strip().lower()
    fmt = str(data.get("format") or "wav").strip().lower()
    return (
        sample_rate == SUPPORTED_SAMPLE_RATE
        and bit_rate == SUPPORTED_BIT_RATE
        and channels == SUPPORTED_CHANNELS
        and codec == SUPPORTED_CODEC
        and fmt in _ACCEPTED_FORMATS
    )


@router.websocket("/ws/voice")
async def ws_voice(
    websocket: WebSocket,
    _: str = Depends(require_api_key_ws),
):
    """Voice-turn audio stream for speaker identification (one socket per turn)."""
    client_ip = _client_ip(websocket)
    async with _get_active_lock():
        current = _active_connections.get(client_ip, 0)
        if current >= _MAX_WS_CONNECTIONS_PER_IP:
            logger.warning("Rejected voice WebSocket handshake from %s: connection limit exceeded", client_ip)
            await websocket.close(code=1008, reason="Connection limit exceeded")
            return
        if len(_active_connections) >= _MAX_WS_TRACKED_IPS and client_ip not in _active_connections:
            _active_connections.pop(next(iter(_active_connections)), None)
        _active_connections[client_ip] = current + 1
    try:
        await websocket.accept()
        origin = websocket.headers.get("origin")
        allowed: set[str] = getattr(websocket.app.state, "allowed_ws_origins", set())
        if origin and (not allowed or origin not in allowed):
            logger.warning("Rejected voice WebSocket connection from disallowed origin: %s", origin)
            await websocket.close(code=1008, reason="Origin not allowed")
            return
        await VoiceTurnSession(websocket, get_speaker_service()).run()
    except WebSocketDisconnect:
        logger.debug("Voice WebSocket client disconnected")
    finally:
        async with _get_active_lock():
            remaining = _active_connections.get(client_ip, 0) - 1
            if remaining > 0:
                _active_connections[client_ip] = remaining
            else:
                _active_connections.pop(client_ip, None)


def _transcript_chars(data: dict[str, Any]) -> int | None:
    """Transcript length from an ``end`` frame (``transcript_chars``, or a legacy ``transcript``)."""
    chars = data.get("transcript_chars")
    if isinstance(chars, int) and not isinstance(chars, bool) and chars >= 0:
        return chars
    transcript = data.get("transcript")
    if isinstance(transcript, str):
        return len(transcript)
    return None


def _format_ms(value: Any) -> str:
    return f"{value:.1f}ms" if isinstance(value, (int, float)) else "-"


def _format_s(value: Any) -> str:
    return f"{value:.2f}s" if isinstance(value, (int, float)) else "-"


class VoiceTurnSession:
    """State machine for one ``/ws/voice`` connection (one voice turn)."""

    def __init__(self, websocket: WebSocket, service: SpeakerIdService) -> None:
        self._ws = websocket
        self._service = service
        self._buffer = PcmBuffer()
        self._started = False
        self._audio_ended = False
        # Set when the result was decided at ``start`` (already sent).
        self._terminal = False
        self._result_sent = False
        self._conversation_id: str | None = None
        self._token: int | None = None
        self._resolved = False
        self._config: VoiceConfig | None = None
        self._processing: asyncio.Task[dict[str, Any]] | None = None
        # ``time.perf_counter()`` when the analysis was triggered (latency_ms origin).
        self._received_at: float | None = None
        self._pending_enrollment: PendingEnrollment | None = None
        # A rejected enrollment sample, recorded only when ``end`` confirms the turn.
        self._pending_rejection: dict[str, Any] | None = None
        self._transcript_chars: int | None = None

    async def run(self) -> None:
        try:
            await self._loop()
        finally:
            await self._cleanup()

    async def _loop(self) -> None:
        while True:
            try:
                message = await asyncio.wait_for(self._ws.receive(), timeout=_IDLE_TIMEOUT_S)
            except TimeoutError:
                logger.debug("Voice WebSocket idle timeout (conversation=%s)", self._conversation_id)
                await self._finish_without_end(send_result=True, reason="idle_timeout")
                await self._close()
                return
            if message.get("type") == "websocket.disconnect":
                await self._finish_without_end(send_result=False, reason="disconnected")
                return
            chunk = message.get("bytes")
            if chunk is not None:
                if not self._started:
                    await self._protocol_error("start_required")
                    return
                self._on_audio(chunk)
                continue
            text = message.get("text")
            if text is None:
                continue
            if len(text) > _MAX_TEXT_FRAME_BYTES:
                await self._protocol_error("message_too_large")
                return
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if not isinstance(data, dict):
                await self._protocol_error("invalid_message")
                return
            msg_type = data.get("type")
            if msg_type == "start":
                if self._started:
                    await self._protocol_error("duplicate_start")
                    return
                await self._on_start(data)
            elif not self._started:
                await self._protocol_error("start_required")
                return
            elif msg_type == "audio_end":
                self._on_audio_end()
            elif msg_type == "end":
                self._transcript_chars = _transcript_chars(data)
                await self._on_end()
                return
            elif msg_type == "abort":
                await self._on_abort()
                return
            else:
                logger.debug("Ignoring unknown voice WebSocket message type %r", msg_type)

    # -- message handlers ----------------------------------------------

    async def _on_start(self, data: dict[str, Any]) -> None:
        self._started = True
        config = await load_voice_config()
        if not config.enabled:
            await self._send_terminal(make_result(STATUS_DISABLED))
            return
        raw_id = data.get("conversation_id")
        if raw_id is None:
            await self._send_terminal(make_result(STATUS_ERROR, reason="no_conversation_id"))
            return
        conversation_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not conversation_id or len(conversation_id) > _MAX_CONVERSATION_ID_LENGTH:
            await self._send_terminal(make_result(STATUS_ERROR, reason="invalid_conversation_id"))
            return
        if not _audio_format_supported(data):
            result = make_result(STATUS_UNSUPPORTED_AUDIO)
            # Diagnosis aid: the dashboard shows that the pipeline sends a
            # format the engine cannot use. ``disabled`` is never recorded.
            self._service.store.record_recent(result)
            await self._send_terminal(result)
            return
        self._config = config
        self._conversation_id = conversation_id
        # Register before any audio so the conversation request can wait. A
        # turn started while an enrollment is armed is marked here, once: the
        # conversation side answers it with a short fixed reply instead of
        # running the agents, even if the final sample's commit disarms the
        # enrollment before the conversation turn arrives.
        enrollment_turn = self._service.enrollment.current() is not None
        self._token = self._service.store.begin(conversation_id, enrollment=enrollment_turn)
        if self._service.engine.status != "ready":
            self._service.schedule_warm_up()

    def _on_audio(self, chunk: bytes) -> None:
        if self._terminal or self._audio_ended:
            return
        self._buffer.append(chunk)

    def _on_audio_end(self) -> None:
        if self._terminal or self._audio_ended:
            return
        self._audio_ended = True
        self._start_processing()

    async def _on_end(self) -> None:
        if self._terminal:
            await self._close()
            return
        self._audio_ended = True
        result = await self._await_result()
        pending = self._pending_enrollment
        if pending is not None:
            # ``end`` confirms the turn: only now is the enrollment sample stored.
            self._pending_enrollment = None
            try:
                result = await self._service.commit_enrollment(pending)
            except Exception:
                logger.warning("Storing the voice enrollment sample failed", exc_info=True)
                result = make_result(STATUS_ERROR, profile_id=pending.profile_id, reason="sample_not_stored")
        rejection = self._pending_rejection
        if rejection is not None:
            # ``end`` confirms the turn: only now does the rejection count and show.
            self._pending_rejection = None
            result = self._service.commit_rejection(rejection)
        await self._send_result(result)
        await self._close()

    async def _on_abort(self) -> None:
        if self._terminal:
            await self._close()
            return
        await self._discard()
        await self._send_result(make_result(STATUS_ERROR, reason="aborted"))
        await self._close()

    async def _finish_without_end(self, *, send_result: bool, reason: str) -> None:
        """Disconnect or idle timeout before ``end``: use the audio received so far."""
        if self._terminal or not self._started or self._token is None:
            return
        if self._buffer.byte_count == 0 and self._processing is None:
            await self._discard()
            if send_result:
                await self._send_result(make_result(STATUS_ERROR, reason=reason))
            return
        self._audio_ended = True
        result = await self._await_result()
        # An unconfirmed turn never counts as a rejected sample either.
        self._pending_rejection = None
        pending = self._pending_enrollment
        if pending is not None:
            # An unconfirmed turn never becomes an enrollment sample.
            self._pending_enrollment = None
            logger.debug("Discarding unconfirmed voice enrollment sample (%s)", reason)
            result = make_result(
                STATUS_ERROR,
                duration_s=pending.duration_s,
                profile_id=pending.profile_id,
                name=pending.name,
                reason="enrollment_unconfirmed",
                compute_ms=pending.compute_ms,
                latency_ms=pending.latency_ms,
            )
        if send_result:
            await self._send_result(result)

    async def _protocol_error(self, reason: str) -> None:
        logger.warning("Voice WebSocket protocol error: %s", reason)
        await self._discard()
        if not self._result_sent:
            await self._send_result(make_result(STATUS_ERROR, reason=reason))
        await self._close(code=1008)

    # -- processing ----------------------------------------------------

    def _start_processing(self) -> None:
        if self._processing is not None or self._token is None:
            return
        self._received_at = time.perf_counter()
        pcm = self._buffer.finish()
        self._processing = asyncio.create_task(self._process(pcm))

    async def _process(self, pcm: bytes) -> dict[str, Any]:
        assert self._config is not None
        try:
            analysis = await self._service.analyze_turn(
                pcm, SUPPORTED_SAMPLE_RATE, self._config, received_at=self._received_at
            )
        except Exception:
            logger.warning("Speaker identification failed", exc_info=True)
            result = make_result(STATUS_ERROR, reason="processing_failed")
        else:
            result = analysis.result
            self._pending_enrollment = analysis.pending
            self._pending_rejection = analysis.rejection
        # No await between the end of the analysis and ``resolve``: ``latency_ms``
        # is the time until a waiting conversation can see the result.
        if self._conversation_id is not None and self._token is not None:
            self._service.store.resolve(self._conversation_id, self._token, result)
            self._resolved = True
        logger.info(
            "Voice turn processed: status=%s reason=%s duration=%.2fs speech=%s vad=%s compute=%s latency=%s "
            "conversation=%s",
            result.get("status"),
            result.get("reason") or "-",
            result.get("duration_s") or 0.0,
            _format_s(result.get("speech_s")),
            result.get("vad"),
            _format_ms(result.get("compute_ms")),
            _format_ms(result.get("latency_ms")),
            self._conversation_id,
        )
        if self._transcript_chars is not None:
            logger.debug("Voice turn transcript length: %d chars", self._transcript_chars)
        return result

    async def _await_result(self) -> dict[str, Any]:
        self._start_processing()
        if self._processing is None:
            return make_result(STATUS_ERROR, reason="no_session")
        return await self._processing

    async def _discard(self) -> None:
        if self._processing is not None and not self._processing.done():
            self._processing.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._processing
        self._pending_enrollment = None
        self._pending_rejection = None
        if self._conversation_id is not None and self._token is not None:
            self._service.store.discard(self._conversation_id, self._token)
            self._resolved = True

    async def _cleanup(self) -> None:
        # Any exit without a resolved entry (exception, server shutdown)
        # releases waiters so a conversation turn never hangs on it.
        if not self._resolved:
            await self._discard()

    # -- transport -----------------------------------------------------

    async def _send_terminal(self, result: dict[str, Any]) -> None:
        self._terminal = True
        await self._send_result(result)

    async def _send_result(self, result: dict[str, Any]) -> None:
        if self._result_sent:
            return
        self._result_sent = True
        with contextlib.suppress(Exception):
            await self._ws.send_json({"type": "result", **result})

    async def _close(self, code: int = 1000) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close(code=code)
