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

    # Build a mock litellm module with a proper RateLimitError exception class
    mock_litellm = MagicMock()
    mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
    mock_litellm.exceptions.APIError = type("APIError", (Exception,), {})

    call_count = 0

    def _fake_embedding(*, model, input):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise mock_litellm.RateLimitError("rate limited")
        return MagicMock(data=[{"embedding": [0.1, 0.2, 0.3]}])

    mock_litellm.embedding = _fake_embedding

    with (
        patch.dict("sys.modules", {"litellm": mock_litellm}),
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await engine.embed_batch(["hello"])

    assert call_count == 2
    mock_sleep.assert_awaited_once()
    assert result == [[0.1, 0.2, 0.3]]
