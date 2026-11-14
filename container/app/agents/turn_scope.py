"""Which stored conversation turns may steer the current request.

The current request has priority over the conversation history:

* Speaker change: when the current voice speaker is confidently identified
  and the most recent stored user turn was spoken by a different identified
  speaker, the exchanges of other identified speakers are hidden from
  routing and agent context. Turns without a speaker stay (unknown speaker
  means no evidence of a change).
* Freshness: the previous-agent follow-up hint only applies while the newest
  turn is fresh (or a clarifying question is open); older history is
  background information only.

Pure functions over the turn dicts kept by
:class:`app.agents.conversation_manager.ConversationManager`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Keys stamped on stored turn dicts by ConversationManager.store_turn.
TURN_TS_KEY = "ts"
TURN_SPEAKER_KEY = "speaker_profile_id"

# Newest turn older than this: the history no longer counts as a direct
# follow-up (no previous-agent hint, background-only note).
TURN_FRESH_SECONDS = 120.0

# Upper bound the prelude waits for the speaker result before classification.
# The speaker analysis starts at audio end, so the result is normally ready.
SPEAKER_SCOPE_WAIT_S = 0.2


@dataclass(frozen=True)
class TurnScope:
    """Turns usable for the current request and why some were hidden."""

    turns: list[dict[str, Any]] = field(default_factory=list)
    speaker_changed: bool = False
    dropped_turns: int = 0
    previous_speaker_profile_id: int | None = None


def _speaker_of(turn: dict[str, Any]) -> int | None:
    value = turn.get(TURN_SPEAKER_KEY)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def last_user_speaker(turns: list[dict[str, Any]]) -> int | None:
    """Speaker profile id of the most recent stored user turn (None when unknown)."""
    for turn in reversed(turns):
        if turn.get("role") == "user":
            return _speaker_of(turn)
    return None


def scope_turns_to_speaker(turns: list[dict[str, Any]], current_profile_id: int | None) -> TurnScope:
    """Hide the exchanges of other identified speakers after a speaker change.

    A change needs two identified speakers: the current one and the speaker
    of the most recent stored user turn. Without a change the turns are
    returned unchanged. On a change every user turn of a different
    identified speaker is dropped together with the assistant reply that
    follows it; turns with an unknown speaker are kept.
    """
    if current_profile_id is None:
        return TurnScope(turns=list(turns))
    previous = last_user_speaker(turns)
    if previous is None or previous == current_profile_id:
        return TurnScope(turns=list(turns))

    kept: list[dict[str, Any]] = []
    dropping = False
    for turn in turns:
        if turn.get("role") == "user":
            speaker = _speaker_of(turn)
            dropping = speaker is not None and speaker != current_profile_id
        if not dropping:
            kept.append(turn)
    return TurnScope(
        turns=kept,
        speaker_changed=True,
        dropped_turns=len(turns) - len(kept),
        previous_speaker_profile_id=previous,
    )


def turns_are_fresh(
    turns: list[dict[str, Any]],
    *,
    now: float | None = None,
    max_age_s: float = TURN_FRESH_SECONDS,
) -> bool:
    """True when the newest turn is at most ``max_age_s`` old.

    Turns without a timestamp have an unknown age and count as fresh, so
    the follow-up behavior for them is unchanged.
    """
    stamps = [ts for turn in turns if isinstance(ts := turn.get(TURN_TS_KEY), int | float)]
    if not stamps:
        return True
    current = time.time() if now is None else now
    return current - max(stamps) <= max_age_s
