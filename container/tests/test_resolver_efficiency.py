"""Tests for P2 resolver efficiency: lazy listing and visible-entries reuse.

Covers:
- The exact entity_id stage runs BEFORE the full index listing; an
  exact-id hit performs no listing at all (lazy listing).
- A caller-supplied ``visible_entries`` snapshot skips the listing.
- ``resolve_and_validate_entity`` reuses the per-request ContextVar
  snapshot published via ``set_request_visible_entries`` (filtered by
  domain), and falls back to a fresh listing when the snapshot's domain
  coverage is not a superset of the requested domains.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.action_executor import (
    reset_request_visible_entries,
    resolve_and_validate_entity,
    set_request_visible_entries,
)
from app.entity.deterministic_resolver import resolve_entity_deterministic_first


def _entry(entity_id: str, friendly_name: str, *, aliases=None, area="", domain=None):
    return SimpleNamespace(
        entity_id=entity_id,
        friendly_name=friendly_name,
        aliases=aliases or [],
        area=area,
        domain=domain or entity_id.split(".", 1)[0],
    )


def _index(entries):
    index = MagicMock()
    index.list_entries_async = AsyncMock(return_value=list(entries))
    by_id = {e.entity_id: e for e in entries}
    index.get_by_id = MagicMock(side_effect=lambda eid: by_id.get(eid))
    index.get_by_id_async = AsyncMock(side_effect=lambda eid: by_id.get(eid))
    return index


@pytest.mark.asyncio
async def test_exact_entity_id_hit_performs_no_full_listing():
    """Lazy listing: an exact entity_id hit must not list the index."""
    entry = _entry("light.kitchen", "Kitchen")
    index = _index([entry])

    result = await resolve_entity_deterministic_first(
        "light.kitchen",
        index,
        None,
        None,
        allowed_domains=frozenset({"light"}),
    )

    assert result["entity_id"] == "light.kitchen"
    assert result["metadata"]["resolution_path"] == "exact_entity_id"
    index.list_entries_async.assert_not_called()
    # No listing happened -> no snapshot attached (callers must treat the
    # missing key as "no snapshot", not "no visible entities").
    assert "_visible_entries" not in result


@pytest.mark.asyncio
async def test_non_exact_query_lists_once_and_resolves_friendly_name():
    entry = _entry("light.kitchen", "Kitchen")
    index = _index([entry])

    result = await resolve_entity_deterministic_first(
        "kitchen",
        index,
        None,
        None,
        allowed_domains=frozenset({"light"}),
    )

    assert result["entity_id"] == "light.kitchen"
    assert result["metadata"]["resolution_path"] == "exact_friendly_name"
    index.list_entries_async.assert_awaited_once()
    assert result["_visible_entries"] == [entry]


@pytest.mark.asyncio
async def test_supplied_visible_entries_snapshot_skips_listing():
    entry = _entry("light.kitchen", "Kitchen")
    index = _index([entry])

    result = await resolve_entity_deterministic_first(
        "kitchen",
        index,
        None,
        None,
        allowed_domains=frozenset({"light"}),
        visible_entries=[entry],
    )

    assert result["entity_id"] == "light.kitchen"
    index.list_entries_async.assert_not_called()
    assert result["_visible_entries"] == [entry]


@pytest.mark.asyncio
async def test_alias_and_area_stages_match_with_normalized_reuse():
    entries = [
        _entry("light.floor_lamp", "Floor Lamp", aliases=["Stehlampe"], area="living room"),
        _entry("light.desk", "Desk", area="office"),
    ]
    index = _index(entries)

    alias_result = await resolve_entity_deterministic_first(
        "Stehlampe",
        index,
        None,
        None,
        allowed_domains=frozenset({"light"}),
    )
    assert alias_result["entity_id"] == "light.floor_lamp"
    assert alias_result["metadata"]["resolution_path"] == "exact_alias"

    area_result = await resolve_entity_deterministic_first(
        "office",
        index,
        None,
        None,
        allowed_domains=frozenset({"light"}),
        enable_area_fallback=True,
    )
    assert area_result["entity_id"] == "light.desk"
    assert area_result["metadata"]["resolution_path"] == "exact_area"


@pytest.mark.asyncio
async def test_resolve_and_validate_reuses_request_snapshot_filtered_by_domain():
    """The ContextVar snapshot skips re-listing and is domain-filtered."""
    entries = [
        _entry("light.kitchen", "Kitchen"),
        _entry("switch.kitchen", "Kitchen"),
    ]
    index = _index(entries)
    token = set_request_visible_entries(frozenset({"light", "switch"}), "light-agent", entries)
    try:
        with patch(
            "app.entity.deterministic_resolver.filter_visible_results",
            new=AsyncMock(side_effect=lambda _agent, results, _index: results),
        ) as vis_mock:
            resolved = await resolve_and_validate_entity(
                "kitchen",
                index,
                None,
                "light-agent",
                frozenset({"light"}),
                lambda eid: eid.startswith("light."),
            )
    finally:
        reset_request_visible_entries(token)

    assert resolved["entity_id"] == "light.kitchen"
    index.list_entries_async.assert_not_called()
    vis_mock.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_and_validate_falls_back_to_listing_when_domains_not_covered():
    """A snapshot whose domains do not cover the request must not be reused."""
    snapshot_entries = [_entry("light.kitchen", "Kitchen")]
    db_entries = [_entry("climate.living_room", "Living Room")]
    index = _index(db_entries)
    token = set_request_visible_entries(frozenset({"light"}), "climate-agent", snapshot_entries)
    try:
        with patch(
            "app.entity.deterministic_resolver.filter_visible_results",
            new=AsyncMock(side_effect=lambda _agent, results, _index: results),
        ):
            resolved = await resolve_and_validate_entity(
                "living room",
                index,
                None,
                "climate-agent",
                frozenset({"climate"}),
                lambda eid: eid.startswith("climate."),
            )
    finally:
        reset_request_visible_entries(token)

    assert resolved["entity_id"] == "climate.living_room"
    index.list_entries_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_and_validate_ignores_snapshot_of_other_agent():
    entries = [_entry("light.kitchen", "Kitchen")]
    index = _index(entries)
    token = set_request_visible_entries(frozenset({"light"}), "other-agent", entries)
    try:
        with patch(
            "app.entity.deterministic_resolver.filter_visible_results",
            new=AsyncMock(side_effect=lambda _agent, results, _index: results),
        ):
            resolved = await resolve_and_validate_entity(
                "kitchen",
                index,
                None,
                "light-agent",
                frozenset({"light"}),
                lambda eid: eid.startswith("light."),
            )
    finally:
        reset_request_visible_entries(token)

    assert resolved["entity_id"] == "light.kitchen"
    index.list_entries_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_visible_entries_param_still_supported():
    """Direct pass-through via the new parameter (no ContextVar involved)."""
    entry = _entry("cover.bedroom", "Bedroom Cover")
    index = _index([entry])
    resolved = await resolve_and_validate_entity(
        "bedroom cover",
        index,
        None,
        None,
        frozenset({"cover"}),
        lambda eid: eid.startswith("cover."),
        visible_entries=[entry],
    )
    assert resolved["entity_id"] == "cover.bedroom"
    index.list_entries_async.assert_not_called()
