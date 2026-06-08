"""Tests for app.agents.calendar_executor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.calendar_executor import (
    _resolve_calendar_entity,
    execute_calendar_action,
)

pytestmark = pytest.mark.asyncio


class TestResolveCalendarEntity:
    async def test_resolve_calendar_entity_fuzzy_fallback(self):
        """_resolve_calendar_entity falls back to fuzzy search via resolve_entity_deterministic_first."""
        action = {"entity": "work calendar", "parameters": {}}
        ha_client = AsyncMock()
        entity_index = MagicMock()
        entity_matcher = MagicMock()

        with patch(
            "app.agents.calendar_executor.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={
                "entity_id": "calendar.work",
                "friendly_name": "Work Calendar",
                "speech": None,
                "metadata": {
                    "query": "work calendar",
                    "match_count": 1,
                    "resolution_path": "hybrid_matcher",
                },
            },
        ) as mock_resolve:
            entity_id, friendly_name, error = await _resolve_calendar_entity(
                action,
                ha_client,
                entity_index,
                entity_matcher,
                agent_id="calendar-agent",
            )

        assert entity_id == "calendar.work"
        assert friendly_name == "Work Calendar"
        assert error is None
        mock_resolve.assert_awaited_once()
        call_kwargs = mock_resolve.call_args.kwargs
        assert call_kwargs.get("allowed_domains") == frozenset({"calendar"})


class TestListEvents:
    async def test_list_events_success(self):
        action = {
            "action": "list_events",
            "entity": "work",
            "parameters": {
                "start_date_time": "2026-06-01T00:00:00",
                "end_date_time": "2026-06-30T23:59:59",
            },
        }
        ha_client = AsyncMock()
        ha_client.get_calendar_events = AsyncMock(
            return_value=[
                {"summary": "Meeting", "start": "2026-06-08T10:00:00"},
                {"summary": "Lunch", "start": "2026-06-08T12:00:00"},
            ]
        )
        with patch(
            "app.agents.calendar_executor.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={
                "entity_id": "calendar.work",
                "friendly_name": "Work Calendar",
                "speech": None,
                "metadata": {},
            },
        ):
            result = await execute_calendar_action(
                action,
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )

        assert result["success"] is True
        assert result["entity_id"] == "calendar.work"
        assert "Meeting" in result["speech"]
        assert "Lunch" in result["speech"]
        assert len(result["metadata"]["events"]) == 2
        ha_client.get_calendar_events.assert_awaited_once_with(
            "calendar.work", "2026-06-01T00:00:00", "2026-06-30T23:59:59"
        )

    async def test_list_events_empty(self):
        action = {
            "action": "list_events",
            "entity": "work",
            "parameters": {
                "start_date_time": "2026-06-01T00:00:00",
                "end_date_time": "2026-06-30T23:59:59",
            },
        }
        ha_client = AsyncMock()
        ha_client.get_calendar_events = AsyncMock(return_value=[])
        with patch(
            "app.agents.calendar_executor.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={
                "entity_id": "calendar.work",
                "friendly_name": "Work Calendar",
                "speech": None,
                "metadata": {},
            },
        ):
            result = await execute_calendar_action(
                action,
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )

        assert result["success"] is True
        assert "No events found" in result["speech"]
        assert result["metadata"]["events"] == []


class TestQueryEvent:
    async def test_query_event_found(self):
        action = {
            "action": "query_event",
            "entity": "work",
            "parameters": {"summary": "standup"},
        }
        ha_client = AsyncMock()
        ha_client.get_calendar_events = AsyncMock(
            return_value=[
                {"summary": "Daily Standup", "start": "2026-06-08T09:00:00"},
                {"summary": "Team Lunch", "start": "2026-06-08T12:00:00"},
            ]
        )
        with patch(
            "app.agents.calendar_executor.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={
                "entity_id": "calendar.work",
                "friendly_name": "Work Calendar",
                "speech": None,
                "metadata": {},
            },
        ):
            result = await execute_calendar_action(
                action,
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )

        assert result["success"] is True
        assert "Daily Standup" in result["speech"]
        assert result["metadata"]["events"][0]["summary"] == "Daily Standup"

    async def test_query_event_not_found(self):
        action = {
            "action": "query_event",
            "entity": "work",
            "parameters": {"summary": "nonexistent"},
        }
        ha_client = AsyncMock()
        ha_client.get_calendar_events = AsyncMock(
            return_value=[
                {"summary": "Daily Standup", "start": "2026-06-08T09:00:00"},
            ]
        )
        with patch(
            "app.agents.calendar_executor.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={
                "entity_id": "calendar.work",
                "friendly_name": "Work Calendar",
                "speech": None,
                "metadata": {},
            },
        ):
            result = await execute_calendar_action(
                action,
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )

        assert result["success"] is True
        assert "No upcoming events matching" in result["speech"]
        assert result["metadata"]["events"] == []


class TestCreateEvent:
    async def test_create_event_success(self):
        action = {
            "action": "create_event",
            "entity": "work",
            "parameters": {
                "summary": "Meeting",
                "start_date_time": "2026-06-08T10:00:00",
                "end_date_time": "2026-06-08T11:00:00",
                "description": "Project sync",
                "location": "Room A",
            },
        }
        ha_client = AsyncMock()
        ha_client.call_service = AsyncMock(return_value={"success": True})
        with patch(
            "app.agents.calendar_executor.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={
                "entity_id": "calendar.work",
                "friendly_name": "Work Calendar",
                "speech": None,
                "metadata": {},
            },
        ):
            result = await execute_calendar_action(
                action,
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )

        assert result["success"] is True
        assert "Created event" in result["speech"]
        ha_client.call_service.assert_awaited_once()
        call_args = ha_client.call_service.await_args
        assert call_args.args[0] == "calendar"
        assert call_args.args[1] == "create_event"
        assert call_args.args[2] == "calendar.work"
        assert call_args.args[3]["summary"] == "Meeting"
        assert call_args.args[3]["description"] == "Project sync"
        assert call_args.args[3]["location"] == "Room A"

    async def test_create_event_validation_error(self):
        action = {
            "action": "create_event",
            "entity": "work",
            "parameters": {"start_date_time": "2026-06-08T10:00:00"},
        }
        ha_client = AsyncMock()
        result = await execute_calendar_action(
            action,
            ha_client,
            MagicMock(),
            MagicMock(),
            agent_id="calendar-agent",
        )

        assert result["success"] is False
        assert "Summary is required" in result["speech"]


class TestDeleteEvent:
    async def test_delete_event_success(self):
        action = {
            "action": "delete_event",
            "entity": "work",
            "parameters": {"uid": "event-123"},
        }
        ha_client = AsyncMock()
        ha_client.call_service = AsyncMock(return_value={"success": True})
        with patch(
            "app.agents.calendar_executor.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={
                "entity_id": "calendar.work",
                "friendly_name": "Work Calendar",
                "speech": None,
                "metadata": {},
            },
        ):
            result = await execute_calendar_action(
                action,
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )

        assert result["success"] is True
        assert "Deleted event" in result["speech"]
        ha_client.call_service.assert_awaited_once_with(
            "calendar", "delete_event", "calendar.work", {"uid": "event-123"}
        )

    async def test_delete_event_not_found(self):
        action = {
            "action": "delete_event",
            "entity": "work",
            "parameters": {
                "summary": "nonexistent",
                "start_date_time": "2026-06-08T10:00:00",
            },
        }
        ha_client = AsyncMock()
        ha_client.get_calendar_events = AsyncMock(
            return_value=[
                {
                    "summary": "Other Event",
                    "start": "2026-06-08T10:00:00",
                    "uid": "event-456",
                },
            ]
        )
        with patch(
            "app.agents.calendar_executor.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={
                "entity_id": "calendar.work",
                "friendly_name": "Work Calendar",
                "speech": None,
                "metadata": {},
            },
        ):
            result = await execute_calendar_action(
                action,
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )

        assert result["success"] is False
        assert "No matching event found" in result["speech"]


class TestUpdateEvent:
    async def test_update_event_success(self):
        action = {
            "action": "update_event",
            "entity": "work",
            "parameters": {
                "summary": "Meeting",
                "start_date_time": "2026-06-08T10:00:00",
                "end_date_time": "2026-06-08T12:00:00",
            },
        }
        ha_client = AsyncMock()
        ha_client.get_calendar_events = AsyncMock(
            return_value=[
                {
                    "summary": "Team Meeting",
                    "start": "2026-06-08T10:00:00",
                    "uid": "event-123",
                },
            ]
        )
        ha_client.call_service = AsyncMock(return_value={"success": True})
        with patch(
            "app.agents.calendar_executor.resolve_entity_deterministic_first",
            new_callable=AsyncMock,
            return_value={
                "entity_id": "calendar.work",
                "friendly_name": "Work Calendar",
                "speech": None,
                "metadata": {},
            },
        ):
            result = await execute_calendar_action(
                action,
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )

        assert result["success"] is True
        assert "Updated event" in result["speech"]
        ha_client.call_service.assert_awaited_once_with(
            "calendar",
            "update_event",
            "calendar.work",
            {
                "uid": "event-123",
                "summary": "Meeting",
                "start_date_time": "2026-06-08T10:00:00",
                "end_date_time": "2026-06-08T12:00:00",
            },
        )

    async def test_update_event_no_changes(self):
        action = {
            "action": "update_event",
            "entity": "work",
            "parameters": {},
        }
        result = await execute_calendar_action(
            action,
            AsyncMock(),
            MagicMock(),
            MagicMock(),
            agent_id="calendar-agent",
        )

        assert result["success"] is False
        assert "summary or start_date_time is required" in result["speech"]
