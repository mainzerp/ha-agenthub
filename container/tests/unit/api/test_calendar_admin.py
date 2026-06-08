"""Unit tests for calendar_admin API routes.

Mocks repositories and HA client to test all calendar admin endpoints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from tests.conftest import build_integration_test_app


def _build_app(**kwargs):
    """Build test app with admin session overridden."""
    return build_integration_test_app(
        setup_complete=True,
        override_api_key=True,
        override_admin_session=True,
        **kwargs,
    )


async def _client_for(app):
    """Return an httpx client with SetupState patched to complete."""
    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=True,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.mark.asyncio
class TestCalendarUsers:
    async def test_list_and_create_calendar_user(self, db_repository):
        app = _build_app()

        list_rows = [
            {
                "id": 1,
                "display_name": "Alice",
                "calendar_entity_ids_json": '["calendar.alice_work"]',
                "reminder_offsets_json": "[1440, 60]",
                "is_default_user": 0,
                "person_entity_id": None,
            }
        ]

        with (
            patch(
                "app.api.routes.calendar_admin.CalendarUserMappingRepository.list_all",
                new_callable=AsyncMock,
                return_value=list_rows,
            ),
            patch(
                "app.api.routes.calendar_admin.CalendarUserMappingRepository.create",
                new_callable=AsyncMock,
                return_value=2,
            ),
        ):
            async for client in _client_for(app):
                resp_list = await client.get("/api/admin/calendar/users")
                resp_create = await client.post(
                    "/api/admin/calendar/users",
                    json={
                        "display_name": "Bob",
                        "calendar_entity_ids": ["calendar.bob_home"],
                        "reminder_offsets": [60, 15],
                        "is_default_user": False,
                    },
                )

        assert resp_list.status_code == 200
        data_list = resp_list.json()
        assert len(data_list) == 1
        assert data_list[0]["display_name"] == "Alice"
        assert data_list[0]["calendar_entity_ids"] == ["calendar.alice_work"]
        assert data_list[0]["reminder_offsets"] == [1440, 60]

        assert resp_create.status_code == 200
        assert resp_create.json()["id"] == 2

    async def test_update_and_delete_calendar_user(self, db_repository):
        app = _build_app()

        with (
            patch(
                "app.api.routes.calendar_admin.CalendarUserMappingRepository.update",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.api.routes.calendar_admin.CalendarUserMappingRepository.delete",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            async for client in _client_for(app):
                resp_update = await client.patch(
                    "/api/admin/calendar/users/1",
                    json={"display_name": "Alice Updated"},
                )
                resp_delete = await client.delete("/api/admin/calendar/users/1")

        assert resp_update.status_code == 200
        assert resp_update.json()["ok"] is True

        assert resp_delete.status_code == 200
        assert resp_delete.json()["ok"] is True


@pytest.mark.asyncio
class TestCalendarEvents:
    async def test_list_calendar_events_and_crud_events(self, db_repository):
        ha_client = AsyncMock()
        ha_client.get_calendar_events = AsyncMock(return_value=[{"summary": "Meeting"}])
        ha_client.call_service = AsyncMock(return_value={"success": True})

        app = _build_app(ha_client=ha_client)

        async for client in _client_for(app):
            resp_list = await client.get(
                "/api/admin/calendar/events?calendar_id=calendar.work&start=2024-01-01T00:00:00&end=2024-01-02T00:00:00"
            )
            resp_create = await client.post(
                "/api/admin/calendar/events",
                json={
                    "calendar_id": "calendar.work",
                    "summary": "Dentist",
                    "start_date_time": "2024-01-01T10:00:00",
                    "end_date_time": "2024-01-01T11:00:00",
                },
            )
            resp_delete = await client.request(
                "DELETE",
                "/api/admin/calendar/events",
                json={"calendar_id": "calendar.work", "uid": "evt-123"},
            )

        assert resp_list.status_code == 200
        assert resp_list.json()["events"] == [{"summary": "Meeting"}]

        assert resp_create.status_code == 200
        assert resp_create.json()["ok"] is True

        assert resp_delete.status_code == 200
        assert resp_delete.json()["ok"] is True


@pytest.mark.asyncio
class TestCalendarsSettingsSyncAndEntitySettings:
    async def test_calendars_settings_sync_and_entity_settings(self, db_repository):
        ha_client = AsyncMock()
        entity_index = MagicMock()

        class _FakeEntry:
            entity_id = "calendar.work"
            friendly_name = "Work Calendar"

        entity_index.list_entries_async = AsyncMock(return_value=[_FakeEntry()])

        app = _build_app(ha_client=ha_client)
        app.state.entity_index = entity_index

        with (
            patch(
                "app.api.routes.calendar_admin.CalendarEntitySettingsRepository.list_all",
                new_callable=AsyncMock,
                return_value=[{"entity_id": "calendar.work", "enabled": 1, "is_universal": 0}],
            ),
            patch(
                "app.api.routes.calendar_admin.CalendarEntitySettingsRepository.set_enabled",
                new_callable=AsyncMock,
            ),
            patch(
                "app.api.routes.calendar_admin.CalendarEntitySettingsRepository.set_universal",
                new_callable=AsyncMock,
            ),
            patch(
                "app.api.routes.calendar_admin.CalendarEntitySettingsRepository.get",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.api.routes.calendar_admin.CalendarEntitySettingsRepository.upsert",
                new_callable=AsyncMock,
            ),
            patch(
                "app.api.routes.calendar_admin.SettingsRepository.get_value",
                new_callable=AsyncMock,
                side_effect=lambda key, default=None: {
                    "calendar.reminder_injection.enabled": "true",
                    "calendar.reminder_injection.offsets": "[1440, 60, 15]",
                    "calendar.reminder_injection.lookahead_hours": "24",
                }.get(key, default),
            ),
            patch(
                "app.api.routes.calendar_admin.SettingsRepository.set",
                new_callable=AsyncMock,
            ),
            patch(
                "app.api.routes.calendar_admin.CalendarReminderStateRepository.cleanup_old",
                new_callable=AsyncMock,
                return_value=3,
            ),
        ):
            async for client in _client_for(app):
                resp_calendars = await client.get("/api/admin/calendar/calendars")
                resp_entity_settings = await client.get("/api/admin/calendar/entity-settings")
                resp_update_entity = await client.put(
                    "/api/admin/calendar/entity-settings/calendar.work",
                    json={"enabled": True, "is_universal": False},
                )
                resp_sync = await client.post("/api/admin/calendar/entity-settings/sync")
                resp_settings = await client.get("/api/admin/calendar/settings")
                resp_update_settings = await client.post(
                    "/api/admin/calendar/settings",
                    json={"enabled": True, "offsets": [60, 15], "lookahead_hours": 12},
                )
                resp_clear = await client.delete("/api/admin/calendar/reminder-state")

        assert resp_calendars.status_code == 200
        calendars = resp_calendars.json()
        assert len(calendars) == 1
        assert calendars[0]["entity_id"] == "calendar.work"
        assert calendars[0]["enabled"] is True

        assert resp_entity_settings.status_code == 200
        assert resp_entity_settings.json() == [{"entity_id": "calendar.work", "enabled": 1, "is_universal": 0}]

        assert resp_update_entity.status_code == 200
        data = resp_update_entity.json()
        assert data["ok"] is True
        assert data["entity_id"] == "calendar.work"
        assert data["enabled"] is True
        assert data["is_universal"] is False

        assert resp_sync.status_code == 200
        assert resp_sync.json()["synced"] == 1

        assert resp_settings.status_code == 200
        settings = resp_settings.json()
        assert settings["enabled"] is True
        assert settings["offsets"] == [1440, 60, 15]
        assert settings["lookahead_hours"] == 24

        assert resp_update_settings.status_code == 200
        assert resp_update_settings.json()["ok"] is True

        assert resp_clear.status_code == 200
        assert resp_clear.json()["cleared"] == 3
