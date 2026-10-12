"""Speaker-identification service: voice-turn processing and conversation attach.

A recognized speaker is CONTEXT ONLY. It never sets or overrides
``user_id`` and never influences entity visibility or authorization.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.analytics.tracer import _optional_span
from app.db.repository import SettingsRepository, VoiceProfileRepository
from app.defaults import VOICE_DEFAULTS, VOICE_SETTING_RANGES
from app.models.agent import SpeakerContext
from app.util.tasks import spawn
from app.voice.audio import pcm_duration_s
from app.voice.enrollment import EnrollmentState
from app.voice.profiles import ProfileCentroidCache, match_embedding
from app.voice.speaker_engine import SpeakerEngine, SpeakerEngineUnavailableError
from app.voice.turn_store import SpeakerTurnStore

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
STATUS_ERROR = "error"


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool
    threshold: float
    margin: float
    min_duration_s: float
    min_enroll_duration_s: float
    wait_ms: int
    num_threads: int


@dataclass(frozen=True)
class PendingEnrollment:
    """An enrollment embedding computed at ``audio_end``, stored only once the turn is confirmed."""

    profile_id: int
    name: str | None
    vector: np.ndarray
    model_id: str
    duration_s: float


@dataclass(frozen=True)
class TurnAnalysis:
    """Outcome of :meth:`SpeakerIdService.analyze_turn`.

    ``pending`` is set for an enrollment turn whose sample still awaits
    confirmation (:meth:`SpeakerIdService.commit_enrollment`).
    """

    result: dict[str, Any]
    pending: PendingEnrollment | None = None


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
) -> dict[str, Any]:
    """Build the canonical speaker-identification result dict."""
    return {
        "status": status,
        "profile_id": profile_id,
        "name": name,
        "ha_user_id": ha_user_id,
        "score": score,
        "margin": margin,
        "duration_s": round(float(duration_s), 3),
        "reason": reason,
    }


class SpeakerIdService:
    """Processes finished voice turns and serves results to conversations."""

    def __init__(
        self,
        *,
        engine: SpeakerEngine | Any | None = None,
        store: SpeakerTurnStore | None = None,
        enrollment: EnrollmentState | None = None,
        centroids: ProfileCentroidCache | None = None,
    ) -> None:
        self.engine = engine if engine is not None else SpeakerEngine()
        self.store = store if store is not None else SpeakerTurnStore()
        self.enrollment = enrollment if enrollment is not None else EnrollmentState()
        self.centroids = centroids if centroids is not None else ProfileCentroidCache()

    def invalidate_profiles(self) -> None:
        self.centroids.invalidate()

    async def warm_up(self, num_threads: int | None = None) -> bool:
        """Load the engine (downloading the model when missing). Never raises."""
        threads = num_threads if num_threads is not None else await _read_int("voice.speaker_id.num_threads")
        try:
            return bool(await self.engine.ensure_ready(threads))
        except Exception:
            logger.warning("Speaker-embedding engine warm-up failed", exc_info=True)
            return False

    def schedule_warm_up(self) -> None:
        """Start a background warm-up unless the engine is ready or loading."""
        if self.engine.status in ("ready", "loading"):
            return
        spawn(self.warm_up(), name="speaker_engine_warm_up")

    async def process_turn(self, pcm: bytes, sample_rate: int, config: VoiceConfig) -> dict[str, Any]:
        """Identify the speaker of one confirmed voice turn (or store an enrollment sample).

        Convenience for callers that already know the turn is confirmed; the
        ``/ws/voice`` session uses :meth:`analyze_turn` and
        :meth:`commit_enrollment` so an enrollment sample is stored only after
        the client confirmed the turn with ``end``.
        """
        analysis = await self.analyze_turn(pcm, sample_rate, config)
        if analysis.pending is not None:
            return await self.commit_enrollment(analysis.pending)
        return analysis.result

    async def analyze_turn(self, pcm: bytes, sample_rate: int, config: VoiceConfig) -> TurnAnalysis:
        """Identify the speaker of one finished voice turn, or prepare an enrollment sample.

        Never persists anything: an enrollment embedding is returned as
        ``pending`` and stored only by :meth:`commit_enrollment`.
        """
        duration = pcm_duration_s(pcm, sample_rate)
        armed = self.enrollment.current()
        if armed is not None:
            result, pending = await self._prepare_enrollment(pcm, sample_rate, duration, armed, config)
            if pending is None:
                self.store.record_recent(result)
            return TurnAnalysis(result=result, pending=pending)
        result, candidate = await self._identify(pcm, sample_rate, duration, config)
        recent = dict(result)
        if candidate is not None and recent.get("name") is None:
            # Calibration aid: show the nearest profile for unknown turns.
            recent["name"] = candidate
        self.store.record_recent(recent)
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

    async def _identify(
        self, pcm: bytes, sample_rate: int, duration: float, config: VoiceConfig
    ) -> tuple[dict[str, Any], str | None]:
        if duration < config.min_duration_s:
            return make_result(STATUS_TOO_SHORT, duration_s=duration), None
        try:
            centroids = await self.centroids.get(self.engine.model_id)
        except Exception:
            logger.warning("Loading voice profiles failed", exc_info=True)
            return make_result(STATUS_ERROR, duration_s=duration, reason="profiles_unavailable"), None
        if not centroids:
            return make_result(STATUS_NO_PROFILES, duration_s=duration), None
        if not self._engine_ready_or_warm_up():
            return make_result(STATUS_UNAVAILABLE, duration_s=duration, reason="engine_not_ready"), None
        try:
            embedding = await self.engine.embed(pcm, sample_rate, num_threads=config.num_threads)
        except SpeakerEngineUnavailableError:
            return make_result(STATUS_UNAVAILABLE, duration_s=duration), None
        except Exception:
            logger.warning("Speaker embedding failed", exc_info=True)
            return make_result(STATUS_ERROR, duration_s=duration, reason="embedding_failed"), None
        match = match_embedding(embedding, centroids, threshold=config.threshold, margin=config.margin)
        candidate = match.best.name if match.best is not None else None
        if match.identified and match.best is not None:
            return (
                make_result(
                    STATUS_IDENTIFIED,
                    duration_s=duration,
                    profile_id=match.best.profile_id,
                    name=match.best.name,
                    ha_user_id=match.best.ha_user_id,
                    score=match.score,
                    margin=match.margin,
                ),
                candidate,
            )
        return make_result(STATUS_UNKNOWN, duration_s=duration, score=match.score, margin=match.margin), candidate

    async def _prepare_enrollment(
        self,
        pcm: bytes,
        sample_rate: int,
        duration: float,
        armed: dict[str, Any],
        config: VoiceConfig,
    ) -> tuple[dict[str, Any], PendingEnrollment | None]:
        profile_id = int(armed["profile_id"])
        name = armed.get("name")
        if duration < config.min_enroll_duration_s:
            return make_result(STATUS_TOO_SHORT, duration_s=duration, profile_id=profile_id, name=name), None
        if not self._engine_ready_or_warm_up():
            result = make_result(
                STATUS_UNAVAILABLE, duration_s=duration, profile_id=profile_id, name=name, reason="engine_not_ready"
            )
            return result, None
        try:
            embedding = await self.engine.embed(pcm, sample_rate, num_threads=config.num_threads)
        except SpeakerEngineUnavailableError:
            return make_result(STATUS_UNAVAILABLE, duration_s=duration, profile_id=profile_id, name=name), None
        except Exception:
            logger.warning("Speaker embedding failed during enrollment", exc_info=True)
            result = make_result(
                STATUS_ERROR, duration_s=duration, profile_id=profile_id, name=name, reason="embedding_failed"
            )
            return result, None
        pending = PendingEnrollment(
            profile_id=profile_id,
            name=name,
            vector=embedding.astype("float32", copy=False).reshape(-1),
            model_id=self.engine.model_id,
            duration_s=round(duration, 3),
        )
        # Provisional result: the sample is stored only by ``commit_enrollment``.
        return make_result(STATUS_ENROLLED_SAMPLE, duration_s=duration, profile_id=profile_id, name=name), pending

    async def commit_enrollment(self, pending: PendingEnrollment) -> dict[str, Any]:
        """Store a confirmed enrollment sample and count it against the armed enrollment."""
        profile_id = pending.profile_id
        name = pending.name
        duration = pending.duration_s
        armed = self.enrollment.current()
        if armed is None or int(armed["profile_id"]) != profile_id:
            # Cancelled, expired or re-armed for another profile meanwhile.
            result = make_result(
                STATUS_ERROR, duration_s=duration, profile_id=profile_id, name=name, reason="enrollment_not_armed"
            )
            self.store.record_recent(result)
            return result
        vector = pending.vector
        try:
            await VoiceProfileRepository.add_sample(
                profile_id,
                vector.tobytes(),
                int(vector.size),
                pending.model_id,
                duration,
            )
        except Exception:
            # The armed profile may have been deleted meanwhile.
            logger.warning("Storing voice enrollment sample failed for profile %s", profile_id, exc_info=True)
            self.enrollment.disarm_profile(profile_id)
            result = make_result(
                STATUS_ERROR, duration_s=duration, profile_id=profile_id, name=name, reason="sample_not_stored"
            )
            self.store.record_recent(result)
            return result
        self.centroids.invalidate()
        remaining = self.enrollment.record_sample(profile_id)
        logger.info("Stored voice enrollment sample for profile %s (%d remaining)", profile_id, remaining)
        result = make_result(STATUS_ENROLLED_SAMPLE, duration_s=duration, profile_id=profile_id, name=name)
        self.store.record_recent(result)
        return result


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
    """Warm up the engine in the background when speaker identification is switched on.

    Runs for every settings write path (admin API, dashboard, restore), so the
    first voice turn after enabling does not hit a cold engine. Never waits for
    the model load.
    """
    if key != "voice.speaker_id.enabled":
        return
    if await is_speaker_id_enabled():
        get_speaker_service().schedule_warm_up()


SettingsRepository.register_write_listener(_on_settings_write)


def has_pending_speaker(conversation_id: str | None) -> bool:
    """True when a voice turn registered a speaker entry for ``conversation_id``."""
    service = _service
    return service is not None and service.store.has(conversation_id)


def discard_pending_speaker(conversation_id: str | None) -> None:
    """Drop an unconsumed entry (e.g. on an action-cache hit) so it never leaks into a later turn."""
    service = _service
    if service is not None and conversation_id:
        service.store.discard(conversation_id)


async def attach_speaker_to_task(task: Any, conversation_id: str, span_collector: Any) -> None:
    """Consume the voice-turn result for ``conversation_id`` and attach it to ``task.context``.

    Waits at most ``voice.speaker_id.wait_ms`` for a result still being
    computed. Only an ``identified`` result is attached; every consumed
    entry is traced as a ``speaker_id`` span. ``user_id`` is never touched.
    Never raises (cancellation excepted).
    """
    service = _service
    context = getattr(task, "context", None)
    if service is None or context is None or not service.store.has(conversation_id):
        return
    try:
        config = await load_voice_config()
        if not config.enabled:
            service.store.discard(conversation_id)
            return
        async with _optional_span(span_collector, "speaker_id", agent_id="orchestrator") as span:
            lookup = await service.store.pop(conversation_id, config.wait_ms / 1000.0)
            result = lookup.result or {}
            if lookup.result is not None:
                status = str(result.get("status") or STATUS_ERROR)
            else:
                status = "timeout" if lookup.timed_out else "discarded"
            metadata = span["metadata"]
            metadata["status"] = status
            metadata["profile_id"] = result.get("profile_id")
            metadata["score"] = result.get("score")
            metadata["margin"] = result.get("margin")
            metadata["duration_s"] = result.get("duration_s")
            metadata["waited_ms"] = lookup.waited_ms
            if status == STATUS_IDENTIFIED:
                metadata["speaker_name"] = result.get("name")
                context.speaker = SpeakerContext(
                    status=status,
                    profile_id=result.get("profile_id"),
                    name=result.get("name"),
                    ha_user_id=result.get("ha_user_id"),
                    score=result.get("score"),
                )
    except Exception:
        logger.warning("Speaker lookup failed for conversation %s", conversation_id, exc_info=True)
