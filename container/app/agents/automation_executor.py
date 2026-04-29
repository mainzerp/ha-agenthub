"""Automation-specific action execution via HA automation services."""

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

_AUTOMATION_ACTION_MAP: dict[str, tuple[str, str]] = {
    "enable_automation": ("automation", "turn_on"),
    "disable_automation": ("automation", "turn_off"),
    "trigger_automation": ("automation", "trigger"),
}

# FLOW-VERIFY-SHARED (0.18.5): enable/disable land in deterministic "on"/
# "off". ``trigger_automation`` does NOT change the entity state (the
# automation runs once) -- we keep expected_state=None and rely on intent-
# first speech so we don't falsely claim the automation "is now off" when
# it stays enabled.
_EXPECTED_STATE_BY_ACTION: dict[str, str] = {
    "enable_automation": "on",
    "disable_automation": "off",
}

_ACTION_PHRASES: dict[str, str] = {
    "enable_automation": "enabled",
    "disable_automation": "disabled",
    "trigger_automation": "triggered",
}

_ALLOWED_DOMAINS: frozenset[str] = frozenset({"automation"})

# FLOW-DOMAIN-1 (0.19.2): single-domain agent; the per-action filter
# matches _ALLOWED_DOMAINS today but the helper makes the executor
# regression-proof if the allow-set ever broadens.
_ACTION_DOMAINS: frozenset[str] = frozenset({"automation"})


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def _build_automation_service_data(action: dict) -> dict[str, Any]:
    """Build HA service_data from an automation action's parameters."""
    params = action.get("parameters") or {}
    data: dict[str, Any] = {}

    if "skip_condition" in params:
        data["skip_condition"] = bool(params["skip_condition"])
    if "variables" in params and isinstance(params["variables"], dict):
        data["variables"] = params["variables"]

    return data


async def execute_automation_action(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None = None,
    span_collector=None,
    *,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Resolve an entity, call an automation HA service, and verify the result.

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
    if action_name in ("query_automation_state", "list_automations"):
        return await _handle_automation_read_action(
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
    mapping = _AUTOMATION_ACTION_MAP.get(action_name)
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
    service_data = _build_automation_service_data(action)

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
# Read-only automation action handlers
# ---------------------------------------------------------------------------


def _format_automation_state(entity_id: str, state_resp: dict) -> str:
    state = state_resp.get("state", "unknown")
    attrs = state_resp.get("attributes", {})
    friendly_name = attrs.get("friendly_name", entity_id)
    status = "enabled" if state == "on" else "disabled" if state == "off" else state
    parts = [f"{friendly_name} is {status}"]
    last_triggered = attrs.get("last_triggered")
    if last_triggered:
        parts.append(f"last triggered {last_triggered}")
    return ", ".join(parts) + "."


async def _query_automation_state(
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
        speech = _format_automation_state(entity_id, state_resp)
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
            "speech": f"Failed to query automation status: {exc}",
            "cacheable": False,
        }


async def _list_automations(ha_client: Any) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception as exc:
        logger.error("Failed to fetch states for list_automations", exc_info=True)
        return {"success": False, "entity_id": "", "new_state": None, "speech": f"Failed to list automations: {exc}"}

    automations = [s for s in states if s.get("entity_id", "").startswith("automation.")]

    if not automations:
        return {"success": True, "entity_id": "", "new_state": None, "speech": "No automation entities found."}

    enabled = []
    disabled = []
    for a in automations:
        attrs = a.get("attributes", {})
        name = attrs.get("friendly_name", a.get("entity_id", ""))
        state = a.get("state", "unknown")
        last_triggered = attrs.get("last_triggered")
        info = name
        if last_triggered:
            info += f" (last triggered: {last_triggered})"
        if state == "on":
            enabled.append(info)
        else:
            disabled.append(info)

    parts = []
    if enabled:
        parts.append(f"Enabled ({len(enabled)}): {', '.join(enabled)}")
    if disabled:
        parts.append(f"Disabled ({len(disabled)}): {', '.join(disabled)}")
    speech = ". ".join(parts) + "."
    return {"success": True, "entity_id": "", "new_state": None, "speech": speech, "cacheable": False}


async def _handle_automation_read_action(
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
    if action_name == "query_automation_state":
        return await _query_automation_state(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "list_automations":
        return await _list_automations(ha_client)
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}
