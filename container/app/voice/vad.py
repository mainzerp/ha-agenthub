"""Voice-activity trimming before speaker embedding (sherpa-onnx Silero VAD).

Only the outer silence of a voice turn is cut off before embedding, for
identification and enrollment alike: the embedded region runs from the start
of the first detected speech span to the end of the last one, padded by
``_EDGE_PAD_S`` on each side. Leading and trailing silence would otherwise
pull every embedding towards the same "room noise" direction and make
different speakers look alike; pauses inside the utterance are kept (see
``_EDGE_PAD_S`` for why).

The detector loads lazily in the background (model download included when
the image does not ship it) and every detection runs in a worker thread via
``asyncio.to_thread`` -- never on the event loop. VAD is best effort: while
it is not ready, or when it fails, the whole buffer is used and the result
is marked ``vad=False``. A voice turn never fails or waits because of VAD.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

from app.util.tasks import spawn
from app.voice import model as voice_model
from app.voice.audio import BYTES_PER_SAMPLE, MAX_AUDIO_SECONDS, SUPPORTED_SAMPLE_RATE, pcm_s16le_to_float32

logger = logging.getLogger(__name__)

VadStatus = Literal["ready", "loading", "unavailable"]
# ``pending``: not loaded yet but expected to load (not started or loading);
# ``failed``: unavailable for now (load failed / backing off, or sherpa-onnx missing).
VadState = Literal["ready", "pending", "failed"]

# After a failed load, warm-ups are skipped for this long before a retry.
_LOAD_FAILURE_BACKOFF_S = 60.0

# Silero parameters: pauses shorter than ``_MIN_SILENCE_S`` do not split a
# speech span, speech bursts shorter than ``_MIN_SPEECH_S`` (clicks, breaths)
# are ignored.
_SPEECH_THRESHOLD = 0.5
_MIN_SILENCE_S = 0.25
_MIN_SPEECH_S = 0.25

# Padding kept around the speech region (seconds, clamped to the buffer).
# Only the outer silence is trimmed: an offline experiment with the CAM++
# engine and Silero VAD showed that cutting the pauses inside an utterance
# (joining the speech spans) destabilises same-speaker embeddings -- removing
# a 0.2-0.4 s pause could turn a clip's embedding almost orthogonal. Over 72
# leave-one-out centroid trials, joined spans misidentified 14, the whole
# buffer 10, and this padded first-to-last-speech region 7 (no padding: 13;
# the hand-cut true speech region: 5).
_EDGE_PAD_S = 0.2

# ``(start_sample, sample_count)`` spans of detected speech.
SpeechSpans = list[tuple[int, int]]
SpanDetector = Callable[[np.ndarray], SpeechSpans]
DetectorFactory = Callable[[Path], SpanDetector]
ModelPathResolver = Callable[[], Awaitable[Path]]


class VadUnavailableError(RuntimeError):
    """The VAD cannot run (missing dependency, model or load error)."""


def sherpa_available() -> bool:
    """True when the ``sherpa_onnx`` package can be imported (cheap spec lookup, no import)."""
    return importlib.util.find_spec("sherpa_onnx") is not None


def _discard_data_copy(model_path: Path) -> None:
    """Delete a VAD model that failed to load when it is the data-directory copy (worker thread).

    The next load attempt (after the backoff) downloads a fresh copy. The
    image copy is read-only and never deleted.
    """
    data_copy = voice_model.data_model_dir() / voice_model.VAD_MODEL_FILENAME
    try:
        if model_path.resolve() == data_copy.resolve() and model_path.is_file():
            model_path.unlink()
            logger.warning("Deleted the VAD model %s after a failed load; it is downloaded again", model_path)
    except OSError:
        logger.debug("Could not delete the VAD model %s", model_path, exc_info=True)


@dataclass(frozen=True)
class SpeechTrim:
    """Audio to embed for one voice turn.

    With ``vad`` True, ``pcm`` is the speech region (first speech start to
    last speech end, padded by ``_EDGE_PAD_S``, inner pauses included) and
    empty when no speech was detected; otherwise it is the whole buffer.
    ``speech_s`` is the detected speech in seconds (the summed span lengths,
    so the minimum-duration rules measure real speech, not pauses or
    padding); ``embedded_s`` is the duration of ``pcm``. Without VAD both
    are the whole-buffer duration.
    """

    pcm: bytes
    speech_s: float
    vad: bool
    embedded_s: float | None = None


def untrimmed(pcm: bytes, sample_rate: int) -> SpeechTrim:
    """The fallback trim: the whole buffer, ``vad=False``."""
    seconds = round(len(pcm) / (BYTES_PER_SAMPLE * sample_rate), 3) if sample_rate > 0 else 0.0
    return SpeechTrim(pcm=pcm, speech_s=seconds, vad=False, embedded_s=seconds)


def _clamped_spans(spans: SpeechSpans, total: int) -> list[tuple[int, int]]:
    """``(begin, end)`` sample ranges of ``spans`` clamped to ``total`` samples, sorted, empty ones dropped."""
    ranges: list[tuple[int, int]] = []
    for start, count in spans:
        begin = max(0, int(start))
        end = min(total, int(start) + max(0, int(count)))
        if end > begin:
            ranges.append((begin, end))
    ranges.sort()
    return ranges


def speech_sample_count(spans: SpeechSpans, total: int) -> int:
    """Samples covered by ``spans`` within a ``total``-sample buffer (overlaps counted once)."""
    covered = 0
    cursor = 0
    for begin, end in _clamped_spans(spans, total):
        begin = max(begin, cursor)
        if end > begin:
            covered += end - begin
            cursor = end
    return covered


def speech_region(pcm: bytes, spans: SpeechSpans, pad_samples: int) -> bytes:
    """The s16le samples from the first span start to the last span end, padded by ``pad_samples``.

    Everything between the spans (pauses inside the utterance) is kept;
    the padding is clamped to the buffer. No spans: empty bytes.
    """
    total = len(pcm) // BYTES_PER_SAMPLE
    ranges = _clamped_spans(spans, total)
    if not ranges:
        return b""
    pad = max(0, int(pad_samples))
    begin = max(0, ranges[0][0] - pad)
    end = min(total, max(stop for _, stop in ranges) + pad)
    return pcm[begin * BYTES_PER_SAMPLE : end * BYTES_PER_SAMPLE]


def _sherpa_vad_factory(model_path: Path) -> SpanDetector:
    """Build a Silero ``sherpa_onnx.VoiceActivityDetector`` wrapper (runs in a worker thread)."""
    try:
        import sherpa_onnx  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VadUnavailableError("sherpa_onnx is not installed") from exc
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(model_path)
    config.silero_vad.threshold = _SPEECH_THRESHOLD
    config.silero_vad.min_silence_duration = _MIN_SILENCE_S
    config.silero_vad.min_speech_duration = _MIN_SPEECH_S
    # A voice turn is capped at MAX_AUDIO_SECONDS; never split inside it.
    config.silero_vad.max_speech_duration = MAX_AUDIO_SECONDS
    config.sample_rate = SUPPORTED_SAMPLE_RATE
    config.num_threads = 1
    config.provider = "cpu"
    if not config.validate():
        raise VadUnavailableError("Invalid VAD config")
    window = int(config.silero_vad.window_size) or 512
    detector = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=MAX_AUDIO_SECONDS + 5)

    def _drain(spans: SpeechSpans) -> None:
        while not detector.empty():
            segment = detector.front
            spans.append((int(segment.start), len(segment.samples)))
            detector.pop()

    def detect(waveform: np.ndarray) -> SpeechSpans:
        detector.reset()
        spans: SpeechSpans = []
        for offset in range(0, len(waveform), window):
            detector.accept_waveform(waveform[offset : offset + window])
            _drain(spans)
        detector.flush()
        _drain(spans)
        return spans

    return detect


class VoiceActivityTrimmer:
    """Trims a voice turn to its detected speech behind an async API.

    ``detector_factory(model_path)`` and ``model_path_resolver`` are
    injectable so tests never need sherpa-onnx or the model file.
    """

    def __init__(
        self,
        *,
        detector_factory: DetectorFactory | None = None,
        model_path_resolver: ModelPathResolver | None = None,
    ) -> None:
        self._detector_factory = detector_factory
        self._model_path_resolver = model_path_resolver
        self._detector: SpanDetector | None = None
        self._lock = threading.Lock()
        self._load_lock: asyncio.Lock | None = None
        self._load_lock_loop_id: int | None = None
        self._loading = False
        self._failed_at: float | None = None
        self._last_error: str | None = None
        self._warm_up_task: asyncio.Task[Any] | None = None

    @property
    def status(self) -> VadStatus:
        if self._detector is not None:
            return "ready"
        if self._loading:
            return "loading"
        return "unavailable"

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def state(self) -> VadState:
        """Whether trimming is ready, expected soon (``pending``) or unavailable for now (``failed``)."""
        if self._detector is not None:
            return "ready"
        if self._loading:
            return "pending"
        if self._detector_factory is None and not sherpa_available():
            return "failed"
        if self._failed_at is not None:
            return "failed"
        return "pending"

    def _get_load_lock(self) -> asyncio.Lock:
        loop_id = id(asyncio.get_running_loop())
        if self._load_lock is None or self._load_lock_loop_id != loop_id:
            self._load_lock = asyncio.Lock()
            self._load_lock_loop_id = loop_id
        return self._load_lock

    def _in_backoff(self) -> bool:
        return self._failed_at is not None and time.monotonic() - self._failed_at < _LOAD_FAILURE_BACKOFF_S

    async def ensure_ready(self) -> bool:
        """Load the detector if needed. Returns False when it is unavailable. Never raises."""
        if self._detector is not None:
            return True
        if self._in_backoff():
            return False
        async with self._get_load_lock():
            if self._detector is not None:
                return True
            self._loading = True
            model_path: Path | None = None
            try:
                factory = self._detector_factory
                if factory is None:
                    # Fail fast without sherpa-onnx instead of downloading a model it cannot use.
                    if not sherpa_available():
                        raise VadUnavailableError("sherpa_onnx is not installed")
                    factory = _sherpa_vad_factory
                resolver = self._model_path_resolver or voice_model.ensure_vad_model_path
                model_path = await resolver()
                detector = await asyncio.to_thread(factory, model_path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if model_path is not None:
                    # A corrupt data-directory copy would fail forever: re-download it next time.
                    await asyncio.to_thread(_discard_data_copy, model_path)
                self._failed_at = time.monotonic()
                self._last_error = str(exc) or type(exc).__name__
                logger.warning("Voice-activity detector unavailable (whole turns are embedded): %s", self._last_error)
                return False
            finally:
                self._loading = False
            self._detector = detector
            self._failed_at = None
            self._last_error = None
            logger.info("Voice-activity detector ready")
            return True

    def schedule_warm_up(self) -> None:
        """Load the detector in the background unless it is ready, loading or backing off."""
        if self._detector is not None or self._loading or self._in_backoff():
            return
        if self._warm_up_task is not None and not self._warm_up_task.done():
            return
        self._warm_up_task = spawn(self.ensure_ready(), name="voice_vad_warm_up")

    async def trim(self, pcm: bytes, sample_rate: int) -> SpeechTrim:
        """Return the speech region of ``pcm`` (falls back to the whole buffer). Never raises.

        Only the outer silence is trimmed (see :func:`speech_region`). Only
        16 kHz audio is trimmed (the rate the detector is configured for);
        any other rate is returned untrimmed.
        """
        if sample_rate != SUPPORTED_SAMPLE_RATE:
            return untrimmed(pcm, sample_rate)
        if self._detector is None:
            self.schedule_warm_up()
            return untrimmed(pcm, sample_rate)
        try:
            spans = await asyncio.to_thread(self._detect, pcm)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Voice-activity detection failed; embedding the whole turn", exc_info=True)
            return untrimmed(pcm, sample_rate)
        region = speech_region(pcm, spans, round(_EDGE_PAD_S * sample_rate))
        speech_s = speech_sample_count(spans, len(pcm) // BYTES_PER_SAMPLE) / sample_rate
        embedded_s = len(region) / (BYTES_PER_SAMPLE * sample_rate)
        return SpeechTrim(pcm=region, speech_s=round(speech_s, 3), vad=True, embedded_s=round(embedded_s, 3))

    def _detect(self, pcm: bytes) -> SpeechSpans:
        waveform = pcm_s16le_to_float32(pcm)
        # The sherpa detector is stateful: one detection at a time.
        with self._lock:
            detector = self._detector
            if detector is None:
                raise VadUnavailableError("Voice-activity detector unavailable")
            return list(detector(waveform))
