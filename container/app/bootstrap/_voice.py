"""Bootstrap: speaker-identification engine warm-up (only when enabled)."""

from __future__ import annotations

import logging

from app.voice.service import get_speaker_service, is_speaker_id_enabled

logger = logging.getLogger(__name__)


async def setup_voice() -> None:
    """Warm up the speaker-embedding engine in the background when the feature is on.

    The model load (and its one-time download when the image does not ship
    it) runs as a background task; failures only mark the engine
    ``unavailable`` and never block or fail startup.
    """
    try:
        if not await is_speaker_id_enabled():
            return
        get_speaker_service().schedule_warm_up()
    except Exception:
        logger.warning("Speaker-identification warm-up could not be scheduled", exc_info=True)
