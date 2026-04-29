"""Calendar admin REST API endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.db.repository import (
    CalendarEntitySettingsRepository,
    CalendarReminderStateRepository,
    CalendarUserMappingRepository,
    SettingsRepository,
)
from app.security.auth import require_admin_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/calendar",
    tags=["calendar-admin"],
    dependencies=[Depends(require_admin_session)],
)


# --- Request models ---


class UserMappingCreate(BaseModel):
    display_name: str
    calendar_entity_ids: list[str]
    reminder_offsets: list[int]
    is_default_user: bool = False


class UserMappingUpdate(BaseModel):
    display_name: str | None = None
    calendar_entity_ids: list[str] | None = None
    reminder_offsets: list[int] | None = None
    is_default_user: bool | None = None


class EventDeletePayload(BaseModel):
    calendar_id: str
    uid: str


class EventCreatePayload(BaseModel):
    calendar_id: str
    summary: str
    start_date_time: str
    end_date_time: str
    description: str | None = None
    location: str | None = None
    rrule: str | None = None


class SettingsUpdatePayload(BaseModel):
    enabled: bool | None = None
    offsets: list[int] | None = None
    lookahead_hours: int | None = None


class EntitySettingPayload(BaseModel):
    enabled: bool


# --- User Mappings ---


@router.get("/users")
async def list_calendar_users():
    """List all calendar user mappings."""
    rows = await CalendarUserMappingRepository.list_all()
    for row in rows:
        try:
            row["calendar_entity_ids"] = json.loads(row.get("calendar_entity_ids_json", "[]"))
        except Exception:
            row["calendar_entity_ids"] = []
        try:
            row["reminder_offsets"] = json.loads(row.get("reminder_offsets_json", "[1440, 60, 15]"))
        except Exception:
            row["reminder_offsets"] = [1440, 60, 15]
    return rows


@router.post("/users")
async def create_calendar_user(body: UserMappingCreate):
    """Create a new calendar user mapping."""
    if not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name is required")
    row_id = await CalendarUserMappingRepository.create(
        display_name=body.display_name.strip(),
        calendar_entity_ids_json=json.dumps(body.calendar_entity_ids),
        reminder_offsets_json=json.dumps(body.reminder_offsets),
        is_default_user=1 if body.is_default_user else 0,
    )
    return {"id": row_id}


@router.patch("/users/{mapping_id}")
async def update_calendar_user(mapping_id: int, body: UserMappingUpdate):
    """Update an existing calendar user mapping."""
    fields: dict[str, Any] = {}
    if body.display_name is not None:
        fields["display_name"] = body.display_name.strip()
    if body.calendar_entity_ids is not None:
        fields["calendar_entity_ids_json"] = json.dumps(body.calendar_entity_ids)
    if body.reminder_offsets is not None:
        fields["reminder_offsets_json"] = json.dumps(body.reminder_offsets)
    if body.is_default_user is not None:
        fields["is_default_user"] = 1 if body.is_default_user else 0
    ok = await CalendarUserMappingRepository.update(mapping_id, **fields)
    if not ok:
        raise HTTPException(status_code=404, detail="Mapping not found")
    return {"ok": True}


@router.delete("/users/{mapping_id}")
async def delete_calendar_user(mapping_id: int):
    """Delete a calendar user mapping."""
    ok = await CalendarUserMappingRepository.delete(mapping_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Mapping not found")
    return {"ok": True}


# --- Events (proxy to HA) ---


@router.get("/events")
async def list_calendar_events(request: Request, calendar_id: str, start: str, end: str):
    """List events from a HA calendar."""
    ha_client = request.app.state.ha_client
    if not ha_client:
        raise HTTPException(status_code=503, detail="HA client not available")
    try:
        events = await ha_client.get_calendar_events(calendar_id, start, end)
        return {"events": events or []}
    except Exception as exc:
        logger.warning("Failed to list calendar events: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/events")
async def create_calendar_event(request: Request, body: EventCreatePayload):
    """Create a calendar event via HA."""
    ha_client = request.app.state.ha_client
    if not ha_client:
        raise HTTPException(status_code=503, detail="HA client not available")
    service_data: dict[str, str] = {
        "summary": body.summary,
        "start_date_time": body.start_date_time,
        "end_date_time": body.end_date_time,
    }
    if body.description:
        service_data["description"] = body.description
    if body.location:
        service_data["location"] = body.location
    if body.rrule:
        service_data["rrule"] = body.rrule
    try:
        await ha_client.call_service("calendar", "create_event", body.calendar_id, service_data)
        return {"ok": True}
    except Exception as exc:
        logger.warning("Failed to create calendar event: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/events")
async def delete_calendar_event(request: Request, body: EventDeletePayload):
    """Delete a calendar event via HA."""
    ha_client = request.app.state.ha_client
    if not ha_client:
        raise HTTPException(status_code=503, detail="HA client not available")
    try:
        await ha_client.call_service("calendar", "delete_event", body.calendar_id, {"uid": body.uid})
        return {"ok": True}
    except Exception as exc:
        logger.warning("Failed to delete calendar event: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# --- Calendars ---


@router.get("/calendars")
async def list_calendars(request: Request):
    """List visible calendar entities with their enabled status."""
    entity_index = request.app.state.entity_index
    if not entity_index:
        return []
    entries = []
    if hasattr(entity_index, "list_entries_async"):
        entries = await entity_index.list_entries_async(domains={"calendar"})
    elif hasattr(entity_index, "list_entries"):
        entries = entity_index.list_entries(domains={"calendar"})

    # Load enabled states from DB
    db_settings = {s["entity_id"]: s["enabled"] for s in await CalendarEntitySettingsRepository.list_all()}

    result = []
    for e in entries:
        eid = getattr(e, "entity_id", "")
        fname = getattr(e, "friendly_name", "") or eid
        # Default to enabled if no explicit DB row
        enabled = bool(db_settings.get(eid, 1))
        result.append({
            "entity_id": eid,
            "friendly_name": fname,
            "enabled": enabled,
        })
    return result


# --- Entity Settings ---


@router.get("/entity-settings")
async def list_entity_settings():
    """List all calendar entity enablement settings."""
    return await CalendarEntitySettingsRepository.list_all()


@router.put("/entity-settings/{entity_id}")
async def update_entity_setting(entity_id: str, body: EntitySettingPayload):
    """Enable or disable a specific calendar entity for reminders."""
    await CalendarEntitySettingsRepository.set_enabled(entity_id, 1 if body.enabled else 0)
    return {"ok": True, "entity_id": entity_id, "enabled": body.enabled}


@router.post("/entity-settings/sync")
async def sync_entity_settings(request: Request):
    """Sync all visible calendar entities into the settings table (default enabled)."""
    entity_index = request.app.state.entity_index
    if not entity_index:
        return {"synced": 0}
    entries = []
    if hasattr(entity_index, "list_entries_async"):
        entries = await entity_index.list_entries_async(domains={"calendar"})
    elif hasattr(entity_index, "list_entries"):
        entries = entity_index.list_entries(domains={"calendar"})

    count = 0
    for e in entries:
        eid = getattr(e, "entity_id", "")
        fname = getattr(e, "friendly_name", "")
        if eid:
            existing = await CalendarEntitySettingsRepository.get(eid)
            if existing is None:
                await CalendarEntitySettingsRepository.upsert(eid, friendly_name=fname or None, enabled=1)
                count += 1
    return {"synced": count}


# --- Settings ---


@router.get("/settings")
async def get_calendar_settings():
    """Get calendar injection settings."""
    enabled = await SettingsRepository.get_value("calendar.reminder_injection.enabled", "true")
    offsets_raw = await SettingsRepository.get_value("calendar.reminder_injection.offsets", "[1440, 60, 15]")
    lookahead = await SettingsRepository.get_value("calendar.reminder_injection.lookahead_hours", "24")
    try:
        offsets = json.loads(offsets_raw) if offsets_raw else [1440, 60, 15]
    except Exception:
        offsets = [1440, 60, 15]
    return {
        "enabled": str(enabled).lower() == "true",
        "offsets": offsets,
        "lookahead_hours": int(lookahead) if lookahead else 24,
    }


@router.post("/settings")
async def update_calendar_settings(body: SettingsUpdatePayload):
    """Update calendar injection settings."""
    if body.enabled is not None:
        await SettingsRepository.set(
            "calendar.reminder_injection.enabled",
            "true" if body.enabled else "false",
            value_type="bool",
            category="calendar",
        )
    if body.offsets is not None:
        await SettingsRepository.set(
            "calendar.reminder_injection.offsets",
            json.dumps(body.offsets),
            value_type="json",
            category="calendar",
        )
    if body.lookahead_hours is not None:
        await SettingsRepository.set(
            "calendar.reminder_injection.lookahead_hours",
            str(body.lookahead_hours),
            value_type="int",
            category="calendar",
        )
    return {"ok": True}


# --- Debug ---


@router.delete("/reminder-state")
async def clear_reminder_state():
    """Clear all fired reminder state."""
    import time
    count = await CalendarReminderStateRepository.cleanup_old(int(time.time()) + 1)
    return {"cleared": count}
