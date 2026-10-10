"""Tests for app.agents.lists_executor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from app.agents.lists_executor import (
    _find_items_by_query,
    _format_item,
    _get_todo_items,
    execute_lists_action,
)
from app.ha_client.rest import HARestClient, allow_internal_ha_service_calls
from tests.helpers import make_entity_index_entry


def _make_mock_entity_index(entries=None):
    entries = entries or []
    index = MagicMock()
    index.list_entries_async = AsyncMock(return_value=entries)
    index.list_entries = MagicMock(return_value=entries)
    return index


def _make_mock_matcher():
    matcher = MagicMock()
    matcher.match = AsyncMock(return_value=[])
    matcher.filter_visible_results = AsyncMock(side_effect=lambda agent_id, results: results)
    return matcher


def _make_ha_client():
    client = MagicMock()
    client.call_service = AsyncMock(return_value={})
    return client


@pytest.fixture
def ha_client():
    return _make_ha_client()


@pytest.fixture
def entity_index():
    return _make_mock_entity_index()


@pytest.fixture
def entity_matcher():
    return _make_mock_matcher()


@pytest.fixture(autouse=True)
def _no_visibility_rules():
    """Visibility is always applied now; default to "no rules" (tests may patch their own inside)."""
    with patch("app.entity.visibility.EntityVisibilityRepository.get_rules", new=AsyncMock(return_value=[])):
        yield


class TestFindItemsByQuery:
    def test_exact_match(self):
        items = [{"summary": "Milk", "uid": "1"}, {"summary": "Eggs", "uid": "2"}]
        result = _find_items_by_query(items, "milk")
        assert len(result) == 1
        assert result[0]["summary"] == "Milk"

    def test_substring_match(self):
        items = [{"summary": "Almond milk", "uid": "1"}, {"summary": "Eggs", "uid": "2"}]
        result = _find_items_by_query(items, "milk")
        assert len(result) == 1
        assert result[0]["summary"] == "Almond milk"

    def test_exact_match_wins_over_substring(self):
        items = [{"summary": "Oat milk", "uid": "1"}, {"summary": "Milk", "uid": "2"}]
        result = _find_items_by_query(items, " MILK ")
        assert [r["uid"] for r in result] == ["2"]

    def test_reverse_substring_does_not_match(self):
        """'oat milk' must never select the plain 'Milk' item."""
        items = [{"summary": "Milk", "uid": "1"}]
        assert _find_items_by_query(items, "oat milk") == []

    def test_no_match(self):
        items = [{"summary": "Eggs", "uid": "1"}]
        result = _find_items_by_query(items, "milk")
        assert result == []

    def test_empty_query(self):
        items = [{"summary": "Milk", "uid": "1"}]
        result = _find_items_by_query(items, "")
        assert result == []


class TestFormatItem:
    def test_incomplete_item(self):
        assert _format_item({"summary": "Milk", "status": "needs_action"}) == "Milk"

    def test_completed_item(self):
        assert _format_item({"summary": "Milk", "status": "completed"}) == "Milk (done)"

    def test_default_status(self):
        assert _format_item({"summary": "Milk"}) == "Milk"


class TestListLists:
    @pytest.mark.asyncio
    async def test_no_lists_available(self, ha_client, entity_index, entity_matcher):
        result = await execute_lists_action(
            {"action": "list_lists"},
            ha_client,
            entity_index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "No todo lists" in result["speech"]

    @pytest.mark.asyncio
    async def test_lists_available(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        result = await execute_lists_action(
            {"action": "list_lists"},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Shopping List" in result["speech"]
        assert result["metadata"]["lists"][0]["entity_id"] == "todo.shopping_list"


class TestListListsVisibility:
    """Tests that list_lists and default list selection respect visibility."""

    @pytest.fixture()
    def _visible_todo_entries(self):
        return [
            make_entity_index_entry("todo.shopping_list", "Shopping List", domain="todo", area="kitchen"),
            make_entity_index_entry("todo.work_list", "Work List", domain="todo", area="office"),
        ]

    @pytest.mark.asyncio
    async def test_list_lists_filters_hidden_lists(self, ha_client, entity_matcher, _visible_todo_entries):
        index = _make_mock_entity_index(_visible_todo_entries)

        async def _rules(agent_id: str):
            if agent_id == "restricted-lists-agent":
                return [{"rule_type": "area_include", "rule_value": "kitchen"}]
            return []

        with patch(
            "app.entity.visibility.EntityVisibilityRepository.get_rules",
            new=AsyncMock(side_effect=_rules),
        ):
            result = await execute_lists_action(
                {"action": "list_lists"},
                ha_client,
                index,
                entity_matcher,
                agent_id="restricted-lists-agent",
            )

        assert result["success"] is True
        assert "Shopping List" in result["speech"]
        assert "Work List" not in result["speech"]
        assert len(result["metadata"]["lists"]) == 1
        assert result["metadata"]["lists"][0]["entity_id"] == "todo.shopping_list"

    @pytest.mark.asyncio
    async def test_default_list_selection_respects_visibility(self, ha_client, entity_matcher, _visible_todo_entries):
        index = _make_mock_entity_index(_visible_todo_entries)
        ha_client.call_service = AsyncMock(
            return_value={"todo.shopping_list": {"items": [{"summary": "Milk", "status": "needs_action"}]}}
        )

        async def _rules(agent_id: str):
            if agent_id == "restricted-lists-agent":
                return [{"rule_type": "area_include", "rule_value": "kitchen"}]
            return []

        with patch(
            "app.entity.visibility.EntityVisibilityRepository.get_rules",
            new=AsyncMock(side_effect=_rules),
        ):
            result = await execute_lists_action(
                {"action": "list_items", "entity": ""},
                ha_client,
                index,
                entity_matcher,
                agent_id="restricted-lists-agent",
            )

        assert result["success"] is True
        assert "Shopping List" in result["speech"]
        assert "Milk" in result["speech"]

    @pytest.mark.asyncio
    async def test_cannot_read_items_from_hidden_list(self, ha_client, entity_matcher, _visible_todo_entries):
        index = _make_mock_entity_index(_visible_todo_entries)

        async def _rules(agent_id: str):
            if agent_id == "restricted-lists-agent":
                return [{"rule_type": "area_include", "rule_value": "kitchen"}]
            return []

        with patch(
            "app.entity.visibility.EntityVisibilityRepository.get_rules",
            new=AsyncMock(side_effect=_rules),
        ):
            result = await execute_lists_action(
                {"action": "list_items", "entity": "work list"},
                ha_client,
                index,
                entity_matcher,
                agent_id="restricted-lists-agent",
            )

        assert result["success"] is False
        assert "Could not find" in result["speech"]


class TestListItems:
    @pytest.mark.asyncio
    async def test_empty_list(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(return_value={"todo.shopping_list": {"items": []}})

        result = await execute_lists_action(
            {"action": "list_items", "entity": "shopping list"},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "empty" in result["speech"]

    @pytest.mark.asyncio
    async def test_items_in_list(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            return_value={
                "todo.shopping_list": {
                    "items": [
                        {"summary": "Milk", "status": "needs_action"},
                        {"summary": "Eggs", "status": "completed"},
                    ]
                }
            }
        )

        result = await execute_lists_action(
            {"action": "list_items", "entity": "shopping list"},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Milk" in result["speech"]
        assert "Eggs (done)" in result["speech"]
        assert len(result["metadata"]["items"]) == 2


class TestAddItem:
    @pytest.mark.asyncio
    async def test_add_single_item(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        result = await execute_lists_action(
            {"action": "add_item", "entity": "shopping list", "parameters": {"item": "Milk"}},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Added Milk" in result["speech"]
        ha_client.call_service.assert_awaited_with("todo", "add_item", "todo.shopping_list", {"item": "Milk"})

    @pytest.mark.asyncio
    async def test_add_multiple_items(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        result = await execute_lists_action(
            {
                "action": "add_item",
                "entity": "shopping list",
                "parameters": {"item": "Milk, Eggs, Bread"},
            },
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Milk" in result["speech"]
        assert "Eggs" in result["speech"]
        assert "Bread" in result["speech"]
        assert ha_client.call_service.await_count == 3

    @pytest.mark.asyncio
    async def test_add_item_no_text(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        result = await execute_lists_action(
            {"action": "add_item", "entity": "shopping list", "parameters": {"item": ""}},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is False
        assert "Please specify" in result["speech"]


class TestCompleteItem:
    @pytest.mark.asyncio
    async def test_complete_single_item(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            side_effect=[
                {
                    "todo.shopping_list": {
                        "items": [
                            {"summary": "Milk", "status": "needs_action", "uid": "1"},
                            {"summary": "Eggs", "status": "needs_action", "uid": "2"},
                        ]
                    }
                },
                {},  # update_item response
            ]
        )

        result = await execute_lists_action(
            {
                "action": "complete_item",
                "entity": "shopping list",
                "parameters": {"item": "Milk"},
            },
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Completed Milk" in result["speech"]

    @pytest.mark.asyncio
    async def test_complete_multiple_items(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            side_effect=[
                {
                    "todo.shopping_list": {
                        "items": [
                            {"summary": "Milk", "status": "needs_action", "uid": "1"},
                            {"summary": "Eggs", "status": "needs_action", "uid": "2"},
                        ]
                    }
                },
                {},  # update Milk
                {},  # update Eggs
            ]
        )

        result = await execute_lists_action(
            {
                "action": "complete_item",
                "entity": "shopping list",
                "parameters": {"item": "Milk, Eggs"},
            },
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Completed Milk" in result["speech"]
        assert "Eggs" in result["speech"]

    @pytest.mark.asyncio
    async def test_complete_item_not_found(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            return_value={"todo.shopping_list": {"items": [{"summary": "Eggs", "status": "needs_action", "uid": "1"}]}}
        )

        result = await execute_lists_action(
            {
                "action": "complete_item",
                "entity": "shopping list",
                "parameters": {"item": "Milk"},
            },
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is False
        assert "Could not find" in result["speech"]


class TestRemoveItem:
    @pytest.mark.asyncio
    async def test_remove_single_item(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            side_effect=[
                {
                    "todo.shopping_list": {
                        "items": [
                            {"summary": "Milk", "status": "needs_action", "uid": "1"},
                        ]
                    }
                },
                {},  # remove_item response
            ]
        )

        result = await execute_lists_action(
            {
                "action": "remove_item",
                "entity": "shopping list",
                "parameters": {"item": "Milk"},
            },
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Removed Milk" in result["speech"]

    @pytest.mark.asyncio
    async def test_remove_item_exact_match_wins(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            side_effect=[
                {
                    "todo.shopping_list": {
                        "items": [
                            {"summary": "Almond milk", "status": "needs_action", "uid": "2"},
                            {"summary": "Milk", "status": "needs_action", "uid": "1"},
                        ]
                    }
                },
                {},
            ]
        )

        result = await execute_lists_action(
            {
                "action": "remove_item",
                "entity": "shopping list",
                "parameters": {"item": "milk"},
            },
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert ha_client.call_service.await_args_list[-1].args == (
            "todo",
            "remove_item",
            "todo.shopping_list",
            {"item": "1"},
        )
        assert result["cacheable"] is False

    @pytest.mark.asyncio
    async def test_remove_item_ambiguous_asks(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            return_value={
                "todo.shopping_list": {
                    "items": [
                        {"summary": "Oat milk", "status": "needs_action", "uid": "1"},
                        {"summary": "Almond milk", "status": "needs_action", "uid": "2"},
                    ]
                }
            }
        )

        result = await execute_lists_action(
            {
                "action": "remove_item",
                "entity": "shopping list",
                "parameters": {"item": "milk"},
            },
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is False
        assert result["voice_followup"] is True
        assert "Oat milk" in result["speech"] and "Almond milk" in result["speech"]
        # Only the get_items read happened -- nothing was removed.
        assert ha_client.call_service.await_count == 1


class TestT8Regressions:
    @pytest.mark.asyncio
    async def test_complete_milk_does_not_complete_oat_milk(self, ha_client, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])
        ha_client.call_service = AsyncMock(
            side_effect=[
                {
                    "todo.shopping_list": {
                        "items": [
                            {"summary": "Oat milk", "status": "needs_action", "uid": "oat"},
                            {"summary": "Milk", "status": "needs_action", "uid": "milk"},
                        ]
                    }
                },
                {},
            ]
        )
        result = await execute_lists_action(
            {"action": "complete_item", "entity": "shopping list", "parameters": {"item": "milk"}},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert ha_client.call_service.await_args_list[-1].args[3] == {"item": "milk", "status": "completed"}

    @pytest.mark.asyncio
    async def test_no_list_named_and_several_visible_asks(self, ha_client, entity_matcher):
        shopping = make_entity_index_entry("todo.shopping_list", "Shopping List", area=None)
        hardware = make_entity_index_entry("todo.hardware", "Hardware Store", area=None)
        index = _make_mock_entity_index([shopping, hardware])
        result = await execute_lists_action(
            {"action": "add_item", "entity": "", "parameters": {"item": "milk"}},
            ha_client,
            index,
            entity_matcher,
            agent_id="lists-agent",
        )
        assert result["success"] is False
        assert result["voice_followup"] is True
        assert result["metadata"]["resolution_path"] == "list_ambiguous"
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_visibility_applies_without_agent_id(self, ha_client, entity_matcher):
        shopping = make_entity_index_entry("todo.shopping_list", "Shopping List", area="kitchen")
        work = make_entity_index_entry("todo.work", "Work List", area="office")
        index = _make_mock_entity_index([shopping, work])

        async def _rules(agent_id: str):
            assert agent_id == "lists-agent"
            return [{"rule_type": "area_include", "rule_value": "kitchen"}]

        with patch("app.entity.visibility.EntityVisibilityRepository.get_rules", new=AsyncMock(side_effect=_rules)):
            result = await execute_lists_action({"action": "list_lists"}, ha_client, index, entity_matcher)
        assert "Shopping List" in result["speech"]
        assert "Work List" not in result["speech"]

    @pytest.mark.asyncio
    async def test_complete_already_done_item_reports_it(self, ha_client, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])
        ha_client.call_service = AsyncMock(
            return_value={"todo.shopping_list": {"items": [{"summary": "Milk", "status": "completed", "uid": "1"}]}}
        )
        result = await execute_lists_action(
            {"action": "complete_item", "entity": "shopping list", "parameters": {"item": "milk"}},
            ha_client,
            index,
            entity_matcher,
        )
        assert "already done" in result["speech"]
        assert ha_client.call_service.await_count == 1


class TestClearCompleted:
    @pytest.mark.asyncio
    async def test_clear_completed_success(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            side_effect=[
                {
                    "todo.shopping_list": {
                        "items": [
                            {"summary": "Milk", "status": "completed", "uid": "1"},
                            {"summary": "Eggs", "status": "completed", "uid": "2"},
                            {"summary": "Bread", "status": "needs_action", "uid": "3"},
                        ]
                    }
                },
                {},  # remove Milk
                {},  # remove Eggs
            ]
        )

        result = await execute_lists_action(
            {"action": "clear_completed", "entity": "shopping list"},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Cleared 2 completed" in result["speech"]

    @pytest.mark.asyncio
    async def test_clear_completed_empty(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            return_value={
                "todo.shopping_list": {
                    "items": [
                        {"summary": "Bread", "status": "needs_action", "uid": "1"},
                    ]
                }
            }
        )

        result = await execute_lists_action(
            {"action": "clear_completed", "entity": "shopping list"},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "No completed items" in result["speech"]


class TestUnknownAction:
    @pytest.mark.asyncio
    async def test_unknown_action(self, ha_client, entity_index, entity_matcher):
        result = await execute_lists_action(
            {"action": "fly_to_moon"},
            ha_client,
            entity_index,
            entity_matcher,
        )
        assert result["success"] is False
        assert "Unknown lists action" in result["speech"]


class TestGetTodoItemsRestEnvelope:
    @respx.mock
    @pytest.mark.asyncio
    async def test_todo_get_items_with_real_rest_client_unwraps_envelope(self):
        """A real HARestClient must unwrap HA's {changed_states, service_response}
        envelope so _get_todo_items sees the per-entity items dict."""
        respx.post("http://ha.local/api/services/todo/get_items").mock(
            return_value=httpx.Response(
                200,
                json={
                    "changed_states": [],
                    "service_response": {
                        "todo.shopping_list": {
                            "items": [{"summary": "Milk", "status": "needs_action"}],
                        }
                    },
                },
            )
        )

        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})

        with allow_internal_ha_service_calls("test"):
            items = await _get_todo_items(client, "todo.shopping_list")

        assert items == [{"summary": "Milk", "status": "needs_action"}]
        await client.close()


class TestGetTodoItemsResponseFormats:
    @pytest.mark.asyncio
    async def test_response_nested_under_entity_id(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            return_value={"todo.shopping_list": {"items": [{"summary": "Milk", "status": "needs_action"}]}}
        )

        result = await execute_lists_action(
            {"action": "list_items", "entity": "shopping list"},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Milk" in result["speech"]

    @pytest.mark.asyncio
    async def test_response_nested_under_response_key(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            return_value={
                "response": {"todo.shopping_list": {"items": [{"summary": "Milk", "status": "needs_action"}]}}
            }
        )

        result = await execute_lists_action(
            {"action": "list_items", "entity": "shopping list"},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Milk" in result["speech"]

    @pytest.mark.asyncio
    async def test_response_plain_list(self, ha_client, entity_index, entity_matcher):
        entry = MagicMock()
        entry.entity_id = "todo.shopping_list"
        entry.friendly_name = "Shopping List"
        index = _make_mock_entity_index([entry])

        ha_client.call_service = AsyncMock(
            return_value={"todo.shopping_list": [{"summary": "Milk", "status": "needs_action"}]}
        )

        result = await execute_lists_action(
            {"action": "list_items", "entity": "shopping list"},
            ha_client,
            index,
            entity_matcher,
        )
        assert result["success"] is True
        assert "Milk" in result["speech"]
