"""Admin sub-router: settings and wake-briefing management."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.db.repository import SettingsRepository

from ._shared import _bool_from_setting

logger = logging.getLogger(__name__)

_ENTITY_ID_LOOKS_VALID_RE = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")


class SettingsUpdatePayload(BaseModel):
    """Validated settings update payload."""

    items: dict[str, Any]

    @field_validator("items")
    @classmethod
    def validate_items(cls, v):
        if not v:
            raise ValueError("items must not be empty")
        for key in v:
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError(f"Invalid setting key: {key}")
        return v


class WakeBriefingSourcesPayload(BaseModel):
    weather: bool = True
    date: bool = True
    news: bool = True
    calendar: bool = True
    sensors: bool = False


class WakeBriefingSettingsPayload(BaseModel):
    enabled: bool = True
    sources: WakeBriefingSourcesPayload = WakeBriefingSourcesPayload()
    sensor_entities: list[str] = []
    news_query: str = "top news today"
    news_count: int = 3
    timeout_seconds: int = 10
    composer_prompt: str

    @field_validator("news_count")
    @classmethod
    def validate_news_count(cls, v: int) -> int:
        if v < 1 or v > 10:
            raise ValueError("news_count must be between 1 and 10")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout_seconds(cls, v: int) -> int:
        if v < 1 or v > 60:
            raise ValueError("timeout_seconds must be between 1 and 60")
        return v

    @field_validator("sensor_entities")
    @classmethod
    def validate_sensor_entities(cls, v: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for entity_id in v:
            candidate = str(entity_id or "").strip()
            if not candidate:
                continue
            if not _ENTITY_ID_LOOKS_VALID_RE.match(candidate):
                raise ValueError(f"Invalid sensor entity_id: {entity_id}")
            if candidate not in seen:
                seen.add(candidate)
                normalized.append(candidate)
        return normalized

    @field_validator("composer_prompt")
    @classmethod
    def validate_composer_prompt(cls, v: str) -> str:
        prompt = (v or "").strip()
        if not prompt:
            raise ValueError("composer_prompt must not be empty")
        return prompt


def _validate_setting_value(key: str, value: str, value_type: str) -> None:
    """Validate a setting value against its stored type. Raises HTTPException on failure."""
    # COR-6: typed numeric/boolean settings must not accept the empty string,
    # otherwise the dashboard can blank out a value and silently store ""
    # which later coerces to a default in unrelated code paths.
    if value_type in ("int", "float", "bool") and value == "":
        raise HTTPException(
            status_code=400,
            detail=f"Invalid value for '{key}': empty string is not a valid {value_type}",
        )
    try:
        if value_type == "int":
            int(value)
        elif value_type == "float":
            float(value)
        elif value_type == "bool" and str(value).lower() not in ("true", "false", "1", "0"):
            raise ValueError("Expected boolean")
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid value for '{key}': expected {value_type}",
        ) from None


async def _load_wake_briefing_settings() -> dict[str, Any]:
    try:
        sensor_entities = json.loads(
            (await SettingsRepository.get_value("wake_briefing.sensor_entities", "[]")) or "[]"
        )
    except json.JSONDecodeError:
        sensor_entities = []
    if not isinstance(sensor_entities, list):
        sensor_entities = []

    timeout_raw = await SettingsRepository.get_value("wake_briefing.timeout_seconds", "10")
    try:
        timeout_seconds = int(timeout_raw or "10")
    except (TypeError, ValueError):
        timeout_seconds = 10

    news_count_raw = await SettingsRepository.get_value("wake_briefing.news_count", "3")
    try:
        news_count = int(news_count_raw or "3")
    except (TypeError, ValueError):
        news_count = 3

    return {
        "enabled": _bool_from_setting(await SettingsRepository.get_value("wake_briefing.enabled", "true"), True),
        "sources": {
            "weather": _bool_from_setting(
                await SettingsRepository.get_value("wake_briefing.sources.weather", "true"),
                True,
            ),
            "date": _bool_from_setting(await SettingsRepository.get_value("wake_briefing.sources.date", "true"), True),
            "news": _bool_from_setting(await SettingsRepository.get_value("wake_briefing.sources.news", "true"), True),
            "calendar": _bool_from_setting(
                await SettingsRepository.get_value("wake_briefing.sources.calendar", "true"),
                True,
            ),
            "sensors": _bool_from_setting(
                await SettingsRepository.get_value("wake_briefing.sources.sensors", "false"),
                False,
            ),
        },
        "sensor_entities": [str(entity_id).strip() for entity_id in sensor_entities if str(entity_id).strip()],
        "news_query": (await SettingsRepository.get_value("wake_briefing.news_query", "top news today"))
        or "top news today",
        "news_count": max(1, min(10, news_count)),
        "timeout_seconds": max(1, min(60, timeout_seconds)),
        "composer_prompt": (await SettingsRepository.get_value("wake_briefing.composer_prompt", "")) or "",
    }


def _wake_briefing_updates_from_payload(
    payload: WakeBriefingSettingsPayload,
    *,
    force_enabled: bool = False,
) -> dict[str, str]:
    return {
        "wake_briefing.enabled": "true" if (force_enabled or payload.enabled) else "false",
        "wake_briefing.sources.weather": "true" if payload.sources.weather else "false",
        "wake_briefing.sources.date": "true" if payload.sources.date else "false",
        "wake_briefing.sources.news": "true" if payload.sources.news else "false",
        "wake_briefing.sources.calendar": "true" if payload.sources.calendar else "false",
        "wake_briefing.sources.sensors": "true" if payload.sources.sensors else "false",
        "wake_briefing.sensor_entities": json.dumps(payload.sensor_entities),
        "wake_briefing.news_query": (payload.news_query or "").strip() or "top news today",
        "wake_briefing.news_count": str(payload.news_count),
        "wake_briefing.timeout_seconds": str(payload.timeout_seconds),
        "wake_briefing.composer_prompt": payload.composer_prompt,
    }


class _WakeBriefingPreviewSettingsRepository:
    def __init__(self, overrides: dict[str, str]) -> None:
        self._overrides = overrides

    async def get_value(self, key: str, default: Any = None) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        return await SettingsRepository.get_value(key, default)


router = APIRouter()


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    """Get all settings grouped by category."""
    rows = await SettingsRepository.get_all()
    grouped: dict[str, list] = {}
    for row in rows:
        cat = row.get("category", "general")
        grouped.setdefault(cat, []).append(row)
    return {"settings": grouped}


@router.put("/settings")
async def update_settings(payload: SettingsUpdatePayload) -> dict[str, str]:
    """Update multiple settings. Payload: {"items": {key: value, ...}}."""
    # Pass 1: resolve and validate every key before any write so a single
    # invalid key aborts the whole update without partial state.
    resolved: list[tuple[str, str, dict[str, Any]]] = []
    for key, value in payload.items.items():
        existing = await SettingsRepository.get(key)
        if existing is None:
            raise HTTPException(status_code=400, detail=f"Unknown setting key: {key}")
        value_type = existing.get("value_type", "str")
        _validate_setting_value(key, str(value), value_type)
        stored_value = str(value).lower() if value_type == "bool" else str(value)
        resolved.append((key, stored_value, existing))
    # Pass 2: apply.
    for key, stored_value, existing in resolved:
        await SettingsRepository.set(
            key,
            stored_value,
            value_type=existing["value_type"],
            category=existing.get("category", "general"),
            description=existing.get("description"),
        )
    return {"status": "ok"}


@router.get("/settings/wake-briefing")
async def get_wake_briefing_settings() -> dict[str, Any]:
    """Return structured wake-briefing settings for the timers dashboard."""
    return await _load_wake_briefing_settings()


@router.put("/settings/wake-briefing")
async def update_wake_briefing_settings(payload: WakeBriefingSettingsPayload) -> dict[str, Any]:
    """Persist wake-briefing settings from the timers dashboard."""
    updates = _wake_briefing_updates_from_payload(payload)

    for key, value in updates.items():
        existing = await SettingsRepository.get(key)
        if existing is None:
            raise HTTPException(status_code=400, detail=f"Unknown setting key: {key}")
        await SettingsRepository.set(
            key,
            value,
            value_type=existing["value_type"],
            category=existing.get("category", "general"),
            description=existing.get("description"),
        )

    return {"status": "ok", "settings": await _load_wake_briefing_settings()}


@router.post("/settings/wake-briefing/test")
async def test_wake_briefing_settings(payload: WakeBriefingSettingsPayload, request: Request) -> dict[str, Any]:
    """Compose a wake briefing preview from unsaved dashboard values."""
    gateway = getattr(request.app.state, "dispatcher", None)
    ha_client = getattr(request.app.state, "ha_client", None)
    entity_index = getattr(request.app.state, "entity_index", None)
    if gateway is None or ha_client is None:
        raise HTTPException(status_code=503, detail="Wake briefing preview unavailable")

    import time as _time

    from app.agents.wake_briefing import compose_wake_briefing
    from app.ha_client.home_context import home_context_provider

    timezone_name = "UTC"
    with contextlib.suppress(Exception):
        home_context = await home_context_provider.get(ha_client)
        timezone_name = getattr(home_context, "timezone", "UTC") or "UTC"

    language = str(await SettingsRepository.get_value("language", "en") or "en").strip() or "en"
    if language == "auto":
        language = "en"

    preview = await compose_wake_briefing(
        gateway,
        {
            "alarm_name": "Wake Briefing Test",
            "alarm_label": "Wake Briefing Test",
            "briefing": True,
            "language": language,
            "scheduled_for_epoch": int(_time.time()) + 60,
            "timezone": timezone_name,
            "origin_device_id": None,
            "origin_area": None,
        },
        ha_client=ha_client,
        entity_index=entity_index,
        settings_repo=_WakeBriefingPreviewSettingsRepository(
            _wake_briefing_updates_from_payload(payload, force_enabled=True)
        ),
    )
    return {"status": "ok", "preview": preview}


@router.put("/settings/{key}")
async def update_single_setting(key: str, payload: dict) -> dict[str, Any]:
    """Update a single setting by key."""
    value = payload.get("value")
    if value is None:
        return {"status": "error", "detail": "Missing value"}

    existing = await SettingsRepository.get(key)
    if existing is None:
        raise HTTPException(status_code=400, detail=f"Unknown setting key: {key}")

    value_type = existing.get("value_type", "str")
    _validate_setting_value(key, str(value), value_type)

    await SettingsRepository.set(
        key,
        str(value),
        value_type=existing["value_type"],
        category=existing.get("category", "general"),
        description=existing.get("description"),
    )
    return {"status": "ok", "key": key}
