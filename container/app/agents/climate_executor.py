"""Climate-specific action execution via HA climate services."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.action_executor import (
    _ensure_str,
    build_verified_speech,
    call_service_with_verification,
)
from app.agents.executor_state_check import _state_matches
from app.analytics.tracer import _optional_span
from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.entity.matcher import MatchResult
from app.entity.visibility import filter_visible_results
from app.ha_client.history_query import execute_recorder_history_query
from app.models.agent import TaskContext

logger = logging.getLogger(__name__)


def _resolve_turn_on_off_domain(entity_id: str, action_name: str) -> tuple[str, str]:
    """Derive the HA service domain for turn_on/turn_off from the entity_id."""
    if entity_id.startswith("climate."):
        return ("climate", action_name)
    if entity_id.startswith("fan."):
        return ("fan", action_name)
    if entity_id.startswith("humidifier."):
        return ("humidifier", action_name)
    return ("climate", action_name)


_CLIMATE_ACTION_MAP: dict[str, tuple[str, str]] = {
    "set_temperature": ("climate", "set_temperature"),
    "set_hvac_mode": ("climate", "set_hvac_mode"),
    "set_fan_mode": ("climate", "set_fan_mode"),
    "set_humidity": ("climate", "set_humidity"),
    "turn_on": ("climate", "turn_on"),
    "turn_off": ("climate", "turn_off"),
    "set_fan_percentage": ("fan", "set_percentage"),
    "set_fan_preset_mode": ("fan", "set_preset_mode"),
    "fan_oscillate": ("fan", "oscillate"),
    "set_fan_direction": ("fan", "set_direction"),
    "set_humidifier_humidity": ("humidifier", "set_humidity"),
    "set_humidifier_mode": ("humidifier", "set_mode"),
}

# FLOW-VERIFY-SHARED (0.18.5): climate entities have several meaningful
# post-action states. ``turn_off`` deterministically ends in "off"; for
# ``turn_on`` HA leaves it to the integration (often "heat"/"cool"/"auto")
# so we don't pin an expected state. ``set_hvac_mode`` is handled
# dynamically below because the target is the user-supplied mode.
_EXPECTED_STATE_BY_ACTION: dict[str, str] = {
    "turn_off": "off",
    "fan_turn_on": "on",
    "fan_turn_off": "off",
    "humidifier_turn_on": "on",
    "humidifier_turn_off": "off",
}

# Intent-first phrasing when verification is inconclusive or ambiguous.
_ACTION_PHRASES: dict[str, str] = {
    "set_temperature": "temperature updated",
    "set_fan_mode": "fan mode updated",
    "set_humidity": "humidity target updated",
    "set_fan_percentage": "fan speed updated",
    "set_fan_preset_mode": "preset mode updated",
    "fan_oscillate": "oscillation updated",
    "set_fan_direction": "direction updated",
    "set_humidifier_humidity": "humidity target updated",
    "set_humidifier_mode": "mode updated",
}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"climate", "sensor", "weather", "fan", "humidifier"})

# FLOW-DOMAIN-1 (0.19.2): per-action HA-domain allow-set used to filter
# the hybrid matcher before picking matches[0]. All write actions target
# climate.*, fan.*, and humidifier.* entities; weather and read paths get
# their own constants.
_CLIMATE_WRITE_DOMAINS: frozenset[str] = frozenset({"climate", "fan", "humidifier"})
# Read path explicitly spans climate + sensor + fan + humidifier: "what's
# the temperature in the living room?" should resolve to a sensor.* entity
# even when a climate.* exists in the same area. Do NOT tighten this.
_CLIMATE_READ_DOMAINS: frozenset[str] = frozenset({"climate", "sensor", "fan", "humidifier"})
_WEATHER_DOMAINS: frozenset[str] = frozenset({"weather"})
_HISTORY_DOMAINS: frozenset[str] = frozenset({"climate", "sensor", "weather", "fan", "humidifier"})


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _build_climate_service_data(action: dict) -> dict[str, Any]:
    """Build HA service_data from a climate action's parameters."""
    params = action.get("parameters") or {}
    data: dict[str, Any] = {}

    if "temperature" in params:
        data["temperature"] = float(params["temperature"])
    if "target_temp_high" in params:
        data["target_temp_high"] = float(params["target_temp_high"])
    if "target_temp_low" in params:
        data["target_temp_low"] = float(params["target_temp_low"])
    if "hvac_mode" in params:
        data["hvac_mode"] = params["hvac_mode"]
    if "fan_mode" in params:
        data["fan_mode"] = params["fan_mode"]
    if "humidity" in params:
        data["humidity"] = int(params["humidity"])
    # Fan parameters
    if "percentage" in params:
        data["percentage"] = int(params["percentage"])
    if "preset_mode" in params:
        data["preset_mode"] = params["preset_mode"]
    if "oscillating" in params:
        data["oscillating"] = bool(params["oscillating"])
    if "direction" in params:
        data["direction"] = params["direction"]
    # Humidifier parameters
    if "mode" in params:
        data["mode"] = params["mode"]

    return data


async def execute_climate_action(
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
    """Resolve an entity, call a climate HA service, and verify the result.

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
    if action_name in (
        "query_climate_state",
        "list_climate",
        "query_weather",
        "query_weather_forecast",
        "query_entity_history",
    ):
        return await _handle_climate_read_action(
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
    mapping = _CLIMATE_ACTION_MAP.get(action_name)
    if not mapping:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": f"Unknown action: {action_name}",
        }

    domain, service = mapping
    # Generic turn_on/turn_off: resolve domain from matched entity_id at runtime
    if action_name in ("turn_on", "turn_off") and domain == "climate":
        pass  # will re-resolve after entity_id is known

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
                    agent_id=agent_id,
                    allowed_domains=_CLIMATE_WRITE_DOMAINS,
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

    # Generic turn_on/turn_off: derive HA service domain from matched entity_id
    if action_name in ("turn_on", "turn_off"):
        domain, service = _resolve_turn_on_off_domain(entity_id, action_name)

    # Build service data
    service_data = _build_climate_service_data(action)

    # FLOW-VERIFY-SHARED: set_hvac_mode has a dynamic target equal to the
    # requested mode; other actions use the static map.
    expected_state = _EXPECTED_STATE_BY_ACTION.get(action_name)
    if action_name == "set_hvac_mode":
        mode = service_data.get("hvac_mode")
        if isinstance(mode, str) and mode:
            expected_state = mode

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
# Read-only climate action handlers
# ---------------------------------------------------------------------------


def _format_climate_state(entity_id: str, state_resp: dict) -> str:
    state = state_resp.get("state", "unknown")
    attrs = state_resp.get("attributes", {})
    friendly_name = attrs.get("friendly_name", entity_id)

    if entity_id.startswith("sensor."):
        unit = attrs.get("unit_of_measurement", "")
        return f"{friendly_name}: {state} {unit}".strip() + "."

    if entity_id.startswith("fan."):
        parts = [f"{friendly_name} is {state}"]
        percentage = attrs.get("percentage")
        if percentage is not None:
            parts.append(f"speed {percentage}%")
        preset_mode = attrs.get("preset_mode")
        if preset_mode:
            parts.append(f"preset {preset_mode}")
        oscillating = attrs.get("oscillating")
        if oscillating is not None:
            parts.append("oscillating" if oscillating else "not oscillating")
        direction = attrs.get("direction")
        if direction:
            parts.append(f"direction {direction}")
        return ", ".join(parts) + "."

    if entity_id.startswith("humidifier."):
        parts = [f"{friendly_name} is {state}"]
        target_humidity = attrs.get("humidity")
        if target_humidity is not None:
            parts.append(f"target humidity {target_humidity}%")
        current_humidity = attrs.get("current_humidity")
        if current_humidity is not None:
            parts.append(f"current humidity {current_humidity}%")
        mode = attrs.get("mode")
        if mode:
            parts.append(f"mode {mode}")
        return ", ".join(parts) + "."

    # climate.* entity
    parts = [f"{friendly_name} is in {state} mode"]
    current_temp = attrs.get("current_temperature")
    if current_temp is not None:
        parts.append(f"current temperature {current_temp}")
    target_temp = attrs.get("temperature")
    if target_temp is not None:
        parts.append(f"target {target_temp}")
    target_high = attrs.get("target_temp_high")
    target_low = attrs.get("target_temp_low")
    if target_high is not None and target_low is not None:
        parts.append(f"range {target_low}-{target_high}")
    humidity = attrs.get("current_humidity")
    if humidity is not None:
        parts.append(f"humidity {humidity}%")
    fan_mode = attrs.get("fan_mode")
    if fan_mode:
        parts.append(f"fan {fan_mode}")
    return ", ".join(parts) + "."


async def _query_climate_state(
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
                    allowed_domains=_CLIMATE_READ_DOMAINS,
                    preferred_area_id=preferred_area_id,
                    verbatim_terms=verbatim_terms,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = _ensure_str(resolution["entity_id"])
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
        speech = _format_climate_state(entity_id, state_resp)
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
            "speech": f"Failed to query climate status: {exc}",
            "cacheable": False,
        }


async def _list_climate(ha_client: Any) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception as exc:
        logger.error("Failed to fetch states for list_climate", exc_info=True)
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": f"Failed to list climate devices: {exc}",
        }

    climate_entities = []
    fan_entities = []
    humidifier_entities = []
    sensors = []
    _sensor_keywords = ("temperature", "humidity", "pressure", "dew_point")
    for s in states:
        eid = s.get("entity_id", "")
        if eid.startswith("climate."):
            climate_entities.append(s)
        elif eid.startswith("fan."):
            fan_entities.append(s)
        elif eid.startswith("humidifier."):
            humidifier_entities.append(s)
        elif eid.startswith("sensor.") and any(k in eid for k in _sensor_keywords):
            sensors.append(s)

    if not climate_entities and not fan_entities and not humidifier_entities and not sensors:
        return {"success": True, "entity_id": "", "new_state": None, "speech": "No climate devices or sensors found."}

    parts = []
    if climate_entities:
        lines = []
        for c in climate_entities:
            attrs = c.get("attributes", {})
            name = attrs.get("friendly_name", c.get("entity_id", ""))
            state = c.get("state", "unknown")
            current_temp = attrs.get("current_temperature")
            target_temp = attrs.get("temperature")
            info = f"{name}: {state}"
            if current_temp is not None:
                info += f", current {current_temp}"
            if target_temp is not None:
                info += f", target {target_temp}"
            lines.append(info)
        parts.append("Climate devices: " + "; ".join(lines))
    if fan_entities:
        lines = []
        for f in fan_entities:
            attrs = f.get("attributes", {})
            name = attrs.get("friendly_name", f.get("entity_id", ""))
            state = f.get("state", "unknown")
            percentage = attrs.get("percentage")
            info = f"{name}: {state}"
            if percentage is not None:
                info += f", speed {percentage}%"
            lines.append(info)
        parts.append("Fans: " + "; ".join(lines))
    if humidifier_entities:
        lines = []
        for h in humidifier_entities:
            attrs = h.get("attributes", {})
            name = attrs.get("friendly_name", h.get("entity_id", ""))
            state = h.get("state", "unknown")
            target_humidity = attrs.get("humidity")
            info = f"{name}: {state}"
            if target_humidity is not None:
                info += f", target {target_humidity}%"
            lines.append(info)
        parts.append("Humidifiers: " + "; ".join(lines))
    if sensors:
        lines = []
        for s in sensors:
            attrs = s.get("attributes", {})
            name = attrs.get("friendly_name", s.get("entity_id", ""))
            state = s.get("state", "unknown")
            unit = attrs.get("unit_of_measurement", "")
            lines.append(f"{name}: {state} {unit}".strip())
        parts.append("Sensors: " + "; ".join(lines))

    speech = ". ".join(parts) + "."
    return {"success": True, "entity_id": "", "new_state": None, "speech": speech, "cacheable": False}


# ---------------------------------------------------------------------------
# Weather helpers and query functions
# ---------------------------------------------------------------------------


def _format_weather_state(entity_id: str, state_resp: dict) -> str:
    """Format a weather entity state into a human-readable summary."""
    attrs = state_resp.get("attributes", {})
    friendly_name = attrs.get("friendly_name", entity_id)
    condition = state_resp.get("state", "unknown")

    parts = [f"{friendly_name}: currently {condition}"]
    temp = attrs.get("temperature")
    if temp is not None:
        unit = attrs.get("temperature_unit", "")
        parts.append(f"temperature {temp}{unit}")
    humidity = attrs.get("humidity")
    if humidity is not None:
        parts.append(f"humidity {humidity}%")
    pressure = attrs.get("pressure")
    if pressure is not None:
        p_unit = attrs.get("pressure_unit", "")
        parts.append(f"pressure {pressure} {p_unit}".strip())
    wind_speed = attrs.get("wind_speed")
    if wind_speed is not None:
        ws_unit = attrs.get("wind_speed_unit", "")
        parts.append(f"wind speed {wind_speed} {ws_unit}".strip())
    wind_bearing = attrs.get("wind_bearing")
    if wind_bearing is not None:
        parts.append(f"wind bearing {wind_bearing}")
    visibility = attrs.get("visibility")
    if visibility is not None:
        v_unit = attrs.get("visibility_unit", "")
        parts.append(f"visibility {visibility} {v_unit}".strip())
    return ", ".join(parts) + "."


def _format_weather_forecast(forecasts: list[dict]) -> str:
    """Format a list of forecast entries into a human-readable multi-day summary."""
    if not forecasts:
        return "No forecast data available."
    lines = []
    for entry in forecasts[:7]:
        dt = entry.get("datetime", "")
        date_str = dt[:10] if len(dt) >= 10 else dt
        condition = entry.get("condition", "unknown")
        temp_high = entry.get("temperature")
        temp_low = entry.get("templow")
        parts = [f"{date_str}: {condition}"]
        if temp_high is not None and temp_low is not None:
            parts.append(f"high {temp_high}, low {temp_low}")
        elif temp_high is not None:
            parts.append(f"temp {temp_high}")
        precipitation = entry.get("precipitation")
        if precipitation is not None:
            parts.append(f"precipitation {precipitation}")
        wind_speed = entry.get("wind_speed")
        if wind_speed is not None:
            parts.append(f"wind {wind_speed}")
        lines.append(", ".join(parts))
    return "; ".join(lines) + "."


async def _resolve_weather_entity(
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    preferred_area_id: str | None = None,
    verbatim_terms: list[str] | None = None,
) -> tuple[str | None, str, str | None]:
    """Resolve a weather entity from query or auto-discover the first visible weather.* entity."""
    entity_id = None
    friendly_name = entity_query or "weather"
    resolution_speech = None

    if entity_query and (entity_index or entity_matcher):
        try:
            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    agent_id,
                    allowed_domains=_WEATHER_DOMAINS,
                    preferred_area_id=preferred_area_id,
                    verbatim_terms=verbatim_terms,
                )
                em_span["metadata"] = resolution["metadata"]
                entity_id = resolution["entity_id"]
                friendly_name = resolution["friendly_name"]
                resolution_speech = resolution["speech"]
        except Exception:
            logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    if entity_id and not _validate_domain(entity_id):
        entity_id = None

    # Auto-discover the first visible weather.* entity only when resolution
    # found no candidate and did not surface an ambiguity or visibility error.
    if not entity_id and not resolution_speech:
        effective_agent_id = agent_id or "climate-agent"
        weather_results: list[MatchResult] = []

        if entity_index is not None:
            try:
                entries: list[Any] = []
                list_entries_async = getattr(entity_index, "list_entries_async", None)
                list_entries = getattr(entity_index, "list_entries", None)
                if callable(list_entries_async):
                    entries = await list_entries_async(domains=_WEATHER_DOMAINS)
                elif callable(list_entries):
                    entries = list_entries(domains=_WEATHER_DOMAINS)
                weather_results = [
                    MatchResult(
                        entity_id=entry.entity_id,
                        friendly_name=entry.friendly_name or entry.entity_id,
                        score=1.0,
                    )
                    for entry in entries
                    if getattr(entry, "entity_id", "").startswith("weather.")
                ]
            except Exception:
                logger.warning("Failed to auto-discover weather entity from entity index", exc_info=True)
        else:
            try:
                states = await ha_client.get_states()
                weather_results = [
                    MatchResult(
                        entity_id=state.get("entity_id", ""),
                        friendly_name=state.get("attributes", {}).get("friendly_name") or state.get("entity_id", ""),
                        score=1.0,
                    )
                    for state in states
                    if state.get("entity_id", "").startswith("weather.")
                ]
            except Exception:
                logger.warning("Failed to auto-discover weather entity", exc_info=True)

        if weather_results:
            try:
                visible_results = await filter_visible_results(
                    effective_agent_id,
                    weather_results,
                    entity_index,
                )
            except Exception:
                logger.warning("Failed to filter visible weather entities", exc_info=True)
                visible_results = weather_results

            if visible_results:
                chosen = sorted(visible_results, key=lambda result: result.entity_id.casefold())[0]
                entity_id = chosen.entity_id
                friendly_name = chosen.friendly_name or chosen.entity_id

    return entity_id, friendly_name, resolution_speech


async def _query_weather(
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
    entity_id, _friendly_name, resolution_speech = await _resolve_weather_entity(
        entity_query,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector=span_collector,
        preferred_area_id=preferred_area_id,
        verbatim_terms=verbatim_terms,
    )
    if not entity_id:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution_speech
            or "No weather entities found in Home Assistant. Please add a weather integration.",
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
        speech = _format_weather_state(entity_id, state_resp)
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": state_resp.get("state"),
            "speech": speech,
            "cacheable": False,
        }
    except Exception as exc:
        logger.error("Weather query failed for %s", entity_id, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to query weather: {exc}",
            "cacheable": False,
        }


async def _query_weather_forecast(
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    parameters: dict[str, Any] | None = None,
    preferred_area_id: str | None = None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    entity_id, friendly_name, resolution_speech = await _resolve_weather_entity(
        entity_query,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector=span_collector,
        preferred_area_id=preferred_area_id,
        verbatim_terms=verbatim_terms,
    )
    if not entity_id:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution_speech
            or "No weather entities found in Home Assistant. Please add a weather integration.",
            "cacheable": False,
        }
    forecast_type = (parameters or {}).get("type", "daily")
    try:
        resp = await ha_client.call_service(
            "weather", "get_forecasts", entity_id, {"type": forecast_type}, return_response=True
        )
        forecasts = []
        if isinstance(resp, dict):
            # HA returns {entity_id: {"forecast": [...]}}
            entity_data = resp.get(entity_id, resp)
            if isinstance(entity_data, dict):
                forecasts = entity_data.get("forecast", [])
            elif isinstance(entity_data, list):
                forecasts = entity_data
        if forecasts:
            speech = f"{friendly_name} forecast: {_format_weather_forecast(forecasts)}"
            return {"success": True, "entity_id": entity_id, "new_state": None, "speech": speech, "cacheable": False}
    except Exception:
        logger.warning(
            "weather.get_forecasts service call failed for %s, falling back to state", entity_id, exc_info=True
        )

    # Fallback: try forecast attribute from entity state
    try:
        state_resp = await ha_client.get_state(entity_id)
        if state_resp:
            forecast_attr = state_resp.get("attributes", {}).get("forecast", [])
            if forecast_attr:
                speech = f"{friendly_name} forecast: {_format_weather_forecast(forecast_attr)}"
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "new_state": None,
                    "speech": speech,
                    "cacheable": False,
                }
    except Exception:
        logger.warning("Fallback forecast query failed for %s", entity_id, exc_info=True)

    return {
        "success": False,
        "entity_id": entity_id,
        "new_state": None,
        "speech": "Forecast data not available.",
        "cacheable": False,
    }


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
    """Fetch Recorder history for a resolved climate/sensor/weather entity (visibility-respected)."""
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
                    allowed_domains=_HISTORY_DOMAINS,
                    preferred_area_id=preferred_area_id,
                    verbatim_terms=verbatim_terms,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = _ensure_str(resolution["entity_id"])
    friendly_name = _ensure_str(resolution["friendly_name"]) or entity_query
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


async def _handle_climate_read_action(
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
    if action_name == "query_climate_state":
        return await _query_climate_state(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "list_climate":
        return await _list_climate(ha_client)
    if action_name == "query_weather":
        return await _query_weather(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            preferred_area_id=preferred_area_id,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "query_weather_forecast":
        return await _query_weather_forecast(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            parameters=params,
            preferred_area_id=preferred_area_id,
            verbatim_terms=verbatim_terms,
        )
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
