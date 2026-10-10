"""Cover-specific action execution via HA cover services."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agents.action_executor import (
    _synthesize_direct_entity_metadata,
    _validate_direct_entity_id,
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
# Other positions do not have a deterministic target state. Tilt actions have
# none either: the cover's state describes its position, not its tilt.
_EXPECTED_STATE_BY_ACTION: dict[str, str] = {
    "open_cover": "open",
    "close_cover": "closed",
}

# Intent-first phrasing when verification is inconclusive or ambiguous.
_ACTION_PHRASES: dict[str, str] = {
    "stop_cover": "stopped",
    "stop_cover_tilt": "tilt stopped",
    "set_cover_position": "position updated",
    "open_cover_tilt": "tilt opened",
    "close_cover_tilt": "tilt closed",
    "set_cover_tilt_position": "tilt position updated",
}

# HA ``CoverEntityFeature`` bit each action needs, plus the wording used in
# the honest "not supported" answer.
_REQUIRED_FEATURES: dict[str, tuple[int, str]] = {
    "open_cover": (1, "opening"),
    "close_cover": (2, "closing"),
    "set_cover_position": (4, "setting a position"),
    "stop_cover": (8, "stopping"),
    "open_cover_tilt": (16, "tilting"),
    "close_cover_tilt": (32, "tilting"),
    "stop_cover_tilt": (64, "tilting"),
    "set_cover_tilt_position": (128, "setting a tilt position"),
}

# Service-data keys each cover service accepts; other services take none.
_SERVICE_DATA_KEYS: dict[str, frozenset[str]] = {
    "set_cover_position": frozenset({"position"}),
    "set_cover_tilt_position": frozenset({"tilt_position"}),
}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"cover"})

# FLOW-DOMAIN-1 (0.19.2): per-action HA-domain allow-set used to filter
# the hybrid matcher before picking matches[0].
_COVER_WRITE_DOMAINS: frozenset[str] = frozenset({"cover"})
_COVER_READ_DOMAINS: frozenset[str] = frozenset({"cover"})
_HISTORY_DOMAINS: frozenset[str] = frozenset({"cover"})

# Entity-candidate declaration (read by the agent's ``@agent`` call, see
# ``ActionableAgent._entity_actions``): every action this executor
# dispatches, split into actions that act on one target entity (they need a
# recalled entity candidate) and actions that run without one. Derived from
# the dispatch tables so the declaration cannot drift from the executor.
_READ_ACTIONS: frozenset[str] = frozenset({"query_cover_state", "list_covers", "query_entity_history"})
ENTITY_FREE_ACTIONS: frozenset[str] = frozenset({"list_covers"})
ENTITY_ACTIONS: frozenset[str] = (frozenset(_COVER_ACTION_MAP) | _READ_ACTIONS) - ENTITY_FREE_ACTIONS


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
    if action_name in _READ_ACTIONS:
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
            action=action,
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

    # Build service data (only the keys the target service accepts).
    try:
        raw_data = _build_cover_service_data(action)
    except (TypeError, ValueError):
        logger.warning("Invalid cover parameters for %s: %r", entity_id, action.get("parameters"))
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": current_state,
            "speech": f"I could not understand the requested position for {friendly_name}.",
            "cacheable": False,
        }
    allowed_keys = _SERVICE_DATA_KEYS.get(action_name, frozenset())
    service_data = {k: v for k, v in raw_data.items() if k in allowed_keys}

    # Honest "not supported" answer when HA reports a feature mask without
    # the capability this action needs (e.g. tilt on a plain roller shutter).
    required = _REQUIRED_FEATURES.get(action_name)
    if required is not None and lacks_feature(state_resp, required[0]):
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": current_state,
            "speech": f"{friendly_name} does not support {required[1]}.",
            "cacheable": False,
        }

    # Deterministic skip: only a parameterless open/close on a single cover
    # that is already in the target state is redundant (groups always run).
    if is_redundant_action(action_name, state_resp, service_data):
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": current_state,
            "noop": True,
            "speech": f"Done, {friendly_name} is already {current_state}.",
        }

    # Resolve expected state
    expected_state = _resolve_expected_state(action_name, service_data)

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
        "executed_command": {
            "domain": domain,
            "service": service,
            "entity_id": entity_id,
            "service_data": service_data,
        },
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
    action: dict | None = None,
) -> dict:
    entity_id_direct = await _validate_direct_entity_id(
        action.get("entity_id") if action else None,
        _validate_domain,
        agent_id=agent_id,
        entity_index=entity_index,
    )
    if entity_id_direct:
        entity_id = entity_id_direct
        resolution_metadata = _synthesize_direct_entity_metadata(entity_id, entity_index)
    else:
        resolved = await resolve_and_validate_entity(
            entity_query,
            entity_index,
            entity_matcher,
            agent_id,
            _COVER_READ_DOMAINS,
            _validate_domain,
            preferred_area_id=preferred_area_id,
            span_collector=span_collector,
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
        speech = _format_cover_state(entity_id, state_resp)
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
            "speech": "Sorry, I could not query cover status.",
            "cacheable": False,
            "metadata": resolution_metadata,
        }


async def _list_covers(ha_client: Any, agent_id: str | None = None, entity_index: Any = None) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception:
        logger.error("Failed to fetch states for list_covers", exc_info=True)
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": "Sorry, I could not list covers.",
            "cacheable": False,
        }

    cover_states = [s for s in states if s.get("entity_id", "").startswith("cover.")]
    if agent_id and entity_index is not None:
        visibility = await asyncio.gather(
            *[entity_is_visible(agent_id, s.get("entity_id", ""), entity_index) for s in cover_states]
        )
        cover_states = [s for s, ok in zip(cover_states, visibility, strict=True) if ok]

    covers = []
    for s in cover_states:
        eid = s.get("entity_id", "")
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
    action: dict | None = None,
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
            action=action,
        )
    if action_name == "list_covers":
        return await _list_covers(ha_client, agent_id=agent_id, entity_index=entity_index)
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
        )
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}
