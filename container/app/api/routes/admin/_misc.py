"""Admin sub-router: container API key, entity weights, fernet backup, notification profile, alarm monitor."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, field_validator

from app.api.routes import admin as _admin_pkg
from app.db.repository import EntityMatchingConfigRepository, SettingsRepository
from app.security.auth import API_KEY_SECRET_NAME
from app.security.encryption import store_secret

logger = logging.getLogger(__name__)


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


class FernetKeyBackupPayload(BaseModel):
    passphrase: str


router = APIRouter()


@router.get("/container-api-key")
async def get_container_api_key_status() -> dict[str, Any]:
    """Return whether the HA integration key is configured."""
    raw = await _admin_pkg.retrieve_secret(API_KEY_SECRET_NAME)
    return {"configured": bool(raw)}


@router.post("/container-api-key/rotate")
async def rotate_container_api_key() -> dict[str, Any]:
    """Generate a new random key, replace the stored secret, return it once for the UI.

    After rotation, update the **HA-AgentHub** integration in Home Assistant
    with the new value (Settings - Devices - HA-AgentHub - Reconfigure).
    """
    api_key = secrets.token_urlsafe(32)
    await store_secret(API_KEY_SECRET_NAME, api_key)
    logger.info("Container API key rotated from admin dashboard")
    return {"status": "ok", "api_key": api_key}


@router.put("/container-api-key")
async def set_container_api_key(payload: ContainerApiKeySetPayload) -> dict[str, Any]:
    """Persist a user-supplied API key (same auth semantics as setup wizard)."""
    await store_secret(API_KEY_SECRET_NAME, payload.api_key)
    logger.info("Container API key set manually from admin dashboard")
    return {"status": "ok"}


@router.get("/entity-matching-weights")
async def get_entity_matching_weights() -> dict[str, Any]:
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


@router.post("/fernet-key-backup")
async def get_fernet_key_backup(payload: FernetKeyBackupPayload):
    """Export the Fernet key encrypted with a user-supplied passphrase."""
    from app.security.encryption import export_fernet_key

    passphrase = (payload.passphrase or "").strip()
    if not passphrase:
        return {"status": "error", "detail": "Passphrase required for key backup"}

    import base64
    import os

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    key_plaintext = export_fernet_key().encode("utf-8")
    salt = os.urandom(16)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600000,
    )
    aes_key = kdf.derive(passphrase.encode("utf-8"))
    aesgcm = AESGCM(aes_key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, key_plaintext, None)
    envelope = base64.b64encode(salt + nonce + ciphertext).decode("ascii")
    return {
        "status": "ok",
        "encrypted_key": envelope,
        "warning": "Store this key securely. Loss of this key makes all encrypted secrets unrecoverable.",
    }


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
