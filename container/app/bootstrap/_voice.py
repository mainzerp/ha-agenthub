"""Bootstrap: speaker-engine warm-up, recording re-embed and voice-recording retention."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.bootstrap._tasks import spawn_background
from app.voice.recordings import cleanup_recordings
from app.voice.reembed import ReembedAlreadyRunningError
from app.voice.service import get_speaker_service, is_speaker_id_enabled, read_audio_retention_days

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Recorded voice turns are expired shortly after startup and then hourly.
RECORDING_RETENTION_INTERVAL_S = 3600.0
# The first pass waits until startup work (entity index, model warm-up) settled.
RECORDING_RETENTION_STARTUP_DELAY_S = 60.0


async def run_recording_retention(
    interval_s: float = RECORDING_RETENTION_INTERVAL_S,
    startup_delay_s: float = RECORDING_RETENTION_STARTUP_DELAY_S,
) -> None:
    """Delete recorded voice turns older than ``voice.speaker_id.audio_retention_days`` and orphaned files.

    Runs regardless of ``keep_audio``: turning recording off stops new
    recordings, existing ones still expire. A failed pass is logged and
    retried on the next interval.
    """
    await asyncio.sleep(startup_delay_s)
    while True:
        try:
            removed = await cleanup_recordings(await read_audio_retention_days())
            if removed["expired"] or removed["orphans"]:
                logger.info(
                    "Voice recording retention: %d expired turns and %d orphaned files deleted",
                    removed["expired"],
                    removed["orphans"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Voice recording retention pass failed", exc_info=True)
        await asyncio.sleep(interval_s)


async def warm_up_and_reembed() -> None:
    """Warm up the speaker engine, then re-embed linked recordings once (see :mod:`app.voice.reembed`).

    Brings profiles back after a speaker-model change without re-enrolling.
    Never raises: an unavailable engine skips the run (the admin endpoint
    ``POST /api/admin/voice/reembed`` can start it later).
    """
    service = get_speaker_service()
    try:
        if not await service.warm_up():
            logger.info("Voice re-embed skipped at startup: speaker engine unavailable")
            return
        await service.reembed_recordings("startup")
    except asyncio.CancelledError:
        raise
    except ReembedAlreadyRunningError:
        logger.debug("Voice re-embed at startup skipped: a run is already in progress")
    except Exception:
        logger.warning("Voice re-embed at startup failed", exc_info=True)


async def setup_voice(app: FastAPI) -> None:
    """Start the recording retention task; with the feature on, warm up the engine and re-embed recordings.

    The model load (and its one-time download when the image does not ship
    it) and the re-embed run as one background task; failures only mark the
    engine ``unavailable`` and never block or fail startup.
    """
    retention = getattr(app.state, "voice_retention_task", None)
    if retention is None or retention.done():
        spawn_background(app, run_recording_retention(), "voice_retention_task", name="voice_recording_retention")
    try:
        if not await is_speaker_id_enabled():
            return
        startup = getattr(app.state, "voice_reembed_task", None)
        if startup is None or startup.done():
            spawn_background(app, warm_up_and_reembed(), "voice_reembed_task", name="voice_warm_up_reembed")
    except Exception:
        logger.warning("Speaker-identification warm-up could not be scheduled", exc_info=True)
