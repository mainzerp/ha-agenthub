"""Vacuum-specific action execution via HA vacuum services."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.action_executor import (
    build_verified_speech,
    call_service_with_verification,
    resolve_and_validate_entity,
)
from app.agents.executor_state_check import _state_matches
from app.models.agent import TaskContext

logger = logging.getLogger(__name__)

_VACUUM_ACTION_MAP: dict[str, tuple[str, str]] = {
    "start": ("vacuum", "start"),
    "pause": ("vacuum", "pause"),
    "stop": ("vacuum", "stop"),
    "return_to_base": ("vacuum", "return_to_base"),
    "clean_spot": ("vacuum", "clean_spot"),
    "set_fan_speed": ("vacuum", "set_fan_speed"),
    "locate": ("vacuum", "locate"),
    "send_command": ("vacuum", "send_command"),
}

# FLOW-VERIFY-SHARED: expected post-action states for verification.
_EXPECTED_STATE_BY_ACTION: dict[str, str] = {
    "start": "cleaning",
    "pause": "paused",
    "stop": "idle",
    "return_to_base": "returning",
    "clean_spot": "cleaning",
}

# Intent-first phrasing when verification is inconclusive or ambiguous.
_ACTION_PHRASES: dict[str, str] = {
    "set_fan_speed": "fan speed updated",
    "locate": "located",
    "send_command": "command sent",
}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"vacuum"})

# FLOW-DOMAIN-1 (0.19.2): per-action HA-domain allow-set.
_VACUUM_WRITE_DOMAINS: frozenset[str] = frozenset({"vacuum"})
_VACUUM_READ_DOMAINS: frozenset[str] = frozenset({"vacuum"})
_HISTORY_DOMAINS: frozenset[str] = frozenset({"vacuum"})


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _build_vacuum_service_data(action: dict) -> dict[str, Any]:
    """Build HA service_data from a vacuum action's parameters."""
    params = action.get("parameters") or {}
    data: dict[str, Any] = {}

    if "fan_speed" in params:
        data["fan_speed"] = str(params["fan_speed"])
    if "command" in params:
        data["command"] = str(params["command"])
    if "params" in params and isinstance(params["params"], dict):
        data["params"] = dict(params["params"])

    return data


async def execute_vacuum_action(
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
    """Resolve an entity, call a vacuum HA service, and verify the result.

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
    if action_name in ("query_vacuum_state", "list_vacuums", "query_entity_history"):
        return await _handle_vacuum_read_action(
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
    mapping = _VACUUM_ACTION_MAP.get(action_name)
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
        _VACUUM_WRITE_DOMAINS,
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
    service_data = _build_vacuum_service_data(action)

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
# Read-only vacuum action handlers
# ---------------------------------------------------------------------------


def _format_vacuum_state(entity_id: str, state_resp: dict) -> str:
    state = state_resp.get("state", "unknown")
    attrs = state_resp.get("attributes", {})
    friendly_name = attrs.get("friendly_name", entity_id)

    parts = [f"{friendly_name} is {state}"]
    battery = attrs.get("battery_level")
    if battery is not None:
        parts.append(f"battery {battery}%")
    fan_speed = attrs.get("fan_speed")
    if fan_speed:
        parts.append(f"fan speed {fan_speed}")
    status = attrs.get("status")
    if status:
        parts.append(f"status {status}")
    return ", ".join(parts) + "."


async def _query_vacuum_state(
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
        _VACUUM_READ_DOMAINS,
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
        speech = _format_vacuum_state(entity_id, state_resp)
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
            "speech": f"Failed to query vacuum status: {exc}",
            "cacheable": False,
        }


async def _list_vacuums(ha_client: Any) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception as exc:
        logger.error("Failed to fetch states for list_vacuums", exc_info=True)
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": f"Failed to list vacuums: {exc}",
            "cacheable": False,
        }

    vacuums = []
    for s in states:
        eid = s.get("entity_id", "")
        if eid.startswith("vacuum."):
            attrs = s.get("attributes", {})
            name = attrs.get("friendly_name", eid)
            state = s.get("state", "unknown")
            info = f"{name}: {state}"
            battery = attrs.get("battery_level")
            if battery is not None:
                info += f", battery {battery}%"
            fan_speed = attrs.get("fan_speed")
            if fan_speed:
                info += f", fan {fan_speed}"
            vacuums.append(info)

    if not vacuums:
        return {
            "success": True,
            "entity_id": "",
            "new_state": None,
            "speech": "No vacuum entities found.",
            "cacheable": False,
        }

    speech = "Vacuums: " + "; ".join(vacuums) + "."
    return {"success": True, "entity_id": "", "new_state": None, "speech": speech, "cacheable": False}


async def _handle_vacuum_read_action(
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
    if action_name == "query_vacuum_state":
        return await _query_vacuum_state(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "list_vacuums":
        return await _list_vacuums(ha_client)
    if action_name == "query_entity_history":
        from app.ha_client.history_query import execute_recorder_history_query

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
            params,
            ha_client,
            allowed_domains=_ALLOWED_DOMAINS,
            task_context=task_context,
        )
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}
