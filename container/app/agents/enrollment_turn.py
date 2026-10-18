"""Voice-enrollment turns: a short fixed reply instead of agent processing.

While a voice-profile enrollment is armed, every satellite voice turn is an
enrollment sample. The conversation side of such a turn is not processed by
the agents: it gets a short fixed reply right away, so the user can speak the
enrollment sentences in fast succession. The turn creates no trace rows, no
cache lookups or stores, no conversation turn or memory writes, no filler, no
classification, no mediation and no voice follow-up.

A turn is an enrollment turn only when its ``/ws/voice`` session registered
the turn-store entry while the enrollment was armed
(:func:`app.voice.service.is_enrollment_turn`); typed, API and AI-task turns
are always processed normally.
"""

from __future__ import annotations

import logging
from typing import Any

from app.analytics.tracer import discard_trace
from app.voice.service import (
    ENROLLMENT_OUTCOME_REJECTED,
    consume_enrollment_turn,
    is_enrollment_turn,
)

__all__ = ["enrollment_reply", "handle_enrollment_turn"]

logger = logging.getLogger(__name__)

# Spoken reply keyed by sample outcome, then language key (English fallback).
_ENROLLMENT_REPLIES: dict[str, dict[str, str]] = {
    "saved": {"en": "OK", "de": "OK"},
    "not_saved": {"en": "Not saved", "de": "Nicht gespeichert"},
}


def enrollment_reply(outcome: str, language: str | None) -> str:
    """Return the fixed reply for a sample ``outcome`` in ``language``.

    A rejected sample says so; an accepted sample or one whose result did not
    arrive in time gets the plain acknowledgement.
    """
    key = "not_saved" if outcome == ENROLLMENT_OUTCOME_REJECTED else "saved"
    lang_key = (language or "en").strip().lower().replace("_", "-").split("-", 1)[0]
    replies = _ENROLLMENT_REPLIES[key]
    return replies.get(lang_key, replies["en"])


async def handle_enrollment_turn(
    conversation_id: str, language: str | None, span_collector: Any
) -> dict[str, Any] | None:
    """Answer an enrollment turn with its fixed reply, or return None for a normal turn.

    Consumes the turn-store entry (waiting briefly for the sample result) and
    discards the request's spans so neither the trace summary nor any span is
    written.
    """
    if not is_enrollment_turn(conversation_id):
        return None
    discard_trace(span_collector)
    outcome = await consume_enrollment_turn(conversation_id)
    speech = enrollment_reply(outcome.outcome, language)
    logger.info(
        "Voice enrollment turn answered without agents: conversation=%s outcome=%s status=%s reason=%s waited=%.1fms",
        conversation_id,
        outcome.outcome,
        outcome.status or "-",
        outcome.reason or "-",
        outcome.waited_ms,
    )
    return {
        "speech": speech,
        "routed_to": "orchestrator",
        "action_executed": None,
        "voice_followup": False,
        "error": None,
    }
