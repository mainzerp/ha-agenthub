"""Deterministic state-check helpers for action executors."""

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
    "open_cover_tilt": frozenset({"open"}),
    "close_cover_tilt": frozenset({"closed"}),
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
