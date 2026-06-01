"""Cover-specific action execution via HA cover services."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.action_executor import (
    build_verified_speech,
    call_service_with_verification,
    resolve_and_validate_entity,
)
from app.agents.executor_state_check import _state_matches
from app.ha_client.history_query import execute_recorder_history_query
from app.models.agent import TaskContext

logger = logging.getLogger(__name__)

_COVER_ACTION_MAP: dict[str, tuple[str, str]] = {
    "open_cover": ("cover", "open_cover"),
    "close_cover": ("cover", "close_cover"),
    "stop_cover": ("cover", "stop_cover"),
    "set_cover_position": ("cover", "set_cover_position"),
    "open_cover_tilt": ("cover", "open_cover_tilt"),
    "close_cover_tilt": ("cover", "close_cover_tilt"),
    "stop_cover_tilt": ("cover", "stop_cover_tilt"),
    "set_cover_tilt_position": ("cover", "set_cover_tilt_position"),
}

# FLOW-VERIFY-SHARED (0.18.5): cover actions have deterministic post-action states.
# set_cover_position with position=0 -> "closed", position=100 -> "open".
# Other positions do not have a deterministic target state.
_EXPECTED_STATE_BY_ACTION: dict[str, str] = {
    "open_cover": "open",
    "close_cover": "closed",
    "open_cover_tilt": "open",
    "close_cover_tilt": "closed",
}

# Intent-first phrasing when verification is inconclusive or ambiguous.
_ACTION_PHRASES: dict[str, str] = {
    "stop_cover": "stopped",
    "stop_cover_tilt": "tilt stopped",
    "set_cover_position": "position updated",
    "set_cover_tilt_position": "tilt position updated",
}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"cover"})

# FLOW-DOMAIN-1 (0.19.2): per-action HA-domain allow-set used to filter
# the hybrid matcher before picking matches[0].
_COVER_WRITE_DOMAINS: frozenset[str] = frozenset({"cover"})
_COVER_READ_DOMAINS: frozenset[str] = frozenset({"cover"})
_HISTORY_DOMAINS: frozenset[str] = frozenset({"cover"})


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _build_cover_service_data(action: dict) -> dict[str, Any]:
    """Build HA service_data from a cover action's parameters."""
    params = action.get("parameters") or {}
    data: dict[str, Any] = {}

    if "position" in params:
        data["position"] = int(params["position"])
    if "tilt_position" in params:
        data["tilt_position"] = int(params["tilt_position"])

    return data


def _resolve_expected_state(action_name: str, service_data: dict[str, Any]) -> str | None:
    """Resolve the expected state for a cover action."""
    expected = _EXPECTED_STATE_BY_ACTION.get(action_name)
    if expected:
        return expected
    if action_name == "set_cover_position":
        position = service_data.get("position")
        if position == 0:
            return "closed"
        if position == 100:
            return "open"
        # Other positions: no deterministic target
        return None
    if action_name == "set_cover_tilt_position":
        tilt_position = service_data.get("tilt_position")
        if tilt_position == 0:
            return "closed"
        if tilt_position == 100:
            return "open"
        return None
    return None


async def execute_cover_action(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None = None,
    span_collector=None,
    *,
    preferred_area_id: str | None = None,
    task_context: TaskContext | None = None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Resolve an entity, call a cover HA service, and verify the result.

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
    if action_name in ("query_cover_state", "list_covers", "query_entity_history"):
        return await _handle_cover_read_action(
            action_name,
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            parameters=action.get("parameters") or {},
            preferred_area_id=preferred_area_id,
            task_context=task_context,
            verbatim_terms=verbatim_terms,
        )

    # Validate action name
    mapping = _COVER_ACTION_MAP.get(action_name)
    if not mapping:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"Unknown action: {action_name}",
        }

    domain, service = mapping

    resolved = await resolve_and_validate_entity(
        entity_query,
        entity_index,
        entity_matcher,
        agent_id,
        _COVER_WRITE_DOMAINS,
        _validate_domain,
        preferred_area_id=preferred_area_id,
        verbatim_terms=verbatim_terms,
        span_collector=span_collector,
    )
    if resolved["not_found_result"] is not None:
        return resolved["not_found_result"]
    entity_id = resolved["entity_id"]
    friendly_name = resolved["friendly_name"]

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

    # Build service data
    service_data = _build_cover_service_data(action)

    # Resolve expected state
    expected_state = _resolve_expected_state(action_name, service_data)

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
# Read-only cover action handlers
# ---------------------------------------------------------------------------


def _format_cover_state(entity_id: str, state_resp: dict) -> str:
    state = state_resp.get("state", "unknown")
    attrs = state_resp.get("attributes", {})
    friendly_name = attrs.get("friendly_name", entity_id)

    parts = [f"{friendly_name} is {state}"]
    current_position = attrs.get("current_position")
    if current_position is not None:
        parts.append(f"position {current_position}%")
    current_tilt_position = attrs.get("current_tilt_position")
    if current_tilt_position is not None:
        parts.append(f"tilt {current_tilt_position}%")
    return ", ".join(parts) + "."


async def _query_cover_state(
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
    resolved = await resolve_and_validate_entity(
        entity_query,
        entity_index,
        entity_matcher,
        agent_id,
        _COVER_READ_DOMAINS,
        _validate_domain,
        preferred_area_id=preferred_area_id,
        verbatim_terms=verbatim_terms,
        span_collector=span_collector,
    )
    if resolved["not_found_result"] is not None:
        result = resolved["not_found_result"]
        result["cacheable"] = False
        return result
    entity_id = resolved["entity_id"]

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
        speech = _format_cover_state(entity_id, state_resp)
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
            "speech": f"Failed to query cover status: {exc}",
            "cacheable": False,
        }


async def _list_covers(ha_client: Any) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception as exc:
        logger.error("Failed to fetch states for list_covers", exc_info=True)
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": f"Failed to list covers: {exc}",
            "cacheable": False,
        }

    covers = []
    for s in states:
        eid = s.get("entity_id", "")
        if eid.startswith("cover."):
            attrs = s.get("attributes", {})
            name = attrs.get("friendly_name", eid)
            state = s.get("state", "unknown")
            info = f"{name}: {state}"
            current_position = attrs.get("current_position")
            if current_position is not None:
                info += f", position {current_position}%"
            covers.append(info)

    if not covers:
        return {
            "success": True,
            "entity_id": "",
            "new_state": None,
            "speech": "No cover entities found.",
            "cacheable": False,
        }

    speech = "Covers: " + "; ".join(covers) + "."
    return {"success": True, "entity_id": "", "new_state": None, "speech": speech, "cacheable": False}


async def _query_entity_history(
    entity_query: str,
    parameters: dict[str, Any],
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    preferred_area_id: str | None = None,
    task_context: TaskContext | None = None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Fetch Recorder history for a resolved cover entity (visibility-respected)."""
    resolved = await resolve_and_validate_entity(
        entity_query,
        entity_index,
        entity_matcher,
        agent_id,
        _HISTORY_DOMAINS,
        _validate_domain,
        preferred_area_id=preferred_area_id,
        verbatim_terms=verbatim_terms,
        span_collector=span_collector,
    )
    if resolved["not_found_result"] is not None:
        result = resolved["not_found_result"]
        result["speech"] = result["speech"].replace("an entity", "a visible entity")
        result["cacheable"] = False
        return result
    entity_id = resolved["entity_id"]
    friendly_name = resolved["friendly_name"]

    return await execute_recorder_history_query(
        entity_id,
        friendly_name,
        parameters,
        ha_client,
        allowed_domains=_ALLOWED_DOMAINS,
        task_context=task_context,
    )


async def _handle_cover_read_action(
    action_name: str,
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    parameters: dict[str, Any] | None = None,
    preferred_area_id: str | None = None,
    task_context: TaskContext | None = None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    params = parameters or {}
    if action_name == "query_cover_state":
        return await _query_cover_state(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "list_covers":
        return await _list_covers(ha_client)
    if action_name == "query_entity_history":
        return await _query_entity_history(
            entity_query,
            params,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            task_context=task_context,
            verbatim_terms=verbatim_terms,
        )
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}
