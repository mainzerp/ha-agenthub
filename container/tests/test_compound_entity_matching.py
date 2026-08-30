"""Tests for compound-word entity matching (M-A + M-B).

M-A: the deterministic resolver's exact friendly_name stage compares
space-insensitively, so a compound query like "Innenhofüberdachung"
matches the friendly_name "Innenhof Überdachung".

M-B: the matcher admits a query span without exact token intersection
when an entity token (>= 4 chars) is a substring of a span token
(compound containment) and counts it towards span coverage, so the
Floor-Regel fires for full compounds.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.entity.matcher import EntityMatcher


def _entry(entity_id: str, friendly_name: str, *, aliases=None, area=""):
    return SimpleNamespace(
        entity_id=entity_id,
        friendly_name=friendly_name,
        aliases=aliases or [],
        area=area,
        area_name="",
        domain=entity_id.split(".", 1)[0],
    )


def _index(entries):
    index = MagicMock()
    index.list_entries_async = AsyncMock(return_value=list(entries))
    by_id = {e.entity_id: e for e in entries}
    index.get_by_id = MagicMock(side_effect=lambda eid: by_id.get(eid))
    index.get_by_id_async = AsyncMock(side_effect=lambda eid: by_id.get(eid))
    return index


def _make_matcher(entries) -> EntityMatcher:
    entity_index = MagicMock()
    by_id = {e.entity_id: e for e in entries}
    entity_index.get_by_ids = MagicMock(return_value=by_id)
    entity_index.get_by_ids_async = AsyncMock(return_value=by_id)
    entity_index.find_by_tokens_async = AsyncMock(return_value=[])
    # Empty IDF map: unseen tokens -> plain unweighted span coverage.
    entity_index.token_idf = MagicMock(return_value={})
    matcher = EntityMatcher(entity_index=entity_index, alias_resolver=object())
    matcher._top_n = 3
    matcher._apply_visibility_rules = AsyncMock(side_effect=lambda _agent, results: results)
    return matcher


# --- M-A: space-insensitive exact friendly_name compare -------------------


@pytest.mark.asyncio
async def test_compound_query_matches_spaced_friendly_name():
    entry = _entry("light.innenhof_uberdachung", "Innenhof Überdachung")
    index = _index([entry])

    result = await resolve_entity_deterministic_first(
        "Innenhofüberdachung",
        index,
        None,
        None,
        allowed_domains=frozenset({"light"}),
    )

    assert result["entity_id"] == "light.innenhof_uberdachung"
    assert result["metadata"]["resolution_path"] == "exact_friendly_name"


@pytest.mark.asyncio
async def test_spaced_query_still_matches_exact_friendly_name():
    """Regression: the spaced spelling keeps resolving as before."""
    entry = _entry("light.innenhof_uberdachung", "Innenhof Überdachung")
    index = _index([entry])

    result = await resolve_entity_deterministic_first(
        "Innenhof Überdachung",
        index,
        None,
        None,
        allowed_domains=frozenset({"light"}),
    )

    assert result["entity_id"] == "light.innenhof_uberdachung"
    assert result["metadata"]["resolution_path"] == "exact_friendly_name"


# --- M-B: compound-aware span admission + coverage -------------------------


@pytest.mark.asyncio
async def test_matcher_compound_span_reaches_floor():
    entry = _entry("light.innenhof_uberdachung", "Innenhof Überdachung")
    matcher = _make_matcher([entry])

    with patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)):
        results = await matcher._match_query("Innenhofüberdachung", candidates=[entry])

    by_id = {r.entity_id: r for r in results}
    assert "light.innenhof_uberdachung" in by_id
    assert by_id["light.innenhof_uberdachung"].score >= 0.65


@pytest.mark.asyncio
async def test_matcher_short_token_compound_not_admitted():
    """Tokens shorter than the guard (< 4 chars) never count as compounds."""
    entry = _entry("light.hof", "Hof")
    matcher = _make_matcher([entry])

    with patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)):
        results = await matcher._match_query("Hofüberdachung", candidates=[entry])

    assert "light.hof" not in {r.entity_id for r in results}


@pytest.mark.asyncio
async def test_matcher_spaced_query_still_matches():
    """Regression: spaced queries resolve via exact token intersection."""
    entry = _entry("light.innenhof_uberdachung", "Innenhof Überdachung")
    matcher = _make_matcher([entry])

    with patch("app.entity.matcher.AliasSignal.score", new=AsyncMock(return_value=None)):
        results = await matcher._match_query("Innenhof Überdachung", candidates=[entry])

    assert "light.innenhof_uberdachung" in {r.entity_id for r in results}


# --- Word-boundary containment stage (embedding-free partial-name fallback) --


@pytest.mark.asyncio
async def test_containment_resolves_partial_name_with_domain_filter():
    """ "front door" resolves "Front Door Lock" inside the lock-domain universe."""
    lock = _entry("lock.front_door", "Front Door Lock")
    index = _index([lock])

    result = await resolve_entity_deterministic_first(
        "front door",
        index,
        None,
        None,
        allowed_domains=frozenset({"lock"}),
    )

    assert result["entity_id"] == "lock.front_door"
    assert result["metadata"]["resolution_path"] == "friendly_name_containment"


@pytest.mark.asyncio
async def test_containment_preferred_domain_disambiguates():
    """Two same-named matches across domains: preferred_domain picks one."""
    lock = _entry("lock.front_door", "Front Door Lock")
    camera = _entry("camera.front_door", "Front Door Camera")
    index = _index([lock, camera])

    result = await resolve_entity_deterministic_first(
        "front door",
        index,
        None,
        None,
        allowed_domains=frozenset({"lock", "camera"}),
        preferred_domain="camera",
    )

    assert result["entity_id"] == "camera.front_door"
    assert result["metadata"]["resolution_path"] == "friendly_name_containment"


@pytest.mark.asyncio
async def test_containment_ambiguous_fails_closed():
    """Two same-domain containment matches: no silent pick, ask instead."""
    first = _entry("light.kitchen_ceiling", "Kitchen Ceiling")
    second = _entry("light.kitchen_spots", "Kitchen Spots")
    index = _index([first, second])

    result = await resolve_entity_deterministic_first(
        "kitchen",
        index,
        None,
        None,
        allowed_domains=frozenset({"light"}),
    )

    assert result["entity_id"] is None
    assert result["metadata"]["resolution_path"] == "friendly_name_containment_ambiguous"
    assert "Multiple entities match" in (result["speech"] or "")


@pytest.mark.asyncio
async def test_containment_no_substring_token_boundary_guard():
    """A non-boundary substring ("art" inside "start") must not match."""
    entry = _entry("light.start", "Start")
    index = _index([entry])

    result = await resolve_entity_deterministic_first(
        "art",
        index,
        None,
        None,
        allowed_domains=frozenset({"light"}),
    )

    assert result["entity_id"] is None
