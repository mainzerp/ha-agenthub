"""Security-specific action execution via HA lock, alarm, and camera services."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.action_executor import (
    build_verified_speech,
    call_service_with_verification,
)
from app.analytics.tracer import _optional_span
from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.ha_client.history_query import execute_recorder_history_query
from app.models.agent import TaskContext

logger = logging.getLogger(__name__)

_SECURITY_ACTION_MAP: dict[str, tuple[str, str]] = {
    # Locks
    "lock": ("lock", "lock"),
    "unlock": ("lock", "unlock"),
    # Alarm control panels
    "alarm_arm_home": ("alarm_control_panel", "alarm_arm_home"),
    "alarm_arm_away": ("alarm_control_panel", "alarm_arm_away"),
    "alarm_arm_night": ("alarm_control_panel", "alarm_arm_night"),
    "alarm_disarm": ("alarm_control_panel", "alarm_disarm"),
    # Cameras
    "camera_turn_on": ("camera", "turn_on"),
    "camera_turn_off": ("camera", "turn_off"),
}

# FLOW-DOMAIN-1 (0.19.2): per-action HA-domain allow-set used to filter
# the hybrid matcher's candidate list before picking matches[0]. Without
# this, a camera_turn_on request for "front door" can resolve to
# lock.front_door because the matcher is domain-blind.
_ACTION_DOMAINS: dict[str, frozenset[str]] = {
    "lock": frozenset({"lock"}),
    "unlock": frozenset({"lock"}),
    "alarm_arm_home": frozenset({"alarm_control_panel"}),
    "alarm_arm_away": frozenset({"alarm_control_panel"}),
    "alarm_arm_night": frozenset({"alarm_control_panel"}),
    "alarm_disarm": frozenset({"alarm_control_panel"}),
    "camera_turn_on": frozenset({"camera"}),
    "camera_turn_off": frozenset({"camera"}),
}

# FLOW-VERIFY-SHARED (0.18.5): security actions have strong deterministic
# targets -- correct speech matters here more than anywhere else.
_EXPECTED_STATE_BY_ACTION: dict[str, str] = {
    "lock": "locked",
    "unlock": "unlocked",
    "alarm_arm_home": "armed_home",
    "alarm_arm_away": "armed_away",
    "alarm_arm_night": "armed_night",
    "alarm_disarm": "disarmed",
    "camera_turn_off": "off",
    # camera_turn_on can end in "idle"/"streaming"/"recording" -- leave open
}

_ACTION_PHRASES: dict[str, str] = {
    "lock": "locked",
    "unlock": "unlocked",
    "alarm_arm_home": "armed in home mode",
    "alarm_arm_away": "armed in away mode",
    "alarm_arm_night": "armed in night mode",
    "alarm_disarm": "disarmed",
    "camera_turn_on": "turned on",
    "camera_turn_off": "turned off",
}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"alarm_control_panel", "lock", "camera", "binary_sensor", "sensor"})

# FLOW-DOMAIN-1 (0.19.2): read paths legitimately span every security
# domain (e.g. query_security_state for "front door" may resolve to a
# lock, alarm panel, camera, or binary_sensor depending on the snapshot).
_READ_DOMAINS: frozenset[str] = _ALLOWED_DOMAINS


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _build_security_service_data(action: dict) -> dict[str, Any]:
    """Build HA service_data from a security action's parameters."""
    params = action.get("parameters") or {}
    data: dict[str, Any] = {}

    if "code" in params:
        data["code"] = str(params["code"])

    return data


async def execute_security_action(
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
    """Resolve an entity, call a security HA service, and verify the result.

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
    if action_name in ("query_security_state", "list_security", "query_entity_history"):
        return await _handle_security_read_action(
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
    mapping = _SECURITY_ACTION_MAP.get(action_name)
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
    required_domains = _ACTION_DOMAINS.get(action_name, _READ_DOMAINS)
    try:
        if entity_index or entity_matcher:
            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    agent_id,
                    allowed_domains=required_domains,
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
    service_data = _build_security_service_data(action)

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
# Read-only security action handlers
# ---------------------------------------------------------------------------

_SECURITY_DEVICE_CLASSES = frozenset(
    {
        "motion",
        "door",
        "window",
        "opening",
        "smoke",
        "gas",
        "carbon_monoxide",
        "tamper",
        "vibration",
    }
)


def _format_security_state(entity_id: str, state_resp: dict) -> str:
    state = state_resp.get("state", "unknown")
    attrs = state_resp.get("attributes", {})
    friendly_name = attrs.get("friendly_name", entity_id)

    if entity_id.startswith("binary_sensor."):
        device_class = attrs.get("device_class", "")
        if device_class in ("door", "window", "opening"):
            label = "open" if state == "on" else "closed"
        elif device_class == "motion":
            label = "motion detected" if state == "on" else "clear"
        elif device_class in ("smoke", "gas", "carbon_monoxide"):
            label = "detected" if state == "on" else "clear"
        else:
            label = state
        return f"{friendly_name}: {label}."

    if entity_id.startswith("lock."):
        return f"{friendly_name} is {state}."

    if entity_id.startswith("alarm_control_panel."):
        return f"{friendly_name} is {state}."

    if entity_id.startswith("camera."):
        return f"{friendly_name} is {state}."

    return f"{friendly_name}: {state}."


async def _query_security_state(
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
                    allowed_domains=_READ_DOMAINS,
                    preferred_area_id=preferred_area_id,
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
        speech = _format_security_state(entity_id, state_resp)
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
            "speech": f"Failed to query security status: {exc}",
            "cacheable": False,
        }


async def _query_security_entity_history(
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
    """Recorder history for a single lock, alarm, camera, or security sensor entity."""
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
                    allowed_domains=_READ_DOMAINS,
                    preferred_area_id=preferred_area_id,
                    verbatim_terms=verbatim_terms,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    if entity_id and not _validate_domain(entity_id):
        entity_id = None

    if not entity_id:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution["speech"] or f"Could not find a visible entity matching '{entity_query}'.",
            "cacheable": False,
        }

    return await execute_recorder_history_query(
        entity_id,
        friendly_name,
        parameters,
        ha_client,
        allowed_domains=_ALLOWED_DOMAINS,
        task_context=task_context,
    )


async def _list_security(ha_client: Any) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception as exc:
        logger.error("Failed to fetch states for list_security", exc_info=True)
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": f"Failed to list security devices: {exc}",
        }

    locks = []
    alarms = []
    cameras = []
    binary_sensors = []
    for s in states:
        eid = s.get("entity_id", "")
        if eid.startswith("lock."):
            locks.append(s)
        elif eid.startswith("alarm_control_panel."):
            alarms.append(s)
        elif eid.startswith("camera."):
            cameras.append(s)
        elif eid.startswith("binary_sensor."):
            dc = s.get("attributes", {}).get("device_class", "")
            if dc in _SECURITY_DEVICE_CLASSES:
                binary_sensors.append(s)

    if not locks and not alarms and not cameras and not binary_sensors:
        return {"success": True, "entity_id": "", "new_state": None, "speech": "No security devices found."}

    parts = []
    for label, entities in [("Locks", locks), ("Alarms", alarms), ("Cameras", cameras), ("Sensors", binary_sensors)]:
        if entities:
            items = []
            for e in entities:
                name = e.get("attributes", {}).get("friendly_name", e.get("entity_id", ""))
                state = e.get("state", "unknown")
                items.append(f"{name}: {state}")
            parts.append(f"{label}: {'; '.join(items)}")

    speech = ". ".join(parts) + "."
    return {"success": True, "entity_id": "", "new_state": None, "speech": speech, "cacheable": False}


async def _handle_security_read_action(
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
    if action_name == "query_security_state":
        return await _query_security_state(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "list_security":
        return await _list_security(ha_client)
    if action_name == "query_entity_history":
        return await _query_security_entity_history(
            entity_query,
            parameters or {},
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
