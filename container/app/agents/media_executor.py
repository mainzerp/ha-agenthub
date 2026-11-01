"""Media-player action execution via HA media_player services.

Besides the ``media_player.*`` services this executor shows a camera's live
stream on a media player through Home Assistant's ``camera.play_stream``
action (target: the camera; data: ``media_player`` and ``format: hls``). HA
resolves the camera's HLS stream URL itself and forwards it to the player's
``media_player.play_media`` (requires the ``stream`` integration and a player
that plays HLS video).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.agents.action_executor import (
    build_verified_speech,
    call_service_with_verification,
    resolve_and_validate_entity,
)
from app.agents.executor_state_check import (
    failure_speech,
    is_redundant_action,
    lacks_feature,
    verification_previous_state,
)
from app.entity.tokens import normalize_tokenize, token_in_query
from app.entity.visibility import entity_is_visible
from app.models.agent import TaskContext

logger = logging.getLogger(__name__)

_MEDIA_ACTION_MAP: dict[str, tuple[str, str]] = {
    "turn_on": ("media_player", "turn_on"),
    "turn_off": ("media_player", "turn_off"),
    "play": ("media_player", "media_play"),
    "pause": ("media_player", "media_pause"),
    "stop": ("media_player", "media_stop"),
    "next_track": ("media_player", "media_next_track"),
    "previous_track": ("media_player", "media_previous_track"),
    "set_volume": ("media_player", "volume_set"),
    "volume_up": ("media_player", "volume_up"),
    "volume_down": ("media_player", "volume_down"),
    "mute": ("media_player", "volume_mute"),
    "select_source": ("media_player", "select_source"),
    "play_media": ("media_player", "play_media"),
}

# FLOW-VERIFY-SHARED (0.18.5): only ``turn_off`` reliably lands in "off"
# across media_player integrations. ``turn_on`` can end in "idle",
# "standby", "on", or "playing" depending on the integration, so we leave
# it to the WS observer. Transport actions (play/pause/stop) map cleanly.
_EXPECTED_STATE_BY_ACTION: dict[str, str] = {
    "turn_off": "off",
    "play": "playing",
    "pause": "paused",
    "stop": "idle",
}

_ACTION_PHRASES: dict[str, str] = {
    "set_volume": "volume updated",
    "volume_up": "volume turned up",
    "volume_down": "volume turned down",
    "mute": "muted",
    "next_track": "skipped to the next track",
    "previous_track": "skipped to the previous track",
    "select_source": "source selected",
    "play_media": "playback started",
}

# HA ``MediaPlayerEntityFeature`` bits for the volume actions (any of them).
_FEATURE_VOLUME_SET = 4
_FEATURE_VOLUME_MUTE = 8
_FEATURE_VOLUME_STEP = 1024
_REQUIRED_FEATURES: dict[str, tuple[int, str]] = {
    "set_volume": (_FEATURE_VOLUME_SET, "setting the volume"),
    "volume_up": (_FEATURE_VOLUME_SET | _FEATURE_VOLUME_STEP, "changing the volume"),
    "volume_down": (_FEATURE_VOLUME_SET | _FEATURE_VOLUME_STEP, "changing the volume"),
    "mute": (_FEATURE_VOLUME_MUTE, "muting"),
}

# HA ``MediaPlayerEntityFeature`` bits used by the playback checks.
_FEATURE_TURN_ON = 128
_FEATURE_PLAY_MEDIA = 512
# HA ``CameraEntityFeature.STREAM``: the camera provides a stream source.
_CAMERA_FEATURE_STREAM = 2

# Keyword recall (the LLM's candidate list) and every media_player service
# stay on media_player.*. Cameras are never action targets or recall
# candidates: ``play_camera`` resolves its stream SOURCE from the camera
# name in ``parameters.camera`` within ``_CAMERA_DOMAINS`` (shared resolver,
# visibility applied), so adding cameras cannot crowd players out of the
# candidate list or tie with them.
_ALLOWED_DOMAINS: frozenset[str] = frozenset({"media_player"})

# FLOW-DOMAIN-1 (0.19.2): all media actions target media_player.* entities.
_ACTION_DOMAINS: frozenset[str] = frozenset({"media_player"})
_CAMERA_DOMAINS: frozenset[str] = frozenset({"camera"})

# Actions that show a stream on a media player (the target entity is the player).
_STREAM_ACTIONS: frozenset[str] = frozenset({"play_camera"})

# Entity-candidate declaration (read by the agent's ``@agent`` call, see
# ``ActionableAgent._entity_actions``): every action this executor
# dispatches, split into actions that act on one target entity (they need a
# recalled entity candidate) and actions that run without one. Derived from
# the dispatch tables so the declaration cannot drift from the executor.
_READ_ACTIONS: frozenset[str] = frozenset({"query_media_state", "list_media_players"})
ENTITY_FREE_ACTIONS: frozenset[str] = frozenset({"list_media_players"})
ENTITY_ACTIONS: frozenset[str] = (frozenset(_MEDIA_ACTION_MAP) | _READ_ACTIONS | _STREAM_ACTIONS) - ENTITY_FREE_ACTIONS

# Player states in which a stream target is powered on first.
_OFF_STATES: frozenset[str] = frozenset({"off", "standby"})
# Bounded power-on wait before streaming (the agent's dispatch budget is
# about 10 s and also covers the LLM call and the stream call itself).
_POWER_ON_WAIT_SEC = 4.0
_POWER_ON_POLL_SEC = 0.5
_POWER_ON_WS_TIMEOUT_SEC = 2.0
# ``camera.play_stream`` is a blocking HA call (HA awaits the player's
# ``play_media``), so a raised error is the failure signal; the camera's own
# state is only sampled briefly.
_STREAM_VERIFY_WS_TIMEOUT_SEC = 0.5
_STREAM_FORMAT = "hls"
# Content types that need a screen (HA ``MediaType`` values and MIME prefixes).
_VIDEO_CONTENT_TYPES: frozenset[str] = frozenset({"video", "movie", "tvshow", "episode", "channel", "image"})
_VIDEO_MIME_PREFIXES: tuple[str, ...] = ("video/", "image/", "application/vnd.apple.mpegurl", "application/x-mpegurl")
_MAX_LISTED_CAMERAS = 5


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _validate_camera_domain(entity_id: str) -> bool:
    """A stream source must be a camera.* entity."""
    return entity_id.split(".", 1)[0] in _CAMERA_DOMAINS if "." in entity_id else False


def normalize_volume_level(value: Any) -> float:
    """Return a 0.0-1.0 volume level; a percentage (1 < value <= 100) is scaled down.

    Raises ``ValueError``/``TypeError`` for values that are not numbers or
    are out of range.
    """
    if isinstance(value, bool):
        raise ValueError("volume_level")
    level = float(value)
    if 1.0 < level <= 100.0:
        level = level / 100.0
    if not 0.0 <= level <= 1.0:
        raise ValueError("volume_level")
    return round(level, 2)


def _build_media_service_data(action: dict) -> dict[str, Any]:
    """Build HA service_data from a media action's parameters."""
    params = action.get("parameters") or {}
    action_name = action.get("action", "")
    data: dict[str, Any] = {}

    if action_name == "set_volume":
        if "volume_level" in params:
            data["volume_level"] = normalize_volume_level(params["volume_level"])
    elif action_name == "mute":
        # HA requires the flag; a bare "mute" means mute.
        data["is_volume_muted"] = bool(params.get("is_volume_muted", True))
    elif action_name == "select_source":
        if "source" in params:
            data["source"] = str(params["source"])
    elif action_name == "play_media":
        if "media_content_id" in params:
            data["media_content_id"] = str(params["media_content_id"])
        if "media_content_type" in params:
            data["media_content_type"] = str(params["media_content_type"])

    return data


def _relative_volume_level(state_resp: Any, delta: float) -> float | None:
    """Absolute volume (0.0-1.0) for a relative change, or None when unknown."""
    attrs = state_resp.get("attributes") if isinstance(state_resp, dict) else None
    current = attrs.get("volume_level") if isinstance(attrs, dict) else None
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        return None
    return round(min(1.0, max(0.0, float(current) + delta)), 2)


async def execute_media_action(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None = None,
    span_collector=None,
    *,
    preferred_area_id: str | None = None,
    task_context: TaskContext | None = None,
) -> dict:
    """Resolve an entity, call a media_player HA service, and verify the result.

    Args:
        action: Parsed action dict with "action", "entity", and optional "parameters".
        ha_client: HARestClient instance.
        entity_index: EntityIndex instance.
        entity_matcher: EntityMatcher instance.
        agent_id: Optional agent identifier for entity matching context.

    Returns:
        dict with "success", "entity_id", "new_state", "speech" and, for
        executed writes, "executed_command" (the exact HA call).
    """
    action_name = action.get("action", "").lower()
    entity_query = action.get("entity", "")

    # Read-only actions (no service call)
    if action_name in _READ_ACTIONS:
        return await _handle_media_read_action(
            action_name,
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            action=action,
        )

    mapping = _MEDIA_ACTION_MAP.get(action_name)
    if not mapping and action_name not in _STREAM_ACTIONS:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"Unknown action: {action_name}",
        }

    # Directive 4: shared deterministic-first resolver; an LLM-picked
    # entity_id is validation input only and must pass the candidate gate.
    resolved = await resolve_and_validate_entity(
        entity_query,
        entity_index,
        entity_matcher,
        agent_id,
        _ACTION_DOMAINS,
        _validate_domain,
        preferred_area_id=preferred_area_id,
        span_collector=span_collector,
        direct_entity_id=action.get("entity_id"),
    )
    if resolved["not_found_result"] is not None:
        return resolved["not_found_result"]
    entity_id = resolved["entity_id"]
    friendly_name = resolved["friendly_name"]

    state_resp = await _read_state(ha_client, entity_id)
    current_state = state_resp.get("state") if isinstance(state_resp, dict) else None

    if action_name == "play_camera":
        camera = await _resolve_camera(
            action,
            entity_index,
            entity_matcher,
            agent_id,
            target_id=entity_id,
            target_name=friendly_name,
            target_state=current_state,
            span_collector=span_collector,
        )
        if camera.get("result") is not None:
            return camera["result"]
        return await _play_camera_on_player(
            ha_client,
            camera_id=camera["entity_id"],
            camera_name=camera["friendly_name"],
            target_id=entity_id,
            target_name=friendly_name,
            target_state_resp=state_resp,
        )

    if action_name == "play_media":
        return await _play_media_on_player(
            action,
            ha_client,
            entity_index,
            agent_id,
            target_id=entity_id,
            target_name=friendly_name,
            target_state_resp=state_resp,
            span_collector=span_collector,
        )

    if mapping is None:  # pragma: no cover - stream actions returned above
        return {"success": False, "entity_id": None, "new_state": None, "speech": f"Unknown action: {action_name}"}
    domain, service = mapping

    try:
        service_data = _build_media_service_data(action)
    except (TypeError, ValueError):
        logger.warning("Invalid media parameters for %s: %r", entity_id, action.get("parameters"))
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": current_state,
            "speech": f"I could not understand the requested value for {friendly_name}.",
            "cacheable": False,
        }

    relative = False
    params = action.get("parameters") or {}
    if action_name == "set_volume" and "volume_level" not in service_data and isinstance(params, dict):
        delta = params.get("volume_delta")
        if isinstance(delta, (int, float)) and not isinstance(delta, bool):
            level = _relative_volume_level(state_resp, float(delta))
            if level is None:
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "new_state": current_state,
                    "speech": f"I could not read the current volume of {friendly_name}.",
                    "cacheable": False,
                }
            service_data["volume_level"] = level
            relative = True

    required = _REQUIRED_FEATURES.get(action_name)
    if required is not None and lacks_feature(state_resp, required[0]):
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": current_state,
            "speech": f"{friendly_name} does not support {required[1]}.",
            "cacheable": False,
        }

    # Deterministic skip: only a parameterless action on a single player that
    # is already in the target state is redundant (groups always run).
    if is_redundant_action(action_name, state_resp, service_data):
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": current_state,
            "noop": True,
            "speech": f"Done, {friendly_name} is already {current_state}.",
        }

    expected_state = _EXPECTED_STATE_BY_ACTION.get(action_name)
    verify = await call_service_with_verification(
        ha_client,
        domain,
        service,
        entity_id,
        service_data=service_data,
        expected_state=expected_state,
        previous_state=verification_previous_state(state_resp),
    )
    if not verify["success"]:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": failure_speech(action_name, friendly_name, verify),
        }

    phrases = _ACTION_PHRASES
    if action_name == "mute" and service_data.get("is_volume_muted") is False:
        phrases = {**_ACTION_PHRASES, "mute": "unmuted"}

    new_state = verify["observed_state"]
    result: dict[str, Any] = {
        "success": True,
        "action": action_name,
        "entity_id": entity_id,
        "new_state": new_state,
        "speech": build_verified_speech(
            friendly_name=friendly_name,
            action_name=action_name,
            expected_state=expected_state,
            observed_state=new_state,
            verified=verify["verified"],
            action_phrases=phrases,
        ),
        "executed_command": {
            "domain": domain,
            "service": service,
            "entity_id": entity_id,
            "service_data": service_data,
        },
    }
    if relative:
        # The absolute level was derived from the current volume; replaying
        # it would not repeat "a bit louder".
        result["cacheable"] = False
    return result


# ---------------------------------------------------------------------------
# Streams and media on a target player (play_camera, play_media)
# ---------------------------------------------------------------------------


async def _read_state(ha_client: Any, entity_id: str) -> dict | None:
    """Current HA state object of ``entity_id``, or None when it cannot be read."""
    try:
        state_resp = await ha_client.get_state(entity_id)
    except Exception:
        logger.debug("State read failed for %s", entity_id, exc_info=True)
        return None
    return state_resp if isinstance(state_resp, dict) else None


def _state_of(state_resp: dict | None) -> str | None:
    state = state_resp.get("state") if isinstance(state_resp, dict) else None
    return state.strip().lower() if isinstance(state, str) else None


def _attribute(state_resp: dict | None, key: str) -> Any:
    attrs = state_resp.get("attributes") if isinstance(state_resp, dict) else None
    return attrs.get(key) if isinstance(attrs, dict) else None


def _join_names(names: list[str], conjunction: str = "or") -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" {conjunction} {names[-1]}"


def _refusal(target_id: str, target_state_resp: dict | None, speech: str, reason: str) -> dict:
    """Failure result that names why the request cannot run (no or no further service call)."""
    logger.info("Media playback refused on %s: %s", target_id, reason)
    return {
        "success": False,
        "entity_id": target_id,
        "new_state": target_state_resp.get("state") if isinstance(target_state_resp, dict) else None,
        "speech": speech,
        "cacheable": False,
        "metadata": {"refusal_reason": reason},
    }


def _question(target_id: str, target_state_resp: dict | None, speech: str, reason: str) -> dict:
    """Clarifying question about the content (camera, media) while the target is known."""
    result = _refusal(target_id, target_state_resp, speech, reason)
    result["voice_followup"] = True
    return result


def _player_refusal(
    target_name: str, state_resp: dict | None, *, needs_video: bool, what: str
) -> tuple[str, str] | None:
    """``(reason, speech)`` when the target cannot play the content, else None.

    Only facts HA reports count: ``unavailable``, the ``speaker`` device
    class (audio only) for video content, and a ``supported_features``
    mask without ``PLAY_MEDIA``. A missing mask or device class is unknown,
    not incompatible.
    """
    if _state_of(state_resp) == "unavailable":
        return "target_unavailable", f"{target_name} is unavailable right now, so I cannot play {what} there."
    device_class = _attribute(state_resp, "device_class")
    if needs_video and isinstance(device_class, str) and device_class.lower() == "speaker":
        return "target_audio_only", f"{target_name} is an audio-only speaker and cannot show {what}."
    if lacks_feature(state_resp, _FEATURE_PLAY_MEDIA):
        return (
            "target_lacks_play_media",
            f"{target_name} does not support playing media sent by Home Assistant, so I cannot play {what} there.",
        )
    return None


async def _ensure_player_on(
    ha_client: Any,
    target_id: str,
    target_name: str,
    state_resp: dict | None,
) -> tuple[bool, tuple[str, str] | None]:
    """Power the target on when it is off and wait (bounded) until it reports on.

    Returns ``(powered_on, refusal)``. ``turn_on`` lands in "on", "idle",
    "playing", ... depending on the integration, so any state other than
    off/standby counts as on. The whole step is bounded by
    ``_POWER_ON_WAIT_SEC`` so the stream call still fits into the agent's
    dispatch budget.
    """
    if _state_of(state_resp) not in _OFF_STATES:
        return False, None
    if lacks_feature(state_resp, _FEATURE_TURN_ON):
        return False, ("target_off_no_turn_on", f"{target_name} is off and cannot be turned on by Home Assistant.")
    deadline = time.monotonic() + _POWER_ON_WAIT_SEC
    verify = await call_service_with_verification(
        ha_client,
        "media_player",
        "turn_on",
        target_id,
        expected_state=None,
        ws_timeout=_POWER_ON_WS_TIMEOUT_SEC,
        poll_max=_POWER_ON_POLL_SEC,
    )
    if not verify["success"]:
        if verify.get("approval"):
            return False, ("target_turn_on_held", failure_speech("turn_on", target_name, verify))
        return False, ("target_turn_on_failed", f"I could not turn on {target_name}.")
    observed = verify.get("observed_state")
    current = observed.strip().lower() if isinstance(observed, str) else None
    while (current is None or current in _OFF_STATES) and time.monotonic() < deadline:
        await asyncio.sleep(_POWER_ON_POLL_SEC)
        latest = await _read_state(ha_client, target_id)
        if latest is not None:
            current = _state_of(latest)
    if current in _OFF_STATES or current == "unavailable":
        return True, (
            "target_not_on_in_time",
            f"I turned on {target_name}, but it did not report being on within {int(_POWER_ON_WAIT_SEC)} seconds. "
            "Please try again in a moment.",
        )
    return True, None


async def _play_camera_on_player(
    ha_client: Any,
    *,
    camera_id: str,
    camera_name: str,
    target_id: str,
    target_name: str,
    target_state_resp: dict | None,
) -> dict:
    """Show a camera's live stream on a media player via ``camera.play_stream``."""
    what = f"the camera {camera_name}"
    camera_state = await _read_state(ha_client, camera_id)
    if _state_of(camera_state) == "unavailable":
        return _refusal(
            target_id, target_state_resp, f"The camera {camera_name} is unavailable right now.", "camera_unavailable"
        )
    if lacks_feature(camera_state, _CAMERA_FEATURE_STREAM):
        return _refusal(
            target_id,
            target_state_resp,
            f"The camera {camera_name} does not provide a video stream, so it cannot be shown on {target_name}.",
            "camera_no_stream",
        )
    refusal = _player_refusal(target_name, target_state_resp, needs_video=True, what=what)
    if refusal is not None:
        return _refusal(target_id, target_state_resp, refusal[1], refusal[0])

    powered_on, refusal = await _ensure_player_on(ha_client, target_id, target_name, target_state_resp)
    if refusal is not None:
        return _refusal(target_id, target_state_resp, refusal[1], refusal[0])

    service_data = {"media_player": target_id, "format": _STREAM_FORMAT}
    verify = await call_service_with_verification(
        ha_client,
        "camera",
        "play_stream",
        camera_id,
        service_data=service_data,
        expected_state=None,
        ws_timeout=_STREAM_VERIFY_WS_TIMEOUT_SEC,
        poll_max=_STREAM_VERIFY_WS_TIMEOUT_SEC,
    )
    prefix = f"I turned on {target_name}. " if powered_on else ""
    if not verify["success"]:
        if verify.get("approval"):
            speech = failure_speech("play_camera", camera_name, verify)
        else:
            speech = (
                f"Sorry, Home Assistant could not start the stream of {camera_name} on {target_name}. "
                "The player may not play HLS video streams, or the Home Assistant stream integration is not set up."
            )
        return _refusal(target_id, target_state_resp, prefix + speech, "stream_failed")

    speech = (
        f"Done, I turned on {target_name} and {what} is now showing there."
        if powered_on
        else f"Done, {what} is now showing on {target_name}."
    )
    return {
        "success": True,
        "action": "play_camera",
        "entity_id": target_id,
        "new_state": None,
        "speech": speech,
        # A live stream is never replayed from the action cache.
        "cacheable": False,
        "metadata": {"camera_entity_id": camera_id, "powered_on": powered_on},
        "executed_command": {
            "domain": "camera",
            "service": "play_stream",
            "entity_id": camera_id,
            "service_data": service_data,
        },
    }


def _visible_cameras(resolution: dict) -> list[Any]:
    entries = resolution.get("_visible_entries") if isinstance(resolution, dict) else None
    return [entry for entry in entries or [] if getattr(entry, "domain", None) in _CAMERA_DOMAINS]


def _camera_names(entries: list[Any]) -> list[str]:
    names = {str(getattr(entry, "friendly_name", "") or getattr(entry, "entity_id", "")) for entry in entries}
    return sorted(name for name in names if name)


def _camera_choice_names(query: str, resolution: dict) -> list[str]:
    """Names of the cameras an ambiguous camera query matched (for the clarifying question)."""
    metadata = resolution.get("metadata") if isinstance(resolution, dict) else None
    candidates = metadata.get("candidate_entities") if isinstance(metadata, dict) else None
    names = sorted(
        {
            str(candidate["friendly_name"])
            for candidate in candidates or []
            if isinstance(candidate, dict)
            and candidate.get("friendly_name")
            and _validate_camera_domain(str(candidate.get("entity_id") or ""))
        }
    )
    if len(names) < 2:
        query_tokens = normalize_tokenize(query)
        names = [
            name
            for name in _camera_names(_visible_cameras(resolution))
            if any(token_in_query(token, query_tokens) for token in normalize_tokenize(name))
        ]
    return names[:_MAX_LISTED_CAMERAS]


async def _resolve_camera(
    action: dict,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    *,
    target_id: str,
    target_name: str,
    target_state: str | None,
    span_collector=None,
) -> dict:
    """Resolve the stream source camera of ``play_camera``.

    Same shared deterministic-first resolver as every other target
    (Directive 4: exact id, exact name, alias, containment, then hybrid with
    ``preferred_domains`` from the camera domain; visibility on every stage;
    a camera entity_id written as the name only matches the exact entity_id
    stage, so an LLM-proposed id is validation input, never a selection).
    Cameras are not in the agent's candidate list, so there is no direct
    camera entity_id and the candidate gate does not apply. Returns
    ``{"entity_id", "friendly_name", "result": None}`` or ``{"result": ...}``
    with a clarifying question (unknown, ambiguous or missing camera).
    """
    target_state_resp = {"state": target_state}
    params = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
    raw_query = params.get("camera")
    query = raw_query.strip() if isinstance(raw_query, str) else ""
    if not query:
        return {
            "result": _question(
                target_id, target_state_resp, f"Which camera should I show on {target_name}?", "camera_missing"
            )
        }

    resolved = await resolve_and_validate_entity(
        query,
        entity_index,
        entity_matcher,
        agent_id,
        _CAMERA_DOMAINS,
        _validate_camera_domain,
        span_collector=span_collector,
    )
    if resolved.get("not_found_result") is None and resolved.get("entity_id"):
        return {"entity_id": resolved["entity_id"], "friendly_name": resolved["friendly_name"], "result": None}

    resolution = resolved.get("resolution") or {}
    metadata = resolution.get("metadata") or {}
    if str(metadata.get("resolution_path") or "").endswith("_ambiguous"):
        names = _camera_choice_names(query, resolution)
        if len(names) >= 2:
            speech = f"Several cameras match '{query}': {_join_names(names)}. Which one should I show?"
        else:
            speech = f"Several cameras match '{query}'. Which one should I show?"
        return {"result": _question(target_id, target_state_resp, speech, "camera_ambiguous")}

    available = _camera_names(_visible_cameras(resolution))
    speech = f"I could not find a camera called '{query}'."
    if available and len(available) <= _MAX_LISTED_CAMERAS:
        speech += f" Available cameras: {_join_names(available, 'and')}. Which one should I show?"
    else:
        speech += " Which camera should I show?"
    return {"result": _question(target_id, target_state_resp, speech, "camera_not_found")}


_EXACT_RESOLUTION_PATHS: frozenset[str] = frozenset({"exact_entity_id", "exact_friendly_name", "exact_alias"})


async def _exact_camera_for_content(
    content_id: str,
    entity_index: Any,
    agent_id: str | None,
    span_collector=None,
) -> dict | None:
    """The visible camera a ``media_content_id`` names exactly, else None.

    Guards ``play_media`` calls whose content id is a camera's entity_id or
    name (a live failure: HA cannot play a camera name as media). Only the
    exact deterministic stages count; no fuzzy matcher runs (``None``
    matcher) and visibility applies as on every resolution path.
    """
    if not content_id or "://" in content_id or content_id.startswith("/"):
        return None
    resolved = await resolve_and_validate_entity(
        content_id,
        entity_index,
        None,
        agent_id,
        _CAMERA_DOMAINS,
        _validate_camera_domain,
        span_collector=span_collector,
    )
    if not resolved.get("entity_id"):
        return None
    metadata = (resolved.get("resolution") or {}).get("metadata") or {}
    if metadata.get("resolution_path") not in _EXACT_RESOLUTION_PATHS:
        return None
    return {"entity_id": resolved["entity_id"], "friendly_name": resolved["friendly_name"]}


def _needs_video(content_type: str) -> bool:
    lowered = content_type.strip().lower()
    return lowered in _VIDEO_CONTENT_TYPES or lowered.startswith(_VIDEO_MIME_PREFIXES)


async def _play_media_on_player(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    agent_id: str | None,
    *,
    target_id: str,
    target_name: str,
    target_state_resp: dict | None,
    span_collector=None,
) -> dict:
    """``media_player.play_media`` with compatibility checks and power-on."""
    service_data = _build_media_service_data(action)
    content_id = str(service_data.get("media_content_id") or "").strip()
    content_type = str(service_data.get("media_content_type") or "").strip()
    if not content_id:
        return _question(target_id, target_state_resp, f"What should I play on {target_name}?", "content_missing")

    camera = await _exact_camera_for_content(content_id, entity_index, agent_id, span_collector)
    if camera is not None:
        logger.info("play_media content names the camera %s; streaming it to %s", camera["entity_id"], target_id)
        return await _play_camera_on_player(
            ha_client,
            camera_id=camera["entity_id"],
            camera_name=camera["friendly_name"],
            target_id=target_id,
            target_name=target_name,
            target_state_resp=target_state_resp,
        )

    if not content_type:
        return _refusal(
            target_id,
            target_state_resp,
            f"I could not tell which kind of media to play on {target_name}, so I did not start anything.",
            "content_type_missing",
        )
    needs_video = _needs_video(content_type)
    refusal = _player_refusal(
        target_name, target_state_resp, needs_video=needs_video, what="that video" if needs_video else "that media"
    )
    if refusal is not None:
        return _refusal(target_id, target_state_resp, refusal[1], refusal[0])

    powered_on, refusal = await _ensure_player_on(ha_client, target_id, target_name, target_state_resp)
    if refusal is not None:
        return _refusal(target_id, target_state_resp, refusal[1], refusal[0])

    verify = await call_service_with_verification(
        ha_client,
        "media_player",
        "play_media",
        target_id,
        service_data=service_data,
        expected_state=None,
        previous_state=None if powered_on else verification_previous_state(target_state_resp),
    )
    prefix = f"I turned on {target_name}. " if powered_on else ""
    if not verify["success"]:
        if verify.get("approval") or verify.get("outcome") != "error":
            speech = failure_speech("play_media", target_name, verify)
        else:
            speech = (
                f"Sorry, {target_name} could not play that media. Home Assistant rejected the request; "
                "the player may not support this content or format."
            )
        return _refusal(target_id, target_state_resp, prefix + speech, "play_media_failed")

    new_state = verify["observed_state"]
    speech = (
        f"Done, I turned on {target_name} and started playback."
        if powered_on
        else build_verified_speech(
            friendly_name=target_name,
            action_name="play_media",
            expected_state=None,
            observed_state=new_state,
            verified=verify["verified"],
            action_phrases=_ACTION_PHRASES,
        )
    )
    result: dict[str, Any] = {
        "success": True,
        "action": "play_media",
        "entity_id": target_id,
        "new_state": new_state,
        "speech": speech,
        "executed_command": {
            "domain": "media_player",
            "service": "play_media",
            "entity_id": target_id,
            "service_data": service_data,
        },
    }
    if powered_on:
        # A replay would skip the power-on step.
        result["cacheable"] = False
    return result


# ---------------------------------------------------------------------------
# Read-only media action handlers
# ---------------------------------------------------------------------------


def _format_media_state(entity_id: str, state_resp: dict) -> str:
    state = state_resp.get("state", "unknown")
    attrs = state_resp.get("attributes", {})
    friendly_name = attrs.get("friendly_name", entity_id)

    parts = [f"{friendly_name} is {state}"]
    if state in ("playing", "paused", "on"):
        title = attrs.get("media_title")
        content_type = attrs.get("media_content_type")
        if title:
            parts.append(f'playing "{title}"')
        if content_type:
            parts.append(f"type {content_type}")
        app_name = attrs.get("app_name")
        if app_name:
            parts.append(f"app {app_name}")
    source = attrs.get("source")
    if source:
        parts.append(f"source {source}")
    volume = attrs.get("volume_level")
    if volume is not None:
        parts.append(f"volume {round(float(volume) * 100)}%")
    muted = attrs.get("is_volume_muted")
    if muted:
        parts.append("muted")
    return ", ".join(parts) + "."


async def _query_media_state(
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    preferred_area_id: str | None = None,
    action: dict | None = None,
) -> dict:
    resolved = await resolve_and_validate_entity(
        entity_query,
        entity_index,
        entity_matcher,
        agent_id,
        _ACTION_DOMAINS,
        _validate_domain,
        preferred_area_id=preferred_area_id,
        span_collector=span_collector,
        direct_entity_id=action.get("entity_id") if action else None,
    )
    if resolved["not_found_result"] is not None:
        result = resolved["not_found_result"]
        result["cacheable"] = False
        return result
    entity_id = resolved["entity_id"]
    resolution_metadata = resolved["resolution"].get("metadata", {})

    try:
        state_resp = await ha_client.get_state(entity_id)
        if not state_resp:
            return {
                "success": False,
                "entity_id": entity_id,
                "new_state": None,
                "speech": f"Could not retrieve state for {entity_id}.",
                "cacheable": False,
                "metadata": resolution_metadata,
            }
        speech = _format_media_state(entity_id, state_resp)
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": state_resp.get("state"),
            "speech": speech,
            "cacheable": False,
            "metadata": resolution_metadata,
        }
    except Exception:
        logger.error("State query failed for %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": "Sorry, I could not read the media player status.",
            "cacheable": False,
            "metadata": resolution_metadata,
        }


async def _list_media_players(ha_client: Any, agent_id: str | None = None, entity_index: Any = None) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception:
        logger.error("Failed to fetch states for list_media_players", exc_info=True)
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": "Sorry, I could not list media players.",
        }

    players = [s for s in states if s.get("entity_id", "").startswith("media_player.")]
    if agent_id and entity_index is not None:
        visibility = await asyncio.gather(
            *[entity_is_visible(agent_id, s.get("entity_id", ""), entity_index) for s in players]
        )
        players = [s for s, ok in zip(players, visibility, strict=True) if ok]

    if not players:
        return {"success": True, "entity_id": "", "new_state": None, "speech": "No media players found."}

    lines = []
    for p in players:
        attrs = p.get("attributes", {})
        name = attrs.get("friendly_name", p.get("entity_id", ""))
        state = p.get("state", "unknown")
        source = attrs.get("source")
        info = f"{name}: {state}"
        if source:
            info += f" (source: {source})"
        if state in ("playing", "paused"):
            title = attrs.get("media_title")
            if title:
                info += f' - "{title}"'
        lines.append(info)

    speech = "Media players: " + "; ".join(lines) + "."
    return {"success": True, "entity_id": "", "new_state": None, "speech": speech, "cacheable": False}


async def _handle_media_read_action(
    action_name: str,
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    preferred_area_id: str | None = None,
    action: dict | None = None,
) -> dict:
    if action_name == "query_media_state":
        return await _query_media_state(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            action=action,
        )
    if action_name == "list_media_players":
        return await _list_media_players(ha_client, agent_id=agent_id, entity_index=entity_index)
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}
