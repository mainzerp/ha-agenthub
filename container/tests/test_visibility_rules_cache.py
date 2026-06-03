"""Tests for visibility rules in-memory cache and N+1 metadata lookup elimination."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.entity.visibility import (
    _rules_cache,
    entity_is_visible,
    filter_visible_results,
    invalidate_visibility_rules_cache,
)
from tests.helpers import make_entity_index_entry

pytestmark = pytest.mark.asyncio


class TestVisibilityRulesCache:
    async def test_second_call_with_same_agent_id_hits_cache(self):
        mock_repo = AsyncMock()
        mock_repo.get_rules = AsyncMock(return_value=[{"rule_type": "domain_include", "rule_value": "light"}])

        results = [MagicMock(entity_id="light.kitchen")]

        await filter_visible_results("agent-1", results, None, repository=mock_repo)
        await filter_visible_results("agent-1", results, None, repository=mock_repo)

        mock_repo.get_rules.assert_awaited_once()

    async def test_invalidate_cache_causes_next_call_to_hit_repository(self):
        mock_repo = AsyncMock()
        mock_repo.get_rules = AsyncMock(return_value=[{"rule_type": "domain_include", "rule_value": "light"}])

        results = [MagicMock(entity_id="light.kitchen")]

        await filter_visible_results("agent-1", results, None, repository=mock_repo)
        invalidate_visibility_rules_cache("agent-1")
        await filter_visible_results("agent-1", results, None, repository=mock_repo)

        assert mock_repo.get_rules.await_count == 2

    async def test_invalidate_all_clears_entire_cache(self):
        mock_repo = AsyncMock()
        mock_repo.get_rules = AsyncMock(return_value=[{"rule_type": "domain_include", "rule_value": "light"}])

        results = [MagicMock(entity_id="light.kitchen")]

        await filter_visible_results("agent-1", results, None, repository=mock_repo)
        await filter_visible_results("agent-2", results, None, repository=mock_repo)
        invalidate_visibility_rules_cache()
        await filter_visible_results("agent-1", results, None, repository=mock_repo)
        await filter_visible_results("agent-2", results, None, repository=mock_repo)

        assert mock_repo.get_rules.await_count == 4

    async def test_entity_is_visible_uses_cache(self):
        mock_repo = AsyncMock()
        mock_repo.get_rules = AsyncMock(return_value=[{"rule_type": "domain_include", "rule_value": "light"}])

        await entity_is_visible("agent-1", "light.kitchen", None, repository=mock_repo)
        await entity_is_visible("agent-1", "light.bedroom", None, repository=mock_repo)

        mock_repo.get_rules.assert_awaited_once()


class TestVisibilityPreloadedEntry:
    async def test_filter_with_entity_index_entry_makes_zero_index_lookups(self):
        mock_repo = AsyncMock()
        mock_repo.get_rules = AsyncMock(
            return_value=[
                {"rule_type": "domain_include", "rule_value": "sensor"},
                {"rule_type": "area_include", "rule_value": "kitchen"},
            ]
        )

        entity_index = MagicMock()
        entity_index.get_by_id_async = AsyncMock()

        entries = [
            make_entity_index_entry("sensor.temp", "Temperature", domain="sensor", area="kitchen"),
            make_entity_index_entry("sensor.humidity", "Humidity", domain="sensor", area="bedroom"),
        ]

        filtered = await filter_visible_results("agent-1", entries, entity_index, repository=mock_repo)

        entity_index.get_by_id_async.assert_not_awaited()
        assert len(filtered) == 1
        assert filtered[0].entity_id == "sensor.temp"

    async def test_filter_with_entity_index_entry_uses_device_class_from_preloaded(self):
        mock_repo = AsyncMock()
        mock_repo.get_rules = AsyncMock(
            return_value=[
                {"rule_type": "domain_include", "rule_value": "sensor"},
                {"rule_type": "device_class_include", "rule_value": "temperature"},
            ]
        )

        entity_index = MagicMock()
        entity_index.get_by_id_async = AsyncMock()

        entries = [
            make_entity_index_entry("sensor.temp", "Temperature", domain="sensor", device_class="temperature"),
            make_entity_index_entry("sensor.power", "Power", domain="sensor", device_class="power"),
        ]

        filtered = await filter_visible_results("agent-1", entries, entity_index, repository=mock_repo)

        entity_index.get_by_id_async.assert_not_awaited()
        assert len(filtered) == 1
        assert filtered[0].entity_id == "sensor.temp"

    async def test_filter_fallback_to_index_when_no_preloaded_attributes(self):
        from app.entity.index import EntityIndex

        mock_repo = AsyncMock()
        mock_repo.get_rules = AsyncMock(
            return_value=[
                {"rule_type": "domain_include", "rule_value": "sensor"},
                {"rule_type": "area_include", "rule_value": "kitchen"},
            ]
        )

        kitchen_entry = make_entity_index_entry("sensor.temp", "Temperature", domain="sensor", area="kitchen")
        entity_index = MagicMock(spec=EntityIndex)
        entity_index.get_by_id = MagicMock(return_value=kitchen_entry)

        class SimpleResult:
            entity_id = "sensor.temp"

        results = [SimpleResult()]

        filtered = await filter_visible_results("agent-1", results, entity_index, repository=mock_repo)

        entity_index.get_by_id.assert_called_once_with("sensor.temp")
        assert len(filtered) == 1
        assert filtered[0].entity_id == "sensor.temp"


class TestVisibilityRepositoryInvalidation:
    async def test_set_rules_invalidates_cache(self, db_repository):
        from app.db.repositories.entity_visibility import EntityVisibilityRepository
        from app.entity.visibility import _get_cached_rules

        await EntityVisibilityRepository.set_rules("agent-1", [{"rule_type": "domain_include", "rule_value": "light"}])

        rules = await _get_cached_rules("agent-1", repository=EntityVisibilityRepository)
        assert rules is not None
        assert rules.domain_include == {"light"}

        await EntityVisibilityRepository.set_rules("agent-1", [])

        assert "agent-1" not in _rules_cache

    async def test_add_rule_invalidates_cache(self, db_repository):
        from app.db.repositories.entity_visibility import EntityVisibilityRepository
        from app.entity.visibility import _get_cached_rules

        await EntityVisibilityRepository.set_rules("agent-1", [{"rule_type": "domain_include", "rule_value": "light"}])
        rules = await _get_cached_rules("agent-1", repository=EntityVisibilityRepository)
        assert rules is not None

        await EntityVisibilityRepository.add_rule("agent-1", "domain_include", "switch")

        assert "agent-1" not in _rules_cache

    async def test_remove_rule_invalidates_cache(self, db_repository):
        from app.db.repositories.entity_visibility import EntityVisibilityRepository
        from app.entity.visibility import _get_cached_rules

        await EntityVisibilityRepository.set_rules("agent-1", [{"rule_type": "domain_include", "rule_value": "light"}])
        rules = await _get_cached_rules("agent-1", repository=EntityVisibilityRepository)
        assert rules is not None

        await EntityVisibilityRepository.remove_rule("agent-1", "domain_include", "light")

        assert "agent-1" not in _rules_cache
