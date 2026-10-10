"""Deterministic state-check and speech helpers shared by the device executors."""

from __future__ import annotations

import re
from typing import Any

from app.agents.action_executor import VERIFY_MISMATCH

# An observed HA state is only spoken when it is a short plain word
# ("jammed", "unlocked", "armed_away"); anything else stays generic.
_SPEAKABLE_STATE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_ -]{0,31}")


def _speakable_observed_state(verify: Any) -> str | None:
    """Return the contradicting observed state of a ``mismatch`` verification, if speakable."""
    if not isinstance(verify, dict) or verify.get("outcome") != VERIFY_MISMATCH:
        return None
    observed = verify.get("observed_state")
    if not isinstance(observed, str):
        return None
    observed = observed.strip()
    if not _SPEAKABLE_STATE_RE.fullmatch(observed):
        return None
    return observed.replace("_", " ")


def failure_speech(action_name: str, friendly_name: str, verify: dict[str, Any] | None = None) -> str:
    """Generic user-facing failure line.

    When ``verify`` (a ``call_service_with_verification`` result) reports a
    ``mismatch`` -- the command ran, but the device settled in a
    contradicting state such as ``jammed`` -- the observed state is named in
    plain words. Never embeds exception text, URLs, or other internals:
    callers log the details (``call_service_with_verification`` already
    logs the traceback).
    """
    verb = (action_name or "update").replace("_", " ")
    observed = _speakable_observed_state(verify)
    if observed:
        return f"Sorry, {verb} failed: {friendly_name} reports {observed}."
    return f"Sorry, {verb} failed for {friendly_name}."


# Target states that make a parameterless action redundant. Tilt actions are
# intentionally absent: the cover's main state ("open"/"closed") describes
# the position, not the tilt, so it cannot prove a tilt action is a no-op.
_REDUNDANT_IF_STATE: dict[str, str | frozenset[str]] = {
    "turn_on": frozenset(
        {
            "on",
            "heat",
            "cool",
            "auto",
            "heat_cool",
            "fan_only",
            "dry",
            "playing",
            "cleaning",
            "returning",
            "paused",
            "idle",
        }
    ),
    "turn_off": frozenset({"off", "disarmed"}),
    "toggle": frozenset(),  # toggle is never redundant
    "open_cover": frozenset({"open"}),
    "close_cover": frozenset({"closed"}),
    "lock": frozenset({"locked"}),
    "unlock": frozenset({"unlocked"}),
    "alarm_arm_home": frozenset({"armed_home"}),
    "alarm_arm_away": frozenset({"armed_away"}),
    "alarm_arm_night": frozenset({"armed_night"}),
    "alarm_disarm": frozenset({"disarmed"}),
    "camera_turn_off": frozenset({"off"}),
    "play": frozenset({"playing"}),
    "pause": frozenset({"paused"}),
    "stop": frozenset({"idle", "off"}),
    "start": frozenset({"cleaning"}),
    "return_to_base": frozenset({"returning"}),
}


def _state_matches(action_name: str, current_state: str | None) -> bool:
    """Return True if current_state is already in the target state for action_name."""
    if current_state is None:
        return False
    targets = _REDUNDANT_IF_STATE.get(action_name)
    if targets is None:
        return False
    if isinstance(targets, frozenset):
        return current_state.lower() in {t.lower() for t in targets}
    return current_state.lower() == targets.lower()


def is_group_state(state_resp: Any) -> bool:
    """Return True when the state object describes a group of entities.

    HA group entities (light/switch/cover/fan/media_player groups and
    legacy ``group.*``) expose their members as an ``entity_id`` list
    attribute. Their aggregate state is "on"/"open" as soon as ANY member
    is, so it cannot prove that every member is already in the target state.
    """
    if not isinstance(state_resp, dict):
        return False
    attrs = state_resp.get("attributes")
    if not isinstance(attrs, dict):
        return False
    members = attrs.get("entity_id")
    return isinstance(members, (list, tuple)) and len(members) > 0


def is_redundant_action(
    action_name: str,
    state_resp: Any,
    service_data: dict[str, Any] | None,
) -> bool:
    """Decide whether an action can be skipped because it would change nothing.

    The skip only applies to parameterless actions on a single entity:

    * Non-empty ``service_data`` (brightness, colour, position, code, ...)
      changes something beyond the on/off state, so it is never redundant
      ("set the light to 50%" on a light that is already on must run).
    * Group entities report an aggregate state and are never skipped.
    """
    if service_data:
        return False
    if is_group_state(state_resp):
        return False
    current_state = state_resp.get("state") if isinstance(state_resp, dict) else None
    return _state_matches(action_name, current_state)


_NON_COMPARABLE_STATES: frozenset[str] = frozenset({"", "unknown", "unavailable"})


def verification_previous_state(state_resp: Any) -> str | None:
    """Pre-call state to pass as ``previous_state`` to ``call_service_with_verification``.

    Returns the state the executor read for its no-op check, so a device
    that moved to a different non-target state after the call ("disarmed"
    -> "triggered" after an arm) counts as a contradiction, while an
    unchanged state stays ``unverified``. Returns ``None`` (no comparison)
    for groups, whose aggregate state follows the members, and for
    ``unknown``/``unavailable`` or unreadable pre-call states.
    """
    if not isinstance(state_resp, dict) or is_group_state(state_resp):
        return None
    state = state_resp.get("state")
    if not isinstance(state, str) or state.strip().lower() in _NON_COMPARABLE_STATES:
        return None
    return state


def supported_features(state_resp: Any) -> int | None:
    """Return the ``supported_features`` bitmask, or None when HA did not report one."""
    if not isinstance(state_resp, dict):
        return None
    attrs = state_resp.get("attributes")
    if not isinstance(attrs, dict):
        return None
    value = attrs.get("supported_features")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def lacks_feature(state_resp: Any, required_any: int) -> bool:
    """Return True only when HA reports a feature mask that has none of ``required_any``.

    A missing mask is treated as "unknown" (not lacking) so integrations
    that do not report features keep working.
    """
    mask = supported_features(state_resp)
    if mask is None:
        return False
    return (mask & required_any) == 0
