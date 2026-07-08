"""Media-player action execution via HA media_player services."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.action_executor import (
    _ensure_str,
    _synthesize_direct_entity_metadata,
    _validate_direct_entity_id,
    build_verified_speech,
    call_service_with_verification,
)
from app.agents.executor_state_check import _state_matches
from app.analytics.tracer import _optional_span
from app.entity.deterministic_resolver import resolve_entity_deterministic_first

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
    "mute": "muted",
    "next_track": "skipped to the next track",
    "previous_track": "skipped to the previous track",
    "select_source": "source selected",
    "play_media": "playback started",
}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"media_player"})

# FLOW-DOMAIN-1 (0.19.2): all media actions target media_player.* entities.
_ACTION_DOMAINS: frozenset[str] = frozenset({"media_player"})


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _build_media_service_data(action: dict) -> dict[str, Any]:
    """Build HA service_data from a media action's parameters."""
    params = action.get("parameters") or {}
    action_name = action.get("action", "")
    data: dict[str, Any] = {}

    if action_name == "set_volume":
        if "volume_level" in params:
            data["volume_level"] = float(params["volume_level"])
    elif action_name == "mute":
        if "is_volume_muted" in params:
            data["is_volume_muted"] = bool(params["is_volume_muted"])
    elif action_name == "select_source":
        if "source" in params:
            data["source"] = str(params["source"])
    elif action_name == "play_media":
        if "media_content_id" in params:
            data["media_content_id"] = str(params["media_content_id"])
        if "media_content_type" in params:
            data["media_content_type"] = str(params["media_content_type"])

    return data


async def execute_media_action(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None = None,
    span_collector=None,
    *,
    preferred_area_id: str | None = None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Resolve an entity, call a media_player HA service, and verify the result.

    Args:
        action: Parsed action dict with "action", "entity", and optional "parameters".
        ha_client: HARestClient instance.
        entity_index: EntityIndex instance.
        entity_matcher: EntityMatcher instance.
        agent_id: Optional agent identifier for entity matching context.

    Returns:
        dict with "success", "entity_id", "new_state", and "speech".
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
            verbatim_terms=verbatim_terms,
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
                    preferred_area_id=preferred_area_id,
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

    # Deterministic skip: if already in target state, do not call HA.
    try:
        state_resp = await ha_client.get_state(entity_id)
        current_state = state_resp.get("state") if isinstance(state_resp, dict) else None
    except Exception:
        current_state = None
    if _state_matches(action_name, current_state):
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": current_state,
            "speech": f"Done, {friendly_name} is already {current_state}.",
        }

    service_data = _build_media_service_data(action)

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
    verbatim_terms: list[str] | None = None,
    action: dict | None = None,
) -> dict:
    entity_id_direct = _validate_direct_entity_id(action.get("entity_id") if action else None, _validate_domain)
    if entity_id_direct:
        entity_id = entity_id_direct
        resolution_metadata = _synthesize_direct_entity_metadata(entity_id, entity_index)
    else:
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
                        preferred_area_id=preferred_area_id,
                        verbatim_terms=verbatim_terms,
                    )
                    em_span["metadata"] = resolution["metadata"]
        except Exception:
            logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

        entity_id = _ensure_str(resolution["entity_id"])
        if entity_id and not _validate_domain(entity_id):
            logger.warning("Resolved entity %s not in allowed domains %s", entity_id, _ALLOWED_DOMAINS)
            entity_id = None
        resolution_metadata = resolution.get("metadata", {})

    if not entity_id:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution.get("speech") or f"Could not find an entity matching '{entity_query}'.",
            "cacheable": False,
            "metadata": resolution_metadata,
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
    except Exception as exc:
        logger.error("State query failed for %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to query media player status: {exc}",
            "cacheable": False,
            "metadata": resolution_metadata,
        }


async def _list_media_players(ha_client: Any) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception as exc:
        logger.error("Failed to fetch states for list_media_players", exc_info=True)
        return {"success": False, "entity_id": "", "new_state": None, "speech": f"Failed to list media players: {exc}"}

    players = [s for s in states if s.get("entity_id", "").startswith("media_player.")]

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
    verbatim_terms: list[str] | None = None,
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
            verbatim_terms=verbatim_terms,
            action=action,
        )
    if action_name == "list_media_players":
        return await _list_media_players(ha_client)
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}
