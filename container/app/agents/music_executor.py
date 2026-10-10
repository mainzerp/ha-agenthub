"""Music-specific action execution via Music Assistant and media_player services."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agents.action_executor import (
    build_verified_speech,
    call_service_with_verification,
    resolve_and_validate_entity,
)
from app.agents.executor_state_check import failure_speech, lacks_feature
from app.agents.media_executor import normalize_volume_level
from app.entity.visibility import entity_is_visible
from app.ha_client.rest import allow_internal_ha_service_calls
from app.models.agent import TaskContext

logger = logging.getLogger(__name__)

_MUSIC_ACTION_MAP: dict[str, tuple[str, str]] = {
    "play_media": ("music_assistant", "play_media"),
    "search": ("music_assistant", "search"),
    "volume_set": ("media_player", "volume_set"),
    "volume_up": ("media_player", "volume_up"),
    "volume_down": ("media_player", "volume_down"),
    "media_play": ("media_player", "media_play"),
    "media_pause": ("media_player", "media_pause"),
    "media_next_track": ("media_player", "media_next_track"),
    "media_previous_track": ("media_player", "media_previous_track"),
    "shuffle_set": ("media_player", "shuffle_set"),
    "repeat_set": ("media_player", "repeat_set"),
}

# FLOW-VERIFY-SHARED (0.18.5): transport actions land in deterministic
# media_player states. Search is a read-only service and handled below.
_EXPECTED_STATE_BY_ACTION: dict[str, str] = {
    "media_play": "playing",
    "media_pause": "paused",
    "play_media": "playing",
}

_ACTION_PHRASES: dict[str, str] = {
    "volume_set": "volume updated",
    "volume_up": "volume turned up",
    "volume_down": "volume turned down",
    "media_next_track": "skipped to the next track",
    "media_previous_track": "skipped to the previous track",
    "shuffle_set": "shuffle updated",
    "repeat_set": "repeat mode updated",
}

# HA ``MediaPlayerEntityFeature`` bits for the volume actions (any of them).
_FEATURE_VOLUME_SET = 4
_FEATURE_VOLUME_STEP = 1024
_REQUIRED_FEATURES: dict[str, tuple[int, str]] = {
    "volume_set": (_FEATURE_VOLUME_SET, "setting the volume"),
    "volume_up": (_FEATURE_VOLUME_SET | _FEATURE_VOLUME_STEP, "changing the volume"),
    "volume_down": (_FEATURE_VOLUME_SET | _FEATURE_VOLUME_STEP, "changing the volume"),
}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"media_player"})

# FLOW-DOMAIN-1 (0.19.2): music_assistant.* services still target a media_player.*
# entity_id, so all music actions resolve into the media_player domain.
_ACTION_DOMAINS: frozenset[str] = frozenset({"media_player"})


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _build_music_service_data(action: dict) -> dict[str, Any]:
    """Build HA service_data from a music action's parameters."""
    params = action.get("parameters") or {}
    action_name = action.get("action", "")
    data: dict[str, Any] = {}

    if action_name == "play_media":
        if "media_id" in params:
            data["media_id"] = params["media_id"]
        if "media_type" in params:
            data["media_type"] = params["media_type"]
        if "enqueue" in params:
            data["enqueue"] = params["enqueue"]
        if "artist" in params:
            data["artist"] = params["artist"]
        if "album" in params:
            data["album"] = params["album"]
        if "radio_mode" in params:
            data["radio_mode"] = bool(params["radio_mode"])
    elif action_name == "search":
        if "name" in params:
            data["name"] = params["name"]
        if "media_type" in params:
            data["media_type"] = params["media_type"]
        if "limit" in params:
            data["limit"] = int(params["limit"])
        if "artist" in params:
            data["artist"] = params["artist"]
        if "album" in params:
            data["album"] = params["album"]
        if "library_only" in params:
            data["library_only"] = bool(params["library_only"])
    elif action_name == "volume_set":
        if "volume_level" in params:
            data["volume_level"] = normalize_volume_level(params["volume_level"])
    elif action_name == "shuffle_set":
        if "shuffle" in params:
            data["shuffle"] = bool(params["shuffle"])
    elif action_name == "repeat_set":
        if "repeat" in params:
            data["repeat"] = params["repeat"]

    return data


def _format_search_results(results: Any) -> str:
    """Format search results from music_assistant.search into readable speech."""
    if not results:
        return "No results found for that search."

    if isinstance(results, dict):
        items = results.get("items") or results.get("result") or []
        if not items:
            # Music Assistant groups results per media type
            # ({"artists": [...], "tracks": [...], ...}).
            for value in results.values():
                if isinstance(value, list):
                    items = [*items, *value]
    elif isinstance(results, list):
        items = results
    else:
        return "No results found for that search."

    if not items:
        return "No results found for that search."

    lines = []
    for i, item in enumerate(items[:10], 1):
        if isinstance(item, dict):
            name = item.get("name") or item.get("title") or "Unknown"
            artist = item.get("artist") or item.get("artists") or ""
            if isinstance(artist, list):
                artist = ", ".join(str(a.get("name", "")) if isinstance(a, dict) else str(a) for a in artist if a)
            if artist:
                lines.append(f"{i}. {name} by {artist}")
            else:
                lines.append(f"{i}. {name}")
        else:
            lines.append(f"{i}. {item}")

    return "I found: " + "; ".join(lines) + "."


def _relative_volume_level(state_resp: Any, delta: float) -> float | None:
    """Absolute volume (0.0-1.0) for a relative change, or None when unknown."""
    attrs = state_resp.get("attributes") if isinstance(state_resp, dict) else None
    current = attrs.get("volume_level") if isinstance(attrs, dict) else None
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        return None
    return round(min(1.0, max(0.0, float(current) + delta)), 2)


async def execute_music_action(
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
    """Resolve an entity, call a music HA service, and verify the result.

    Args:
        action: Parsed action dict with "action", "entity", and optional "parameters".
        ha_client: HARestClient instance.
        entity_index: EntityIndex instance.
        entity_matcher: EntityMatcher instance.
        preferred_area_id: Origin area of the request; used as the area
            tie-breaker when resolving the target speaker.

    Returns:
        dict with "success", "entity_id", "new_state", "speech" and, for
        executed writes, "executed_command" (the exact HA call).
    """
    action_name = action.get("action", "").lower()
    entity_query = action.get("entity", "")

    # Read-only actions (no service call)
    if action_name in ("query_music_state", "list_music_players"):
        return await _handle_music_read_action(
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

    # Validate action name
    mapping = _MUSIC_ACTION_MAP.get(action_name)
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

    # Build service data
    try:
        service_data = _build_music_service_data(action)
    except (TypeError, ValueError):
        logger.warning("Invalid music parameters for %s: %r", entity_id, action.get("parameters"))
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"I could not understand the requested value for {friendly_name}.",
            "cacheable": False,
        }

    # Special case: search is read-only and returns its results as speech.
    if action_name == "search":
        try:
            with allow_internal_ha_service_calls(f"music-search:{agent_id or 'unknown'}"):
                results = await ha_client.call_service(
                    domain, service, entity_id, service_data or None, return_response=True
                )
        except Exception:
            logger.error("Search service call failed on %s", entity_id, exc_info=True)
            return {
                "success": False,
                "entity_id": entity_id,
                "new_state": None,
                "speech": failure_speech("search", friendly_name),
                "cacheable": False,
            }
        return {
            "success": True,
            "action": action_name,
            "entity_id": entity_id,
            "new_state": None,
            "speech": _format_search_results(results),
            "cacheable": False,
        }

    state_resp: Any = None
    relative = False
    params = action.get("parameters") or {}
    needs_state = action_name in _REQUIRED_FEATURES
    if needs_state:
        try:
            state_resp = await ha_client.get_state(entity_id)
        except Exception:
            logger.debug("Pre-action state read failed for %s", entity_id, exc_info=True)
            state_resp = None

    if action_name == "volume_set" and "volume_level" not in service_data and isinstance(params, dict):
        delta = params.get("volume_delta")
        if isinstance(delta, (int, float)) and not isinstance(delta, bool):
            level = _relative_volume_level(state_resp, float(delta))
            if level is None:
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "new_state": None,
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
            "new_state": state_resp.get("state") if isinstance(state_resp, dict) else None,
            "speech": f"{friendly_name} does not support {required[1]}.",
            "cacheable": False,
        }

    expected_state = _EXPECTED_STATE_BY_ACTION.get(action_name)
    verify = await call_service_with_verification(
        ha_client,
        domain,
        service,
        entity_id,
        service_data=service_data,
        expected_state=expected_state,
    )
    if not verify["success"]:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": failure_speech(action_name, friendly_name),
        }

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
            action_phrases=_ACTION_PHRASES,
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
# Read-only music action handlers
# ---------------------------------------------------------------------------


def _format_music_player_state(entity_id: str, state_resp: dict) -> str:
    state = state_resp.get("state", "unknown")
    attrs = state_resp.get("attributes", {})
    friendly_name = attrs.get("friendly_name", entity_id)

    parts = [f"{friendly_name} is {state}"]
    if state in ("playing", "paused"):
        title = attrs.get("media_title")
        artist = attrs.get("media_artist")
        album = attrs.get("media_album")
        if title:
            parts.append(f'track "{title}"')
        if artist:
            parts.append(f"by {artist}")
        if album:
            parts.append(f"from {album}")
    volume = attrs.get("volume_level")
    if volume is not None:
        parts.append(f"volume {round(float(volume) * 100)}%")
    shuffle = attrs.get("shuffle")
    if shuffle is not None:
        parts.append(f"shuffle {'on' if shuffle else 'off'}")
    source = attrs.get("source")
    if source:
        parts.append(f"source {source}")
    return ", ".join(parts) + "."


async def _query_music_state(
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
        speech = _format_music_player_state(entity_id, state_resp)
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
            "speech": "Sorry, I could not read the music player status.",
            "cacheable": False,
            "metadata": resolution_metadata,
        }


async def _list_music_players(ha_client: Any, agent_id: str | None = None, entity_index: Any = None) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception:
        logger.error("Failed to fetch states for list_music_players", exc_info=True)
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": "Sorry, I could not list the music players.",
            "cacheable": False,
        }

    players = [s for s in states if s.get("entity_id", "").startswith("media_player.")]
    if agent_id and entity_index is not None:
        visibility = await asyncio.gather(
            *[entity_is_visible(agent_id, s.get("entity_id", ""), entity_index) for s in players]
        )
        players = [s for s, ok in zip(players, visibility, strict=True) if ok]

    if not players:
        return {"success": True, "entity_id": "", "new_state": None, "speech": "No music players found."}

    lines = []
    for p in players:
        attrs = p.get("attributes", {})
        name = attrs.get("friendly_name", p.get("entity_id", ""))
        state = p.get("state", "unknown")
        info = f"{name}: {state}"
        if state in ("playing", "paused"):
            title = attrs.get("media_title")
            artist = attrs.get("media_artist")
            if title:
                info += f' - "{title}"'
                if artist:
                    info += f" by {artist}"
        lines.append(info)

    speech = "Music players: " + "; ".join(lines) + "."
    return {"success": True, "entity_id": "", "new_state": None, "speech": speech, "cacheable": False}


async def _handle_music_read_action(
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
    if action_name == "query_music_state":
        return await _query_music_state(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            action=action,
        )
    if action_name == "list_music_players":
        return await _list_music_players(ha_client, agent_id=agent_id, entity_index=entity_index)
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}
