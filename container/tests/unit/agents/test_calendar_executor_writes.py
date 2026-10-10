"""Regression tests for calendar writes and multi-calendar reads (issue #132, T8)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from tests.helpers import make_entity_index_entry

from app.agents.calendar_executor import execute_calendar_action
from app.ha_client.rest import HARestClient

pytestmark = pytest.mark.asyncio

_RESOLVE = "app.agents.calendar_executor.resolve_entity_deterministic_first"


def _resolved(entity_id: str = "calendar.work", name: str = "Work Calendar"):
    return patch(
        _RESOLVE,
        new_callable=AsyncMock,
        return_value={"entity_id": entity_id, "friendly_name": name, "speech": None, "metadata": {}},
    )


def _index(*entries):
    index = MagicMock()
    index.list_entries_async = AsyncMock(return_value=list(entries))
    return index


@pytest.fixture(autouse=True)
def _no_visibility_rules():
    with patch("app.entity.visibility.EntityVisibilityRepository.get_rules", new=AsyncMock(return_value=[])):
        yield


def _detail(uid, summary, start, end, **extra):
    return {"uid": uid, "summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}


class TestCreateEvent:
    async def test_default_duration_is_one_hour(self):
        ha_client = AsyncMock()
        with _resolved():
            result = await execute_calendar_action(
                {
                    "action": "create_event",
                    "entity": "work",
                    "parameters": {"summary": "Dentist", "start_date_time": "2026-06-08 14:00:00"},
                },
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
                timezone="Europe/Berlin",
            )
        assert result["success"] is True
        data = ha_client.call_service.await_args.args[3]
        assert data["start_date_time"] == "2026-06-08 14:00:00"
        assert data["end_date_time"] == "2026-06-08 15:00:00"
        assert result["cacheable"] is False

    async def test_all_day_event_uses_date_fields(self):
        ha_client = AsyncMock()
        with _resolved():
            result = await execute_calendar_action(
                {
                    "action": "create_event",
                    "entity": "work",
                    "parameters": {"summary": "Birthday", "start_date": "2026-06-20"},
                },
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )
        assert result["success"] is True
        data = ha_client.call_service.await_args.args[3]
        assert data == {"summary": "Birthday", "start_date": "2026-06-20", "end_date": "2026-06-21"}

    async def test_date_only_start_date_time_creates_all_day(self):
        ha_client = AsyncMock()
        with _resolved():
            await execute_calendar_action(
                {
                    "action": "create_event",
                    "entity": "work",
                    "parameters": {"summary": "Trip", "start_date_time": "2026-06-20"},
                },
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )
        data = ha_client.call_service.await_args.args[3]
        assert data["start_date"] == "2026-06-20"
        assert "start_date_time" not in data

    async def test_end_before_start_is_rejected(self):
        ha_client = AsyncMock()
        with _resolved():
            result = await execute_calendar_action(
                {
                    "action": "create_event",
                    "entity": "work",
                    "parameters": {
                        "summary": "X",
                        "start_date_time": "2026-06-08 14:00:00",
                        "end_date_time": "2026-06-08 13:00:00",
                    },
                },
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )
        assert result["success"] is False
        ha_client.call_service.assert_not_awaited()

    async def test_several_candidate_calendars_ask_instead_of_first_pick(self):
        ha_client = AsyncMock()
        index = _index(
            make_entity_index_entry("calendar.work", "Work", area=None),
            make_entity_index_entry("calendar.family", "Family", area=None),
        )
        result = await execute_calendar_action(
            {"action": "create_event", "parameters": {"summary": "Dentist", "start_date_time": "2026-06-08 14:00:00"}},
            ha_client,
            index,
            None,
            agent_id="calendar-agent",
        )
        assert result["success"] is False
        assert result["voice_followup"] is True
        assert "Work" in result["speech"] and "Family" in result["speech"]
        ha_client.call_service.assert_not_awaited()

    async def test_single_default_calendar_is_used(self):
        ha_client = AsyncMock()
        index = _index(
            make_entity_index_entry("calendar.work", "Work", area=None),
            make_entity_index_entry("calendar.family", "Family", area=None),
        )
        result = await execute_calendar_action(
            {"action": "create_event", "parameters": {"summary": "Dentist", "start_date_time": "2026-06-08 14:00:00"}},
            ha_client,
            index,
            None,
            agent_id="calendar-agent",
            default_calendar_ids=["calendar.family"],
        )
        assert result["success"] is True
        assert ha_client.call_service.await_args.args[2] == "calendar.family"

    async def test_non_calendar_default_id_is_ignored(self):
        ha_client = AsyncMock()
        index = _index(make_entity_index_entry("calendar.work", "Work", area=None))
        result = await execute_calendar_action(
            {"action": "create_event", "parameters": {"summary": "Dentist", "start_date_time": "2026-06-08 14:00:00"}},
            ha_client,
            index,
            None,
            agent_id="calendar-agent",
            default_calendar_ids=["light.kitchen"],
        )
        assert result["success"] is True
        assert ha_client.call_service.await_args.args[2] == "calendar.work"


class TestMultiCalendarRead:
    async def test_list_reads_all_default_calendars_merged_sorted(self):
        ha_client = AsyncMock()
        per_calendar = {
            "calendar.work": [{"summary": "Standup", "start": "2026-06-08T10:00:00+00:00"}],
            "calendar.family": [{"summary": "Breakfast", "start": "2026-06-08T08:00:00+00:00"}],
        }
        ha_client.get_calendar_events = AsyncMock(side_effect=lambda eid, _s, _e: per_calendar[eid])
        index = _index(
            make_entity_index_entry("calendar.work", "Work", area=None),
            make_entity_index_entry("calendar.family", "Family", area=None),
            make_entity_index_entry("calendar.holidays", "Holidays", area=None),
        )
        result = await execute_calendar_action(
            {
                "action": "list_events",
                "parameters": {"start_date_time": "2026-06-08 00:00:00", "end_date_time": "2026-06-08 23:59:59"},
            },
            ha_client,
            index,
            None,
            agent_id="calendar-agent",
            default_calendar_ids=["calendar.work", "calendar.family"],
        )
        assert result["success"] is True
        assert ha_client.get_calendar_events.await_count == 2
        assert [e["summary"] for e in result["metadata"]["events"]] == ["Breakfast", "Standup"]
        assert result["speech"].index("Breakfast") < result["speech"].index("Standup")
        assert result["cacheable"] is False

    async def test_query_reads_all_visible_calendars_without_defaults(self):
        ha_client = AsyncMock()
        per_calendar = {
            "calendar.work": [],
            "calendar.family": [{"summary": "Doctor visit", "start": "2099-06-08T08:00:00+00:00"}],
        }
        ha_client.get_calendar_events = AsyncMock(side_effect=lambda eid, _s, _e: per_calendar[eid])
        index = _index(
            make_entity_index_entry("calendar.work", "Work", area=None),
            make_entity_index_entry("calendar.family", "Family", area=None),
        )
        result = await execute_calendar_action(
            {"action": "query_event", "parameters": {"summary": "doctor"}},
            ha_client,
            index,
            None,
            agent_id="calendar-agent",
        )
        assert result["success"] is True
        assert "Doctor visit" in result["speech"]
        assert result["entity_id"] == "calendar.family"


class TestDeleteEvent:
    async def test_delete_uses_ws_with_uid_and_verifies(self):
        ha_client = AsyncMock()
        ha_client.get_calendar_event_details = AsyncMock(
            side_effect=[
                [_detail("event-123", "Team meeting", "2026-06-08T09:00:00+02:00", "2026-06-08T10:00:00+02:00")],
                [],
            ]
        )
        ha_client.send_ws_command = AsyncMock(return_value=None)
        with _resolved():
            result = await execute_calendar_action(
                {
                    "action": "delete_event",
                    "entity": "work",
                    # Naive local time from the LLM vs aware ISO from HA.
                    "parameters": {"summary": "team meeting", "start_date_time": "2026-06-08 09:00:00"},
                },
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
                timezone="Europe/Berlin",
            )
        assert result["success"] is True, result["speech"]
        ha_client.send_ws_command.assert_awaited_once_with(
            "calendar/event/delete", entity_id="calendar.work", uid="event-123"
        )
        ha_client.call_service.assert_not_awaited()
        assert result["cacheable"] is False

    async def test_delete_date_only_matches_and_passes_recurrence_id(self):
        ha_client = AsyncMock()
        ha_client.get_calendar_event_details = AsyncMock(
            side_effect=[
                [
                    _detail(
                        "series-1",
                        "Team meeting",
                        "2026-06-08T09:00:00+02:00",
                        "2026-06-08T10:00:00+02:00",
                        recurrence_id="20260608T070000Z",
                    )
                ],
                [],
            ]
        )
        ha_client.send_ws_command = AsyncMock(return_value=None)
        with _resolved():
            result = await execute_calendar_action(
                {
                    "action": "delete_event",
                    "entity": "work",
                    "parameters": {"summary": "team meeting", "start_date_time": "2026-06-08"},
                },
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
                timezone="Europe/Berlin",
            )
        assert result["success"] is True
        assert ha_client.send_ws_command.await_args.kwargs["recurrence_id"] == "20260608T070000Z"

    async def test_delete_not_confirmed_reports_failure(self):
        event = _detail("event-123", "Team meeting", "2026-06-08T09:00:00+00:00", "2026-06-08T10:00:00+00:00")
        ha_client = AsyncMock()
        ha_client.get_calendar_event_details = AsyncMock(return_value=[event])
        ha_client.send_ws_command = AsyncMock(return_value=None)
        with _resolved():
            result = await execute_calendar_action(
                {"action": "delete_event", "entity": "work", "parameters": {"summary": "Team meeting"}},
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )
        assert result["success"] is False
        assert "did not delete" in result["speech"]

    async def test_delete_without_websocket_is_honest(self):
        ha_client = AsyncMock()
        ha_client.get_calendar_event_details = AsyncMock(
            return_value=[_detail("e1", "Team meeting", "2026-06-08T09:00:00+00:00", "2026-06-08T10:00:00+00:00")]
        )
        ha_client.send_ws_command = AsyncMock(side_effect=RuntimeError("no ws"))
        with _resolved():
            result = await execute_calendar_action(
                {"action": "delete_event", "entity": "work", "parameters": {"summary": "Team meeting"}},
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )
        assert result["success"] is False
        assert "WebSocket" in result["speech"]

    async def test_delete_ambiguous_asks(self):
        ha_client = AsyncMock()
        ha_client.get_calendar_event_details = AsyncMock(
            return_value=[
                _detail("e1", "Team meeting", "2026-06-08T09:00:00+00:00", "2026-06-08T10:00:00+00:00"),
                _detail("e2", "Team meeting", "2026-06-15T09:00:00+00:00", "2026-06-15T10:00:00+00:00"),
            ]
        )
        with _resolved():
            result = await execute_calendar_action(
                {"action": "delete_event", "entity": "work", "parameters": {"summary": "Team meeting"}},
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )
        assert result["success"] is False
        assert result["voice_followup"] is True
        assert result["metadata"]["resolution_path"] == "event_ambiguous"
        ha_client.send_ws_command.assert_not_awaited()

    async def test_delete_prefers_exact_summary(self):
        ha_client = AsyncMock()
        ha_client.get_calendar_event_details = AsyncMock(
            side_effect=[
                [
                    _detail("e1", "Meeting", "2026-06-08T09:00:00+00:00", "2026-06-08T10:00:00+00:00"),
                    _detail("e2", "Meeting prep", "2026-06-08T08:00:00+00:00", "2026-06-08T09:00:00+00:00"),
                ],
                [],
            ]
        )
        ha_client.send_ws_command = AsyncMock(return_value=None)
        with _resolved():
            result = await execute_calendar_action(
                {"action": "delete_event", "entity": "work", "parameters": {"summary": "meeting"}},
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )
        assert result["success"] is True
        assert ha_client.send_ws_command.await_args.kwargs["uid"] == "e1"

    async def test_delete_not_found(self):
        ha_client = AsyncMock()
        ha_client.get_calendar_event_details = AsyncMock(
            return_value=[_detail("e1", "Other Event", "2026-06-08T10:00:00+00:00", "2026-06-08T11:00:00+00:00")]
        )
        with _resolved():
            result = await execute_calendar_action(
                {
                    "action": "delete_event",
                    "entity": "work",
                    "parameters": {"summary": "nonexistent", "start_date_time": "2026-06-08T10:00:00+00:00"},
                },
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )
        assert result["success"] is False
        assert "No matching event found" in result["speech"]


class TestUpdateEvent:
    async def test_update_moves_event_keeping_duration_and_fields(self):
        original = _detail(
            "event-123",
            "Team meeting",
            "2026-06-08T09:00:00+02:00",
            "2026-06-08T10:30:00+02:00",
            description="Weekly sync",
            location="Room A",
        )
        moved = _detail("event-123", "Team meeting", "2026-06-08T11:00:00+02:00", "2026-06-08T12:30:00+02:00")
        ha_client = AsyncMock()
        ha_client.get_calendar_event_details = AsyncMock(side_effect=[[original], [moved]])
        ha_client.send_ws_command = AsyncMock(return_value=None)
        with _resolved():
            result = await execute_calendar_action(
                {
                    "action": "update_event",
                    "entity": "work",
                    "parameters": {
                        "summary": "team meeting",
                        "start_date_time": "2026-06-08",
                        "new_start_date_time": "2026-06-08 11:00:00",
                    },
                },
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
                timezone="Europe/Berlin",
            )
        assert result["success"] is True, result["speech"]
        args = ha_client.send_ws_command.await_args
        assert args.args[0] == "calendar/event/update"
        assert args.kwargs["uid"] == "event-123"
        event = args.kwargs["event"]
        assert event["summary"] == "Team meeting"
        assert event["dtstart"] == "2026-06-08T11:00:00+02:00"
        assert event["dtend"] == "2026-06-08T12:30:00+02:00"
        assert event["description"] == "Weekly sync"
        assert event["location"] == "Room A"
        ha_client.call_service.assert_not_awaited()

    async def test_update_end_only_compat(self):
        original = _detail("event-123", "Meeting", "2026-06-08T10:00:00+00:00", "2026-06-08T11:00:00+00:00")
        updated = _detail("event-123", "Meeting", "2026-06-08T10:00:00+00:00", "2026-06-08T12:00:00+00:00")
        ha_client = AsyncMock()
        ha_client.get_calendar_event_details = AsyncMock(side_effect=[[original], [updated]])
        ha_client.send_ws_command = AsyncMock(return_value=None)
        with _resolved():
            result = await execute_calendar_action(
                {
                    "action": "update_event",
                    "entity": "work",
                    "parameters": {
                        "summary": "Meeting",
                        "start_date_time": "2026-06-08T10:00:00+00:00",
                        "end_date_time": "2026-06-08T12:00:00+00:00",
                    },
                },
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )
        assert result["success"] is True
        assert ha_client.send_ws_command.await_args.kwargs["event"]["dtend"] == "2026-06-08T12:00:00+00:00"

    async def test_update_not_confirmed(self):
        original = _detail("event-123", "Meeting", "2026-06-08T10:00:00+00:00", "2026-06-08T11:00:00+00:00")
        ha_client = AsyncMock()
        ha_client.get_calendar_event_details = AsyncMock(side_effect=[[original], [original]])
        ha_client.send_ws_command = AsyncMock(return_value=None)
        with _resolved():
            result = await execute_calendar_action(
                {
                    "action": "update_event",
                    "entity": "work",
                    "parameters": {"summary": "Meeting", "new_summary": "Renamed"},
                },
                ha_client,
                MagicMock(),
                MagicMock(),
                agent_id="calendar-agent",
            )
        assert result["success"] is False
        assert "did not confirm" in result["speech"]

    async def test_update_requires_identifier(self):
        result = await execute_calendar_action(
            {"action": "update_event", "entity": "work", "parameters": {}},
            AsyncMock(),
            MagicMock(),
            MagicMock(),
            agent_id="calendar-agent",
        )
        assert result["success"] is False
        assert "summary or start_date_time is required" in result["speech"]

    async def test_update_requires_changes(self):
        result = await execute_calendar_action(
            {"action": "update_event", "entity": "work", "parameters": {"summary": "Meeting"}},
            AsyncMock(),
            MagicMock(),
            MagicMock(),
            agent_id="calendar-agent",
        )
        assert result["success"] is False
        assert "No changes" in result["speech"]


class TestRestClientCalendarHelpers:
    @respx.mock
    async def test_get_calendar_event_details_reads_frontend_endpoint(self):
        route = respx.get("http://ha.local/api/calendars/calendar.work").mock(
            return_value=httpx.Response(200, json=[{"uid": "e1", "summary": "A", "start": {"date": "2026-06-08"}}])
        )
        client = HARestClient()
        client._base_url = "http://ha.local"
        client._client = httpx.AsyncClient(base_url="http://ha.local", headers={})
        events = await client.get_calendar_event_details(
            "calendar.work", "2026-06-08T00:00:00+00:00", "2026-06-09T00:00:00+00:00"
        )
        assert events == [{"uid": "e1", "summary": "A", "start": {"date": "2026-06-08"}}]
        assert route.calls.last.request.url.params["start"] == "2026-06-08T00:00:00+00:00"
        await client.close()

    async def test_send_ws_command_requires_connection(self):
        client = HARestClient()
        with pytest.raises(RuntimeError):
            await client.send_ws_command("calendar/event/delete", entity_id="calendar.work", uid="e1")

        ws = MagicMock()
        ws.is_connected = MagicMock(return_value=True)
        ws.send_command = AsyncMock(return_value=None)
        client.set_state_observer(ws)
        await client.send_ws_command("calendar/event/delete", entity_id="calendar.work", uid="e1")
        ws.send_command.assert_awaited_once_with("calendar/event/delete", entity_id="calendar.work", uid="e1")
