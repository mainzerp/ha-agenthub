"""Scene-specific action execution via HA scene services."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.action_executor import (
    _ensure_str,
    call_service_with_verification,
)
from app.analytics.tracer import _optional_span
from app.entity.deterministic_resolver import resolve_entity_deterministic_first

logger = logging.getLogger(__name__)

_SCENE_ACTION_MAP: dict[str, tuple[str, str]] = {
    "activate_scene": ("scene", "turn_on"),
}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"scene"})


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _build_scene_service_data(action: dict) -> dict[str, Any]:
    """Build HA service_data from a scene action's parameters."""
    params = action.get("parameters") or {}
    data: dict[str, Any] = {}

    if "transition" in params:
        data["transition"] = float(params["transition"])

    return data


async def execute_scene_action(
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
    """Resolve an entity, call a scene HA service, and verify the result.

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
    if action_name in ("query_scene", "list_scenes"):
        return await _handle_scene_read_action(
            action_name,
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            verbatim_terms=verbatim_terms,
        )

    # Validate action name
    mapping = _SCENE_ACTION_MAP.get(action_name)
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
                    allowed_domains=_ALLOWED_DOMAINS,
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

    # Build service data
    service_data = _build_scene_service_data(action)

    # FLOW-VERIFY-SHARED (0.18.5): scene.* state is the ISO timestamp of
    # the last activation, not a semantic state -- observing *any* change
    # means the scene fired. No expected_state; speech is intent-first.
    verify = await call_service_with_verification(
        ha_client,
        domain,
        service,
        entity_id,
        service_data=service_data,
        expected_state=None,
    )
    if not verify["success"]:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to execute {action_name} on {friendly_name}: {verify['error']}",
        }

    return {
        "success": True,
        "action": action_name,
        "entity_id": entity_id,
        "new_state": verify["observed_state"],
        "speech": f"Done, {friendly_name} has been activated.",
    }


# ---------------------------------------------------------------------------
# Read-only scene action handlers
# ---------------------------------------------------------------------------


async def _query_scene(
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    preferred_area_id: str | None = None,
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
                    allowed_domains=_ALLOWED_DOMAINS,
                    preferred_area_id=preferred_area_id,
                    verbatim_terms=verbatim_terms,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = _ensure_str(resolution["entity_id"])
    friendly_name = resolution["friendly_name"]
    if entity_id and not _validate_domain(entity_id):
        logger.warning("Resolved entity %s not in allowed domains %s", entity_id, _ALLOWED_DOMAINS)
        entity_id = None

    if not entity_id:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution["speech"] or f"Could not find a scene matching '{entity_query}'.",
            "cacheable": False,
        }

    return {
        "success": True,
        "entity_id": entity_id,
        "new_state": None,
        "speech": f"Scene found: {friendly_name} ({entity_id}).",
        "cacheable": False,
    }


async def _list_scenes(ha_client: Any) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception as exc:
        logger.error("Failed to fetch states for list_scenes", exc_info=True)
        return {"success": False, "entity_id": "", "new_state": None, "speech": f"Failed to list scenes: {exc}"}

    scenes = [s for s in states if s.get("entity_id", "").startswith("scene.")]

    if not scenes:
        return {"success": True, "entity_id": "", "new_state": None, "speech": "No scenes found."}

    names = []
    for s in scenes:
        name = s.get("attributes", {}).get("friendly_name", s.get("entity_id", ""))
        names.append(name)

    speech = f"Available scenes ({len(names)}): {', '.join(names)}."
    return {"success": True, "entity_id": "", "new_state": None, "speech": speech, "cacheable": False}


async def _handle_scene_read_action(
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
) -> dict:
    if action_name == "query_scene":
        return await _query_scene(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "list_scenes":
        return await _list_scenes(ha_client)
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}
