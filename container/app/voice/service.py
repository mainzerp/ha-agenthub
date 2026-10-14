"""Speaker-identification service: voice-turn processing and conversation attach.

A recognized speaker is CONTEXT ONLY. It never sets or overrides
``user_id`` and never influences entity visibility or authorization.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from app.analytics.tracer import _optional_span
from app.db.repository import SettingsRepository, VoiceProfileRepository
from app.defaults import VOICE_DEFAULTS, VOICE_SETTING_RANGES
from app.models.agent import SpeakerContext
from app.util.tasks import spawn
from app.voice.audio import pcm_duration_s
from app.voice.enrollment import EnrollmentState
from app.voice.profiles import (
    REJECTED_TOO_SHORT,
    ProfileCentroidCache,
    ProfileScore,
    check_enrollment_sample,
    find_session_outlier,
    match_embedding,
    scores_payload,
)
from app.voice.speaker_engine import SpeakerEngine, SpeakerEngineUnavailableError
from app.voice.turn_store import SpeakerLookup, SpeakerTurnStore
from app.voice.vad import SpeechTrim, VoiceActivityTrimmer

logger = logging.getLogger(__name__)

# Result statuses (see the /ws/voice protocol).
STATUS_IDENTIFIED = "identified"
STATUS_UNKNOWN = "unknown"
STATUS_TOO_SHORT = "too_short"
STATUS_NO_PROFILES = "no_profiles"
STATUS_DISABLED = "disabled"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNSUPPORTED_AUDIO = "unsupported_audio"
STATUS_ENROLLED_SAMPLE = "enrolled_sample"
STATUS_REJECTED_SAMPLE = "rejected_sample"
STATUS_ERROR = "error"

# Hard upper bound for a background speaker lookup. The turn itself never
# blocks longer than ``voice.speaker_id.wait_ms`` (see PendingSpeakerLookup).
LOOKUP_HARD_CAP_S = 5.0
_DEFAULT_WAIT_MS = int(VOICE_DEFAULTS["voice.speaker_id.wait_ms"])  # type: ignore[call-overload]
_DEFAULT_ENROLL_MIN_SIMILARITY = float(VOICE_DEFAULTS["voice.speaker_id.enroll_min_similarity"])  # type: ignore[arg-type]


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool
    threshold: float
    margin: float
    min_duration_s: float
    min_enroll_duration_s: float
    wait_ms: int
    num_threads: int
    enroll_min_similarity: float = _DEFAULT_ENROLL_MIN_SIMILARITY


@dataclass(frozen=True)
class PendingEnrollment:
    """An enrollment embedding computed at ``audio_end``, stored only once the turn is confirmed."""

    profile_id: int
    name: str | None
    vector: np.ndarray
    model_id: str
    duration_s: float
    compute_ms: float | None = None
    latency_ms: float | None = None
    # Speech seconds after voice-activity trimming (None when the VAD did not run).
    speech_s: float | None = None
    vad: bool | None = None
    # Diagnostic result fields (speech, VAD flag, scores) carried to the commit.
    diagnostics: dict[str, Any] | None = None
    # ``voice.speaker_id.enroll_min_similarity`` of the analysis (session outlier review at the commit).
    enroll_min_similarity: float = _DEFAULT_ENROLL_MIN_SIMILARITY


@dataclass(frozen=True)
class TurnAnalysis:
    """Outcome of :meth:`SpeakerIdService.analyze_turn`.

    ``pending`` is set for an enrollment turn whose sample still awaits
    confirmation (:meth:`SpeakerIdService.commit_enrollment`); ``rejection``
    for an enrollment turn whose sample was rejected -- it is counted and
    shown only once the turn is confirmed
    (:meth:`SpeakerIdService.commit_rejection`), an aborted turn records
    nothing.
    """

    result: dict[str, Any]
    pending: PendingEnrollment | None = None
    rejection: dict[str, Any] | None = None


def _clamp(key: str, value: float) -> float:
    low, high = VOICE_SETTING_RANGES[key]
    return min(max(value, low), high)


async def _read_float(key: str) -> float:
    default = float(VOICE_DEFAULTS[key])  # type: ignore[arg-type]
    try:
        raw = await SettingsRepository.get_value(key, str(default))
        value = float(raw) if raw not in (None, "") else default
    except Exception:
        value = default
    if value != value:  # NaN
        value = default
    return _clamp(key, value)


async def _read_int(key: str) -> int:
    default = int(VOICE_DEFAULTS[key])  # type: ignore[call-overload]
    try:
        raw = await SettingsRepository.get_value(key, str(default))
        value = int(float(raw)) if raw not in (None, "") else default
    except Exception:
        value = default
    return int(_clamp(key, value))


async def is_speaker_id_enabled() -> bool:
    default = bool(VOICE_DEFAULTS["voice.speaker_id.enabled"])
    try:
        raw = await SettingsRepository.get_value("voice.speaker_id.enabled", "true" if default else "false")
    except Exception:
        logger.debug("voice.speaker_id.enabled read failed; using default", exc_info=True)
        return default
    if raw is None:
        return default
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


async def load_voice_config() -> VoiceConfig:
    """Read the ``voice.speaker_id.*`` settings (clamped to their valid ranges)."""
    return VoiceConfig(
        enabled=await is_speaker_id_enabled(),
        threshold=await _read_float("voice.speaker_id.threshold"),
        margin=await _read_float("voice.speaker_id.margin"),
        min_duration_s=await _read_float("voice.speaker_id.min_duration_s"),
        min_enroll_duration_s=await _read_float("voice.speaker_id.min_enroll_duration_s"),
        wait_ms=await _read_int("voice.speaker_id.wait_ms"),
        num_threads=await _read_int("voice.speaker_id.num_threads"),
        enroll_min_similarity=await _read_float("voice.speaker_id.enroll_min_similarity"),
    )


def make_result(
    status: str,
    *,
    duration_s: float = 0.0,
    profile_id: int | None = None,
    name: str | None = None,
    ha_user_id: str | None = None,
    score: float | None = None,
    margin: float | None = None,
    reason: str | None = None,
    compute_ms: float | None = None,
    latency_ms: float | None = None,
    speech_s: float | None = None,
    vad: bool | None = None,
    scores: list[dict[str, Any]] | None = None,
    second_name: str | None = None,
    second_score: float | None = None,
) -> dict[str, Any]:
    """Build the canonical speaker-identification result dict.

    ``compute_ms`` is the wall time of voice-activity trimming, embedding and
    profile matching; ``latency_ms`` runs from the analysis trigger
    (``audio_end``) until the result is available. Both are None when
    nothing was computed. ``duration_s`` is the raw turn length,
    ``speech_s`` the speech kept by the VAD (``vad`` False: the whole turn
    was used; None: not analysed). ``scores`` lists every profile's score
    (best first) and ``second_name``/``second_score`` the runner-up. ``name``
    is set only for an identified speaker or the enrollment target, never
    for an ``unknown`` turn.
    """
    return {
        "status": status,
        "profile_id": profile_id,
        "name": name,
        "ha_user_id": ha_user_id,
        "score": score,
        "margin": margin,
        "duration_s": round(float(duration_s), 3),
        "speech_s": speech_s,
        "vad": vad,
        "scores": scores,
        "second_name": second_name,
        "second_score": second_score,
        "reason": reason,
        "compute_ms": compute_ms,
        "latency_ms": latency_ms,
    }


def _trim_fields(trim: SpeechTrim) -> dict[str, Any]:
    return {"speech_s": trim.speech_s, "vad": trim.vad}


def _score_fields(ranked: tuple[ProfileScore, ...], *, identified: bool = False) -> dict[str, Any]:
    """``scores`` for every analysed turn; the runner-up only next to an identified speaker."""
    second = ranked[1] if identified and len(ranked) > 1 else None
    return {
        "scores": scores_payload(ranked),
        "second_name": second.profile.name if second is not None else None,
        "second_score": round(second.score, 4) if second is not None else None,
    }


def elapsed_ms(started_at: float) -> float:
    """Milliseconds since the ``time.perf_counter()`` value ``started_at`` (0.1 ms resolution)."""
    return round(max(time.perf_counter() - started_at, 0.0) * 1000.0, 1)


class SpeakerIdService:
    """Processes finished voice turns and serves results to conversations."""

    def __init__(
        self,
        *,
        engine: SpeakerEngine | Any | None = None,
        store: SpeakerTurnStore | None = None,
        enrollment: EnrollmentState | None = None,
        centroids: ProfileCentroidCache | None = None,
        vad: VoiceActivityTrimmer | Any | None = None,
    ) -> None:
        self.engine = engine if engine is not None else SpeakerEngine()
        self.store = store if store is not None else SpeakerTurnStore()
        self.enrollment = enrollment if enrollment is not None else EnrollmentState()
        self.centroids = centroids if centroids is not None else ProfileCentroidCache()
        self.vad = vad if vad is not None else VoiceActivityTrimmer()
        # Serialises enrollment commits so the outlier review of a session never interleaves.
        self._enroll_lock = asyncio.Lock()

    def invalidate_profiles(self) -> None:
        self.centroids.invalidate()

    async def warm_up(self, num_threads: int | None = None) -> bool:
        """Load the engine and the VAD (downloading models when missing). Never raises.

        Returns the engine readiness; a missing VAD only means whole turns
        are embedded.
        """
        threads = num_threads if num_threads is not None else await _read_int("voice.speaker_id.num_threads")
        try:
            ready = bool(await self.engine.ensure_ready(threads))
        except Exception:
            logger.warning("Speaker-embedding engine warm-up failed", exc_info=True)
            ready = False
        try:
            await self.vad.ensure_ready()
        except Exception:
            logger.warning("Voice-activity detector warm-up failed", exc_info=True)
        return ready

    def schedule_warm_up(self) -> None:
        """Start background warm-ups for the VAD and, unless ready or loading, the engine."""
        try:
            self.vad.schedule_warm_up()
        except Exception:
            logger.debug("Voice-activity detector warm-up could not be scheduled", exc_info=True)
        if self.engine.status in ("ready", "loading"):
            return
        spawn(self.warm_up(), name="speaker_engine_warm_up")

    def schedule_reload(self, num_threads: int) -> None:
        """Apply a changed thread count in the background; voice turns never wait for it."""
        if self.engine.status != "ready":
            # Not loaded yet: the warm-up reads the current setting.
            self.schedule_warm_up()
            return
        reload = getattr(self.engine, "schedule_reload", None)
        if callable(reload):
            reload(num_threads)

    async def process_turn(self, pcm: bytes, sample_rate: int, config: VoiceConfig) -> dict[str, Any]:
        """Identify the speaker of one confirmed voice turn (or store an enrollment sample).

        Convenience for callers that already know the turn is confirmed; the
        ``/ws/voice`` session uses :meth:`analyze_turn` and
        :meth:`commit_enrollment` so an enrollment sample is stored only after
        the client confirmed the turn with ``end``.
        """
        analysis = await self.analyze_turn(pcm, sample_rate, config, received_at=time.perf_counter())
        if analysis.pending is not None:
            return await self.commit_enrollment(analysis.pending)
        if analysis.rejection is not None:
            return self.commit_rejection(analysis.rejection)
        return analysis.result

    async def analyze_turn(
        self,
        pcm: bytes,
        sample_rate: int,
        config: VoiceConfig,
        *,
        received_at: float | None = None,
    ) -> TurnAnalysis:
        """Identify the speaker of one finished voice turn, or prepare an enrollment sample.

        Never persists anything: an enrollment embedding is returned as
        ``pending`` and stored only by :meth:`commit_enrollment`.
        ``received_at`` is the ``time.perf_counter()`` value of the analysis
        trigger (``audio_end``); it sets ``latency_ms`` of a computed result.
        """
        duration = pcm_duration_s(pcm, sample_rate)
        armed = self.enrollment.current()
        if armed is not None:
            result, pending = await self._prepare_enrollment(pcm, sample_rate, duration, armed, config)
            _set_latency(result, received_at)
            if pending is not None:
                pending = replace(pending, latency_ms=result["latency_ms"])
                return TurnAnalysis(result=result, pending=pending)
            reason = result.get("reason")
            if isinstance(reason, str) and reason.startswith("rejected_"):
                # Counted and shown only when the turn is confirmed (``commit_rejection``).
                return TurnAnalysis(result=result, rejection=dict(result))
            self.store.record_recent(result)
            return TurnAnalysis(result=result)
        result = await self._identify(pcm, sample_rate, duration, config)
        _set_latency(result, received_at)
        self.store.record_recent(result)
        return TurnAnalysis(result=result)

    def _engine_ready_or_warm_up(self) -> bool:
        """True when the engine can embed now; otherwise start a background warm-up.

        The voice path never waits for a model download or load: a turn that
        arrives before the engine is ready reports ``unavailable``.
        """
        if self.engine.status == "ready":
            return True
        self.schedule_warm_up()
        return False

    async def _identify(self, pcm: bytes, sample_rate: int, duration: float, config: VoiceConfig) -> dict[str, Any]:
        if duration < config.min_duration_s:
            # Speech can never exceed the raw length: skip VAD and embedding.
            return make_result(STATUS_TOO_SHORT, duration_s=duration)
        try:
            centroids = await self.centroids.get(self.engine.model_id)
        except Exception:
            logger.warning("Loading voice profiles failed", exc_info=True)
            return make_result(STATUS_ERROR, duration_s=duration, reason="profiles_unavailable")
        if not centroids:
            return make_result(STATUS_NO_PROFILES, duration_s=duration)
        if not self._engine_ready_or_warm_up():
            return make_result(STATUS_UNAVAILABLE, duration_s=duration, reason="engine_not_ready")
        started_at = time.perf_counter()
        trim = await self.vad.trim(pcm, sample_rate)
        trimmed = _trim_fields(trim)
        if trim.speech_s <= 0 or not trim.pcm or trim.speech_s < config.min_duration_s:
            # Never embed an empty buffer, even with ``min_duration_s`` 0.
            reason = "no_speech" if trim.speech_s <= 0 or not trim.pcm else None
            return make_result(
                STATUS_TOO_SHORT, duration_s=duration, reason=reason, compute_ms=elapsed_ms(started_at), **trimmed
            )
        try:
            embedding = await self.engine.embed(trim.pcm, sample_rate, num_threads=config.num_threads)
        except SpeakerEngineUnavailableError:
            return make_result(STATUS_UNAVAILABLE, duration_s=duration, **trimmed)
        except Exception:
            logger.warning("Speaker embedding failed", exc_info=True)
            return make_result(STATUS_ERROR, duration_s=duration, reason="embedding_failed", **trimmed)
        match = match_embedding(embedding, centroids, threshold=config.threshold, margin=config.margin)
        compute_ms = elapsed_ms(started_at)
        identified = match.identified and match.best is not None
        scored = _score_fields(match.ranked, identified=identified)
        if identified and match.best is not None:
            return make_result(
                STATUS_IDENTIFIED,
                duration_s=duration,
                profile_id=match.best.profile_id,
                name=match.best.name,
                ha_user_id=match.best.ha_user_id,
                score=match.score,
                margin=match.margin,
                compute_ms=compute_ms,
                **trimmed,
                **scored,
            )
        # Unknown: no name. The nearest profile is only listed in ``scores``.
        return make_result(
            STATUS_UNKNOWN,
            duration_s=duration,
            score=match.score,
            margin=match.margin,
            compute_ms=compute_ms,
            **trimmed,
            **scored,
        )

    async def _prepare_enrollment(
        self,
        pcm: bytes,
        sample_rate: int,
        duration: float,
        armed: dict[str, Any],
        config: VoiceConfig,
    ) -> tuple[dict[str, Any], PendingEnrollment | None]:
        """Embed an enrollment turn and run the sample guard (nothing is stored here).

        A sample is rejected when it has too little speech
        (``rejected_too_short``) or does not fit the profile (see
        :func:`app.voice.profiles.check_enrollment_sample`). Rejected
        samples never count toward the requested sample count.
        """
        profile_id = int(armed["profile_id"])
        name = armed.get("name")
        target: dict[str, Any] = {"profile_id": profile_id, "name": name}
        if duration < config.min_enroll_duration_s:
            return make_result(STATUS_TOO_SHORT, duration_s=duration, reason=REJECTED_TOO_SHORT, **target), None
        if not self._engine_ready_or_warm_up():
            result = make_result(STATUS_UNAVAILABLE, duration_s=duration, reason="engine_not_ready", **target)
            return result, None
        if getattr(self.vad, "state", "ready") == "pending":
            # Never store a whole-turn sample while silence trimming is about to
            # become available; a permanently unavailable VAD (``failed``) still
            # enrolls, the sample is stored with ``speech_s`` NULL (pre-VAD).
            with contextlib.suppress(Exception):
                self.vad.schedule_warm_up()
            result = make_result(STATUS_UNAVAILABLE, duration_s=duration, reason="vad_not_ready", **target)
            return result, None
        started_at = time.perf_counter()
        trim = await self.vad.trim(pcm, sample_rate)
        trimmed = _trim_fields(trim)
        if trim.speech_s <= 0 or not trim.pcm or trim.speech_s < config.min_enroll_duration_s:
            result = make_result(
                STATUS_TOO_SHORT,
                duration_s=duration,
                reason=REJECTED_TOO_SHORT,
                compute_ms=elapsed_ms(started_at),
                **target,
                **trimmed,
            )
            return result, None
        try:
            embedding = await self.engine.embed(trim.pcm, sample_rate, num_threads=config.num_threads)
        except SpeakerEngineUnavailableError:
            return make_result(STATUS_UNAVAILABLE, duration_s=duration, **target, **trimmed), None
        except Exception:
            logger.warning("Speaker embedding failed during enrollment", exc_info=True)
            result = make_result(STATUS_ERROR, duration_s=duration, reason="embedding_failed", **target, **trimmed)
            return result, None
        try:
            centroids = await self.centroids.get(self.engine.model_id)
        except Exception:
            # Without the profiles the guard cannot run: never store an unchecked sample.
            logger.warning("Loading voice profiles for the enrollment guard failed", exc_info=True)
            result = make_result(STATUS_ERROR, duration_s=duration, reason="profiles_unavailable", **target, **trimmed)
            return result, None
        check = check_enrollment_sample(
            embedding,
            profile_id,
            centroids,
            threshold=config.threshold,
            margin=config.margin,
            min_similarity=config.enroll_min_similarity,
        )
        compute_ms = elapsed_ms(started_at)
        diagnostics: dict[str, Any] = {**trimmed, **_score_fields(check.ranked), "score": check.own_score}
        if check.reason is not None:
            logger.info("Rejected voice enrollment sample for profile %s: %s", profile_id, check.reason)
            result = make_result(
                STATUS_REJECTED_SAMPLE,
                duration_s=duration,
                reason=check.reason,
                compute_ms=compute_ms,
                **target,
                **diagnostics,
            )
            return result, None
        pending = PendingEnrollment(
            profile_id=profile_id,
            name=name,
            vector=embedding.astype("float32", copy=False).reshape(-1),
            model_id=self.engine.model_id,
            duration_s=round(duration, 3),
            compute_ms=compute_ms,
            speech_s=trim.speech_s if trim.vad else None,
            vad=trim.vad,
            diagnostics=diagnostics,
            enroll_min_similarity=config.enroll_min_similarity,
        )
        # Provisional result: the sample is stored only by ``commit_enrollment``.
        result = make_result(
            STATUS_ENROLLED_SAMPLE,
            duration_s=duration,
            compute_ms=compute_ms,
            **target,
            **diagnostics,
        )
        return result, pending

    def commit_rejection(self, rejection: dict[str, Any]) -> dict[str, Any]:
        """Record a rejected enrollment sample of a confirmed turn (recent results and enrollment counters)."""
        profile_id = rejection.get("profile_id")
        reason = rejection.get("reason")
        if profile_id is not None and isinstance(reason, str):
            self.enrollment.record_rejection(int(profile_id), reason)
        self.store.record_recent(rejection)
        return rejection

    async def commit_enrollment(self, pending: PendingEnrollment) -> dict[str, Any]:
        """Store a confirmed enrollment sample and count it against the armed enrollment.

        The sample that reaches the target count triggers the session outlier
        review (:meth:`_review_session`): the worst sample of this session may
        be replaced, keeping the session armed for one more sample.
        """
        async with self._enroll_lock:
            return await self._commit_enrollment(pending)

    async def _commit_enrollment(self, pending: PendingEnrollment) -> dict[str, Any]:
        profile_id = pending.profile_id
        name = pending.name
        duration = pending.duration_s
        # Timing covers the analysis at ``audio_end``, not this commit.
        timing = {"compute_ms": pending.compute_ms, "latency_ms": pending.latency_ms}
        diagnostics = dict(pending.diagnostics or {})
        armed = self.enrollment.current()
        if armed is None or int(armed["profile_id"]) != profile_id:
            # Cancelled, expired or re-armed for another profile meanwhile.
            result = make_result(
                STATUS_ERROR,
                duration_s=duration,
                profile_id=profile_id,
                name=name,
                reason="enrollment_not_armed",
                **timing,
            )
            self.store.record_recent(result)
            return result
        vector = pending.vector
        try:
            sample_id = await VoiceProfileRepository.add_sample(
                profile_id,
                vector.tobytes(),
                int(vector.size),
                pending.model_id,
                duration,
                pending.speech_s,
            )
        except Exception:
            # The armed profile may have been deleted meanwhile.
            logger.warning("Storing voice enrollment sample failed for profile %s", profile_id, exc_info=True)
            self.enrollment.disarm_profile(profile_id)
            result = make_result(
                STATUS_ERROR,
                duration_s=duration,
                profile_id=profile_id,
                name=name,
                reason="sample_not_stored",
                **timing,
            )
            self.store.record_recent(result)
            return result
        self.centroids.invalidate()
        remaining = self.enrollment.record_sample(profile_id, sample_id, finish=False)
        logger.info("Stored voice enrollment sample for profile %s (%d remaining)", profile_id, remaining)
        if remaining == 0:
            await self._review_session(profile_id, pending.model_id, pending.enroll_min_similarity)
        result = make_result(
            STATUS_ENROLLED_SAMPLE, duration_s=duration, profile_id=profile_id, name=name, **timing, **diagnostics
        )
        self.store.record_recent(result)
        return result

    async def _review_session(self, profile_id: int, model_id: str, min_similarity: float) -> None:
        """Replace the worst sample of a session that reached its target, or complete the session.

        Candidates are only the samples stored in this session; the profile
        needs at least ``MIN_SAMPLES_FOR_OUTLIER_REVIEW`` current-model
        samples. A session sample whose leave-one-out score is below
        ``min_similarity`` is deleted and the session stays armed (original
        expiry) for one more sample, at most ``MAX_OUTLIER_REPLACEMENTS``
        times. Never raises: a failed review completes the session.
        """
        outlier: tuple[int, float] | None = None
        session_ids = self.enrollment.session_samples(profile_id)
        if session_ids and self.enrollment.replacements_left(profile_id) > 0:
            try:
                rows = await VoiceProfileRepository.list_samples(profile_id)
                current = [row for row in rows if row.get("model") == model_id]
                outlier = await asyncio.to_thread(find_session_outlier, current, session_ids, min_similarity)
                if outlier is not None and not await VoiceProfileRepository.delete_sample(profile_id, outlier[0]):
                    outlier = None
            except Exception:
                logger.warning("Voice enrollment outlier review failed for profile %s", profile_id, exc_info=True)
                outlier = None
        if outlier is not None:
            sample_id, score = outlier
            self.centroids.invalidate()
            if self.enrollment.replace_sample(profile_id, sample_id, score):
                logger.info(
                    "Replaced voice enrollment outlier sample %s for profile %s (leave-one-out %.4f < %.2f)",
                    sample_id,
                    profile_id,
                    score,
                    min_similarity,
                )
                return
            # Expired or cancelled during the review: the outlier stays deleted.
            logger.info("Deleted voice enrollment outlier sample %s for profile %s", sample_id, profile_id)
        finished = self.enrollment.finish(profile_id)
        if finished is not None:
            logger.info(
                "Voice enrollment complete for profile %s: %d samples stored, %d outliers replaced",
                profile_id,
                finished["collected"],
                finished["replaced"],
            )


def _set_latency(result: dict[str, Any], received_at: float | None) -> None:
    """Set ``latency_ms`` of a computed result (one with ``compute_ms``)."""
    if received_at is not None and result.get("compute_ms") is not None:
        result["latency_ms"] = elapsed_ms(received_at)


_service: SpeakerIdService | None = None


def get_speaker_service() -> SpeakerIdService:
    """Return the process-wide speaker-identification service (created lazily)."""
    global _service
    if _service is None:
        _service = SpeakerIdService()
    return _service


def set_speaker_service(service: SpeakerIdService | None) -> None:
    """Replace the process-wide service (tests)."""
    global _service
    _service = service


async def _on_settings_write(key: str) -> None:
    """Warm up or reload the engine in the background after a voice settings write.

    Runs for every settings write path (admin API, dashboard, restore), so the
    first voice turn after enabling does not hit a cold engine and a changed
    ``num_threads`` is applied before the next turn. Never waits for the model
    load.
    """
    if key == "voice.speaker_id.enabled":
        if await is_speaker_id_enabled():
            get_speaker_service().schedule_warm_up()
    elif key == "voice.speaker_id.num_threads":
        service = _service
        if service is not None and await is_speaker_id_enabled():
            service.schedule_reload(await _read_int("voice.speaker_id.num_threads"))


SettingsRepository.register_write_listener(_on_settings_write)


async def enrollment_active() -> bool:
    """True while a voice-profile enrollment is armed and speaker identification is enabled.

    Reads the in-memory enrollment state of an existing service only (never
    creates one); the enabled setting (TTL-cached) is read only while an
    enrollment is armed. Never raises: any failure means "not active".
    """
    service = _service
    if service is None:
        return False
    try:
        if service.enrollment.current() is None:
            return False
        return await is_speaker_id_enabled()
    except Exception:
        logger.debug("Enrollment state check failed; assuming not armed", exc_info=True)
        return False


def has_pending_speaker(conversation_id: str | None) -> bool:
    """True when a voice turn registered a speaker entry for ``conversation_id``."""
    service = _service
    return service is not None and service.store.has(conversation_id)


def discard_pending_speaker(conversation_id: str | None) -> None:
    """Drop an unconsumed entry (e.g. on an action-cache hit) so it never leaks into a later turn."""
    service = _service
    if service is not None and conversation_id:
        service.store.discard(conversation_id)


class PendingSpeakerLookup:
    """Speaker lookup for one conversation turn, started before classification.

    The background task waits for the voice-turn result without the
    ``wait_ms`` cap (bounded by :data:`LOOKUP_HARD_CAP_S` and the store entry
    lifetime). :meth:`join` at the dispatch point blocks at most the rest of
    the ``wait_ms`` budget counted from the lookup start, so a result that
    arrives any time before dispatch is attached and a turn never waits
    longer than ``wait_ms``.
    """

    def __init__(
        self,
        service: SpeakerIdService,
        task: Any,
        conversation_id: str,
        span_collector: Any,
        *,
        hard_cap_s: float = LOOKUP_HARD_CAP_S,
    ) -> None:
        self._service = service
        self._task = task
        self._conversation_id = conversation_id
        self._span_collector = span_collector
        self._hard_cap_s = hard_cap_s
        self._started_at = time.perf_counter()
        self._config: VoiceConfig | None = None
        self._lookup_ms: float | None = None
        self._lookup: asyncio.Task[SpeakerLookup | None] = asyncio.create_task(self._run(), name="speaker_lookup")

    async def _run(self) -> SpeakerLookup | None:
        config = await load_voice_config()
        self._config = config
        if not config.enabled:
            self._service.store.discard(self._conversation_id)
            return None
        lookup = await self._service.store.pop(self._conversation_id, self._hard_cap_s)
        if lookup.result is not None:
            self._lookup_ms = elapsed_ms(self._started_at)
        return lookup

    def _budget_s(self) -> float:
        """Remaining ``wait_ms`` budget, counted from the lookup start."""
        # Settings not read yet (practically never at dispatch): use the default.
        wait_ms = self._config.wait_ms if self._config is not None else _DEFAULT_WAIT_MS
        return max(0.0, wait_ms / 1000.0 - (time.perf_counter() - self._started_at))

    async def cancel(self) -> None:
        """Stop the lookup without attaching or tracing (early prelude exit).

        Cancelling ``SpeakerTurnStore.pop`` removes the entry, so a late
        result never attaches to a later turn.
        """
        if self._lookup.done():
            return
        self._lookup.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._lookup

    async def wait_ha_user_id(self) -> str | None:
        """Peek at the result for personalization without attaching or tracing it.

        Waits at most the remaining ``wait_ms`` budget (the bound :meth:`join`
        uses anyway), never cancels the lookup and returns the linked Home
        Assistant user id of an ``identified`` result, else None. Used to
        start the session-memory search under the voice identity; :meth:`join`
        still owns attaching ``task.context.speaker``. Never raises
        (cancellation excepted).
        """
        try:
            if not self._lookup.done():
                budget = self._budget_s()
                if budget > 0:
                    await asyncio.wait({self._lookup}, timeout=budget)
            if not self._lookup.done() or self._lookup.cancelled():
                return None
            lookup = self._lookup.result()
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        result = (lookup.result if lookup is not None else None) or {}
        if result.get("status") != STATUS_IDENTIFIED:
            return None
        ha_user_id = result.get("ha_user_id")
        return str(ha_user_id) if ha_user_id else None

    async def join(self) -> None:
        """Wait for the result (at most the remaining budget) and attach it to ``task.context``.

        Only an ``identified`` result is attached; every consumed entry is
        traced as a ``speaker_id`` span whose ``waited_ms`` is the time the
        turn actually blocked here. ``user_id`` is never touched. Never
        raises (cancellation excepted).
        """
        joined_at = time.perf_counter()
        try:
            if not self._lookup.done():
                budget = self._budget_s()
                if budget > 0:
                    await asyncio.wait({self._lookup}, timeout=budget)
        except BaseException:
            await self.cancel()
            raise
        waited_ms = round((time.perf_counter() - joined_at) * 1000, 2)
        timed_out = not self._lookup.done()
        if timed_out:
            await self.cancel()
            lookup: SpeakerLookup | None = None
        else:
            try:
                lookup = self._lookup.result()
            except Exception:
                logger.warning("Speaker lookup failed for conversation %s", self._conversation_id, exc_info=True)
                return
            if lookup is None:  # disabled: entry discarded, nothing traced
                return
        try:
            await self._record(lookup, timed_out=timed_out, waited_ms=waited_ms)
        except Exception:
            logger.warning("Speaker attach failed for conversation %s", self._conversation_id, exc_info=True)

    async def _record(self, lookup: SpeakerLookup | None, *, timed_out: bool, waited_ms: float) -> None:
        result = (lookup.result if lookup is not None else None) or {}
        if lookup is not None and lookup.result is not None:
            status = str(result.get("status") or STATUS_ERROR)
        elif timed_out or (lookup is not None and lookup.timed_out):
            status = "timeout"
        else:
            status = "discarded"
        metadata: dict[str, Any] = {
            "status": status,
            "profile_id": result.get("profile_id"),
            "score": result.get("score"),
            "margin": result.get("margin"),
            "duration_s": result.get("duration_s"),
            "speech_s": result.get("speech_s"),
            "vad": result.get("vad"),
            "scores": result.get("scores"),
            "second_name": result.get("second_name"),
            "second_score": result.get("second_score"),
            "waited_ms": waited_ms,
            "lookup_ms": self._lookup_ms,
            "compute_ms": result.get("compute_ms"),
            "latency_ms": result.get("latency_ms"),
        }
        if status == STATUS_IDENTIFIED:
            metadata["speaker_name"] = result.get("name")
            # The profile's linked HA user (kept verbatim by the trace
            # sanitizer) so a voice-to-user mapping is checkable per trace.
            if result.get("ha_user_id"):
                metadata["speaker_ha_user_id"] = str(result.get("ha_user_id"))
            self._task.context.speaker = SpeakerContext(
                status=status,
                profile_id=result.get("profile_id"),
                name=result.get("name"),
                ha_user_id=result.get("ha_user_id"),
                score=result.get("score"),
            )
        async with _optional_span(self._span_collector, "speaker_id", agent_id="orchestrator") as span:
            span["metadata"].update(metadata)


def start_speaker_lookup(task: Any, conversation_id: str, span_collector: Any) -> PendingSpeakerLookup | None:
    """Start the speaker lookup for ``conversation_id`` when a voice turn registered an entry."""
    service = _service
    if service is None or getattr(task, "context", None) is None or not service.store.has(conversation_id):
        return None
    return PendingSpeakerLookup(service, task, conversation_id, span_collector)


async def attach_speaker_to_task(task: Any, conversation_id: str, span_collector: Any) -> None:
    """Start a speaker lookup and join it right away (waits at most ``voice.speaker_id.wait_ms``)."""
    lookup = start_speaker_lookup(task, conversation_id, span_collector)
    if lookup is not None:
        await lookup.join()
