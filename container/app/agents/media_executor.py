"""Media-player action execution via HA media_player services."""

from __future__ import annotations

import asyncio
import logging
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

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"media_player"})

# FLOW-DOMAIN-1 (0.19.2): all media actions target media_player.* entities.
_ACTION_DOMAINS: frozenset[str] = frozenset({"media_player"})


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


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
    if action_name in ("query_media_state", "list_media_players"):
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
    if not mapping:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"Unknown action: {action_name}",
        }

    domain, service = mapping

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

    try:
        state_resp = await ha_client.get_state(entity_id)
    except Exception:
        logger.debug("Pre-action state read failed for %s", entity_id, exc_info=True)
        state_resp = None
    current_state = state_resp.get("state") if isinstance(state_resp, dict) else None

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
