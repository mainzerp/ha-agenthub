"""Lazy, thread-safe speaker-embedding extractor (sherpa-onnx CAM++).

The extractor is loaded on first use (model download included when the
image does not ship it) and every inference runs in a worker thread via
``asyncio.to_thread`` -- never on the event loop. ``sherpa_onnx`` is an
optional import: without it the engine reports ``unavailable``. A changed
thread count never reloads inline: the loaded extractor keeps serving while
a background reload swaps in the new one.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import numpy as np

from app.util.tasks import spawn
from app.voice.audio import pcm_s16le_to_float32
from app.voice.model import MODEL_ID, ensure_model_path

logger = logging.getLogger(__name__)

EngineStatus = Literal["ready", "loading", "unavailable"]

# After a failed load, callers fail fast for this long before a retry.
_LOAD_FAILURE_BACKOFF_S = 60.0

ExtractorFactory = Callable[[Path, int], Any]
ModelPathResolver = Callable[[], Awaitable[Path]]


class SpeakerEngineUnavailableError(RuntimeError):
    """The engine cannot produce embeddings (missing dependency, model or load error)."""


def _sherpa_extractor_factory(model_path: Path, num_threads: int) -> Any:
    """Build a ``sherpa_onnx.SpeakerEmbeddingExtractor`` (runs in a worker thread)."""
    try:
        import sherpa_onnx  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SpeakerEngineUnavailableError("sherpa_onnx is not installed") from exc
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(model_path),
        num_threads=num_threads,
        debug=False,
        provider="cpu",
    )
    if not config.validate():
        raise SpeakerEngineUnavailableError("Invalid speaker-embedding extractor config")
    return sherpa_onnx.SpeakerEmbeddingExtractor(config)


class SpeakerEngine:
    """Wraps a speaker-embedding extractor behind an async API.

    ``extractor_factory(model_path, num_threads)`` and ``model_path_resolver``
    are injectable so tests never need sherpa-onnx or the model file.
    """

    def __init__(
        self,
        *,
        extractor_factory: ExtractorFactory | None = None,
        model_path_resolver: ModelPathResolver | None = None,
        model_id: str = MODEL_ID,
    ) -> None:
        self._extractor_factory = extractor_factory or _sherpa_extractor_factory
        self._model_path_resolver = model_path_resolver or ensure_model_path
        self._model_id = model_id
        self._extractor: Any | None = None
        self._num_threads: int | None = None
        self._compute_lock = threading.Lock()
        self._load_lock: asyncio.Lock | None = None
        self._load_lock_loop_id: int | None = None
        self._loading = False
        self._failed_at: float | None = None
        self._last_error: str | None = None
        self._reload_task: asyncio.Task[Any] | None = None
        self._reload_threads: int | None = None
        # (num_threads, monotonic time) of the last failed background reload.
        self._reload_failed: tuple[int, float] | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def status(self) -> EngineStatus:
        if self._extractor is not None:
            return "ready"
        if self._loading:
            return "loading"
        return "unavailable"

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _get_load_lock(self) -> asyncio.Lock:
        loop_id = id(asyncio.get_running_loop())
        if self._load_lock is None or self._load_lock_loop_id != loop_id:
            self._load_lock = asyncio.Lock()
            self._load_lock_loop_id = loop_id
        return self._load_lock

    async def ensure_ready(self, num_threads: int = 2) -> bool:
        """Load the extractor if needed. Returns False when it is unavailable."""
        if self._extractor is not None and self._num_threads == num_threads:
            return True
        if (
            self._extractor is None
            and self._failed_at is not None
            and time.monotonic() - self._failed_at < _LOAD_FAILURE_BACKOFF_S
        ):
            return False
        async with self._get_load_lock():
            if self._extractor is not None and self._num_threads == num_threads:
                return True
            self._loading = True
            try:
                model_path = await self._model_path_resolver()
                extractor = await asyncio.to_thread(self._extractor_factory, model_path, num_threads)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                if self._extractor is not None:
                    # Reload with another thread count failed: keep serving with the loaded one.
                    self._reload_failed = (num_threads, time.monotonic())
                    logger.warning(
                        "Speaker-embedding engine reload (threads=%d) failed; keeping threads=%s: %s",
                        num_threads,
                        self._num_threads,
                        error,
                    )
                    return True
                self._failed_at = time.monotonic()
                self._last_error = error
                logger.warning("Speaker-embedding engine unavailable: %s", self._last_error)
                return False
            finally:
                self._loading = False
            # Atomic swap without blocking the event loop on ``_compute_lock``:
            # an inference in progress holds its own reference and finishes on
            # the old extractor; the next one picks up the new extractor.
            self._extractor = extractor
            self._num_threads = num_threads
            self._failed_at = None
            self._last_error = None
            self._reload_failed = None
            logger.info("Speaker-embedding engine ready (model=%s, threads=%d)", self._model_id, num_threads)
            return True

    def schedule_reload(self, num_threads: int) -> bool:
        """Reload a loaded extractor with ``num_threads`` in the background.

        The loaded extractor keeps serving until the new one is swapped in; a
        failed reload keeps it (retried after the failure backoff). Returns
        True when a reload was started.
        """
        if self._extractor is None or self._num_threads == num_threads:
            return False
        if self._reload_task is not None and not self._reload_task.done() and self._reload_threads == num_threads:
            return False
        failed = self._reload_failed
        if failed is not None and failed[0] == num_threads and time.monotonic() - failed[1] < _LOAD_FAILURE_BACKOFF_S:
            return False
        self._reload_threads = num_threads
        self._reload_task = spawn(self.ensure_ready(num_threads), name="speaker_engine_reload")
        return True

    async def embed(self, pcm: bytes, sample_rate: int, *, num_threads: int = 2) -> np.ndarray:
        """Compute the speaker embedding of a mono s16le buffer.

        Raises :class:`SpeakerEngineUnavailableError` when the extractor cannot
        be loaded. A loaded extractor with another thread count is used as is
        while :meth:`schedule_reload` applies ``num_threads`` in the background.
        """
        if self._extractor is None:
            if not await self.ensure_ready(num_threads):
                raise SpeakerEngineUnavailableError(self._last_error or "Speaker-embedding engine unavailable")
        elif self._num_threads != num_threads:
            self.schedule_reload(num_threads)
        return await asyncio.to_thread(self._compute, pcm, sample_rate)

    def _compute(self, pcm: bytes, sample_rate: int) -> np.ndarray:
        waveform = pcm_s16le_to_float32(pcm)
        with self._compute_lock:
            extractor = self._extractor
            if extractor is None:
                raise SpeakerEngineUnavailableError("Speaker-embedding engine unavailable")
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=waveform)
            stream.input_finished()
            vector = extractor.compute(stream)
        return np.asarray(vector, dtype=np.float32)
