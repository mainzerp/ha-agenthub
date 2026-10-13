"""Personalization identity for a turn (Home Assistant user or recognized voice).

The personalization identity selects *whose* data shapes a response: the
session-memory bucket, the calendar user mapping (default calendars and
reminders), the sender name of a sent message and the speaker hint for
mediation. It is never used for entity visibility, authorization or audit:
``TaskContext.user_id`` stays the real request user and is never mutated.
"""

from __future__ import annotations

from typing import Any, Literal

IdentitySource = Literal["ha", "voice", "none"]

# Mirrors ``app.voice.service.STATUS_IDENTIFIED`` (kept local so this helper
# stays import-light for agents that never touch the voice package).
SPEAKER_STATUS_IDENTIFIED = "identified"


def identified_speaker(context: Any) -> Any | None:
    """Return ``context.speaker`` when it is an identified voice match, else None.

    When the request carries a Home Assistant user and the speaker is linked
    to a *different* HA user, the speaker is ignored (None): all
    personalization (memory, calendars, name, sender, prompt line) then
    follows the request user.
    """
    speaker = getattr(context, "speaker", None) if context is not None else None
    if speaker is None or getattr(speaker, "status", None) != SPEAKER_STATUS_IDENTIFIED:
        return None
    request_user_id = getattr(context, "user_id", None)
    speaker_user_id = getattr(speaker, "ha_user_id", None)
    if request_user_id and speaker_user_id and speaker_user_id != request_user_id:
        return None
    return speaker


def personalization_user_id(context: Any) -> tuple[str | None, IdentitySource]:
    """Return ``(user_id, source)`` for personalization of this turn.

    * ``(context.user_id, "ha")`` when the request carries a Home Assistant user.
    * ``(speaker.ha_user_id, "voice")`` when no request user is set and an
      identified voice speaker is linked to a Home Assistant user.
    * ``(None, "none")`` otherwise.

    Personalization only -- never use the result for entity visibility,
    authorization or audit records. ``context.user_id`` is never changed.
    """
    if context is None:
        return None, "none"
    request_user_id = getattr(context, "user_id", None)
    if request_user_id:
        return request_user_id, "ha"
    speaker = identified_speaker(context)
    ha_user_id = getattr(speaker, "ha_user_id", None) if speaker is not None else None
    if ha_user_id:
        return ha_user_id, "voice"
    return None, "none"


def speaker_display_name(context: Any) -> str:
    """Sanitized name of an identified voice speaker ("" when unknown).

    Unverified voice match: use it for wording only, never for authorization.
    """
    speaker = identified_speaker(context)
    if speaker is None:
        return ""
    # Lazy import: app.agents.base imports this module.
    from app.agents.base import UNTRUSTED_NAME_MAX_CHARS, sanitize_untrusted_text

    return sanitize_untrusted_text(getattr(speaker, "name", None), UNTRUSTED_NAME_MAX_CHARS)
