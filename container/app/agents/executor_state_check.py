"""Deterministic state-check and speech helpers shared by the device executors."""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from app.agents.action_executor import VERIFY_MISMATCH
from app.agents.approval_gate import gate_speech

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

    A sensitive call held by the approval gate speaks nothing (the agent
    asks one approval question for the turn); a refused one explains that
    it needs a confirmation (see ``app.agents.approval_gate``).
    """
    if isinstance(verify, dict):
        gated = gate_speech(verify, action_name, friendly_name)
        if gated is not None:
            return gated
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


def unavailable_speech(friendly_name: str) -> str:
    """Speech for a command refused because the target entity is ``unavailable``."""
    return f"{friendly_name} is unavailable right now."


def is_unavailable(state_resp: Any) -> bool:
    """True when the pre-call state object reports the entity as ``unavailable``."""
    return isinstance(state_resp, dict) and state_resp.get("state") == "unavailable"


def unavailable_result(entity_id: str, friendly_name: str) -> dict[str, Any]:
    """Non-success executor result for an ``unavailable`` target (no service call, no wait)."""
    return {
        "success": False,
        "entity_id": entity_id,
        "new_state": "unavailable",
        "speech": unavailable_speech(friendly_name),
        "cacheable": False,
    }


# Players that are not playing anything: a pause or stop has nothing to act on.
_IDLE_PLAYER_STATES: frozenset[str] = frozenset({"idle", "off", "standby"})

# Alexa-style players settle as ``idle`` (not ``paused``) after a pause.
MEDIA_PAUSE_ACCEPTED_STATES: frozenset[str] = frozenset({"idle"})


class TransportNoop(NamedTuple):
    """A ``pause``/``stop`` whose pre-call state says there is nothing to change."""

    speech: str
    # True: the command is still sent (no state wait), because the pre-call
    # state may be stale; False: nothing is sent.
    send_command: bool


def player_transport_noop(transport: str, state_resp: Any, friendly_name: str) -> TransportNoop | None:
    """Classify a ``pause`` or ``stop`` that has nothing to act on, else ``None``.

    An idle/off/standby single player reports nothing playing, but polling
    integrations (Alexa Media Player) can lag, so the command is still sent
    once without waiting for a state (``paused`` could never be reached). A
    ``pause`` on a paused player is already done and sends nothing. Groups
    report an aggregate state and always run the normal verified path.
    """
    if transport not in ("pause", "stop") or is_group_state(state_resp) or not isinstance(state_resp, dict):
        return None
    state = state_resp.get("state")
    if state in _IDLE_PLAYER_STATES:
        return TransportNoop(f"Nothing is playing on {friendly_name}.", True)
    if transport == "pause" and state == "paused":
        return TransportNoop(f"{friendly_name} is already paused.", False)
    return None


def media_pause_accepted_states(transport: str, previous_state: str | None) -> frozenset[str] | None:
    """Extra states that satisfy a ``pause`` issued while the player was ``playing``."""
    if transport == "pause" and previous_state == "playing":
        return MEDIA_PAUSE_ACCEPTED_STATES
    return None


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
