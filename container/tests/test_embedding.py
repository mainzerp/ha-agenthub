"""Tests for app.cache.embedding external provider retry behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache.embedding import EmbeddingEngine, run_embedding_keepalive


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
