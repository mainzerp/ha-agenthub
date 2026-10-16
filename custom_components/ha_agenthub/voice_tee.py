"""Best-effort copy of one voice turn's audio to the container.

The STT wrapper entity (``stt.py``) feeds every audio chunk it forwards to
the inner STT entity into a :class:`VoiceTee`. A background sender task
streams the chunks to ``/ws/voice`` on the container for speaker
identification. The tee must never fail or slow down the STT turn:

- ``feed``/``close_input``/``finish``/``abort`` are synchronous, never await
  and never raise; they are safe to call from inside the audio generator
  (also from ``finally`` blocks during ``GeneratorExit`` or cancellation).
- Chunks are buffered in a bounded deque; on overflow chunks are dropped and
  the turn is aborted instead of blocking the stream.
- All network I/O happens in :meth:`VoiceTee.run`, which swallows every
  failure (debug log, rate-limited warning when the container is
  unreachable) and is bounded by timeouts.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .const import (
    VOICE_TEE_CONNECT_TIMEOUT,
    VOICE_TEE_MAX_DURATION,
    VOICE_TEE_QUEUE_MAX,
    VOICE_TEE_RESULT_TIMEOUT,
    VOICE_TEE_WARN_INTERVAL,
    VOICE_WS_PATH,
)

logger = logging.getLogger(__name__)


def build_voice_ws_url(url: str) -> str:
    """Return the container's voice WebSocket URL for an http(s) base URL."""
    parsed = urlparse(url.rstrip("/"))
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{parsed._replace(scheme=ws_scheme).geturl()}{VOICE_WS_PATH}"


class _UnreachableWarning:
    """Emit at most one warning per interval; debug-log the rest."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._last: float | None = None

    def reset(self) -> None:
        self._last = None

    def log(self, url: str, err: BaseException) -> None:
        now = time.monotonic()
        if self._last is None or now - self._last >= self._interval:
            self._last = now
            logger.warning(
                "Speaker identification: cannot reach the HA-AgentHub container at %s (%s). "
                "Speech-to-text is unaffected; further failures are logged at debug level "
                "for the next %d minutes",
                url,
                err.__class__.__name__,
                int(self._interval // 60),
            )
        else:
            logger.debug(
                "Speaker identification: container at %s unreachable: %r", url, err
            )


unreachable_warning = _UnreachableWarning(VOICE_TEE_WARN_INTERVAL)


class VoiceTee:
    """Audio copy for one voice turn, sent over one WebSocket connection."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        ws_url: str,
        api_key: str,
        start_payload: dict[str, Any],
        *,
        queue_max: int = VOICE_TEE_QUEUE_MAX,
        on_auth_failure: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        # Called (in the event loop) when the container rejects the API key.
        self._on_auth_failure = on_auth_failure
        self._ws_url = ws_url
        self._api_key = api_key
        self._start_payload = start_payload
        self._queue_max = queue_max
        self._chunks: collections.deque[bytes] = collections.deque()
        self._wake = asyncio.Event()
        self._input_closed = False
        self._outcome: dict[str, Any] | None = None
        # Set when the sender gave up (connect failure, error, timeout):
        # further chunks are discarded instead of buffered.
        self._disabled = False
        self.dropped_chunks = 0

    # -- Producer side (synchronous, never raises) -------------------------

    def feed(self, chunk: bytes) -> None:
        """Buffer one audio chunk without blocking; drop it when full."""
        if self._disabled or self._input_closed:
            return
        if len(self._chunks) >= self._queue_max:
            self.dropped_chunks += 1
            return
        self._chunks.append(chunk)
        self._wake.set()

    def close_input(self) -> None:
        """Mark the end of the audio stream. Idempotent."""
        if self._input_closed:
            return
        self._input_closed = True
        self._wake.set()

    def finish(self, transcript_chars: int) -> None:
        """Confirm the turn: the inner STT produced a non-empty transcript.

        Only the transcript length is sent (never the text), so the ``end``
        frame stays a tiny fixed-size JSON object. Idempotent; first outcome
        wins.
        """
        self.close_input()
        if self._outcome is not None:
            return
        if self.dropped_chunks:
            # The container would see gapped audio; do not let it learn or
            # identify from it.
            logger.debug(
                "Speaker identification: aborting turn, %d audio chunk(s) dropped",
                self.dropped_chunks,
            )
            self._outcome = {"type": "abort"}
        else:
            chars = transcript_chars if isinstance(transcript_chars, int) else 0
            self._outcome = {"type": "end", "transcript_chars": max(0, chars)}
        self._wake.set()

    def abort(self) -> None:
        """Report that the inner STT failed or was cancelled. Idempotent."""
        self.close_input()
        if self._outcome is None:
            self._outcome = {"type": "abort"}
            self._wake.set()

    # -- Sender side -------------------------------------------------------

    async def run(self) -> None:
        """Stream the turn to the container; never raises except on cancel."""
        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            async with asyncio.timeout(VOICE_TEE_MAX_DURATION):
                try:
                    async with asyncio.timeout(VOICE_TEE_CONNECT_TIMEOUT):
                        ws = await self._session.ws_connect(
                            self._ws_url,
                            headers={"Authorization": f"Bearer {self._api_key}"},
                            # PCM barely compresses; never zlib it on HA's event loop.
                            compress=0,
                        )
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, TimeoutError, OSError) as err:
                    self._disable()
                    # A rejected key fails the handshake with HTTP 401/403.
                    if getattr(err, "status", None) in {401, 403}:
                        self._report_auth_failure()
                    else:
                        unreachable_warning.log(self._ws_url, err)
                    return
                await ws.send_json(self._start_payload)
                await self._pump_audio(ws)
                outcome = await self._wait_outcome()
                await ws.send_json(outcome)
                await self._log_result(ws)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.debug("Speaker identification: voice tee session timed out")
        except Exception:
            logger.debug(
                "Speaker identification: voice tee session failed", exc_info=True
            )
        finally:
            self._disable()
            if ws is not None and not ws.closed:
                try:
                    async with asyncio.timeout(VOICE_TEE_RESULT_TIMEOUT):
                        await ws.close()
                except Exception:
                    logger.debug(
                        "Speaker identification: closing the voice socket failed",
                        exc_info=True,
                    )

    def _report_auth_failure(self) -> None:
        logger.debug("Speaker identification: container rejected the API key")
        if self._on_auth_failure is None:
            return
        try:
            self._on_auth_failure()
        except Exception:
            logger.debug(
                "Speaker identification: auth failure callback failed", exc_info=True
            )

    def _disable(self) -> None:
        self._disabled = True
        self._chunks.clear()

    async def _wait_wake(self) -> None:
        self._wake.clear()
        await self._wake.wait()

    def _turn_void(self) -> bool:
        """True once the turn will end in ``abort`` (dropped audio or STT failure)."""
        if self.dropped_chunks:
            return True
        return self._outcome is not None and self._outcome.get("type") == "abort"

    async def _pump_audio(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            while self._chunks:
                if self._turn_void():
                    # The turn is void; skip the remaining buffered audio.
                    self._chunks.clear()
                    break
                await ws.send_bytes(self._chunks.popleft())
            if self._input_closed:
                break
            await self._wait_wake()
        if self._turn_void():
            # Send ``abort`` only (from ``run``): ``audio_end`` would make the
            # container start an embedding it then discards.
            return
        await ws.send_json({"type": "audio_end"})

    async def _wait_outcome(self) -> dict[str, Any]:
        while self._outcome is None:
            await self._wait_wake()
        return self._outcome

    async def _log_result(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Read the container's result frame for debug logging only."""
        try:
            async with asyncio.timeout(VOICE_TEE_RESULT_TIMEOUT):
                msg = await ws.receive()
        except TimeoutError:
            logger.debug("Speaker identification: no result frame from the container")
            return
        if msg.type != aiohttp.WSMsgType.TEXT:
            logger.debug("Speaker identification: unexpected frame type %s", msg.type)
            return
        try:
            payload = json.loads(msg.data)
        except (TypeError, ValueError):
            logger.debug("Speaker identification: invalid result frame")
            return
        if isinstance(payload, dict):
            logger.debug(
                "Speaker identification result: status=%s conversation_id=%s",
                payload.get("status"),
                self._start_payload.get("conversation_id"),
            )
