"""Voice follow-up decision shared by every finalization path.

A voice follow-up reopens the satellite microphone right after TTS playback
without a wake word. While a voice-profile enrollment is armed, every voice
turn becomes an enrollment sample, so a follow-up turn would record the tail
of the assistant's own voice (and turn every reply into another sample).
Follow-ups are therefore suppressed for the whole turn while enrollment is
armed: no voice follow-up flag and no organic follow-up hint for mediation.
The spoken text is unchanged (a clarifying question is still spoken and kept
as the pending question; only the microphone stays closed).
"""

from __future__ import annotations

from typing import Any

from app.analytics.tracer import record_request_attribute
from app.models.agent import source_allows_voice_followup
from app.voice.service import enrollment_active

__all__ = [
    "FOLLOWUP_SUPPRESSED_ATTR",
    "FOLLOWUP_SUPPRESSED_ENROLLMENT",
    "organic_followup_allowed",
    "resolve_voice_followup",
]

FOLLOWUP_SUPPRESSED_ATTR = "followup_suppressed"
FOLLOWUP_SUPPRESSED_ENROLLMENT = "enrollment"


def _record_suppressed(span_collector: Any, span_metadata: dict[str, Any] | None) -> None:
    if span_metadata is not None:
        span_metadata[FOLLOWUP_SUPPRESSED_ATTR] = FOLLOWUP_SUPPRESSED_ENROLLMENT
    if span_collector is not None:
        record_request_attribute(span_collector, FOLLOWUP_SUPPRESSED_ATTR, FOLLOWUP_SUPPRESSED_ENROLLMENT)


async def resolve_voice_followup(
    followup_question: bool,
    source: str | None,
    *,
    span_collector: Any = None,
    span_metadata: dict[str, Any] | None = None,
) -> bool:
    """Return whether the turn's final result requests a voice follow-up.

    ``followup_question`` is the merged agent/mediation signal. Text-only
    sources never request one; an armed enrollment suppresses it and records
    ``followup_suppressed: "enrollment"`` on ``span_metadata`` and the root span.
    """
    if not followup_question or not source_allows_voice_followup(source):
        return False
    if await enrollment_active():
        _record_suppressed(span_collector, span_metadata)
        return False
    return True


async def organic_followup_allowed(span_collector: Any = None) -> bool:
    """False while enrollment is armed: mediation must not add a closing follow-up question."""
    if await enrollment_active():
        _record_suppressed(span_collector, None)
        return False
    return True
