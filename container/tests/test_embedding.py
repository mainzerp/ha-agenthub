"""Tests for app.cache.embedding external provider retry behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache.embedding import EmbeddingEngine


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
