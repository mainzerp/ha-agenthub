"""Admin sub-router: Home Assistant connection management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, field_validator

from app.api.routes import admin as _admin_pkg
from app.db.repository import SettingsRepository
from app.ha_client.auth import set_ha_token

from ._shared import _reload_ha_clients_after_settings_change


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


router = APIRouter()


@router.get("/ha-connection")
async def get_ha_connection() -> dict[str, Any]:
    """Return HA base URL and whether a long-lived token is stored."""
    url = await SettingsRepository.get_value("ha_url") or ""
    token = await _admin_pkg.get_ha_token()
    return {
        "ha_url": url or None,
        "token_configured": bool(token),
    }


@router.put("/ha-connection")
async def update_ha_connection(request: Request, payload: HaConnectionUpdate) -> dict[str, Any]:
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
async def test_ha_connection_admin(payload: HaConnectionTestRequest) -> dict[str, Any]:
    """Probe ``GET {ha_url}/api/`` with a bearer token (wizard parity)."""
    url = (payload.ha_url or "").strip().rstrip("/")
    if not url:
        url = (await SettingsRepository.get_value("ha_url") or "").strip().rstrip("/")
    token = payload.ha_token.strip() if payload.ha_token else ""
    if not token:
        raw = await _admin_pkg.get_ha_token()
        token = (raw or "").strip()
    if not url or not token:
        return {
            "status": "error",
            "detail": "Need both URL and token (enter token in the form or save it first).",
        }
    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return {"status": "error", "detail": "URL must start with http:// or https://"}
    ok = await _admin_pkg.test_ha_connection(url, token)
    if ok:
        return {"status": "ok"}
    return {
        "status": "error",
        "detail": "Could not reach Home Assistant /api/ with these credentials.",
    }
