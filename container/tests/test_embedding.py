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


class TestChromaEmbeddingFunction:
    """CRIT-3: ChromaEmbeddingFunction must not deadlock when called from the event loop."""

    def test_call_from_event_loop_does_not_deadlock(self):
        """Calling __call__ from the event loop thread must complete without deadlock."""
        import asyncio

        from app.cache.embedding import ChromaEmbeddingFunction, EmbeddingEngine

        engine = EmbeddingEngine()
        engine._provider = "local"

        async def _fake_embed(texts):
            return [[0.1, 0.2, 0.3]]

        engine.embed_batch = _fake_embed
        fn = ChromaEmbeddingFunction(engine)

        async def _run():
            return fn(["hello"])

        result = asyncio.run(_run())
        assert len(result) == 1
        assert list(result[0]) == [0.1, 0.2, 0.3]

    def test_call_from_thread_does_not_deadlock(self):
        """Calling __call__ from a non-event-loop thread must complete without deadlock."""
        import threading

        from app.cache.embedding import ChromaEmbeddingFunction, EmbeddingEngine

        engine = EmbeddingEngine()
        engine._provider = "local"

        async def _fake_embed(texts):
            return [[0.4, 0.5, 0.6]]

        engine.embed_batch = _fake_embed
        fn = ChromaEmbeddingFunction(engine)

        results = []

        def _target():
            results.append(fn(["world"]))

        t = threading.Thread(target=_target)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "Thread deadlocked"
        assert len(results) == 1
        assert list(results[0][0]) == [0.4, 0.5, 0.6]
