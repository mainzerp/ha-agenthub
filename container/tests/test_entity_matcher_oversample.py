"""Tests for candidate seeding in EntityMatcher after the embedding removal.

ENTITY_RESOLUTION_REWORK removed the embedding shortlist (and with it the
oversample sizing this file originally covered): entity matching is now
keyword/string-signal only. Without explicit ``candidates`` the matcher
seeds its candidate pool via token preselection
(``find_by_tokens_async``); with ``candidates`` the preselection lookup is
skipped entirely.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.entity.matcher import EntityMatcher


def _make_matcher() -> EntityMatcher:
    entity_index = MagicMock()
    entity_index.get_by_ids = MagicMock(return_value={})
    entity_index.get_by_ids_async = AsyncMock(return_value={})
    entity_index.find_by_tokens_async = AsyncMock(return_value=[])
    # Empty IDF map: all tokens count as unseen -> span coverage falls back
    # to plain unweighted token coverage (defined unseen-token behavior).
    entity_index.token_idf = MagicMock(return_value={})
    matcher = EntityMatcher(entity_index=entity_index, alias_resolver=object())
    matcher._top_n = 3
    matcher._apply_visibility_rules = AsyncMock(side_effect=lambda _agent, results: results)
    return matcher


def _entry(entity_id: str, friendly_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        entity_id=entity_id,
        friendly_name=friendly_name,
        aliases=[],
        area=None,
        area_name=None,
        domain=entity_id.split(".", 1)[0],
    )


@pytest.mark.asyncio
async def test_token_preselection_seeds_candidates():
    """Without explicit candidates, find_by_tokens_async seeds the pool."""
    matcher = _make_matcher()
    matcher._confidence_threshold = 0.0
    entry = _entry("light.kitchen", "Kitchen Light")
    matcher._entity_index.find_by_tokens_async = AsyncMock(return_value=[entry])
    with patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)):
        results = await matcher._match_query("kitchen")
    matcher._entity_index.find_by_tokens_async.assert_awaited_once()
    assert any(r.entity_id == "light.kitchen" for r in results)


@pytest.mark.asyncio
async def test_token_preselection_marker_is_diagnostics_only():
    """Rescued candidates carry the token_preselection marker signal."""
    matcher = _make_matcher()
    matcher._confidence_threshold = 0.0
    entry = _entry("light.kitchen", "Kitchen Light")
    matcher._entity_index.find_by_tokens_async = AsyncMock(return_value=[entry])
    with patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)):
        results = await matcher._match_query("kitchen")
    rescued = [r for r in results if r.entity_id == "light.kitchen"]
    assert rescued and rescued[0].signal_scores.get("token_preselection") == 1.0


@pytest.mark.asyncio
async def test_candidates_skip_token_preselection():
    """Explicit candidates: no preselection lookup, no marker."""
    matcher = _make_matcher()
    matcher._confidence_threshold = 0.0
    entry = _entry("light.kitchen", "Kitchen Light")
    with patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)):
        results = await matcher._match_query("kitchen", candidates=[entry])
    matcher._entity_index.find_by_tokens_async.assert_not_awaited()
    assert results and "token_preselection" not in results[0].signal_scores


@pytest.mark.asyncio
async def test_preselection_failure_is_contained():
    """A failing find_by_tokens_async leaves an empty pool, not an exception."""
    matcher = _make_matcher()
    matcher._entity_index.find_by_tokens_async = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)):
        results = await matcher._match_query("kitchen")
    assert results == []


@pytest.mark.asyncio
async def test_no_embedding_signal_in_matcher():
    """The matcher module must not reference the removed embedding signal."""
    import app.entity.matcher as matcher_module

    assert not hasattr(matcher_module, "EmbeddingSignal")


@pytest.mark.asyncio
async def test_load_config_has_no_embedding_weight():
    """The removed weight must not be read even when still present in the DB."""
    matcher = _make_matcher()

    class _FakeCursor:
        async def fetchall(self):
            return [
                ("weight.levenshtein", "0.20"),
                ("weight.embedding", "0.30"),
                ("weight.alias", "0.15"),
            ]

    class _FakeDB:
        async def execute(self, *_args, **_kwargs):
            return _FakeCursor()

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *_exc):
            return None

    def _get_db_read():
        return _FakeCtx()

    async def _get_value(_key, default=None):
        return default

    with (
        patch("app.db.schema.get_db_read", new=_get_db_read),
        patch("app.entity.matcher.SettingsRepository.get_value", new=_get_value),
    ):
        await matcher.load_config()
    assert "embedding" not in matcher._weights
    assert "levenshtein" in matcher._weights


@pytest.mark.asyncio
async def test_load_config_has_no_oversample_factor():
    """The removed setting must not be read or stored anymore."""
    matcher = _make_matcher()

    class _FakeCursor:
        async def fetchall(self):
            return []

    class _FakeDB:
        async def execute(self, *_args, **_kwargs):
            return _FakeCursor()

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *_exc):
            return None

    def _get_db_read():
        return _FakeCtx()

    requested_keys: list[str] = []

    async def _get_value(key, default=None):
        requested_keys.append(key)
        return default

    with (
        patch("app.db.schema.get_db_read", new=_get_db_read),
        patch("app.entity.matcher.SettingsRepository.get_value", new=_get_value),
    ):
        await matcher.load_config()
    assert not hasattr(matcher, "_oversample_factor")
    assert "entity_matching.oversample_factor" not in requested_keys
