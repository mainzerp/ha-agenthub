"""Tests for app.agents.conversation_manager -- Phase 6 (last_entities).

Covers the extended ``store_turn(resolved_entities=...)`` persistence
(JSON in the previously unused ``conversations.action_executed`` TEXT
column), the in-memory + DB-fallback ``get_last_entities`` retrieval, and
the ``extract_resolved_entities`` helper (success path only).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.conversation_manager import ConversationManager, extract_resolved_entities
from app.models.agent import ActionExecuted


@pytest.fixture(autouse=True)
def _mock_repos():
    with (
        patch("app.agents.conversation_manager.ConversationRepository") as mock_repo,
        patch("app.agents.conversation_manager.SettingsRepository") as mock_settings,
    ):
        mock_repo.insert = AsyncMock(return_value=1)
        mock_repo.get_by_conversation_id = AsyncMock(return_value=[])
        mock_settings.get_value = AsyncMock(side_effect=lambda k, d=None: d)
        yield mock_repo


class TestStoreTurnResolvedEntities:
    async def test_persists_resolved_entities_as_json(self, _mock_repos):
        mgr = ConversationManager()
        await mgr.store_turn(
            "conv-1",
            "turn on the couch light",
            "Couch light is on.",
            agent_id="light-agent",
            resolved_entities=[{"entity_id": "light.couch", "friendly_name": "Couch"}],
        )
        kwargs = _mock_repos.insert.call_args.kwargs
        assert kwargs["conversation_id"] == "conv-1"
        payload = json.loads(kwargs["action_executed"])
        assert payload == [{"entity_id": "light.couch", "friendly_name": "Couch", "turn_index": 1}]

    async def test_persists_null_action_executed_without_entities(self, _mock_repos):
        mgr = ConversationManager()
        await mgr.store_turn("conv-2", "hello", "hi", agent_id="general-agent")
        kwargs = _mock_repos.insert.call_args.kwargs
        assert kwargs["action_executed"] is None

    async def test_entries_without_entity_id_are_dropped(self, _mock_repos):
        mgr = ConversationManager()
        await mgr.store_turn(
            "conv-3",
            "do something",
            "done",
            resolved_entities=[{"entity_id": "", "friendly_name": "Broken"}],
        )
        kwargs = _mock_repos.insert.call_args.kwargs
        assert kwargs["action_executed"] is None

    async def test_turn_index_counts_user_turns(self, _mock_repos):
        mgr = ConversationManager()
        await mgr.store_turn(
            "conv-4",
            "turn on the couch light",
            "on",
            resolved_entities=[{"entity_id": "light.couch", "friendly_name": "Couch"}],
        )
        await mgr.store_turn(
            "conv-4",
            "turn it off",
            "off",
            resolved_entities=[{"entity_id": "light.couch", "friendly_name": "Couch"}],
        )
        payload = json.loads(_mock_repos.insert.call_args.kwargs["action_executed"])
        assert payload[0]["turn_index"] == 2


class TestGetLastEntities:
    async def test_returns_most_recent_first(self, _mock_repos):
        mgr = ConversationManager()
        await mgr.store_turn(
            "conv-5",
            "turn on the couch light",
            "on",
            resolved_entities=[{"entity_id": "light.couch", "friendly_name": "Couch"}],
        )
        await mgr.store_turn("conv-5", "what time is it", "10pm")
        await mgr.store_turn(
            "conv-5",
            "close the garage",
            "closed",
            resolved_entities=[{"entity_id": "cover.garage", "friendly_name": "Garage"}],
        )
        entities = await mgr.get_last_entities("conv-5")
        assert [e["entity_id"] for e in entities] == ["cover.garage", "light.couch"]
        assert entities[0]["friendly_name"] == "Garage"
        assert entities[0]["turn_index"] == 3
        assert entities[1]["turn_index"] == 1
        # Hints survive turns that resolved nothing (the anaphora case).
        _mock_repos.get_by_conversation_id.assert_not_awaited()

    async def test_no_conversation_id_returns_empty(self, _mock_repos):
        mgr = ConversationManager()
        assert await mgr.get_last_entities(None) == []

    async def test_db_fallback_on_memory_miss(self, _mock_repos):
        mgr = ConversationManager()
        _mock_repos.get_by_conversation_id.return_value = [
            {"user_text": "u1", "response_text": "a1", "action_executed": None},
            {
                "user_text": "u2",
                "response_text": "a2",
                "action_executed": json.dumps(
                    [{"entity_id": "light.couch", "friendly_name": "Couch", "turn_index": 2}]
                ),
            },
            {
                "user_text": "u3",
                "response_text": "a3",
                "action_executed": json.dumps(
                    [{"entity_id": "cover.garage", "friendly_name": "Garage", "turn_index": 3}]
                ),
            },
        ]
        entities = await mgr.get_last_entities("conv-db")
        assert [e["entity_id"] for e in entities] == ["cover.garage", "light.couch"]
        assert entities[0]["turn_index"] == 3

    async def test_db_fallback_dedupes_and_skips_invalid_json(self, _mock_repos):
        mgr = ConversationManager()
        _mock_repos.get_by_conversation_id.return_value = [
            {
                "user_text": "u1",
                "response_text": "a1",
                "action_executed": json.dumps([{"entity_id": "light.couch", "friendly_name": "Couch"}]),
            },
            {"user_text": "u2", "response_text": "a2", "action_executed": "{not json"},
            {
                "user_text": "u3",
                "response_text": "a3",
                "action_executed": json.dumps([{"entity_id": "light.couch", "friendly_name": "Couch New"}]),
            },
        ]
        entities = await mgr.get_last_entities("conv-db2")
        assert [e["entity_id"] for e in entities] == ["light.couch"]
        # Most recent row wins for a duplicated entity.
        assert entities[0]["friendly_name"] == "Couch New"

    async def test_db_fallback_error_returns_empty(self, _mock_repos):
        mgr = ConversationManager()
        _mock_repos.get_by_conversation_id.side_effect = RuntimeError("db down")
        assert await mgr.get_last_entities("conv-err") == []


class TestExtractResolvedEntities:
    async def test_dict_success(self):
        result = await extract_resolved_entities({"action": "turn_on", "entity_id": "light.couch", "success": True})
        assert result == [{"entity_id": "light.couch", "friendly_name": "light.couch"}]

    async def test_failed_action_returns_none(self):
        assert await extract_resolved_entities({"entity_id": "light.couch", "success": False}) is None

    async def test_missing_entity_id_returns_none(self):
        assert await extract_resolved_entities({"action": "turn_on", "success": True}) is None

    async def test_none_returns_none(self):
        assert await extract_resolved_entities(None) is None

    async def test_model_input(self):
        action = ActionExecuted(action="turn_on", entity_id="light.couch", success=True)
        result = await extract_resolved_entities(action)
        assert result == [{"entity_id": "light.couch", "friendly_name": "light.couch"}]

    async def test_friendly_name_from_index(self):
        entry = MagicMock()
        entry.friendly_name = "Couch"
        index = AsyncMock()
        index.get_by_id_async = AsyncMock(return_value=entry)
        result = await extract_resolved_entities({"entity_id": "light.couch", "success": True}, entity_index=index)
        assert result == [{"entity_id": "light.couch", "friendly_name": "Couch"}]

    async def test_index_failure_falls_back_to_entity_id(self):
        index = AsyncMock()
        index.get_by_id_async = AsyncMock(side_effect=RuntimeError("index down"))
        result = await extract_resolved_entities({"entity_id": "light.couch", "success": True}, entity_index=index)
        assert result == [{"entity_id": "light.couch", "friendly_name": "light.couch"}]
