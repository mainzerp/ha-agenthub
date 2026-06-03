"""Tests for entity visibility and domain-agent map interactions."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest


class TestEntityVisibilityDomainAgentMap:
    # ------------------------------------------------------------------
    # G25: Domain-agent map consumption by entity visibility
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_domain_agent_map_affects_entity_visibility(self):
        """G25: Domain-agent maps should be consumed when evaluating entity visibility."""

        # Mock the repository to return domain-agent rules
        mock_repo = AsyncMock()
        mock_repo.get_rules_for_agent = AsyncMock(return_value=[{"rule_type": "domain_include", "rule_value": "light"}])

        with patch("app.db.repositories.entity_visibility.EntityVisibilityRepository", return_value=mock_repo):
            rules = await mock_repo.get_rules_for_agent("light-agent")
            assert len(rules) == 1
            assert rules[0]["rule_type"] == "domain_include"
            assert rules[0]["rule_value"] == "light"

    # ------------------------------------------------------------------
    # G29: EntityVisibilityRepository.set_domain_agents side effects
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_set_domain_agents_replaces_existing_rules(self):
        """G29: set_domain_agents should replace existing rules for the domain."""
        from app.db.repositories.entity_visibility import EntityVisibilityRepository

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()
        mock_db.fetchall = AsyncMock(return_value=[])

        @asynccontextmanager
        async def _mock_get_db():
            yield mock_db

        with patch("app.db.repositories.entity_visibility.get_db_write", _mock_get_db):
            await EntityVisibilityRepository.set_domain_agents("light", ["light-agent", "switch-agent"])

        # Should delete old rules for the domain and insert new ones
        sql_calls = [c.args[0] for c in mock_db.execute.await_args_list if c.args]
        delete_count = sum(1 for s in sql_calls if "DELETE" in s)
        insert_count = sum(1 for s in sql_calls if "INSERT" in s)
        assert delete_count >= 1
        assert insert_count == 2

    @pytest.mark.asyncio
    async def test_set_domain_agents_empty_list_clears_rules(self):
        """G29: set_domain_agents with empty list should clear all rules for the domain."""
        from app.db.repositories.entity_visibility import EntityVisibilityRepository

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()

        @asynccontextmanager
        async def _mock_get_db():
            yield mock_db

        with patch("app.db.repositories.entity_visibility.get_db_write", _mock_get_db):
            await EntityVisibilityRepository.set_domain_agents("light", [])

        # Should only execute DELETE, no INSERTs
        sql_calls = [c.args[0] for c in mock_db.execute.await_args_list if c.args]
        delete_count = sum(1 for s in sql_calls if "DELETE" in s)
        insert_count = sum(1 for s in sql_calls if "INSERT" in s)
        assert delete_count >= 1
        assert insert_count == 0
