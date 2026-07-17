"""Automation-specific action execution via HA automation services."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from app.agents.action_executor import (
    _ensure_str,
    _synthesize_direct_entity_metadata,
    _validate_direct_entity_id,
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
    "create_automation": "created",
    "update_automation": "updated",
    "delete_automation": "deleted",
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


_ALPHANUM_RE = re.compile(r"[^a-z0-9_]+")


def _sanitize_for_id(text: str) -> str:
    """Lowercase and replace non-alphanumeric runs with single underscores."""
    return _ALPHANUM_RE.sub("_", text.lower()).strip("_")


def _generate_automation_id(alias: str | None = None) -> str:
    """Generate an ah_-prefixed automation ID."""
    if alias:
        base = _sanitize_for_id(alias)
        if base:
            return f"ah_{base}"
    return f"ah_{uuid.uuid4().hex[:8]}"


async def _ensure_unique_automation_id(ha_client: Any, alias: str | None = None) -> str:
    """Generate an ah_ ID and verify via GET that it does not already exist."""
    base_id = _generate_automation_id(alias)
    existing = await ha_client.get_automation_config(base_id)
    if existing is None:
        return base_id
    for counter in range(2, 100):
        candidate = f"{base_id}_{counter}"
        existing = await ha_client.get_automation_config(candidate)
        if existing is None:
            return candidate
    return f"ah_{uuid.uuid4().hex[:8]}"


async def _resolve_config_id_from_entity(entity_id: str, ha_client: Any) -> str | None:
    """Read the automation config id from an automation entity's state attributes."""
    state = await ha_client.get_state(entity_id)
    if not state:
        return None
    return state.get("attributes", {}).get("id")


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

    # Config CRUD actions (no HA service call)
    if action_name in ("create_automation", "update_automation", "delete_automation", "get_automation_config"):
        return await _handle_automation_config_action(
            action_name,
            action,
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
        )

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
            action=action,
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
        speech = _format_automation_state(entity_id, state_resp)
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
            "speech": f"Failed to query automation status: {exc}",
            "cacheable": False,
            "metadata": resolution_metadata,
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
        config_id = attrs.get("id", "")
        info = name
        if config_id and config_id.startswith("ah_"):
            info += " (AgentHub)"
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
    action: dict | None = None,
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
            action=action,
        )
    if action_name == "list_automations":
        return await _list_automations(ha_client)
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}


async def _handle_automation_config_action(
    action_name: str,
    action: dict,
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    verbatim_terms: list[str] | None = None,
) -> dict:
    if action_name == "create_automation":
        return await _create_automation(action, entity_query, ha_client)
    if action_name == "update_automation":
        return await _update_automation(
            action,
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "delete_automation":
        return await _delete_automation(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
        )
    if action_name == "get_automation_config":
        return await _get_automation_config(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
            action=action,
        )
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown config action: {action_name}"}


async def _create_automation(action: dict, entity_query: str, ha_client: Any) -> dict:
    params = action.get("parameters") or {}
    config = params.get("config") or {}
    if not isinstance(config, dict):
        return {"success": False, "entity_id": None, "new_state": None, "speech": "Invalid automation configuration."}
    alias = config.get("alias") or entity_query or "AgentHub Automation"
    config["alias"] = alias
    try:
        automation_id = await _ensure_unique_automation_id(ha_client, alias)
        await ha_client.save_automation_config(automation_id, config)
        return {
            "success": True,
            "entity_id": automation_id,
            "new_state": None,
            "speech": f"Done, automation '{alias}' has been created.",
        }
    except Exception as exc:
        logger.error("Failed to create automation: %s", exc, exc_info=True)
        return {"success": False, "entity_id": None, "new_state": None, "speech": f"Failed to create automation: {exc}"}


async def _update_automation(
    action: dict,
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    verbatim_terms: list[str] | None = None,
) -> dict:
    resolution = await _resolve_automation_entity(
        entity_query,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector=span_collector,
        verbatim_terms=verbatim_terms,
    )
    if not resolution["success"]:
        return resolution
    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    config_id = await _resolve_config_id_from_entity(entity_id, ha_client)
    if not config_id:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Could not find an editable configuration for '{friendly_name}'.",
        }
    params = action.get("parameters") or {}
    config = params.get("config") or {}
    if not isinstance(config, dict):
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": "Invalid automation configuration.",
        }
    if not config.get("alias"):
        config["alias"] = friendly_name
    try:
        await ha_client.save_automation_config(config_id, config)
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Done, automation '{friendly_name}' has been updated.",
        }
    except Exception as exc:
        logger.error("Failed to update automation %s: %s", config_id, exc, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to update automation: {exc}",
        }


async def _delete_automation(
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    verbatim_terms: list[str] | None = None,
) -> dict:
    resolution = await _resolve_automation_entity(
        entity_query,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector=span_collector,
        verbatim_terms=verbatim_terms,
    )
    if not resolution["success"]:
        return resolution
    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    config_id = await _resolve_config_id_from_entity(entity_id, ha_client)
    if not config_id:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Could not find an editable configuration for '{friendly_name}'.",
        }
    try:
        await ha_client.delete_automation_config(config_id)
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Done, automation '{friendly_name}' has been deleted.",
        }
    except Exception as exc:
        logger.error("Failed to delete automation %s: %s", config_id, exc, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to delete automation: {exc}",
        }


async def _get_automation_config(
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    verbatim_terms: list[str] | None = None,
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
        friendly_name = _synthesize_direct_entity_metadata(entity_id, entity_index).get("top_friendly_name", entity_id)
        resolution_metadata = _synthesize_direct_entity_metadata(entity_id, entity_index)
    else:
        resolution = await _resolve_automation_entity(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
        )
        if not resolution["success"]:
            return resolution
        entity_id = resolution["entity_id"]
        friendly_name = resolution["friendly_name"]
        resolution_metadata = {}

    config_id = await _resolve_config_id_from_entity(entity_id, ha_client)
    if not config_id:
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Could not find an editable configuration for '{friendly_name}'.",
            "metadata": resolution_metadata,
        }
    try:
        config = await ha_client.get_automation_config(config_id)
        if not config:
            return {
                "success": False,
                "entity_id": entity_id,
                "new_state": None,
                "speech": f"Could not retrieve configuration for '{friendly_name}'.",
                "metadata": resolution_metadata,
            }
        alias = config.get("alias", friendly_name)
        triggers = config.get("triggers") or config.get("trigger") or []
        conditions = config.get("conditions") or config.get("condition") or []
        actions = config.get("actions") or config.get("action") or []
        t_count = len(triggers) if isinstance(triggers, list) else 1
        c_count = len(conditions) if isinstance(conditions, list) else (1 if conditions else 0)
        a_count = len(actions) if isinstance(actions, list) else 1
        speech = f"{alias} has {t_count} trigger(s), {c_count} condition(s), and {a_count} action(s)."
        return {
            "success": True,
            "entity_id": entity_id,
            "new_state": None,
            "speech": speech,
            "metadata": {**resolution_metadata, "config": config},
        }
    except Exception as exc:
        logger.error("Failed to get automation config %s: %s", config_id, exc, exc_info=True)
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Failed to retrieve automation config: {exc}",
            "metadata": resolution_metadata,
        }


async def _resolve_automation_entity(
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Shared entity resolver for update/delete/get_config. Returns a dict with success/entity_id/friendly_name/speech keys."""
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

    return {"success": True, "entity_id": entity_id, "friendly_name": friendly_name, "speech": None}
