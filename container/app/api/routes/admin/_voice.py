"""Admin sub-router: voice profiles, enrollment and speaker-identification status."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.repositories.voice import (
    MAX_HA_USER_ID_LENGTH,
    MAX_PROFILE_NAME_LENGTH,
    VoiceProfileNameConflictError,
    VoiceProfileRepository,
)
from app.voice.enrollment import (
    DEFAULT_ENROLL_SAMPLES,
    DEFAULT_ENROLL_TIMEOUT_S,
    MAX_ENROLL_SAMPLES,
    MAX_ENROLL_TIMEOUT_S,
    MIN_ENROLL_SAMPLES,
    MIN_ENROLL_TIMEOUT_S,
)
from app.voice.service import get_speaker_service, is_speaker_id_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice")


class VoiceProfileCreate(BaseModel):
    name: str = Field(..., max_length=MAX_PROFILE_NAME_LENGTH * 2)
    ha_user_id: str | None = Field(None, max_length=MAX_HA_USER_ID_LENGTH)


class VoiceProfileUpdate(BaseModel):
    name: str | None = Field(None, max_length=MAX_PROFILE_NAME_LENGTH * 2)
    ha_user_id: str | None = Field(None, max_length=MAX_HA_USER_ID_LENGTH)


class VoiceEnrollRequest(BaseModel):
    # ``default=`` as a keyword so type checkers without the pydantic plugin
    # see the fields as optional (``VoiceEnrollRequest()`` is valid).
    samples: int = Field(default=DEFAULT_ENROLL_SAMPLES, ge=MIN_ENROLL_SAMPLES, le=MAX_ENROLL_SAMPLES)
    timeout_s: int = Field(default=DEFAULT_ENROLL_TIMEOUT_S, ge=MIN_ENROLL_TIMEOUT_S, le=MAX_ENROLL_TIMEOUT_S)


async def _require_profile(profile_id: int) -> dict[str, Any]:
    profile = await VoiceProfileRepository.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    return profile


async def _profile_with_count(profile: dict[str, Any]) -> dict[str, Any]:
    service = get_speaker_service()
    count = await VoiceProfileRepository.count_samples(int(profile["id"]), service.engine.model_id)
    return {**profile, "sample_count": count}


@router.get("/status")
async def voice_status() -> dict[str, Any]:
    """Feature state, engine state, armed enrollment and the recent results ring buffer."""
    service = get_speaker_service()
    enabled = await is_speaker_id_enabled()
    if enabled:
        # Load the engine in the background so the page shows "loading" -> "ready".
        service.schedule_warm_up()
    return {
        "enabled": enabled,
        "engine": service.engine.status,
        "engine_error": service.engine.last_error,
        "model": service.engine.model_id,
        "enrollment": service.enrollment.current(),
        "recent": service.store.recent(),
    }


@router.get("/profiles")
async def list_voice_profiles() -> list[dict[str, Any]]:
    """All voice profiles with the sample count for the current model."""
    return await VoiceProfileRepository.list_profiles(get_speaker_service().engine.model_id)


@router.post("/profiles", status_code=201)
async def create_voice_profile(body: VoiceProfileCreate) -> dict[str, Any]:
    try:
        profile = await VoiceProfileRepository.create_profile(body.name, body.ha_user_id)
    except VoiceProfileNameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    get_speaker_service().invalidate_profiles()
    return {**profile, "sample_count": 0}


@router.patch("/profiles/{profile_id}")
async def update_voice_profile(profile_id: int, body: VoiceProfileUpdate) -> dict[str, Any]:
    await _require_profile(profile_id)
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    kwargs: dict[str, Any] = {}
    if "name" in fields:
        kwargs["name"] = body.name
    if "ha_user_id" in fields:
        kwargs["ha_user_id"] = body.ha_user_id
    try:
        profile = await VoiceProfileRepository.update_profile(profile_id, **kwargs)
    except VoiceProfileNameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if profile is None:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    service = get_speaker_service()
    service.invalidate_profiles()
    enrollment = service.enrollment.current()
    if enrollment is not None and enrollment["profile_id"] == profile_id and "name" in kwargs:
        # Keep the armed enrollment label in sync with a rename.
        service.enrollment.rename(profile_id, profile["name"])
    return await _profile_with_count(profile)


@router.delete("/profiles/{profile_id}")
async def delete_voice_profile(profile_id: int) -> dict[str, Any]:
    deleted = await VoiceProfileRepository.delete_profile(profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    service = get_speaker_service()
    service.enrollment.disarm_profile(profile_id)
    service.invalidate_profiles()
    return {"ok": True}


@router.delete("/profiles/{profile_id}/samples")
async def reset_voice_samples(profile_id: int) -> dict[str, Any]:
    """Delete every enrollment sample of a profile (all models)."""
    await _require_profile(profile_id)
    deleted = await VoiceProfileRepository.delete_samples(profile_id)
    get_speaker_service().invalidate_profiles()
    return {"ok": True, "deleted": deleted}


@router.post("/profiles/{profile_id}/enroll")
async def arm_voice_enrollment(profile_id: int, body: VoiceEnrollRequest | None = None) -> dict[str, Any]:
    """Arm enrollment: the next voice turns become samples for this profile."""
    profile = await _require_profile(profile_id)
    if not await is_speaker_id_enabled():
        # Voice turns are not processed while disabled, so an armed
        # enrollment could never collect a sample.
        raise HTTPException(status_code=409, detail="Speaker identification is disabled")
    request = body or VoiceEnrollRequest()
    service = get_speaker_service()
    enrollment = service.enrollment.arm(profile_id, profile["name"], request.samples, request.timeout_s)
    service.schedule_warm_up()
    logger.info("Voice enrollment armed for profile %s (%d samples)", profile_id, request.samples)
    return {"ok": True, "enrollment": enrollment}


@router.delete("/enroll")
async def cancel_voice_enrollment() -> dict[str, Any]:
    cancelled = get_speaker_service().enrollment.cancel()
    return {"ok": True, "cancelled": cancelled}
