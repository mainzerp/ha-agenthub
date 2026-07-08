"""Light/switch-specific action execution and verification."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import ValidationError

from app.agents.action_executor import (
    ActionCondition,
    _ensure_str,
    _evaluate_condition,
    _synthesize_direct_entity_metadata,
    _validate_direct_entity_id,
    call_service_with_verification,
    resolve_and_validate_entity,
)
from app.agents.executor_state_check import _state_matches
from app.entity.visibility import entity_is_visible
from app.ha_client.history_query import execute_recorder_history_query
from app.ha_client.rest import allow_internal_ha_service_calls
from app.models.agent import TaskContext

logger = logging.getLogger(__name__)


# Map action names to (service, extra_data_builder)
_ACTION_SERVICE_MAP: dict[str, str] = {
    "turn_on": "turn_on",
    "turn_off": "turn_off",
    "toggle": "toggle",
    "set_brightness": "turn_on",
    "set_color": "turn_on",
    "set_color_temp": "turn_on",
}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"light", "switch", "sensor"})

# FLOW-DOMAIN-1 (0.19.2): per-action HA-domain allow-set used to filter
# both the deterministic entity_index sweep and the hybrid matcher's
# top candidates before tie-breaking. Read paths intentionally include
# ``sensor`` because illuminance/light-level sensors can answer
# query_light_state when no light entity matches.
_ACTION_DOMAINS_LIGHT: dict[str, frozenset[str]] = {
    "turn_on": frozenset({"light", "switch"}),
    "turn_off": frozenset({"light", "switch"}),
    "toggle": frozenset({"light", "switch"}),
    "set_brightness": frozenset({"light"}),
    "set_color": frozenset({"light"}),
    "set_color_temp": frozenset({"light"}),
    "query_light_state": frozenset({"light", "switch", "sensor"}),
    "query_entity_history": frozenset({"light", "switch", "sensor"}),
    "list_lights": frozenset({"light", "switch"}),
}

# FLOW-VERIFY-1: map each action to the state we expect HA to end up in.
# ``toggle`` has no deterministic target, so it is intentionally absent and
# the caller must treat ``expected`` as ``None`` (= "any next change").
_EXPECTED_STATE_BY_DOMAIN_ACTION: dict[tuple[str, str], str | frozenset[str] | None] = {
    # Light
    ("light", "turn_on"): "on",
    ("light", "turn_off"): "off",
    ("light", "set_brightness"): "on",
    ("light", "set_color"): "on",
    ("light", "set_color_temp"): "on",
    # Climate
    ("climate", "turn_on"): frozenset({"heat", "cool"}),
    ("climate", "turn_off"): "off",
    # Security
    ("lock", "lock"): "locked",
    ("lock", "unlock"): "unlocked",
    # Media
    ("media_player", "turn_on"): "on",
    ("media_player", "turn_off"): "off",
    # Music
    ("music", "turn_on"): "playing",
    ("music", "turn_off"): "off",
}

# Kept for backward compatibility with callers that expect action-only mapping.
_EXPECTED_STATE_BY_ACTION: dict[str, str] = {
    "turn_on": "on",
    "turn_off": "off",
    "set_brightness": "on",
    "set_color": "on",
    "set_color_temp": "on",
}


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _build_service_data(action: dict) -> dict[str, Any]:
    """Build HA service_data from action parameters."""
    params = action.get("parameters") or {}
    data: dict[str, Any] = {}

    if "brightness" in params:
        data["brightness"] = int(params["brightness"])
    if "color_name" in params:
        data["color_name"] = params["color_name"]
    if "rgb_color" in params:
        rc = params["rgb_color"]
        if isinstance(rc, list) and len(rc) == 3 and all(isinstance(v, int) for v in rc):
            data["rgb_color"] = rc
    if "effect" in params:
        data["effect"] = str(params["effect"])
    if "white" in params:
        data["white"] = int(params["white"]) if isinstance(params["white"], (int, float)) else params["white"]
    if "flash" in params:
        data["flash"] = str(params["flash"])
    if "color_temp" in params:
        data["color_temp"] = int(params["color_temp"])
    if "color_temp_kelvin" in params:
        data["color_temp_kelvin"] = int(params["color_temp_kelvin"])
    if "transition" in params:
        data["transition"] = float(params["transition"])

    return data


def _build_action_speech(
    *,
    action_name: str,
    friendly_name: str,
    expected_state: str | None,
    new_state: str | None,
) -> str:
    """Build an intent-first speech line for a completed action.

    We deliberately avoid claiming a state we did not observe. If the
    observed state matches the intent, we report it. Otherwise we fall
    back to intent language ("turned off X") so that a stale or slow
    state update does not contradict what the user asked for.
    """
    if expected_state and new_state == expected_state:
        return f"Done, {friendly_name} is now {new_state}."
    if expected_state:
        # Covers both "observed but mismatching" and "not observed at all".
        return f"Done, turned {expected_state} {friendly_name}."
    # ``toggle`` path: no deterministic target. Report what we saw if any.
    if new_state:
        return f"Done, {friendly_name} is now {new_state}."
    if action_name:
        return f"Done, {action_name.replace('_', ' ')} {friendly_name}."
    return f"Done, updated {friendly_name}."


async def execute_light_action(
    action: dict,
    ha_client,
    entity_index,
    entity_matcher,
    agent_id: str | None = None,
    span_collector=None,
    *,
    preferred_area_id: str | None = None,
    task_context: TaskContext | None = None,
) -> dict:
    """Resolve an entity, call a HA service, and verify the result.

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
    if action_name in ("query_light_state", "list_lights", "query_entity_history"):
        return await _handle_light_read_action(
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
    service = _ACTION_SERVICE_MAP.get(action_name)
    if not service:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"Unknown action: {action_name}",
        }

    resolved = await resolve_and_validate_entity(
        entity_query,
        entity_index,
        entity_matcher,
        agent_id,
        _ACTION_DOMAINS_LIGHT.get(action_name, _ALLOWED_DOMAINS),
        _validate_domain,
        preferred_area_id=preferred_area_id,
        enable_strip_device_noun=True,
        enable_area_fallback=True,
        preferred_domain="light",
        span_collector=span_collector,
        require_matcher=True,
    )
    if resolved["not_found_result"] is not None:
        result = resolved["not_found_result"]
        result["voice_followup"] = True
        return result
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

    # Extract domain from entity_id
    domain = entity_id.split(".")[0] if "." in entity_id else "light"

    # Evaluate pre-action condition if present. Conditional actions are
    # never cacheable because their outcome depends on runtime state.
    raw_condition = action.get("condition")
    if raw_condition is not None:
        try:
            condition = ActionCondition.model_validate(raw_condition)
        except ValidationError:
            return {
                "success": False,
                "entity_id": entity_id,
                "new_state": None,
                "speech": f"Invalid condition for action on {friendly_name}.",
                "cacheable": False,
            }
        passed, observed, cond_entity_id, error = await _evaluate_condition(
            condition,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id=agent_id,
            allowed_domains=_ALLOWED_DOMAINS,
            preferred_area_id=preferred_area_id,
        )
        if error is not None:
            return {
                "success": False,
                "entity_id": entity_id,
                "new_state": None,
                "speech": f"Could not evaluate condition for {friendly_name}: {error}",
                "cacheable": False,
            }
        if not passed:
            observed_display = observed or "unknown"
            return {
                "success": True,
                "entity_id": entity_id,
                "new_state": observed,
                "speech": f"Skipped {action_name} on {friendly_name} because {cond_entity_id or condition.entity} is {observed_display}.",
                "cacheable": False,
            }
        # Condition passed -- continue to service call, but mark non-cacheable.

    # Build service data
    service_data = _build_service_data(action)

    # FLOW-VERIFY-1 / FLOW-VERIFY-SHARED (0.18.5): delegate the
    # call_service + WS-waiter dance to the shared helper.
    expected_state = _ensure_str(_EXPECTED_STATE_BY_DOMAIN_ACTION.get((domain, action_name)))
    with allow_internal_ha_service_calls(f"action-executor:{agent_id or 'unknown'}"):
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
    result = {
        "success": True,
        "action": action_name,
        "entity_id": entity_id,
        "new_state": new_state,
        "speech": _build_action_speech(
            action_name=action_name,
            friendly_name=friendly_name,
            expected_state=expected_state,
            new_state=new_state,
        ),
    }
    if raw_condition is not None:
        result["cacheable"] = False
    return result


# Backward compatibility alias
execute_action = execute_light_action


# ---------------------------------------------------------------------------
# Read-only light/switch action handlers
# ---------------------------------------------------------------------------


def _format_light_state(entity_id: str, state_resp: dict) -> str:
    state = state_resp.get("state", "unknown")
    attrs = state_resp.get("attributes", {})
    friendly_name = attrs.get("friendly_name", entity_id)

    if entity_id.startswith("switch."):
        return f"{friendly_name} is {state}."

    if entity_id.startswith("sensor."):
        unit = attrs.get("unit_of_measurement", "")
        return f"{friendly_name}: {state} {unit}".strip() + "."

    # light.* entity
    parts = [f"{friendly_name} is {state}"]
    if state == "on":
        brightness = attrs.get("brightness")
        if brightness is not None:
            pct = round(int(brightness) / 255 * 100)
            parts.append(f"brightness {pct}%")
        color_name = attrs.get("color_name")
        if color_name:
            parts.append(f"color {color_name}")
        rgb = attrs.get("rgb_color")
        if rgb and not color_name:
            parts.append(f"RGB {rgb}")
        color_temp = attrs.get("color_temp")
        if color_temp:
            parts.append(f"color temp {color_temp} mireds")
    return ", ".join(parts) + "."


async def _query_light_state(
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
    entity_id_direct = _validate_direct_entity_id(action.get("entity_id") if action else None, _validate_domain)
    if entity_id_direct:
        entity_id = entity_id_direct
        resolution_metadata = _synthesize_direct_entity_metadata(entity_id, entity_index)
    else:
        resolved = await resolve_and_validate_entity(
            entity_query,
            entity_index,
            entity_matcher,
            agent_id,
            _ALLOWED_DOMAINS,
            _validate_domain,
            preferred_area_id=preferred_area_id,
            enable_strip_device_noun=True,
            enable_area_fallback=True,
            preferred_domain="light",
            span_collector=span_collector,
            require_matcher=True,
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
        speech = _format_light_state(entity_id, state_resp)
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
            "speech": f"Failed to query light status: {exc}",
            "cacheable": False,
            "metadata": resolution_metadata,
        }


async def _query_light_entity_history(
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
    """Recorder history for a single light, switch, or illuminance/light-level sensor entity."""
    resolved = await resolve_and_validate_entity(
        entity_query,
        entity_index,
        entity_matcher,
        agent_id,
        _ALLOWED_DOMAINS,
        _validate_domain,
        preferred_area_id=preferred_area_id,
        enable_strip_device_noun=True,
        enable_area_fallback=True,
        preferred_domain="light",
        span_collector=span_collector,
        require_matcher=True,
    )
    if resolved["not_found_result"] is not None:
        result = resolved["not_found_result"]
        result["speech"] = result["speech"].replace("an entity", "a visible entity")
        result["cacheable"] = False
        result["voice_followup"] = True
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


async def _list_lights(
    ha_client: Any,
    agent_id: str | None = None,
    entity_index: Any = None,
) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception as exc:
        logger.error("Failed to fetch states for list_lights", exc_info=True)
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": f"Failed to list lights: {exc}",
            "cacheable": False,
        }

    candidate_states = [s for s in states if s.get("entity_id", "").startswith(("light.", "switch."))]
    if agent_id and entity_index is not None:
        visibility = await asyncio.gather(
            *[entity_is_visible(agent_id, s.get("entity_id", ""), entity_index) for s in candidate_states]
        )
        candidate_states = [s for s, ok in zip(candidate_states, visibility, strict=True) if ok]

    lights_on = []
    lights_off = []
    switches_on = []
    switches_off = []
    for s in candidate_states:
        eid = s.get("entity_id", "")
        state = s.get("state", "unknown")
        name = s.get("attributes", {}).get("friendly_name", eid)
        if eid.startswith("light."):
            if state == "on":
                lights_on.append(name)
            else:
                lights_off.append(name)
        elif eid.startswith("switch."):
            if state == "on":
                switches_on.append(name)
            else:
                switches_off.append(name)

    if not lights_on and not lights_off and not switches_on and not switches_off:
        return {
            "success": True,
            "entity_id": "",
            "new_state": None,
            "speech": "No light or switch entities found.",
            "cacheable": False,
        }

    parts = []
    if lights_on:
        parts.append(f"Lights on: {', '.join(lights_on)}")
    if lights_off:
        parts.append(f"Lights off: {', '.join(lights_off)}")
    if switches_on:
        parts.append(f"Switches on: {', '.join(switches_on)}")
    if switches_off:
        parts.append(f"Switches off: {', '.join(switches_off)}")
    speech = ". ".join(parts) + "."
    return {"success": True, "entity_id": "", "new_state": None, "speech": speech, "cacheable": False}


async def _handle_light_read_action(
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
    if action_name == "query_light_state":
        return await _query_light_state(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            action=action,
        )
    if action_name == "list_lights":
        return await _list_lights(ha_client, agent_id=agent_id, entity_index=entity_index)
    if action_name == "query_entity_history":
        return await _query_light_entity_history(
            entity_query,
            parameters or {},
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            task_context=task_context,
        )
    return {
        "success": False,
        "entity_id": "",
        "new_state": None,
        "speech": f"Unknown read action: {action_name}",
        "cacheable": False,
    }
