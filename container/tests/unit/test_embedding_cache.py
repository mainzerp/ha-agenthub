"""Unit tests for the EmbeddingEngine in-memory embedding cache."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from app.cache.embedding import EmbeddingEngine, _EmbeddingCache


class TestEmbeddingCache:
    def test_cache_hit_returns_embedding_without_recomputing(self):
        cache = _EmbeddingCache(maxsize=10, ttl=60.0)
        cache.set("local", "all-MiniLM-L6-v2", "hello", [0.1, 0.2, 0.3])
        assert cache.get("local", "all-MiniLM-L6-v2", "hello") == [0.1, 0.2, 0.3]

    def test_cache_distinct_texts_store_separate_embeddings(self):
        cache = _EmbeddingCache(maxsize=10, ttl=60.0)
        cache.set("local", "all-MiniLM-L6-v2", "hello", [0.1, 0.2, 0.3])
        cache.set("local", "all-MiniLM-L6-v2", "world", [0.4, 0.5, 0.6])
        assert cache.get("local", "all-MiniLM-L6-v2", "hello") == [0.1, 0.2, 0.3]
        assert cache.get("local", "all-MiniLM-L6-v2", "world") == [0.4, 0.5, 0.6]

    def test_cache_distinct_models_store_separate_embeddings(self):
        cache = _EmbeddingCache(maxsize=10, ttl=60.0)
        cache.set("local", "model-a", "hello", [0.1, 0.2])
        cache.set("local", "model-b", "hello", [0.3, 0.4])
        assert cache.get("local", "model-a", "hello") == [0.1, 0.2]
        assert cache.get("local", "model-b", "hello") == [0.3, 0.4]

    def test_cache_ttl_eviction(self):
        cache = _EmbeddingCache(maxsize=10, ttl=0.1)
        cache.set("local", "all-MiniLM-L6-v2", "hello", [0.1, 0.2, 0.3])
        assert cache.get("local", "all-MiniLM-L6-v2", "hello") == [0.1, 0.2, 0.3]
        time.sleep(0.15)
        assert cache.get("local", "all-MiniLM-L6-v2", "hello") is None

    def test_cache_lru_eviction(self):
        cache = _EmbeddingCache(maxsize=2, ttl=60.0)
        cache.set("local", "model", "a", [0.1])
        cache.set("local", "model", "b", [0.2])
        cache.set("local", "model", "c", [0.3])
        assert cache.get("local", "model", "a") is None
        assert cache.get("local", "model", "b") == [0.2]
        assert cache.get("local", "model", "c") == [0.3]

    def test_cache_lru_updates_on_access(self):
        cache = _EmbeddingCache(maxsize=2, ttl=60.0)
        cache.set("local", "model", "a", [0.1])
        cache.set("local", "model", "b", [0.2])
        cache.get("local", "model", "a")  # touch a
        cache.set("local", "model", "c", [0.3])
        assert cache.get("local", "model", "a") == [0.1]
        assert cache.get("local", "model", "b") is None
        assert cache.get("local", "model", "c") == [0.3]


class TestEmbeddingEngineCache:
    @pytest.mark.asyncio
    async def test_embed_batch_local_caches_second_call(self):
        engine = EmbeddingEngine()
        engine._provider = "local"
        engine._model_name = "all-MiniLM-L6-v2"

        with patch.object(engine, "_embed_local", return_value=[[0.1, 0.2]]) as mock_embed:
            result1 = await engine.embed_batch(["hello"])
            result2 = await engine.embed_batch(["hello"])

        assert result1 == [[0.1, 0.2]]
        assert result2 == [[0.1, 0.2]]
        assert mock_embed.call_count == 1

    @pytest.mark.asyncio
    async def test_embed_batch_external_caches_second_call(self):
        engine = EmbeddingEngine()
        engine._provider = "openrouter"
        engine._model_name = "openrouter/text-embedding-3-small"

        with patch.object(engine, "_embed_external", new_callable=AsyncMock, return_value=[[0.1, 0.2]]) as mock_embed:
            result1 = await engine.embed_batch(["hello"])
            result2 = await engine.embed_batch(["hello"])

        assert result1 == [[0.1, 0.2]]
        assert result2 == [[0.1, 0.2]]
        assert mock_embed.await_count == 1

    @pytest.mark.asyncio
    async def test_embed_batch_mixed_cache_hit_and_miss(self):
        engine = EmbeddingEngine()
        engine._provider = "local"
        engine._model_name = "all-MiniLM-L6-v2"

        with patch.object(engine, "_embed_local", return_value=[[0.1], [0.2]]) as mock_embed:
            result1 = await engine.embed_batch(["hello", "world"])
            assert mock_embed.call_count == 1
            assert result1 == [[0.1], [0.2]]

        with patch.object(engine, "_embed_local", return_value=[[0.3]]) as mock_embed:
            result2 = await engine.embed_batch(["hello", "new"])
            assert mock_embed.call_count == 1
            assert result2 == [[0.1], [0.3]]

    @pytest.mark.asyncio
    async def test_embed_uses_batch_cache(self):
        engine = EmbeddingEngine()
        engine._provider = "local"
        engine._model_name = "all-MiniLM-L6-v2"

        with patch.object(engine, "_embed_local", return_value=[[0.1, 0.2]]) as mock_embed:
            result1 = await engine.embed("hello")
            result2 = await engine.embed("hello")

        assert result1 == [0.1, 0.2]
        assert result2 == [0.1, 0.2]
        assert mock_embed.call_count == 1
