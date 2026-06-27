"""Admin sub-router: timers and internal alarms."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from app.db.repository import ScheduledTimersRepository

from ._shared import _bool_from_setting

logger = logging.getLogger(__name__)

_ENTITY_ID_SAFE_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


class AlarmRecurrencePayload(BaseModel):
    freq: str
    interval: int = 1
    byweekday: list[str] | None = None

    @field_validator("freq")
    @classmethod
    def validate_freq(cls, v: str) -> str:
        normalized = str(v or "").strip().casefold()
        if normalized not in {"daily", "weekly"}:
            raise ValueError("freq must be 'daily' or 'weekly'")
        return normalized

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("interval must be >= 1")
        return v

    @field_validator("byweekday")
    @classmethod
    def validate_byweekday(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        normalized: list[str] = []
        for item in v:
            code = str(item or "").strip().upper()
            if code not in _WEEKDAY_CODES:
                raise ValueError("byweekday must contain only MO,TU,WE,TH,FR,SA,SU")
            if code not in normalized:
                normalized.append(code)
        return normalized

    @model_validator(mode="after")
    def validate_shape(self):
        if self.freq == "weekly" and not self.byweekday:
            raise ValueError("weekly recurrence requires a non-empty byweekday list")
        if self.freq == "daily":
            self.byweekday = None
        return self

    def to_runtime_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "freq": self.freq,
            "interval": self.interval,
        }
        if self.freq == "weekly":
            payload["byweekday"] = list(self.byweekday or [])
        return payload


class TimerPatchPayload(BaseModel):
    """Validated payload for PATCH /api/admin/timers/{timer_id}."""

    logical_name: str | None = None
    fires_at: int | None = None
    duration_seconds: int | None = None
    briefing: bool | None = None
    is_recurring: bool | None = None
    recurrence: AlarmRecurrencePayload | None = None

    @field_validator("logical_name")
    @classmethod
    def validate_logical_name(cls, v: str | None) -> str | None:
        if v is not None and (not v.strip() or len(v) > 128):
            raise ValueError("logical_name must be 1-128 non-blank characters")
        return v.strip() if v is not None else None

    @field_validator("fires_at")
    @classmethod
    def validate_fires_at(cls, v: int | None) -> int | None:
        import time as _time

        if v is not None and v <= int(_time.time()):
            raise ValueError("fires_at must be a future Unix timestamp")
        return v

    @field_validator("duration_seconds")
    @classmethod
    def validate_duration_seconds(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("duration_seconds must be positive")
        return v


class TimerCreatePayload(BaseModel):
    """Validated payload for POST /api/admin/timers."""

    logical_name: str
    kind: str
    duration_seconds: int | None = None
    fires_at: int | None = None
    origin_device_id: str | None = None
    origin_area: str | None = None

    @field_validator("logical_name")
    @classmethod
    def validate_logical_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v or len(v) > 128:
            raise ValueError("logical_name must be 1-128 non-blank characters")
        return v

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: str) -> str:
        allowed = {"plain", "notification", "delayed_action", "sleep", "snooze", "alarm"}
        if v not in allowed:
            raise ValueError(f"kind must be one of {sorted(allowed)}")
        return v

    @field_validator("fires_at")
    @classmethod
    def validate_fires_at(cls, v: int | None) -> int | None:
        import time as _time

        if v is not None and v <= int(_time.time()):
            raise ValueError("fires_at must be a future Unix timestamp")
        return v

    @field_validator("origin_device_id", "origin_area")
    @classmethod
    def normalize_optional_origin_fields(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed or None


async def _resolve_origin_label(
    ha_client: Any,
    area_registry: dict[str, str],
    origin_device_id: str | None,
    origin_area: str | None,
) -> str | None:
    if not ha_client:
        return origin_device_id or origin_area
    if origin_device_id:
        # legitimate fail-soft: template rendering failure falls back to raw device_id
        with contextlib.suppress(OSError, ValueError, KeyError):
            raw = await ha_client.render_template(
                "{{ device_attr(origin_device_id, 'name_by_user') or device_attr(origin_device_id, 'name') or '' }}",
                variables={"origin_device_id": str(origin_device_id)},
            )
            name = (str(raw or "")).strip()
            if name and name.lower() != "none":
                return name
        return origin_device_id
    if origin_area:
        return area_registry.get(origin_area) or origin_area
    return None


def _validate_entity_id_safe(entity_id: str) -> bool:
    """Reject entity IDs that do not match the safe regex."""
    if not entity_id:
        return False
    return bool(_ENTITY_ID_SAFE_RE.match(entity_id))


async def _resolve_ha_device_id(
    ha_client: Any,
    entity_id: str,
) -> str | None:
    if not ha_client or not entity_id:
        return None
    rendered = None
    # legitimate fail-soft: template rendering failure falls back to None
    with contextlib.suppress(OSError, ValueError, KeyError):
        rendered = await ha_client.render_template(
            "{{ device_id(entity_id) }}",
            variables={"entity_id": entity_id},
        )
    if not rendered:
        return None
    cleaned = str(rendered).strip()
    if not cleaned or cleaned.lower() == "none":
        return None
    return cleaned


def _normalize_alarm_recurrence_for_response(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        recurrence = AlarmRecurrencePayload.model_validate(raw)
    except ValidationError:
        return None
    return recurrence.to_runtime_dict()


async def _build_alarm_recurrence_patch(
    row: dict[str, Any],
    payload: TimerPatchPayload,
    request: Request,
) -> tuple[dict[str, Any] | None, bool]:
    if payload.is_recurring is None and payload.recurrence is None:
        return None, False
    if payload.is_recurring is False:
        return None, True
    if payload.is_recurring is not True:
        return None, False
    if payload.recurrence is None:
        raise HTTPException(status_code=422, detail="recurrence is required when is_recurring=true")

    try:
        payload_dict = json.loads(row.get("payload_json") or "{}")
    except json.JSONDecodeError:
        payload_dict = {}

    current_recurrence = payload_dict.get("recurrence") if isinstance(payload_dict.get("recurrence"), dict) else {}
    timezone_name = str(current_recurrence.get("timezone") or payload_dict.get("timezone") or "").strip()
    if not timezone_name:
        timezone_name = "UTC"
        ha_client = getattr(request.app.state, "ha_client", None)
        if ha_client is not None:
            # legitimate fail-soft: home context failure falls back to UTC
            with contextlib.suppress(OSError, ValueError, KeyError):
                from app.ha_client.home_context import home_context_provider

                home_context = await home_context_provider.get(ha_client)
                timezone_name = getattr(home_context, "timezone", "UTC") or "UTC"

    tzinfo = None
    try:
        tzinfo = ZoneInfo(timezone_name)
    except (KeyError, TypeError):
        timezone_name = "UTC"

    fires_at_epoch = int(payload.fires_at or row.get("fires_at") or 0)
    if fires_at_epoch <= 0:
        raise HTTPException(status_code=422, detail="fires_at is required to build recurring alarm metadata")
    local_dt = (
        datetime.fromtimestamp(fires_at_epoch, tz=tzinfo)
        if tzinfo is not None
        else datetime.fromtimestamp(fires_at_epoch)
    )

    recurrence = payload.recurrence.to_runtime_dict()
    recurrence["anchor_time"] = local_dt.strftime("%H:%M:%S")
    recurrence["timezone"] = timezone_name
    return recurrence, False


router = APIRouter()


@router.get("/timers")
async def get_timers_info(request: Request):
    """Return scheduler-managed timer state plus internal and legacy alarm visibility."""
    ha_client = getattr(request.app.state, "ha_client", None)
    scheduler = getattr(request.app.state, "timer_scheduler", None)

    timers: list[dict] = []
    alarms: list[dict] = []
    area_registry: dict[str, str] = {}

    if ha_client:
        try:
            area_registry = await ha_client.get_area_registry()
        except Exception:
            logger.debug("Failed to load area registry", exc_info=True)
            area_registry = {}

    rows = []
    if scheduler is not None:
        with contextlib.suppress(Exception):
            rows = await scheduler.list()
        import time as _time

        now = int(_time.time())
        for row in rows:
            if row.get("kind") == "alarm":
                fires_at = int(row.get("fires_at") or 0)
                try:
                    alarm_payload = json.loads(row.get("payload_json") or "{}")
                except json.JSONDecodeError:
                    alarm_payload = {}
                recurrence = _normalize_alarm_recurrence_for_response(alarm_payload.get("recurrence"))
                alarms.append(
                    {
                        "id": row.get("id"),
                        "entity_id": f"agenthub_alarm:{row.get('id')}",
                        "name": row.get("logical_name") or "alarm",
                        "state": datetime.fromtimestamp(fires_at).strftime("%Y-%m-%d %H:%M:%S"),
                        "type": "internal",
                        "source": "internal",
                        "fires_at": fires_at,
                        "briefing": _bool_from_setting(str(row.get("briefing") or "0"), False),
                        "is_recurring": recurrence is not None,
                        "recurrence": recurrence,
                        "origin_area": row.get("origin_area"),
                        "origin_device_id": row.get("origin_device_id"),
                        "origin_label": await _resolve_origin_label(
                            ha_client,
                            area_registry,
                            row.get("origin_device_id"),
                            row.get("origin_area"),
                        ),
                    }
                )
                continue

            remaining_seconds = max(0, int(row["fires_at"]) - now)
            timers.append(
                {
                    "id": row["id"],
                    "logical_name": row["logical_name"],
                    "kind": row["kind"],
                    "duration_seconds": row["duration_seconds"],
                    "remaining_seconds": remaining_seconds,
                    "fires_at": int(row["fires_at"]),
                    "origin_area": row.get("origin_area"),
                    "origin_device_id": row.get("origin_device_id"),
                    "origin_label": await _resolve_origin_label(
                        ha_client,
                        area_registry,
                        row.get("origin_device_id"),
                        row.get("origin_area"),
                    ),
                    "state": row["state"],
                }
            )

    if ha_client:
        try:
            states = await ha_client.get_states()
        except Exception:
            logger.debug("Failed to load HA states", exc_info=True)
            states = []

        for s in states:
            entity_id = s.get("entity_id", "")
            state = s.get("state", "unknown")
            attrs = s.get("attributes", {})
            friendly_name = attrs.get("friendly_name", entity_id)
            if entity_id.startswith("input_datetime."):
                has_date = attrs.get("has_date", False)
                has_time = attrs.get("has_time", False)
                dtype = "datetime" if (has_date and has_time) else ("date" if has_date else "time")
                alarms.append(
                    {
                        "id": entity_id,
                        "entity_id": entity_id,
                        "name": friendly_name,
                        "state": state,
                        "type": dtype,
                        "source": "ha_legacy",
                    }
                )

    alarms.sort(key=lambda row: (str(row.get("source") or ""), int(row.get("fires_at") or 0), str(row.get("id") or "")))

    return {
        "timers": timers,
        "alarms": alarms,
    }


@router.get("/timers/satellites")
async def get_timer_satellites(request: Request):
    """Return known origin device IDs for timer/alarm creation dropdowns."""
    ha_client = getattr(request.app.state, "ha_client", None)
    area_registry: dict[str, str] = {}
    if ha_client:
        try:
            area_registry = await ha_client.get_area_registry()
        except Exception:
            logger.debug("Failed to load area registry for satellites", exc_info=True)
            area_registry = {}

    satellite_entities: set[str] = set()
    entity_index = getattr(request.app.state, "entity_index", None)
    if entity_index is not None:
        try:
            entries = await entity_index.list_entries_async(domains={"assist_satellite"})
            for entry in entries:
                entity_id = str(getattr(entry, "entity_id", "") or "").strip()
                if entity_id.startswith("assist_satellite."):
                    satellite_entities.add(entity_id)
        except Exception:
            logger.debug("Entity index satellite lookup failed", exc_info=True)

    if ha_client:
        try:
            states = await ha_client.get_states()
        except Exception:
            logger.debug("Failed to load HA states for satellites", exc_info=True)
            states = []
        for state in states:
            entity_id = str(state.get("entity_id", "") or "").strip()
            if entity_id.startswith("assist_satellite."):
                satellite_entities.add(entity_id)

    known_ids: set[str] = set()
    for entity_id in satellite_entities:
        if not _validate_entity_id_safe(entity_id):
            logger.warning("Skipping invalid entity_id in satellite lookup: %s", entity_id)
            continue
        device_id = await _resolve_ha_device_id(ha_client, entity_id)
        if device_id:
            known_ids.add(device_id)

    satellites: list[dict[str, str]] = []
    for device_id in known_ids:
        label = await _resolve_origin_label(ha_client, area_registry, device_id, None)
        satellites.append(
            {
                "device_id": device_id,
                "label": str(label or device_id),
            }
        )
    satellites.sort(key=lambda row: (row.get("label", "").lower(), row.get("device_id", "")))
    return {"satellites": satellites}


@router.delete("/timers/{timer_id}")
async def cancel_timer_by_id(timer_id: str, request: Request):
    """Cancel a pending scheduler timer or internal alarm by its row ID."""
    scheduler = getattr(request.app.state, "timer_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Timer scheduler unavailable")
    count = await scheduler.cancel(id_=timer_id)
    if count == 0:
        raise HTTPException(status_code=404, detail="Timer not found or already completed")
    return {"status": "ok", "cancelled": count}


@router.patch("/timers/{timer_id}")
async def patch_timer(timer_id: str, payload: TimerPatchPayload, request: Request):
    """Rename and/or reschedule a pending timer or internal alarm."""
    scheduler = getattr(request.app.state, "timer_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Timer scheduler unavailable")
    if (
        payload.logical_name is None
        and payload.fires_at is None
        and payload.duration_seconds is None
        and payload.briefing is None
        and payload.is_recurring is None
        and payload.recurrence is None
    ):
        raise HTTPException(status_code=422, detail="At least one field must be provided")

    row = await ScheduledTimersRepository.get(timer_id)
    if not row or row.get("state") != "pending":
        raise HTTPException(status_code=404, detail="Timer not found or already completed")

    if row.get("kind") != "alarm" and (
        payload.briefing is not None or payload.is_recurring is not None or payload.recurrence is not None
    ):
        raise HTTPException(status_code=422, detail="briefing and recurrence updates are only supported for alarms")

    recurrence, clear_recurrence = await _build_alarm_recurrence_patch(row, payload, request)
    updated = await scheduler.reschedule(
        timer_id,
        logical_name=payload.logical_name,
        new_fires_at=payload.fires_at,
        new_duration_seconds=payload.duration_seconds,
        briefing=payload.briefing,
        recurrence=recurrence,
        clear_recurrence=clear_recurrence,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Timer not found or already completed")
    return {"status": "ok"}


@router.post("/timers", status_code=201)
async def create_timer(payload: TimerCreatePayload, request: Request):
    """Create a new scheduler timer or internal alarm from the dashboard."""
    import time as _time

    scheduler = getattr(request.app.state, "timer_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Timer scheduler unavailable")

    now = int(_time.time())
    if payload.kind == "alarm":
        if payload.fires_at is None:
            raise HTTPException(status_code=422, detail="fires_at is required for kind=alarm")
        duration_seconds = payload.fires_at - now
        if duration_seconds <= 0:
            raise HTTPException(status_code=422, detail="fires_at must be in the future")
        alarm_payload = {
            "alarm_label": payload.logical_name,
            "scheduled_for_epoch": payload.fires_at,
        }
        timer_id = await scheduler.schedule(
            logical_name=payload.logical_name,
            kind="alarm",
            duration_seconds=duration_seconds,
            origin_device_id=payload.origin_device_id,
            origin_area=payload.origin_area,
            payload=alarm_payload,
        )
    else:
        if not payload.duration_seconds or payload.duration_seconds <= 0:
            raise HTTPException(status_code=422, detail="duration_seconds is required and must be positive for timers")
        timer_id = await scheduler.schedule(
            logical_name=payload.logical_name,
            kind=payload.kind,
            duration_seconds=payload.duration_seconds,
            origin_device_id=payload.origin_device_id,
            origin_area=payload.origin_area,
        )

    return {"status": "ok", "id": timer_id}


@router.get("/timers/recently-expired")
async def get_recently_expired_timers():
    """Recently-expired timers are no longer tracked separately.

    The AgentHub-managed scheduler stores firing time in the
    ``scheduled_timers`` table; clients should query ``/timers`` for
    state. This endpoint is kept for compatibility and returns an
    empty list.
    """
    return {"recently_expired": []}
