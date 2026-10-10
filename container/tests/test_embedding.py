"""Tests for app.cache.embedding: engine init, local model loading, retries, keep-alive."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.cache.embedding as embedding_module
from app.cache.embedding import EmbeddingEngine, get_embedding_engine, run_embedding_keepalive


@pytest.mark.asyncio
async def test_embed_batch_rate_limit_retries_with_asyncio_sleep():
    """When litellm.embedding raises RateLimitError, asyncio.sleep must be awaited between retries."""
    engine = EmbeddingEngine()
    engine._provider = "openrouter"
    engine._model_name = "openrouter/text-embedding-3-small"

    class FakeRateLimitError(Exception):
        pass

    call_count = 0

    def _fake_embedding(*, model, input, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise FakeRateLimitError("rate limited")
        return MagicMock(data=[{"embedding": [0.1, 0.2, 0.3]}])

    with (
        patch("litellm.embedding", _fake_embedding),
        patch("litellm.RateLimitError", FakeRateLimitError),
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch("app.llm.providers.retrieve_secret", new_callable=AsyncMock, return_value="sk-test"),
    ):
        result = await engine.embed_batch(["hello"])

    assert call_count == 2
    mock_sleep.assert_awaited_once()
    assert result == [[0.1, 0.2, 0.3]]


class TestEmbeddingEngineCallsEngine:
    """EmbeddingEngine.embed_batch must be awaitable from sync contexts (sqlite-vec VectorStore shim).

    The sqlite-vec VectorStore embeds query/document text synchronously via
    the same event-loop-safe shim the old ChromaEmbeddingFunction used. These
    tests verify EmbeddingEngine.embed_batch is callable from both a thread
    (no running loop) and the event-loop thread without deadlocking.
    """

    def test_embed_batch_from_thread_does_not_deadlock(self):
        import asyncio
        import threading

        engine = EmbeddingEngine()
        engine._provider = "local"

        async def _fake_embed(texts):
            return [[0.1, 0.2, 0.3]]

        engine.embed_batch = _fake_embed

        results = []

        def _target():
            coro = engine.embed_batch(["hello"])
            results.append(asyncio.run(coro))

        t = threading.Thread(target=_target)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "Thread deadlocked"
        assert len(results) == 1
        assert list(results[0][0]) == [0.1, 0.2, 0.3]

    def test_embed_batch_from_event_loop_does_not_deadlock(self):
        import asyncio

        engine = EmbeddingEngine()
        engine._provider = "local"

        async def _fake_embed(texts):
            return [[0.4, 0.5, 0.6]]

        engine.embed_batch = _fake_embed

        async def _run():
            return await engine.embed_batch(["world"])

        result = asyncio.run(_run())
        assert len(result) == 1
        assert list(result[0]) == [0.4, 0.5, 0.6]
        result = asyncio.run(_run())
        assert len(result) == 1
        assert list(result[0]) == [0.4, 0.5, 0.6]


class TestRunEmbeddingKeepalive:
    """Periodic keep-alive loop (run_embedding_keepalive).

    Follows the run_periodic test pattern from test_cache_validator.py:
    SettingsRepository.get_value is stubbed via AsyncMock side_effect and
    asyncio.sleep records durations, raising CancelledError after N calls.
    """

    @pytest.mark.asyncio
    async def test_local_provider_embeds_each_iteration(self):
        engine = MagicMock()
        engine.embed = AsyncMock(return_value=[0.1])
        sleep_calls: list[float] = []

        async def _mock_sleep(duration):
            sleep_calls.append(duration)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError()

        with (
            patch("app.cache.embedding.SettingsRepository") as mock_settings,
            patch("app.cache.embedding.get_embedding_engine", new=AsyncMock(return_value=engine)),
            patch("asyncio.sleep", _mock_sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            mock_settings.get_value = AsyncMock(
                side_effect=lambda key, default="": {
                    "embedding.keepalive_interval_minutes": "15",
                    "embedding.provider": "local",
                }.get(key, default)
            )
            await run_embedding_keepalive()

        assert engine.embed.await_count == 2
        assert sleep_calls == [900, 900]

    @pytest.mark.asyncio
    async def test_external_provider_skips_embed_but_still_sleeps(self):
        sleep_calls: list[float] = []

        async def _mock_sleep(duration):
            sleep_calls.append(duration)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError()

        with (
            patch("app.cache.embedding.SettingsRepository") as mock_settings,
            patch("app.cache.embedding.get_embedding_engine", new=AsyncMock()) as mock_get_engine,
            patch("asyncio.sleep", _mock_sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            mock_settings.get_value = AsyncMock(
                side_effect=lambda key, default="": {
                    "embedding.keepalive_interval_minutes": "15",
                    "embedding.provider": "openrouter",
                }.get(key, default)
            )
            await run_embedding_keepalive()

        mock_get_engine.assert_not_called()
        assert sleep_calls == [900, 900]

    @pytest.mark.asyncio
    async def test_disabled_interval_sleeps_short_recheck(self):
        sleep_calls: list[float] = []

        async def _mock_sleep(duration):
            sleep_calls.append(duration)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError()

        with (
            patch("app.cache.embedding.SettingsRepository") as mock_settings,
            patch("app.cache.embedding.get_embedding_engine", new=AsyncMock()) as mock_get_engine,
            patch("asyncio.sleep", _mock_sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            mock_settings.get_value = AsyncMock(
                side_effect=lambda key, default="": {
                    "embedding.keepalive_interval_minutes": "0",
                    "embedding.provider": "local",
                }.get(key, default)
            )
            await run_embedding_keepalive()

        mock_get_engine.assert_not_called()
        assert sleep_calls == [300, 300]


def _fake_sentence_transformers(factory) -> dict[str, types.ModuleType]:
    """sys.modules overrides so _get_local_model never imports torch or loads weights."""
    sentence_transformers_module = types.ModuleType("sentence_transformers")
    sentence_transformers_module.SentenceTransformer = factory
    huggingface_hub_module = types.ModuleType("huggingface_hub")
    huggingface_hub_module.disable_progress_bars = MagicMock()
    return {"sentence_transformers": sentence_transformers_module, "huggingface_hub": huggingface_hub_module}


def _reset_engine_singleton(monkeypatch, *, cooldown: float = 30.0) -> None:
    monkeypatch.setattr(embedding_module, "_engine", None)
    monkeypatch.setattr(embedding_module, "_engine_init_task", None)
    monkeypatch.setattr(embedding_module, "_engine_init_failed_at", None)
    monkeypatch.setattr(embedding_module, "_engine_init_error", None)
    monkeypatch.setattr(embedding_module, "_ENGINE_INIT_RETRY_COOLDOWN_S", cooldown)


class TestGetEmbeddingEngineSingleton:
    """The singleton is published only after initialize() completed."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", [RuntimeError("settings read failed"), asyncio.CancelledError()])
    async def test_failed_initialize_does_not_publish_singleton_and_retries(self, monkeypatch, failure):
        # Cooldown 0: this test covers publish/retry, not the failure backoff.
        _reset_engine_singleton(monkeypatch, cooldown=0.0)
        calls = 0

        async def _initialize(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise failure
            self._provider = "local"
            self._model_name = "all-MiniLM-L6-v2"

        monkeypatch.setattr(EmbeddingEngine, "initialize", _initialize)

        with pytest.raises(type(failure)):
            await get_embedding_engine()
        assert embedding_module._engine is None

        engine = await get_embedding_engine()
        assert calls == 2
        assert embedding_module._engine is engine
        assert engine._model_name == "all-MiniLM-L6-v2"
        assert await get_embedding_engine() is engine
        assert calls == 2

    @pytest.mark.asyncio
    async def test_failed_initialize_backs_off_before_retrying(self, monkeypatch):
        """#132: a failed init must not be re-run on every miss/store."""
        _reset_engine_singleton(monkeypatch, cooldown=30.0)
        calls = 0

        async def _initialize(self):
            nonlocal calls
            calls += 1
            raise RuntimeError("model download failed")

        monkeypatch.setattr(EmbeddingEngine, "initialize", _initialize)

        with pytest.raises(RuntimeError, match="model download failed"):
            await get_embedding_engine()
        for _ in range(5):
            with pytest.raises(embedding_module.EmbeddingEngineUnavailableError):
                await get_embedding_engine()
        assert calls == 1

        # Once the cooldown has elapsed, the next caller retries.
        monkeypatch.setattr(embedding_module, "_engine_init_failed_at", time.monotonic() - 31.0)
        with pytest.raises(RuntimeError, match="model download failed"):
            await get_embedding_engine()
        assert calls == 2

    @pytest.mark.asyncio
    async def test_cancelled_caller_does_not_abandon_or_duplicate_init(self, monkeypatch):
        """#132: cancelling a waiting caller must not start a second, parallel model load."""
        _reset_engine_singleton(monkeypatch)
        calls = 0
        release = asyncio.Event()

        async def _initialize(self):
            nonlocal calls
            calls += 1
            await release.wait()
            self._provider = "local"
            self._model_name = "all-MiniLM-L6-v2"

        monkeypatch.setattr(EmbeddingEngine, "initialize", _initialize)

        first = asyncio.create_task(get_embedding_engine())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(get_embedding_engine())
        await asyncio.sleep(0)
        release.set()
        engine = await second

        assert calls == 1
        assert embedding_module._engine is engine
        assert engine.model_id == "local:all-MiniLM-L6-v2"


class TestEmbeddingCacheThreadSafety:
    def test_concurrent_get_set_from_threads(self):
        """#132: the LRU is mutated from the loop and from worker threads."""
        cache = embedding_module._EmbeddingCache(maxsize=16, ttl=300.0)
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def _worker(worker_id: int) -> None:
            try:
                barrier.wait()
                for i in range(2000):
                    text = f"t-{(worker_id * 7 + i) % 64}"
                    cache.set("local", "m", text, [float(i)])
                    cache.get("local", "m", text)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors
        assert len(cache._cache) <= 16


class TestLocalModelLoading:
    def test_concurrent_get_local_model_constructs_model_once(self):
        engine = EmbeddingEngine()
        engine._model_name = "all-MiniLM-L6-v2"
        constructed: list[str] = []

        def _slow_sentence_transformer(model_name):
            constructed.append(model_name)
            time.sleep(0.05)  # widen the race window
            return object()

        thread_count = 8
        barrier = threading.Barrier(thread_count)
        results: list[object] = []
        results_lock = threading.Lock()

        def _worker():
            barrier.wait()
            model = engine._get_local_model()
            with results_lock:
                results.append(model)

        with patch.dict(sys.modules, _fake_sentence_transformers(_slow_sentence_transformer)):
            threads = [threading.Thread(target=_worker) for _ in range(thread_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        assert constructed == ["all-MiniLM-L6-v2"]
        assert len(results) == thread_count
        assert all(model is engine._local_model for model in results)

    @pytest.mark.parametrize("model_name", [None, ""])
    def test_get_local_model_without_model_name_raises_clear_error(self, model_name):
        engine = EmbeddingEngine()
        engine._provider = "local"
        engine._model_name = model_name
        factory = MagicMock()

        with (
            patch.dict(sys.modules, _fake_sentence_transformers(factory)),
            pytest.raises(RuntimeError, match="model name is not configured"),
        ):
            engine._get_local_model()

        factory.assert_not_called()
        assert engine._local_model is None

    @pytest.mark.asyncio
    async def test_initialize_loads_local_model_off_event_loop(self):
        engine = EmbeddingEngine()
        loop_thread = threading.get_ident()
        load_thread: list[int] = []
        released = threading.Event()
        released_seen: list[bool] = []

        def _blocking_load():
            load_thread.append(threading.get_ident())
            # Only a free event loop can run _release(); on the loop this would
            # block until the timeout and record False.
            released_seen.append(released.wait(timeout=5))
            return MagicMock()

        async def _release():
            released.set()

        async def _get_value(key, default=None):
            return "local" if key == "embedding.provider" else default

        with (
            patch("app.cache.embedding.SettingsRepository.get_value", new=AsyncMock(side_effect=_get_value)),
            patch.object(engine, "_get_local_model", side_effect=_blocking_load),
        ):
            release_task = asyncio.create_task(_release())
            await engine.initialize()
            await release_task

        assert load_thread and load_thread[0] != loop_thread
        assert released_seen == [True]
