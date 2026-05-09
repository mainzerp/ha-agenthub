"""Music-specific action execution via Music Assistant and media_player services."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.action_executor import (
    build_verified_speech,
    call_service_with_verification,
)
from app.analytics.tracer import _optional_span
from app.entity.deterministic_resolver import resolve_entity_deterministic_first

logger = logging.getLogger(__name__)

_MUSIC_ACTION_MAP: dict[str, tuple[str, str]] = {
    "play_media": ("music_assistant", "play_media"),
    "search": ("music_assistant", "search"),
    "volume_set": ("media_player", "volume_set"),
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
    "media_next_track": "skipped to the next track",
    "media_previous_track": "skipped to the previous track",
    "shuffle_set": "shuffle updated",
    "repeat_set": "repeat mode updated",
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
            data["volume_level"] = float(params["volume_level"])
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
                artist = ", ".join(str(a) for a in artist)
            if artist:
                lines.append(f"{i}. {name} by {artist}")
            else:
                lines.append(f"{i}. {name}")
        else:
            lines.append(f"{i}. {item}")

    return "I found: " + "; ".join(lines) + "."


async def execute_music_action(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None = None,
    span_collector=None,
    *,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Resolve an entity, call a music HA service, and verify the result.

    Args:
        action: Parsed action dict with "action", "entity", and optional "parameters".
        ha_client: HARestClient instance.
        entity_index: EntityIndex instance.
        entity_matcher: EntityMatcher instance.

    Returns:
        dict with "success", "entity_id", "new_state", and "speech".
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
            verbatim_terms=verbatim_terms,
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

    resolution = {
        "entity_id": None,
        "friendly_name": entity_query,
        "speech": None,
        "metadata": {"query": entity_query, "match_count": 0, "resolution_path": "not_attempted"},
    }
    try:
        if entity_index or entity_matcher:
            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    agent_id,
                    allowed_domains=_ACTION_DOMAINS,
                    verbatim_terms=verbatim_terms,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    if entity_id and not _validate_domain(entity_id):
        logger.warning("Resolved entity %s not in allowed domains %s", entity_id, _ALLOWED_DOMAINS)
        entity_id = None

    if not entity_id:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution["speech"] or f"Could not find an entity matching '{entity_query}'.",
        }

    # Build service data
    service_data = _build_music_service_data(action)

    # Special case: search returns speech with results
    if action_name == "search":
        try:
            results = await ha_client.call_service(domain, service, entity_id, service_data or None)
            speech = _format_search_results(results)
            return {
                "success": True,
                "action": action_name,
                "entity_id": entity_id,
                "new_state": None,
                "speech": speech,
            }
        except Exception as exc:
            logger.error("Search service call failed on %s", entity_id, exc_info=True)
            return {
                "success": False,
                "entity_id": entity_id,
                "new_state": None,
                "speech": f"Failed to search on {friendly_name}: {exc}",
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
            "speech": f"Failed to execute {action_name} on {friendly_name}: {verify['error']}",
        }

    new_state = verify["observed_state"]
    return {
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
    }


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
    verbatim_terms: list[str] | None = None,
) -> dict:
    resolution = {
        "entity_id": None,
        "friendly_name": entity_query,
        "speech": None,
        "metadata": {"query": entity_query, "match_count": 0, "resolution_path": "not_attempted"},
    }
    try:
        if entity_index or entity_matcher:
            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    agent_id,
                    allowed_domains=_ACTION_DOMAINS,
                    verbatim_terms=verbatim_terms,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = resolution["entity_id"]
    resolution["friendly_name"]
    if entity_id and not _validate_domain(entity_id):
        logger.warning("Resolved entity %s not in allowed domains %s", entity_id, _ALLOWED_DOMAINS)
        entity_id = None

    if not entity_id:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution["speech"] or f"Could not find an entity matching '{entity_query}'.",
            "cacheable": False,
        }

    try:
        state_resp = await ha_client.get_state(entity_id)
        if not state_resp:
            return {
                "success": False,
                "entity_id": entity_id,
                "new_state": None,
                "speech": f"Could not retrieve state for {entity_id}.",
                "cacheable": False,
            }
        speech = _format_music_player_state(entity_id, state_resp)
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": state_resp.get("state"),
            "speech": speech,
            "cacheable": False,
        }
    except Exception as exc:
        logger.error("State query failed for %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to query music player status: {exc}",
            "cacheable": False,
        }


async def _list_music_players(ha_client: Any) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception as exc:
        logger.error("Failed to fetch states for list_music_players", exc_info=True)
        return {"success": False, "entity_id": "", "new_state": None, "speech": f"Failed to list music players: {exc}"}

    players = [s for s in states if s.get("entity_id", "").startswith("media_player.")]

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
    verbatim_terms: list[str] | None = None,
) -> dict:
    if action_name == "query_music_state":
        return await _query_music_state(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "list_music_players":
        return await _list_music_players(ha_client)
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}
