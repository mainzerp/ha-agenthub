"""Unified embedding engine for local and external providers."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

from app.db.repository import SettingsRepository
from app.defaults import DEFAULT_LOCAL_EMBEDDING_MODEL

logger = logging.getLogger(__name__)

_MODEL_LOAD_LOGGER_LEVELS = (
    ("httpx", logging.WARNING),
    ("huggingface_hub.utils._http", logging.ERROR),
    ("transformers.modeling_utils", logging.ERROR),
    ("sentence_transformers.base.model", logging.WARNING),
)


class _EmbeddingCache:
    """In-memory LRU + TTL cache for text embeddings.

    Keys are ``(provider, model, text)`` so that embeddings from different
    providers or models do not collide.

    Thread-safe: ``embed_batch`` runs on the event loop and, through the
    vector store's sync embedding shim, on worker threads, so every access
    to the OrderedDict happens under one lock.
    """

    def __init__(self, maxsize: int = 1024, ttl: float = 300.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._cache: OrderedDict[tuple[str, str, str], tuple[list[float], float]] = OrderedDict()
        self._lock = threading.Lock()

    def _is_expired(self, timestamp: float) -> bool:
        return time.monotonic() - timestamp > self._ttl

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [key for key, (_embedding, timestamp) in self._cache.items() if now - timestamp > self._ttl]
        for key in expired:
            del self._cache[key]

    def get(self, provider: str, model: str, text: str) -> list[float] | None:
        """Return a cached embedding if it exists and has not expired."""
        with self._lock:
            self._evict_expired()
            key = (provider, model, text)
            if key in self._cache:
                embedding, timestamp = self._cache[key]
                if not self._is_expired(timestamp):
                    self._cache.move_to_end(key)
                    return embedding
                del self._cache[key]
            return None

    def set(self, provider: str, model: str, text: str, embedding: list[float]) -> None:
        """Store an embedding, evicting expired or oldest entries if needed."""
        with self._lock:
            self._evict_expired()
            key = (provider, model, text)
            self._cache[key] = (embedding, time.monotonic())
            self._cache.move_to_end(key)
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def clear(self) -> None:
        """Drop all cached embeddings."""
        with self._lock:
            self._cache.clear()


@contextmanager
def _suppress_model_load_startup_logs():
    previous_levels = []
    try:
        for logger_name, temporary_level in _MODEL_LOAD_LOGGER_LEVELS:
            noisy_logger = logging.getLogger(logger_name)
            previous_levels.append((noisy_logger, noisy_logger.level))
            noisy_logger.setLevel(temporary_level)
        yield
    finally:
        for noisy_logger, previous_level in reversed(previous_levels):
            noisy_logger.setLevel(previous_level)


class EmbeddingEngine:
    """Unified embedding engine supporting local and external providers."""

    def __init__(self) -> None:
        self._provider: str | None = None
        self._model_name: str | None = None
        self._local_model = None  # SentenceTransformer instance, lazy-loaded
        # Guards the one-time model load: embed_batch runs _embed_local in
        # worker threads, so concurrent first calls could otherwise race.
        self._local_model_lock = threading.Lock()
        self._cache = _EmbeddingCache()

    async def _load_config(self) -> None:
        """Read embedding.provider and embedding.*_model from settings table."""
        self._provider = await SettingsRepository.get_value("embedding.provider", "local")
        if self._provider == "local":
            self._model_name = await SettingsRepository.get_value(
                "embedding.local_model",
                DEFAULT_LOCAL_EMBEDDING_MODEL,
            )
        else:
            self._model_name = await SettingsRepository.get_value("embedding.external_model", "")

    def _get_local_model(self):
        """Lazy-load the sentence-transformers model on first use.

        Blocking (torch import + weight load): call it only off the event loop.
        Thread-safe; the model is constructed at most once per engine.
        """
        if self._local_model is not None:
            return self._local_model
        with self._local_model_lock:
            if self._local_model is not None:
                return self._local_model
            if not self._model_name:
                raise RuntimeError("Local embedding model name is not configured; EmbeddingEngine is not initialized")
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
            from sentence_transformers import SentenceTransformer

            try:
                import huggingface_hub

                if hasattr(huggingface_hub, "disable_progress_bars"):
                    huggingface_hub.disable_progress_bars()
                else:
                    from huggingface_hub.utils import logging as hf_logging

                    hf_logging.disable_progress_bars()
            except Exception:
                logger.debug("Failed to disable HuggingFace progress bars", exc_info=True)

            with _suppress_model_load_startup_logs():
                self._local_model = SentenceTransformer(self._model_name)
            logger.info("Loaded local embedding model: %s", self._model_name)
            return self._local_model

    async def initialize(self) -> None:
        """Load config from DB and pre-load the model. Must call before embed/embed_batch."""
        await self._load_config()
        if self._provider == "local":
            # Model load is blocking and CPU-heavy (Directive 9): keep it off the loop.
            await asyncio.to_thread(self._get_local_model)

    @property
    def model_id(self) -> str:
        """Stable identity of the producing model, ``"<provider>:<model>"``.

        Persisted next to stored vectors so vectors from a different model
        (even of the same dimension) are recognised as unusable.
        """
        return f"{self._provider or 'unknown'}:{self._model_name or 'unknown'}"

    def get_info(self) -> dict:
        """Return embedding model configuration info."""
        dimensions = None
        if self._provider == "local" and self._local_model is not None:
            dimensions = self._local_model.get_sentence_embedding_dimension()
        elif self._provider == "local":
            defaults = {
                "all-MiniLM-L6-v2": 384,
                "all-mpnet-base-v2": 768,
                # 0.23.0: multilingual default (now e5-base, 768d).
                DEFAULT_LOCAL_EMBEDDING_MODEL: 768,
                # Rollback target: previous multilingual default (384d).
                "intfloat/multilingual-e5-small": 384,
                "paraphrase-multilingual-MiniLM-L12-v2": 384,
            }
            dimensions = defaults.get(self._model_name or "")
        name = (self._model_name or "").lower()
        is_multilingual = "multilingual" in name or name.startswith("intfloat/multilingual")
        return {
            "provider": self._provider or "unknown",
            "model": self._model_name or "unknown",
            "dimensions": dimensions,
            "is_multilingual": is_multilingual,
        }

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string. Returns a float list with the model's embedding dimension."""
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, using the in-memory cache when possible."""
        provider = self._provider or "unknown"
        model = self._model_name or "unknown"

        results: list[list[float] | None] = [None] * len(texts)
        missing_texts: list[str] = []
        missing_indices: list[int] = []

        for i, text in enumerate(texts):
            cached = self._cache.get(provider, model, text)
            if cached is not None:
                results[i] = cached
            else:
                missing_texts.append(text)
                missing_indices.append(i)

        if missing_texts:
            if self._provider == "local":
                computed = await asyncio.to_thread(self._embed_local, missing_texts)
            else:
                computed = await self._embed_external(missing_texts)

            for idx, text, embedding in zip(missing_indices, missing_texts, computed, strict=True):
                self._cache.set(provider, model, text, embedding)
                results[idx] = embedding

        return results  # type: ignore[return-value]

    def _embed_local(self, texts: list[str]) -> list[list[float]]:
        """Use sentence-transformers for local embedding."""
        model = self._get_local_model()
        # show_progress_bar=False suppresses the per-call tqdm "Batches"
        # progress bar that would otherwise spam logs on every embed
        # (entity matcher queries, cache lookups, periodic HA syncs).
        embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [emb.tolist() for emb in embeddings]

    async def _embed_external(self, texts: list[str]) -> list[list[float]]:
        """Use litellm for external provider embedding with retry."""
        import litellm

        from app.llm.providers import resolve_provider_params

        if self._model_name is None:
            raise ValueError("No model name configured for external embedding")
        provider_params = await resolve_provider_params(self._model_name)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(
                    litellm.embedding, model=self._model_name, input=texts, **provider_params
                )
                return [item["embedding"] for item in response.data]
            except litellm.RateLimitError as exc:
                last_exc = exc
                await asyncio.sleep(2**attempt)
            except asyncio.CancelledError:
                raise
            except litellm.exceptions.APIError as exc:
                last_exc = exc
                raise RuntimeError(f"External embedding failed: {exc}") from exc
        raise RuntimeError(f"External embedding rate-limited after retries: {last_exc}") from last_exc


_engine: EmbeddingEngine | None = None
# The in-flight initialization, shared by every concurrent caller. It runs as
# its own task so a cancelled caller cannot abandon a half-finished model
# load (whose worker thread keeps running) and make the next caller start a
# second, parallel load.
_engine_init_task: asyncio.Task | None = None
# Failure backoff: after a failed initialization, callers fail fast for this
# many seconds instead of re-running (and queueing behind) a doomed init on
# every cache miss and store.
_ENGINE_INIT_RETRY_COOLDOWN_S = 30.0
_engine_init_failed_at: float | None = None
_engine_init_error: BaseException | None = None


class EmbeddingEngineUnavailableError(RuntimeError):
    """Raised while the embedding engine is in its post-failure cooldown."""


# Fixed keep-alive payload. Safe because the default interval (15 min)
# exceeds the 300 s _EmbeddingCache TTL, so every run performs a real
# model.encode instead of being served from the embedding LRU.
_KEEPALIVE_WARMUP_TEXT = "embedding keep-alive warmup"


async def _initialize_engine() -> EmbeddingEngine:
    engine = EmbeddingEngine()
    await engine.initialize()
    return engine


def _on_engine_init_done(task: asyncio.Task) -> None:
    """Publish a successful init; record a failure for the retry cooldown."""
    global _engine, _engine_init_task, _engine_init_failed_at, _engine_init_error
    if _engine_init_task is task:
        _engine_init_task = None
    if task.cancelled():
        # Cancellation of the init itself (loop shutdown) is not a model
        # failure: no cooldown, the next caller simply starts over.
        return
    exc = task.exception()
    if exc is not None:
        _engine_init_failed_at = time.monotonic()
        _engine_init_error = exc
        logger.warning(
            "Embedding engine initialization failed; retrying after %.0f s cooldown",
            _ENGINE_INIT_RETRY_COOLDOWN_S,
            exc_info=exc,
        )
        return
    # Publish only a fully initialized engine: an interrupted or failed
    # initialize() must not leave a half-configured singleton
    # (model_name=None) behind for later callers.
    _engine = task.result()
    _engine_init_failed_at = None
    _engine_init_error = None


async def get_embedding_engine() -> EmbeddingEngine:
    """Return the singleton EmbeddingEngine, initializing on first call.

    Concurrent callers share one initialization task. A caller that is
    cancelled stops waiting but does not cancel the shared init. After a
    failed init, callers raise :class:`EmbeddingEngineUnavailableError`
    without retrying until the cooldown has elapsed.
    """
    global _engine_init_task
    if _engine is not None:
        return _engine
    task = _engine_init_task
    if task is not None and task.get_loop() is not asyncio.get_running_loop():
        # A task bound to a closed loop (test harness, restart) can never finish here.
        task = None
    if task is None:
        if (
            _engine_init_failed_at is not None
            and time.monotonic() - _engine_init_failed_at < _ENGINE_INIT_RETRY_COOLDOWN_S
        ):
            raise EmbeddingEngineUnavailableError(
                "Embedding engine initialization failed recently; retry pending"
            ) from _engine_init_error
        task = asyncio.create_task(_initialize_engine(), name="embedding-engine-init")
        task.add_done_callback(_on_engine_init_done)
        _engine_init_task = task
    return await asyncio.shield(task)


async def get_embedding_info() -> dict:
    """Return embedding config info from the singleton engine."""
    engine = await get_embedding_engine()
    return engine.get_info()


async def run_embedding_keepalive() -> None:
    """Periodic dummy encode to keep the local embedding model warm.

    No-op for external providers (they need no warmup and a keep-alive
    would burn paid API tokens). Interval is re-read each iteration;
    ``0`` disables the encode with a 300 s recheck (entity-sync precedent).
    """
    while True:
        try:
            interval_raw = await SettingsRepository.get_value("embedding.keepalive_interval_minutes", "15")
            try:
                interval_min = int(str(interval_raw))
            except (TypeError, ValueError):
                interval_min = 15

            if interval_min <= 0:
                await asyncio.sleep(300)
                continue

            provider = await SettingsRepository.get_value("embedding.provider", "local")
            if provider == "local":
                # engine.embed offloads the CPU-bound encode internally
                # (asyncio.to_thread, Directive 9) -- no extra offload here.
                engine = await get_embedding_engine()
                await engine.embed(_KEEPALIVE_WARMUP_TEXT)
            await asyncio.sleep(interval_min * 60)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Embedding keep-alive run failed", exc_info=True)
            await asyncio.sleep(300)
