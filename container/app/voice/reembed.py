"""Re-embed recorded enrollment samples with the current speaker model.

Voice samples store the model that computed them and profile centroids only
use samples of the current model, so a model change leaves every profile
empty. Recorded turns that are linked to a sample -- recorded enrollment
samples (status ``enrolled``) and turns an admin assigned to a profile --
keep the exact voice-activity-trimmed PCM that was embedded (see
:mod:`app.voice.recordings`). A run embeds that audio again with the
current engine (no second trim), stores a new current-model sample for the
linked profile and moves the turn's link and stored embedding to it, so
profiles work again without re-enrolling. Old-model samples stay; centroids
ignore them.

- A turn whose linked sample already uses the current model is skipped, so
  a run is idempotent.
- A turn whose link was cleared is no source: the link is cleared only when
  its sample was deleted (by an admin, a profile reset or the enrollment
  outlier review), and re-creating the sample would undo that deletion.
- Unlinked identification turns keep their stored embedding: assigning one
  re-embeds it from its audio when it was recorded with another model.

One run at a time per process. Files are processed one after another, each
embedding in a worker thread; a failing file is logged and counted and the
run continues with the next one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np

from app.db.repositories.voice import VoiceTurnRepository
from app.voice.recordings import audio_file_path, read_wav_pcm
from app.voice.speaker_engine import SpeakerEngineUnavailableError

logger = logging.getLogger(__name__)

ReembedTrigger = Literal["startup", "manual"]
_Outcome = Literal["reembedded", "skipped", "failed"]


class ReembedAlreadyRunningError(RuntimeError):
    """A re-embed run is already in progress."""


@dataclass(frozen=True)
class ReembedRun:
    """Result of one re-embed run (counts of linked recordings)."""

    trigger: str
    model: str
    started_at: str
    finished_at: str
    reembedded: int
    skipped: int
    failed: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RecordingReembedder:
    """Re-embeds linked recordings with ``engine`` and invalidates ``centroids`` afterwards.

    ``engine`` needs ``model_id`` and ``embed(pcm, sample_rate, *,
    num_threads)``; ``centroids`` needs ``invalidate()``.
    """

    def __init__(self, engine: Any, centroids: Any) -> None:
        self._engine = engine
        self._centroids = centroids
        self._running = False
        self._last_run: ReembedRun | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_run(self) -> ReembedRun | None:
        return self._last_run

    def status(self) -> dict[str, Any]:
        """``{"running": bool, "last_run": dict | None}`` for the voice status endpoint."""
        return {"running": self._running, "last_run": self._last_run.as_dict() if self._last_run else None}

    async def run(self, *, num_threads: int, trigger: ReembedTrigger) -> ReembedRun:
        """Re-embed every linked recording whose sample is missing or of another model.

        Raises :class:`ReembedAlreadyRunningError` while another run is in
        progress. Never raises for a single file.
        """
        # Check and set without an await in between: no second run can slip in.
        if self._running:
            raise ReembedAlreadyRunningError("A re-embed run is already in progress")
        self._running = True
        try:
            run = await self._run(num_threads, trigger)
        finally:
            self._running = False
        self._last_run = run
        return run

    async def _run(self, num_threads: int, trigger: ReembedTrigger) -> ReembedRun:
        started_at = _now_iso()
        model = str(self._engine.model_id)
        counts: dict[_Outcome, int] = {"reembedded": 0, "skipped": 0, "failed": 0}
        rows = await VoiceTurnRepository.list_sample_links()
        engine_error: str | None = None
        for row in rows:
            if row.get("sample_model") == model:
                counts["skipped"] += 1
                continue
            if engine_error is not None:
                counts["failed"] += 1
                continue
            try:
                outcome = await self._reembed_one(row, model, num_threads)
            except SpeakerEngineUnavailableError as exc:
                # Every remaining file would fail the same way.
                engine_error = str(exc) or type(exc).__name__
                logger.warning("Voice re-embed: speaker engine unavailable, stopping: %s", engine_error)
                outcome = "failed"
            counts[outcome] += 1
        if counts["reembedded"]:
            self._centroids.invalidate()
        run = ReembedRun(
            trigger=trigger,
            model=model,
            started_at=started_at,
            finished_at=_now_iso(),
            reembedded=counts["reembedded"],
            skipped=counts["skipped"],
            failed=counts["failed"],
        )
        level = logging.INFO if run.reembedded or run.failed or trigger == "manual" else logging.DEBUG
        logger.log(
            level,
            "Voice re-embed (%s, model=%s): %d re-embedded, %d skipped, %d failed",
            trigger,
            model,
            run.reembedded,
            run.skipped,
            run.failed,
        )
        return run

    async def _reembed_one(self, row: dict[str, Any], model: str, num_threads: int) -> _Outcome:
        """Re-embed one linked recording. Raises only :class:`SpeakerEngineUnavailableError`."""
        turn_id = str(row["id"])
        path = audio_file_path(row.get("audio_path"))
        if path is None:
            logger.warning("Voice re-embed: turn %s has no valid audio file name", turn_id)
            return "failed"
        try:
            pcm, sample_rate = await asyncio.to_thread(read_wav_pcm, path)
        except FileNotFoundError:
            logger.warning("Voice re-embed: recording of turn %s is missing", turn_id)
            return "failed"
        except Exception:
            logger.warning("Voice re-embed: reading the recording of turn %s failed", turn_id, exc_info=True)
            return "failed"
        if not pcm:
            logger.warning("Voice re-embed: recording of turn %s is empty", turn_id)
            return "failed"
        try:
            # Stored recordings are already voice-activity trimmed: embed as is.
            embedded = await self._engine.embed(pcm, sample_rate, num_threads=num_threads)
        except SpeakerEngineUnavailableError:
            raise
        except Exception:
            logger.warning("Voice re-embed: embedding turn %s failed", turn_id, exc_info=True)
            return "failed"
        vector = np.asarray(embedded, dtype=np.float32).reshape(-1)
        if vector.size == 0 or not bool(np.all(np.isfinite(vector))):
            logger.warning("Voice re-embed: turn %s produced an invalid embedding", turn_id)
            return "failed"
        profile_id = int(row["assigned_profile_id"])
        try:
            sample_id = await VoiceTurnRepository.replace_linked_sample(
                turn_id,
                profile_id=profile_id,
                expected_sample_id=row.get("assigned_sample_id"),
                embedding=vector.tobytes(),
                dim=int(vector.size),
                model=model,
                duration_s=row.get("duration_s"),
                speech_s=row.get("speech_s"),
            )
        except Exception:
            logger.warning("Voice re-embed: storing the sample of turn %s failed", turn_id, exc_info=True)
            return "failed"
        if sample_id is None:
            # Unlinked, relinked, deleted or the profile is gone since the listing.
            logger.debug("Voice re-embed: turn %s changed during the run; skipped", turn_id)
            return "skipped"
        logger.debug("Voice re-embed: turn %s -> sample %s of profile %s", turn_id, sample_id, profile_id)
        return "reembedded"
