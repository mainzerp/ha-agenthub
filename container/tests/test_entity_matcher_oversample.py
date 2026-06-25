"""Tests for the embedding shortlist oversample behaviour in EntityMatcher.

Validates §3 of docs/SubAgent/area_only_climate_agent_plan.md: when
``agent_id`` or ``preferred_domains`` is supplied, the matcher must
enlarge the embedding shortlist by ``oversample_factor``; otherwise the
shortlist stays at ``top_n * 2``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.entity.matcher import EntityMatcher, MatchResult


def _make_matcher() -> EntityMatcher:
    entity_index = MagicMock()
    entity_index.get_by_ids = MagicMock(return_value={})
    entity_index.get_by_ids_async = AsyncMock(return_value={})
    matcher = EntityMatcher(entity_index=entity_index, alias_resolver=object())
    matcher._top_n = 3
    matcher._oversample_factor = 20
    matcher._apply_visibility_rules = AsyncMock(side_effect=lambda _agent, results: results)
    return matcher


@pytest.mark.asyncio
async def test_oversample_factor_applied_when_agent_id_present():
    matcher = _make_matcher()
    with (
        patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)),
        patch("app.entity.matcher.EmbeddingSignal.score", new=AsyncMock(return_value=[])) as embed_mock,
    ):
        await matcher._match_query("flur", agent_id="climate-agent")
    embed_mock.assert_awaited_once()
    assert embed_mock.await_args.kwargs.get("n") == 20


@pytest.mark.asyncio
async def test_oversample_factor_applied_when_preferred_domains_present():
    matcher = _make_matcher()
    with (
        patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)),
        patch("app.entity.matcher.EmbeddingSignal.score", new=AsyncMock(return_value=[])) as embed_mock,
    ):
        await matcher._match_query("flur", preferred_domains=("climate", "sensor", "weather"))
    embed_mock.assert_awaited_once()
    assert embed_mock.await_args.kwargs.get("n") == 20


@pytest.mark.asyncio
async def test_oversample_factor_not_applied_for_unfiltered_query():
    matcher = _make_matcher()
    with (
        patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)),
        patch("app.entity.matcher.EmbeddingSignal.score", new=AsyncMock(return_value=[])) as embed_mock,
    ):
        await matcher._match_query("flur")
    embed_mock.assert_awaited_once()
    assert embed_mock.await_args.kwargs.get("n") == 6


@pytest.mark.asyncio
async def test_oversample_factor_clamped_on_load():
    matcher = _make_matcher()

    async def _settings(values):
        async def _get(key, default=None):
            return values.get(key, default)

        return _get

    # Stub entity_matching_config DB read to return empty rows.
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

    # Low value -> clamped to 2.
    with (
        patch("app.db.schema.get_db_read", new=_get_db_read),
        patch(
            "app.entity.matcher.SettingsRepository.get_value",
            new=AsyncMock(
                side_effect=lambda key, default=None: "0" if key == "entity_matching.oversample_factor" else default
            ),
        ),
    ):
        await matcher.load_config()
    assert matcher._oversample_factor == 2

    # High value -> clamped to 200.
    with (
        patch("app.db.schema.get_db_read", new=_get_db_read),
        patch(
            "app.entity.matcher.SettingsRepository.get_value",
            new=AsyncMock(
                side_effect=lambda key, default=None: "9999" if key == "entity_matching.oversample_factor" else default
            ),
        ),
    ):
        await matcher.load_config()
    assert matcher._oversample_factor == 200

    # Non-numeric -> falls back to default 20.
    with (
        patch("app.db.schema.get_db_read", new=_get_db_read),
        patch(
            "app.entity.matcher.SettingsRepository.get_value",
            new=AsyncMock(
                side_effect=lambda key, default=None: "abc" if key == "entity_matching.oversample_factor" else default
            ),
        ),
    ):
        await matcher.load_config()
    assert matcher._oversample_factor == 20


@pytest.mark.asyncio
async def test_oversample_factor_default_is_20():
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

    with (
        patch("app.db.schema.get_db_read", new=_get_db_read),
        patch(
            "app.entity.matcher.SettingsRepository.get_value",
            new=AsyncMock(side_effect=lambda key, default=None: default),
        ),
    ):
        await matcher.load_config()
    assert matcher._oversample_factor == 20


@pytest.mark.asyncio
async def test_query_normalization_regression():
    """CRIT-1: query must be lowercased and stripped before _normalize_for_containment."""
    matcher = _make_matcher()
    captured = []

    def _capture_normalize(text: str) -> str:
        captured.append(text)
        return text.lower().strip()

    with (
        patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)),
        patch("app.entity.matcher.EmbeddingSignal.score", new=AsyncMock(return_value=[])),
        patch("app.entity.matcher._normalize_for_containment", side_effect=_capture_normalize),
    ):
        await matcher._match_query("  KiTcHeN  ")

    assert captured, "_normalize_for_containment was not called"
    assert captured[0] == "kitchen", f"expected 'kitchen', got {captured[0]!r}"


@pytest.mark.asyncio
async def test_hidden_entities_excluded_before_scoring():
    """Visibility filtering must run before Levenshtein/phonetic scoring."""
    matcher = _make_matcher()
    matcher._weights = {
        "levenshtein": 0.2,
        "jaro_winkler": 0.2,
        "phonetic": 0.2,
        "embedding": 0.2,
        "alias": 0.2,
    }
    matcher._confidence_threshold = 0.0

    visible = MatchResult(
        entity_id="light.visible",
        friendly_name="Visible Light",
        score=0.0,
        signal_scores={"embedding": 0.5},
    )

    with (
        patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)),
        patch(
            "app.entity.matcher.EmbeddingSignal.score",
            new=AsyncMock(
                return_value=[
                    ("light.hidden", "Hidden Light", 0.95),
                    ("light.visible", "Visible Light", 0.5),
                ]
            ),
        ) as embed_mock,
        patch("app.entity.matcher.LevenshteinSignal.score", return_value=0.0) as lev_mock,
        patch.object(
            matcher,
            "_apply_visibility_rules",
            new=AsyncMock(return_value=[visible]),
        ) as vis_mock,
    ):
        results = await matcher._match_query("hidden light", agent_id="restricted")

    embed_mock.assert_awaited_once()
    vis_mock.assert_awaited_once()
    called_ids = {r.entity_id for r in vis_mock.await_args.args[1]}
    assert called_ids == {"light.hidden", "light.visible"}
    assert not any(r.entity_id == "light.hidden" for r in results)
    scored_friendly_names = {call.args[1] for call in lev_mock.call_args_list}
    assert "Hidden Light" not in scored_friendly_names
