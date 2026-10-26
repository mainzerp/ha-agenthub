"""Admin sub-router: voice profiles, enrollment and speaker-identification status."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiosqlite
import numpy as np
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.db.repositories.voice import (
    MAX_HA_USER_ID_LENGTH,
    MAX_PROFILE_NAME_LENGTH,
    VoiceProfileNameConflictError,
    VoiceProfileRepository,
    VoiceTurnRepository,
)
from app.voice.enrollment import (
    DEFAULT_ENROLL_SAMPLES,
    DEFAULT_ENROLL_TIMEOUT_S,
    MAX_ENROLL_SAMPLES,
    MAX_ENROLL_TIMEOUT_S,
    MIN_ENROLL_SAMPLES,
    MIN_ENROLL_TIMEOUT_S,
)
from app.voice.profiles import EnrollmentCheck, check_enrollment_sample, sample_diagnostics, scores_payload
from app.voice.recordings import audio_file_path, delete_audio_files, is_valid_turn_id, read_wav_pcm
from app.voice.service import (
    STATUS_IDENTIFIED,
    STATUS_NO_PROFILES,
    STATUS_UNKNOWN,
    VoiceConfig,
    get_speaker_service,
    is_speaker_id_enabled,
    load_voice_config,
)
from app.voice.speaker_engine import SpeakerEngineUnavailableError

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


class VoiceTurnAssign(BaseModel):
    profile_id: int
    # Store the sample even when the enrollment guard rejects it.
    force: bool = False


MAX_TURNS_PAGE = 200
_TURN_STATUSES = frozenset({STATUS_IDENTIFIED, STATUS_UNKNOWN, STATUS_NO_PROFILES})


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
    """Feature state, engine state, armed enrollment, recent results and the voice-recording state."""
    service = get_speaker_service()
    enabled = await is_speaker_id_enabled()
    if enabled:
        # Load the engine in the background so the page shows "loading" -> "ready".
        service.schedule_warm_up()
    config = await load_voice_config()
    try:
        recorded_turns: int | None = await VoiceTurnRepository.count()
    except Exception:
        logger.debug("Counting recorded voice turns failed", exc_info=True)
        recorded_turns = None
    return {
        "enabled": enabled,
        "engine": service.engine.status,
        "engine_error": service.engine.last_error,
        "model": service.engine.model_id,
        "vad": service.vad.status,
        "vad_state": getattr(service.vad, "state", None),
        "vad_error": service.vad.last_error,
        "enrollment": service.enrollment.current(),
        "recent": service.store.recent(),
        "keep_audio": config.keep_audio,
        "audio_retention_days": config.audio_retention_days,
        "recorded_turns": recorded_turns,
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
async def delete_voice_profile(profile_id: int, delete_recordings: bool = Query(False)) -> dict[str, Any]:
    """Delete a profile and its samples; ``delete_recordings`` also deletes the recorded turns
    identified as or assigned to it (rows and audio). Otherwise those turns stay, unlinked."""
    await _require_profile(profile_id)
    audio_paths = await VoiceTurnRepository.delete_for_profile(profile_id) if delete_recordings else []
    deleted = await VoiceProfileRepository.delete_profile(profile_id)
    await delete_audio_files(audio_paths)
    if not deleted:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    service = get_speaker_service()
    service.enrollment.disarm_profile(profile_id)
    service.invalidate_profiles()
    return {"ok": True, "deleted_recordings": len(audio_paths)}


@router.delete("/profiles/{profile_id}/samples")
async def reset_voice_samples(profile_id: int) -> dict[str, Any]:
    """Delete every enrollment sample of a profile (all models)."""
    await _require_profile(profile_id)
    deleted = await VoiceProfileRepository.delete_samples(profile_id)
    get_speaker_service().invalidate_profiles()
    return {"ok": True, "deleted": deleted}


@router.get("/profiles/{profile_id}/samples")
async def list_voice_samples(profile_id: int) -> dict[str, Any]:
    """A profile's samples with quality numbers (no embeddings are returned).

    For samples of the current model: ``loo_score`` (cosine to the centroid
    of the profile's other samples) and ``best_other_name`` /
    ``best_other_score`` (closest other profile). Samples of another model
    never join a centroid and carry no scores. ``pre_vad`` marks a sample
    stored without silence trimming (``speech_s`` NULL): it was embedded with
    its silence and should be re-enrolled.
    """
    await _require_profile(profile_id)
    service = get_speaker_service()
    model = service.engine.model_id
    rows = await VoiceProfileRepository.list_samples(profile_id)
    current = [row for row in rows if row.get("model") == model]
    centroids = await service.centroids.get(model)
    others = [centroid for centroid in centroids if centroid.profile_id != profile_id]
    diagnostics = await asyncio.to_thread(sample_diagnostics, current, others)
    by_id = {item["id"]: item for item in diagnostics}
    samples = []
    for row in rows:
        diag = by_id.get(int(row["id"]), {})
        samples.append(
            {
                "id": row["id"],
                "created_at": row.get("created_at"),
                "model": row.get("model"),
                "current_model": row.get("model") == model,
                "duration_s": row.get("duration_s"),
                "speech_s": row.get("speech_s"),
                "pre_vad": row.get("speech_s") is None,
                "loo_score": diag.get("loo_score"),
                "best_other_name": diag.get("best_other_name"),
                "best_other_score": diag.get("best_other_score"),
            }
        )
    return {"profile_id": profile_id, "model": model, "samples": samples}


@router.delete("/profiles/{profile_id}/samples/{sample_id}")
async def delete_voice_sample(profile_id: int, sample_id: int) -> dict[str, Any]:
    """Delete one enrollment sample of a profile."""
    await _require_profile(profile_id)
    if not await VoiceProfileRepository.delete_sample(profile_id, sample_id):
        raise HTTPException(status_code=404, detail="Voice sample not found")
    get_speaker_service().invalidate_profiles()
    return {"ok": True}


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


# -- recorded voice turns ------------------------------------------------


def _turn_error(status_code: int, error: str, detail: str, **extra: Any) -> JSONResponse:
    """Error body with a readable ``detail`` and a machine-readable ``error`` code."""
    return JSONResponse(status_code=status_code, content={"detail": detail, "error": error, **extra})


def _speech_seconds(turn: dict[str, Any]) -> float:
    value = turn.get("speech_s")
    if value is None:
        value = turn.get("duration_s")
    return float(value or 0.0)


def _turn_payload(row: dict[str, Any], config: VoiceConfig, model: str) -> dict[str, Any]:
    assigned = row.get("assigned_sample_id") is not None
    return {
        "id": row["id"],
        "created_at": row.get("created_at"),
        "conversation_id": row.get("conversation_id"),
        "trace_id": row.get("linked_trace_id"),
        # ``lookup``: linked by the conversation turn's speaker lookup;
        # ``time_window``: nearest trace of the same conversation (lookup timed out or never ran).
        "trace_match": ("lookup" if row.get("trace_id") else "time_window") if row.get("linked_trace_id") else None,
        "transcript": row.get("transcript"),
        "duration_s": row.get("duration_s"),
        "speech_s": row.get("speech_s"),
        "status": row.get("status"),
        "speaker_profile_id": row.get("speaker_profile_id"),
        "speaker_name": row.get("speaker_name"),
        "score": row.get("score"),
        "margin": row.get("margin"),
        "model": row.get("model"),
        "current_model": row.get("model") == model,
        "assigned": assigned,
        "assigned_profile_id": row.get("assigned_profile_id") if assigned else None,
        "assigned_name": row.get("assigned_name") if assigned else None,
        "assigned_sample_id": row.get("assigned_sample_id"),
        "assigned_at": row.get("assigned_at") if assigned else None,
        "assignable": _speech_seconds(row) >= config.min_enroll_duration_s,
    }


async def _require_turn(turn_id: str) -> dict[str, Any]:
    turn = await VoiceTurnRepository.get_turn(turn_id) if is_valid_turn_id(turn_id) else None
    if turn is None:
        raise HTTPException(status_code=404, detail="Voice turn not found")
    return turn


def _guard_payload(check: EnrollmentCheck, profile_id: int) -> dict[str, Any]:
    """Enrollment-guard outcome: ``reason`` (None = accepted) and the scores it decided on."""
    others = [item for item in check.ranked if item.profile.profile_id != profile_id]
    best_other = others[0] if others else None
    best_other_score = round(best_other.score, 4) if best_other is not None else None
    lead = (
        round(check.own_score - best_other_score, 4)
        if check.own_score is not None and best_other_score is not None
        else None
    )
    return {
        "reason": check.reason,
        "scores": {
            "own": check.own_score,
            "best_other": best_other_score,
            "best_other_name": best_other.profile.name if best_other is not None else None,
            "lead": lead,
            "all": scores_payload(check.ranked),
        },
    }


async def _drop_sample(profile_id: int, sample_id: int) -> None:
    """Delete a sample stored by a failed assignment (it may already be gone) and refresh the centroids."""
    try:
        await VoiceProfileRepository.delete_sample(profile_id, sample_id)
    except Exception:
        logger.warning("Removing sample %s of a failed voice-turn assignment failed", sample_id, exc_info=True)
    get_speaker_service().invalidate_profiles()


@router.get("/turns")
async def list_voice_turns(
    limit: int = Query(50, ge=1, le=MAX_TURNS_PAGE),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    assigned: bool | None = Query(None),
) -> dict[str, Any]:
    """Recorded voice turns, newest first (no embeddings or audio).

    ``transcript`` is the user input of the linked trace (None without one;
    ``trace_match`` says whether the speaker lookup linked it or the
    conversation's nearest trace was used);
    ``assignable`` means the turn has enough speech for an enrollment sample.
    """
    if status is not None and status not in _TURN_STATUSES:
        raise HTTPException(status_code=422, detail="status must be identified, unknown or no_profiles")
    config = await load_voice_config()
    model = get_speaker_service().engine.model_id
    rows, total = await VoiceTurnRepository.list_turns(limit=limit, offset=offset, status=status, assigned=assigned)
    return {
        "turns": [_turn_payload(row, config, model) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "keep_audio": config.keep_audio,
        "audio_retention_days": config.audio_retention_days,
        "min_enroll_duration_s": config.min_enroll_duration_s,
    }


@router.get("/turns/{turn_id}/audio")
async def get_voice_turn_audio(turn_id: str) -> FileResponse:
    """The recorded audio of a turn (16 kHz mono 16-bit WAV)."""
    turn = await _require_turn(turn_id)
    path = audio_file_path(turn.get("audio_path"))
    if path is None or not await asyncio.to_thread(path.is_file):
        raise HTTPException(status_code=404, detail="Voice recording not found")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


@router.post("/turns/{turn_id}/assign", response_model=None)
async def assign_voice_turn(turn_id: str, body: VoiceTurnAssign) -> dict[str, Any] | JSONResponse:
    """Store a recorded turn as an enrollment sample of a profile.

    Independent of an armed enrollment (whose state stays untouched). The
    enrollment guard runs against the target profile; a rejection is
    returned as 409 ``guard_rejected`` unless ``force`` overrides it. A turn
    recorded with another speaker model is re-embedded from its audio.
    """
    turn = await _require_turn(turn_id)
    profile = await _require_profile(body.profile_id)
    if turn.get("assigned_sample_id") is not None:
        return _turn_error(409, "already_assigned", "This voice turn is already assigned to a profile")
    config = await load_voice_config()
    if _speech_seconds(turn) < config.min_enroll_duration_s:
        return _turn_error(
            422,
            "too_short",
            f"Too little speech for an enrollment sample (minimum {config.min_enroll_duration_s:g} s)",
        )
    service = get_speaker_service()
    model = service.engine.model_id
    vector = np.frombuffer(turn["embedding"], dtype=np.float32).copy()
    recomputed = turn.get("model") != model
    if recomputed:
        if service.engine.status != "ready":
            service.schedule_warm_up()
            return _turn_error(
                503,
                "engine_unavailable",
                "The turn was recorded with another speaker model and the engine is not ready to re-embed it",
            )
        path = audio_file_path(turn.get("audio_path"))
        if path is None or not await asyncio.to_thread(path.is_file):
            return _turn_error(404, "audio_missing", "The recording of this voice turn is missing")
        try:
            pcm, sample_rate = await asyncio.to_thread(read_wav_pcm, path)
            embedded = await service.engine.embed(pcm, sample_rate, num_threads=config.num_threads)
        except FileNotFoundError:
            # Deleted (retention, delete) after the existence check.
            return _turn_error(404, "audio_missing", "The recording of this voice turn is missing")
        except SpeakerEngineUnavailableError:
            return _turn_error(503, "engine_unavailable", "The speaker engine is not available to re-embed the turn")
        except Exception:
            logger.warning("Re-embedding voice turn %s failed", turn_id, exc_info=True)
            return _turn_error(500, "embedding_failed", "Re-embedding the voice turn failed")
        vector = np.asarray(embedded, dtype=np.float32).reshape(-1)
    centroids = await service.centroids.get(model)
    check = check_enrollment_sample(
        vector,
        body.profile_id,
        centroids,
        threshold=config.threshold,
        margin=config.margin,
        min_similarity=config.enroll_min_similarity,
    )
    guard = _guard_payload(check, body.profile_id)
    if check.reason is not None and not body.force:
        return _turn_error(
            409,
            "guard_rejected",
            f"The enrollment guard rejected this turn for {profile['name']}: {check.reason}",
            reason=check.reason,
            scores=guard["scores"],
        )
    try:
        sample_id = await VoiceProfileRepository.add_sample(
            body.profile_id,
            vector.tobytes(),
            int(vector.size),
            model,
            turn.get("duration_s"),
            turn.get("speech_s"),
        )
    except aiosqlite.IntegrityError:
        # The profile was deleted meanwhile (foreign key).
        raise HTTPException(status_code=404, detail="Voice profile not found") from None
    try:
        linked = await VoiceTurnRepository.mark_assigned(turn_id, body.profile_id, sample_id)
    except aiosqlite.IntegrityError:
        # The profile was deleted after the sample was stored (which removed the sample too).
        await _drop_sample(body.profile_id, sample_id)
        raise HTTPException(status_code=404, detail="Voice profile not found") from None
    if not linked:
        # Assigned or deleted concurrently: never keep a duplicate or orphaned sample.
        await _drop_sample(body.profile_id, sample_id)
        if await VoiceTurnRepository.get_turn(turn_id) is None:
            raise HTTPException(status_code=404, detail="Voice turn not found")
        return _turn_error(409, "already_assigned", "This voice turn is already assigned to a profile")
    service.invalidate_profiles()
    forced = check.reason is not None
    logger.info(
        "Assigned voice turn %s to profile %s as sample %s%s",
        turn_id,
        body.profile_id,
        sample_id,
        f" (guard override: {check.reason})" if forced else "",
    )
    return {
        "ok": True,
        "turn_id": turn_id,
        "profile_id": body.profile_id,
        "sample_id": sample_id,
        "forced": forced,
        "recomputed": recomputed,
        "guard": guard,
    }


@router.delete("/turns/{turn_id}")
async def delete_voice_turn(turn_id: str) -> dict[str, Any]:
    """Delete a recorded turn and its audio (an enrollment sample made from it stays)."""
    audio_path = await VoiceTurnRepository.delete_turn(turn_id) if is_valid_turn_id(turn_id) else None
    if audio_path is None:
        raise HTTPException(status_code=404, detail="Voice turn not found")
    await delete_audio_files([audio_path])
    return {"ok": True}


@router.delete("/turns")
async def delete_all_voice_turns() -> dict[str, Any]:
    """Delete every recorded turn and its audio."""
    paths = await VoiceTurnRepository.delete_all()
    await delete_audio_files(paths)
    logger.info("Deleted all %d recorded voice turns", len(paths))
    return {"ok": True, "deleted": len(paths)}
