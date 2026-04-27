"""Admin REST API endpoints."""

from __future__ import annotations

import logging
import json
import re
import secrets
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from app.db.repository import (
    AgentConfigRepository,
    EntityMatchingConfigRepository,
    EntityVisibilityRepository,
    ScheduledTimersRepository,
    SecretsRepository,
    SettingsRepository,
)
from app.ha_client.auth import get_ha_token, set_ha_token
from app.ha_client.rest import test_ha_connection
from app.security.auth import API_KEY_SECRET_NAME, require_admin_session
from app.security.encryption import delete_secret, retrieve_secret, store_secret

logger = logging.getLogger(__name__)

# Maps provider name to its secret key in the secrets table
PROVIDER_SECRET_KEYS = {
    "openrouter": "openrouter_api_key",
    "groq": "groq_api_key",
    "anthropic": "anthropic_api_key",
}
_ENTITY_ID_LOOKS_VALID_RE = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")
_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


class ProviderKeyUpdate(BaseModel):
    provider: str
    api_key: str


class OllamaUrlUpdate(BaseModel):
    url: str


class ProviderTestRequest(BaseModel):
    provider: str
    api_key: str | None = None


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


class HaConnectionUpdate(BaseModel):
    """Dashboard: Home Assistant REST/WebSocket endpoint + optional new token."""

    ha_url: str
    ha_token: str | None = None

    @field_validator("ha_url")
    @classmethod
    def normalize_ha_url(cls, v: str) -> str:
        v = (v or "").strip().rstrip("/")
        if not v:
            raise ValueError("Home Assistant URL is required")
        low = v.lower()
        if not low.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


class HaConnectionTestRequest(BaseModel):
    """Optional overrides; when omitted, stored settings are used."""

    ha_url: str | None = None
    ha_token: str | None = None


class ContainerApiKeySetPayload(BaseModel):
    """Set the HA-to-container API key manually (paste from backup or external generator)."""

    api_key: str

    @field_validator("api_key")
    @classmethod
    def strip_and_min_len(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) < 16:
            raise ValueError("API key must be at least 16 characters")
        return v


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


async def _reload_ha_clients_after_settings_change(request: Request) -> None:
    """Rebuild REST client and drop WS so ``run()`` reconnects with new URL/token."""
    ha_client = getattr(request.app.state, "ha_client", None)
    if ha_client is not None:
        try:
            await ha_client.reload()
        except Exception:
            logger.warning("HARestClient.reload() after HA settings change failed", exc_info=True)
    ws_client = getattr(request.app.state, "ws_client", None)
    if ws_client is not None:
        try:
            await ws_client.drop_connection()
        except Exception:
            logger.warning("HA WebSocket drop_connection() failed", exc_info=True)


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin_session)])

# The registry is set by main.py during startup
_registry = None


def set_registry(reg) -> None:
    """Called by main.py to inject the A2A registry."""
    global _registry
    _registry = reg


async def _resolve_origin_label(
    ha_client: Any,
    area_registry: dict[str, str],
    origin_device_id: str | None,
    origin_area: str | None,
) -> str | None:
    if not ha_client:
        return origin_device_id or origin_area
    if origin_device_id:
        try:
            raw = await ha_client.render_template(
                "{{ device_attr('"
                + str(origin_device_id)
                + "', 'name_by_user') or device_attr('"
                + str(origin_device_id)
                + "', 'name') or '' }}"
            )
            name = (str(raw or "")).strip()
            if name and name.lower() != "none":
                return name
        except Exception:
            logger.debug("Failed to resolve timer origin device label for %s", origin_device_id, exc_info=True)
        return origin_device_id
    if origin_area:
        return area_registry.get(origin_area) or origin_area
    return None


async def _resolve_ha_device_id(
    ha_client: Any,
    entity_id: str,
) -> str | None:
    if not ha_client or not entity_id:
        return None
    template = "{{ device_id('" + entity_id + "') }}"
    rendered: str | None = None
    try:
        rendered = await ha_client.render_template(template)
    except Exception:
        logger.debug("Failed to resolve device_id for %s", entity_id, exc_info=True)
        return None

    if not rendered:
        return None
    cleaned = str(rendered).strip()
    if not cleaned or cleaned.lower() == "none":
        return None
    return cleaned


@router.get("/agents")
async def list_agents():
    """List all agents (registered + disabled from DB)."""
    agents = await _registry.list_agents()
    seen_ids = set()
    result = []
    for a in agents:
        card = a.model_dump()
        config = await AgentConfigRepository.get(a.agent_id)
        if config:
            card.update(config)
        result.append(card)
        seen_ids.add(a.agent_id)

    # Known built-in agent IDs (from seed data)
    builtin_agents = {
        "orchestrator",
        "general-agent",
        "light-agent",
        "music-agent",
        "timer-agent",
        "climate-agent",
        "media-agent",
        "scene-agent",
        "automation-agent",
        "security-agent",
        "rewrite-agent",
        "send-agent",
    }

    # Include disabled built-in agents from DB that are not yet registered
    all_configs = await AgentConfigRepository.list_all()
    for config in all_configs:
        aid = config["agent_id"]
        if aid not in seen_ids and aid in builtin_agents:
            entry = {
                "agent_id": aid,
                "name": aid.replace("-", " ").title(),
                "description": config.get("description", ""),
                "skills": [],
                "input_types": ["text/plain"],
                "output_types": ["text/plain", "application/json"],
                "endpoint": f"local://{aid}",
            }
            entry.update(config)
            result.append(entry)
    return {"agents": result}


@router.get("/settings")
async def get_settings():
    """Get all settings grouped by category."""
    rows = await SettingsRepository.get_all()
    grouped: dict[str, list] = {}
    for row in rows:
        cat = row.get("category", "general")
        grouped.setdefault(cat, []).append(row)
    return {"settings": grouped}


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


def _bool_from_setting(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def _load_wake_briefing_settings() -> dict[str, Any]:
    try:
        sensor_entities = json.loads((await SettingsRepository.get_value("wake_briefing.sensor_entities", "[]")) or "[]")
    except Exception:
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


def _normalize_alarm_recurrence_for_response(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        recurrence = AlarmRecurrencePayload.model_validate(raw)
    except ValidationError:
        return None
    return recurrence.to_runtime_dict()


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
    except Exception:
        payload_dict = {}

    current_recurrence = payload_dict.get("recurrence") if isinstance(payload_dict.get("recurrence"), dict) else {}
    timezone_name = str(current_recurrence.get("timezone") or payload_dict.get("timezone") or "").strip()
    if not timezone_name:
        timezone_name = "UTC"
        ha_client = getattr(request.app.state, "ha_client", None)
        if ha_client is not None:
            try:
                from app.ha_client.home_context import home_context_provider

                home_context = await home_context_provider.get(ha_client)
                timezone_name = getattr(home_context, "timezone", "UTC") or "UTC"
            except Exception:
                logger.debug("Failed to resolve HomeContext while patching alarm recurrence", exc_info=True)

    tzinfo = None
    try:
        tzinfo = ZoneInfo(timezone_name)
    except Exception:
        timezone_name = "UTC"

    fires_at_epoch = int(payload.fires_at or row.get("fires_at") or 0)
    if fires_at_epoch <= 0:
        raise HTTPException(status_code=422, detail="fires_at is required to build recurring alarm metadata")
    local_dt = datetime.fromtimestamp(fires_at_epoch, tz=tzinfo) if tzinfo is not None else datetime.fromtimestamp(fires_at_epoch)

    recurrence = payload.recurrence.to_runtime_dict()
    recurrence["anchor_time"] = local_dt.strftime("%H:%M:%S")
    recurrence["timezone"] = timezone_name
    return recurrence, False


@router.put("/settings")
async def update_settings(payload: SettingsUpdatePayload):
    """Update multiple settings. Payload: {"items": {key: value, ...}}."""
    for key, value in payload.items.items():
        existing = await SettingsRepository.get(key)
        if existing is None:
            raise HTTPException(status_code=400, detail=f"Unknown setting key: {key}")
        # Validate value type against stored type
        value_type = existing.get("value_type", "str")
        _validate_setting_value(key, str(value), value_type)
        await SettingsRepository.set(
            key,
            str(value),
            value_type=existing["value_type"],
            category=existing.get("category", "general"),
            description=existing.get("description"),
        )
    return {"status": "ok"}


@router.get("/settings/wake-briefing")
async def get_wake_briefing_settings():
    """Return structured wake-briefing settings for the timers dashboard."""
    return await _load_wake_briefing_settings()


@router.put("/settings/wake-briefing")
async def update_wake_briefing_settings(payload: WakeBriefingSettingsPayload):
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
async def test_wake_briefing_settings(payload: WakeBriefingSettingsPayload, request: Request):
    """Compose a wake briefing preview from unsaved dashboard values."""
    gateway = getattr(request.app.state, "orchestrator_gateway", None)
    ha_client = getattr(request.app.state, "ha_client", None)
    entity_index = getattr(request.app.state, "entity_index", None)
    if gateway is None or ha_client is None:
        raise HTTPException(status_code=503, detail="Wake briefing preview unavailable")

    from app.agents.wake_briefing import compose_wake_briefing
    from app.ha_client.home_context import home_context_provider

    import time as _time

    timezone_name = "UTC"
    try:
        home_context = await home_context_provider.get(ha_client)
        timezone_name = getattr(home_context, "timezone", "UTC") or "UTC"
    except Exception:
        logger.debug("Failed to resolve HomeContext for wake briefing preview", exc_info=True)

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
async def update_single_setting(key: str, payload: dict):
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


@router.get("/ha-connection")
async def get_ha_connection():
    """Return HA base URL and whether a long-lived token is stored (masked)."""
    url = await SettingsRepository.get_value("ha_url") or ""
    token = await get_ha_token()
    masked = None
    if token:
        masked = f"...{token[-4:]}" if len(token) >= 4 else "****"
    return {
        "ha_url": url or None,
        "token_configured": bool(token),
        "token_masked": masked,
    }


@router.put("/ha-connection")
async def update_ha_connection(request: Request, payload: HaConnectionUpdate):
    """Persist HA URL and optionally replace the long-lived access token.

    Empty or omitted ``ha_token`` leaves the existing encrypted token
    unchanged. After a successful save, the REST client reloads and the
    WebSocket client is dropped so it reconnects with fresh settings.
    """
    await SettingsRepository.set(
        "ha_url",
        payload.ha_url,
        "string",
        "ha",
        "Home Assistant URL",
    )
    if payload.ha_token and payload.ha_token.strip():
        await set_ha_token(payload.ha_token.strip())
    await _reload_ha_clients_after_settings_change(request)
    return {"status": "ok"}


@router.post("/ha-connection/test")
async def test_ha_connection_admin(payload: HaConnectionTestRequest):
    """Probe ``GET {ha_url}/api/`` with a bearer token (wizard parity)."""
    url = (payload.ha_url or "").strip().rstrip("/")
    if not url:
        url = (await SettingsRepository.get_value("ha_url") or "").strip().rstrip("/")
    token = payload.ha_token.strip() if payload.ha_token else ""
    if not token:
        raw = await get_ha_token()
        token = (raw or "").strip()
    if not url or not token:
        return {
            "status": "error",
            "detail": "Need both URL and token (enter token in the form or save it first).",
        }
    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return {"status": "error", "detail": "URL must start with http:// or https://"}
    ok = await test_ha_connection(url, token)
    if ok:
        return {"status": "ok"}
    return {
        "status": "error",
        "detail": "Could not reach Home Assistant /api/ with these credentials.",
    }


@router.get("/container-api-key")
async def get_container_api_key_status():
    """Return whether the HA integration key exists and a last-4 mask (never full key)."""
    raw = await retrieve_secret(API_KEY_SECRET_NAME)
    if not raw:
        return {"configured": False, "token_masked": None}
    masked = f"...{raw[-4:]}" if len(raw) >= 4 else "****"
    return {"configured": True, "token_masked": masked}


@router.post("/container-api-key/rotate")
async def rotate_container_api_key():
    """Generate a new random key, replace the stored secret, return it once for the UI.

    After rotation, update the **HA-AgentHub** integration in Home Assistant
    with the new value (Settings - Devices - HA-AgentHub - Reconfigure).
    """
    api_key = secrets.token_urlsafe(32)
    await store_secret(API_KEY_SECRET_NAME, api_key)
    logger.info("Container API key rotated from admin dashboard")
    return {"status": "ok", "api_key": api_key}


@router.put("/container-api-key")
async def set_container_api_key(payload: ContainerApiKeySetPayload):
    """Persist a user-supplied API key (same auth semantics as setup wizard)."""
    await store_secret(API_KEY_SECRET_NAME, payload.api_key)
    logger.info("Container API key set manually from admin dashboard")
    return {"status": "ok"}


@router.get("/entity-matching-weights")
async def get_entity_matching_weights():
    """Get all entity matching signal weights."""
    rows = await EntityMatchingConfigRepository.get_all()
    return {"weights": {row["key"]: row["value"] for row in rows}}


@router.put("/entity-matching-weights")
async def update_entity_matching_weights(payload: dict):
    """Update entity matching signal weights. Payload: {key: value, ...}."""
    allowed_keys = {
        "weight.levenshtein",
        "weight.jaro_winkler",
        "weight.phonetic",
        "weight.embedding",
        "weight.alias",
    }
    items = payload.get("items", payload)
    if isinstance(items, dict):
        for key, value in items.items():
            if key in allowed_keys:
                await EntityMatchingConfigRepository.set(key, str(value))
    return {"status": "ok"}


# =========================================================================
# LLM Provider Management
# =========================================================================


@router.get("/llm-providers")
async def get_llm_provider_status():
    """Return status of all LLM providers with masked keys."""
    stored_keys = await SecretsRepository.list_keys()
    providers: dict = {}
    for provider, secret_key in PROVIDER_SECRET_KEYS.items():
        configured = secret_key in stored_keys
        masked_key = None
        if configured:
            raw = await retrieve_secret(secret_key)
            if raw and len(raw) >= 4:
                masked_key = raw[-4:]
            elif raw:
                masked_key = "****"
        providers[provider] = {"configured": configured, "masked_key": masked_key}
    # Ollama
    ollama_url = await SettingsRepository.get_value("ollama_base_url")
    providers["ollama"] = {
        "configured": ollama_url is not None,
        "url": ollama_url,
    }
    return {"providers": providers}


@router.put("/llm-providers")
async def update_llm_provider_key(payload: ProviderKeyUpdate):
    """Save an encrypted API key for a provider."""
    if payload.provider not in PROVIDER_SECRET_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {payload.provider}")
    secret_key = PROVIDER_SECRET_KEYS[payload.provider]
    await store_secret(secret_key, payload.api_key)
    return {"status": "ok", "provider": payload.provider}


@router.put("/llm-providers/ollama")
async def update_ollama_url(payload: OllamaUrlUpdate):
    """Save the Ollama base URL."""
    await SettingsRepository.set("ollama_base_url", payload.url, "string", "llm", "Ollama API URL")
    return {"status": "ok"}


@router.delete("/llm-providers/{provider}")
async def delete_llm_provider_key(provider: str):
    """Remove a stored API key for a provider."""
    if provider not in PROVIDER_SECRET_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    await delete_secret(PROVIDER_SECRET_KEYS[provider])
    return {"status": "ok"}


@router.post("/llm-providers/test")
async def test_llm_provider(payload: ProviderTestRequest):
    """Test connectivity for an LLM provider."""
    provider = payload.provider
    api_key = payload.api_key

    test_models = {
        "groq": "groq/llama-3.1-8b-instant",
        "openrouter": "openrouter/openai/gpt-4o-mini",
        "anthropic": "anthropic/claude-3-haiku-20240307",
        "ollama": "ollama/llama3",
    }
    if provider not in test_models:
        return {"status": "error", "detail": f"Unknown provider: {provider}"}

    # If no key given, retrieve stored key
    if provider == "ollama":
        base_url = await SettingsRepository.get_value("ollama_base_url", "http://localhost:11434")
        api_key = "not-needed"
    elif not api_key:
        secret_key = PROVIDER_SECRET_KEYS.get(provider)
        if secret_key:
            api_key = await retrieve_secret(secret_key)
        if not api_key:
            return {"status": "error", "detail": "No API key configured for " + provider}

    try:
        import litellm

        kwargs: dict = {
            "model": test_models[provider],
            "messages": [{"role": "user", "content": "Say hello"}],
            "api_key": api_key,
            "max_tokens": 10,
        }
        if provider == "ollama":
            kwargs["api_base"] = base_url
        await litellm.acompletion(**kwargs)
        return {"status": "ok", "provider": provider}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get("/llm-providers/configured")
async def get_configured_providers():
    """Return all known providers with their configuration status."""
    stored_keys = await SecretsRepository.list_keys()
    configured = []
    all_providers = []
    for provider, secret_key in PROVIDER_SECRET_KEYS.items():
        all_providers.append(provider)
        if secret_key in stored_keys:
            configured.append(provider)
    all_providers.append("ollama")
    ollama_url = await SettingsRepository.get_value("ollama_base_url")
    if ollama_url:
        configured.append("ollama")
    return {"providers": all_providers, "configured": configured}


# =========================================================================
# Entity Visibility Summary
# =========================================================================


@router.get("/agents/visibility-summary")
async def get_all_agents_visibility_summary():
    """Return a summary of entity visibility domains per agent."""
    all_rules = await EntityVisibilityRepository.list_all()
    agent_rules: dict[str, list[dict]] = {}
    for rule in all_rules:
        agent_id = rule["agent_id"]
        agent_rules.setdefault(agent_id, []).append(rule)

    summary: dict[str, dict] = {}
    for agent_id, rules in agent_rules.items():
        domains: set[str] = set()
        excluded_domains: set[str] = set()
        device_classes: set[str] = set()
        excluded_device_classes: set[str] = set()
        for r in rules:
            if r["rule_type"] == "domain_include":
                domains.add(r["rule_value"])
            elif r["rule_type"] == "domain_exclude":
                excluded_domains.add(r["rule_value"])
            elif r["rule_type"] == "area_include":
                domains.add("area:" + r["rule_value"])
            elif r["rule_type"] == "area_exclude":
                excluded_domains.add("area:" + r["rule_value"])
            elif r["rule_type"] == "entity_include":
                domain_part = r["rule_value"].split(".")[0] if "." in r["rule_value"] else r["rule_value"]
                domains.add(domain_part)
            elif r["rule_type"] == "entity_exclude":
                domain_part = r["rule_value"].split(".")[0] if "." in r["rule_value"] else r["rule_value"]
                excluded_domains.add(domain_part)
            elif r["rule_type"] == "device_class_include":
                device_classes.add(r["rule_value"])
            elif r["rule_type"] == "device_class_exclude":
                excluded_device_classes.add(r["rule_value"])
        summary[agent_id] = {
            "domains": sorted(domains),
            "excluded_domains": sorted(excluded_domains),
            "device_classes": sorted(device_classes),
            "excluded_device_classes": sorted(excluded_device_classes),
            "has_rules": True,
        }
    return {"summary": summary}


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
            area_registry = {}

    if scheduler is not None:
        try:
            rows = await scheduler.list()
        except Exception:
            rows = []
        import time as _time

        now = int(_time.time())
        for row in rows:
            if row.get("kind") == "alarm":
                fires_at = int(row.get("fires_at") or 0)
                try:
                    alarm_payload = json.loads(row.get("payload_json") or "{}")
                except Exception:
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
            states = []
        for state in states:
            entity_id = str(state.get("entity_id", "") or "").strip()
            if entity_id.startswith("assist_satellite."):
                satellite_entities.add(entity_id)

    known_ids: set[str] = set()
    for entity_id in satellite_entities:
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


@router.get("/fernet-key-backup")
async def get_fernet_key_backup():
    """Export the Fernet key for backup. Handle with extreme care."""
    from app.security.encryption import export_fernet_key

    return {
        "key": export_fernet_key(),
        "warning": "Store this key securely. Loss of this key makes all encrypted secrets unrecoverable.",
    }


# =========================================================================
# Notification Profile
# =========================================================================


@router.get("/notification-profile")
async def get_notification_profile():
    """Get current notification profile."""
    import json as _json

    raw = await SettingsRepository.get_value("notification.profile")
    if raw:
        return {"profile": _json.loads(raw)}
    return {"profile": {}}


@router.put("/notification-profile")
async def update_notification_profile(payload: dict):
    """Update notification profile."""
    import json as _json

    profile = payload.get("profile", payload)
    await SettingsRepository.set(
        "notification.profile",
        _json.dumps(profile),
        value_type="json",
        category="notification",
        description="Timer/alarm notification profile: channels and targets",
    )
    return {"status": "ok"}


# =========================================================================
# Alarm Monitor
# =========================================================================


@router.get("/alarm-monitor")
async def get_alarm_monitor_status(request: Request):
    """Get alarm monitor status."""
    alarm_monitor = getattr(request.app.state, "alarm_monitor", None)
    if not alarm_monitor:
        return {"active": False, "fired_today": [], "check_interval": 30}
    return {
        "active": True,
        "fired_today": alarm_monitor.fired_today,
        "check_interval": 30,
    }


# =========================================================================
# Recently Expired Timers
# =========================================================================


@router.get("/timers/recently-expired")
async def get_recently_expired_timers():
    """Recently-expired timers are no longer tracked separately.

    The AgentHub-managed scheduler stores firing time in the
    ``scheduled_timers`` table; clients should query ``/timers`` for
    state. This endpoint is kept for compatibility and returns an
    empty list.
    """
    return {"recently_expired": []}
