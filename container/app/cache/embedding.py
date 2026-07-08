"""Unified embedding engine for local and external providers."""

from __future__ import annotations

import asyncio
import logging
import os
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
    """

    def __init__(self, maxsize: int = 1024, ttl: float = 300.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._cache: OrderedDict[tuple[str, str, str], tuple[list[float], float]] = OrderedDict()

    def _is_expired(self, timestamp: float) -> bool:
        return time.monotonic() - timestamp > self._ttl

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [key for key, (_embedding, timestamp) in self._cache.items() if now - timestamp > self._ttl]
        for key in expired:
            del self._cache[key]

    def get(self, provider: str, model: str, text: str) -> list[float] | None:
        """Return a cached embedding if it exists and has not expired."""
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
        self._evict_expired()
        key = (provider, model, text)
        self._cache[key] = (embedding, time.monotonic())
        self._cache.move_to_end(key)
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        """Drop all cached embeddings."""
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
        """Lazy-load sentence-transformers model on first use."""
        if self._local_model is None:
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
            self._get_local_model()

    def get_info(self) -> dict:
        """Return embedding model configuration info."""
        dimensions = None
        if self._provider == "local" and self._local_model is not None:
            dimensions = self._local_model.get_sentence_embedding_dimension()
        elif self._provider == "local":
            defaults = {
                "all-MiniLM-L6-v2": 384,
                "all-mpnet-base-v2": 768,
                # 0.23.0: multilingual default.
                DEFAULT_LOCAL_EMBEDDING_MODEL: 384,
                "intfloat/multilingual-e5-base": 768,
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
        """Embed a single text string. Returns 384-dim float list for local model."""
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
_engine_init_lock = asyncio.Lock()


async def get_embedding_engine() -> EmbeddingEngine:
    """Return the singleton EmbeddingEngine, initializing on first call."""
    global _engine
    if _engine is None:
        async with _engine_init_lock:
            if _engine is None:
                _engine = EmbeddingEngine()
                await _engine.initialize()
    return _engine


async def get_embedding_info() -> dict:
    """Return embedding config info from the singleton engine."""
    engine = await get_embedding_engine()
    return engine.get_info()
