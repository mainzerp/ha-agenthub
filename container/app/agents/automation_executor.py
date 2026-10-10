"""Automation-specific action execution via HA automation services.

enable/disable/trigger execute immediately. create/update/delete never
write on the first turn: the change is validated (entities against the
index and automation-agent visibility, services against an allow-list),
stored as a pending proposal (see ``automation_confirmation``) and only
applied after the user confirms on a later turn. Updates are a merge/patch
of the config fetched from HA, never an LLM-invented full replacement.
"""

from __future__ import annotations

import asyncio
import copy
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
from app.agents.automation_confirmation import PendingAutomationChange, confirmation_store
from app.analytics.tracer import _optional_span
from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.entity.visibility import entity_is_visible

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
    conversation_id: str | None = None,
) -> dict:
    """Resolve an entity, call an automation HA service, and verify the result.

    Args:
        action: Parsed action dict with "action", "entity", and optional "parameters".
        ha_client: HARestClient instance.
        entity_index: EntityIndex instance.
        entity_matcher: EntityMatcher instance.
        agent_id: Optional agent identifier for entity matching context.
        conversation_id: Key for the pending-confirmation state of
            create/update/delete; without it those actions are refused.

    Returns:
        dict with "success", "entity_id", "new_state", and "speech".
    """
    action_name = action.get("action", "").lower()
    entity_query = action.get("entity", "")

    # Config CRUD actions (no HA service call; writes only after confirmation)
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
            conversation_id=conversation_id,
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

    # LLM-picked entity_id is validation input only (Directive 4): the
    # same fail-closed gate as the shared helper, applied inline.
    not_found_speech: str | None = None
    entity_id_direct = await _validate_direct_entity_id(
        action.get("entity_id"),
        _validate_domain,
        agent_id=agent_id,
        entity_index=entity_index,
        allowed_domains=_ACTION_DOMAINS,
    )
    if entity_id_direct:
        entity_id = entity_id_direct
        friendly_name = _synthesize_direct_entity_metadata(entity_id, entity_index).get("top_friendly_name", entity_id)
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
            not_found_speech = resolution["speech"]

    if not entity_id:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": not_found_speech or f"Could not find an entity matching '{entity_query}'.",
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


async def _list_automations(ha_client: Any, agent_id: str | None = None, entity_index: Any = None) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception as exc:
        logger.error("Failed to fetch states for list_automations", exc_info=True)
        return {"success": False, "entity_id": "", "new_state": None, "speech": f"Failed to list automations: {exc}"}

    automations = [s for s in states if s.get("entity_id", "").startswith("automation.")]
    if agent_id and entity_index is not None:
        visibility = await asyncio.gather(
            *[entity_is_visible(agent_id, s.get("entity_id", ""), entity_index) for s in automations]
        )
        automations = [s for s, ok in zip(automations, visibility, strict=True) if ok]

    if not automations:
        return {"success": True, "entity_id": "", "new_state": None, "speech": "No automation entities found."}

    enabled = []
    disabled = []
    for a in automations:
        name = a.get("attributes", {}).get("friendly_name", a.get("entity_id", ""))
        if a.get("state", "unknown") == "on":
            enabled.append(name)
        else:
            disabled.append(name)

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
            action=action,
        )
    if action_name == "list_automations":
        return await _list_automations(ha_client, agent_id=agent_id, entity_index=entity_index)
    return {"success": False, "entity_id": "", "new_state": None, "speech": f"Unknown read action: {action_name}"}


# ---------------------------------------------------------------------------
# Automation config changes: validate -> propose -> confirm -> write
# ---------------------------------------------------------------------------

# Service domains an LLM-built automation may call. ``None`` allows every
# service of the domain; a set restricts it. Anything else (homeassistant,
# shell_command, python_script, rest_command, hassio, recorder, ...) is
# rejected. Security-sensitive domains only allow the "safer" direction.
_SERVICE_ALLOWLIST: dict[str, frozenset[str] | None] = {
    "light": None,
    "switch": None,
    "fan": None,
    "cover": None,
    "climate": None,
    "humidifier": None,
    "water_heater": None,
    "media_player": None,
    "remote": None,
    "vacuum": None,
    "lawn_mower": None,
    "valve": None,
    "siren": None,
    "script": None,
    "notify": None,
    "tts": None,
    "persistent_notification": None,
    "input_boolean": None,
    "input_number": None,
    "input_select": None,
    "input_text": None,
    "input_datetime": None,
    "input_button": None,
    "button": None,
    "number": None,
    "select": None,
    "counter": None,
    "timer": None,
    "todo": None,
    # scene.apply/scene.create take entity maps outside ``entity_id``.
    "scene": frozenset({"turn_on"}),
    "lock": frozenset({"lock"}),
    "alarm_control_panel": frozenset(
        {"alarm_arm_home", "alarm_arm_away", "alarm_arm_night", "alarm_arm_vacation", "alarm_arm_custom_bypass"}
    ),
    "automation": frozenset({"turn_on", "turn_off", "trigger"}),
}
_SCRIPT_GENERIC_SERVICES: frozenset[str] = frozenset({"turn_on", "turn_off", "toggle"})
# Target forms that bypass per-entity visibility checks.
_UNSUPPORTED_TARGET_KEYS: frozenset[str] = frozenset({"device_id", "area_id", "floor_id", "label_id"})
_ENTITY_KEYS: frozenset[str] = frozenset({"entity_id", "media_player_entity_id"})
_ENTITY_STRING_KEYS: frozenset[str] = frozenset({"scene", "zone"})
_TEMPLATE_MARKERS: tuple[str, ...] = ("{{", "{%")

_SECTION_ALIASES: dict[str, str] = {
    "trigger": "triggers",
    "triggers": "triggers",
    "condition": "conditions",
    "conditions": "conditions",
    "action": "actions",
    "actions": "actions",
}
_LEGACY_SECTION_KEY: dict[str, str] = {"triggers": "trigger", "conditions": "condition", "actions": "action"}
_SCALAR_KEYS: frozenset[str] = frozenset({"alias", "description", "mode", "max", "max_exceeded"})

_SAVE_QUESTION = "Shall I save this?"
_DELETE_QUESTION = "Shall I delete it?"


def _cfg_result(success: bool, speech: str, entity_id: str | None = "", **extra: Any) -> dict:
    """Config-action result. ``entity_id`` defaults to "" so a validation
    failure is not rewritten into an entity-not-found clarification."""
    result: dict[str, Any] = {
        "success": success,
        "entity_id": entity_id,
        "new_state": None,
        "speech": speech,
        "cacheable": False,
    }
    result.update(extra)
    return result


def _section(config: dict[str, Any], canonical: str) -> list[Any]:
    value = config.get(canonical)
    if value is None:
        value = config.get(_LEGACY_SECTION_KEY[canonical])
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _section_key(config: dict[str, Any], canonical: str) -> str:
    """Key the existing config uses for a section (legacy singular or plural)."""
    legacy = _LEGACY_SECTION_KEY[canonical]
    if legacy in config and canonical not in config:
        return legacy
    return canonical


def _has_template(value: str) -> bool:
    return any(marker in value for marker in _TEMPLATE_MARKERS)


def _collect_references(config: dict[str, Any]) -> tuple[set[str], list[str], list[str]]:
    """Collect entity ids, service names and structural problems from a config.

    Services are only collected from the actions section (including nested
    choose/if/sequence/parallel/repeat blocks).
    """
    entities: set[str] = set()
    services: list[str] = []
    problems: list[str] = []

    def add_problem(text: str) -> None:
        if text not in problems:
            problems.append(text)

    def add_entities(value: Any) -> None:
        values = value if isinstance(value, list) else [value]
        for raw in values:
            if not isinstance(raw, str):
                add_problem("entity_id values must be plain entity ids")
                continue
            if _has_template(raw):
                add_problem("templated entity_id values are not supported")
                continue
            for part in raw.split(","):
                entity_id = part.strip().lower()
                if not entity_id:
                    continue
                if entity_id in ("all", "none") or "." not in entity_id:
                    add_problem(f"entity_id '{entity_id}' is not supported, name the entities explicitly")
                    continue
                entities.add(entity_id)

    def add_service(value: str) -> None:
        if _has_template(value):
            add_problem("templated service names are not supported")
            return
        name = value.strip().lower()
        if "." not in name:
            add_problem(f"invalid service '{value}'")
            return
        services.append(name)

    def visit(node: Any, in_actions: bool) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item, in_actions)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in _UNSUPPORTED_TARGET_KEYS:
                if value:
                    add_problem(f"'{key}' targets are not supported, name the entities instead")
                continue
            if key in _ENTITY_KEYS:
                add_entities(value)
                continue
            if key in _ENTITY_STRING_KEYS and isinstance(value, str) and "." in value:
                add_entities(value)
                continue
            if in_actions and key in ("service", "action") and isinstance(value, str):
                add_service(value)
                continue
            if in_actions and key == "service_template":
                add_problem("templated service names are not supported")
                continue
            visit(value, in_actions)

    for key, value in config.items():
        canonical = _SECTION_ALIASES.get(key)
        visit(value, canonical == "actions")
    return entities, services, problems


def _service_allowed(service: str) -> bool:
    domain, _, name = service.partition(".")
    if domain not in _SERVICE_ALLOWLIST:
        return False
    allowed = _SERVICE_ALLOWLIST[domain]
    return allowed is None or name in allowed


async def _entity_allowed(entity_id: str, entity_index: Any, agent_id: str | None) -> bool:
    if entity_index is None:
        return False
    validated = await _validate_direct_entity_id(
        entity_id,
        lambda eid: "." in eid,
        agent_id=agent_id or "automation-agent",
        entity_index=entity_index,
    )
    return validated == entity_id


async def _validate_automation_config(config: dict[str, Any], entity_index: Any, agent_id: str | None) -> list[str]:
    """Fail-closed validation of every referenced entity and service.

    Returns human-readable problems; empty means the config may be proposed.
    """
    entities, services, problems = _collect_references(config)
    rejected_services = sorted({s for s in services if not _service_allowed(s)})
    for service in services:
        domain, _, name = service.partition(".")
        if domain == "script" and name not in _SCRIPT_GENERIC_SERVICES:
            # ``script.<name>`` runs that script: validate it like an entity.
            entities.add(f"script.{name}")
    rejected_entities = [eid for eid in sorted(entities) if not await _entity_allowed(eid, entity_index, agent_id)]
    messages = list(problems)
    if rejected_entities:
        messages.append("unknown or not permitted entities: " + ", ".join(rejected_entities))
    if rejected_services:
        messages.append("services not permitted: " + ", ".join(rejected_services))
    return messages


def _describe_config(config: dict[str, Any]) -> str:
    triggers = _section(config, "triggers")
    conditions = _section(config, "conditions")
    actions = _section(config, "actions")
    entities, services, _ = _collect_references(config)
    text = f"{len(triggers)} trigger(s), {len(conditions)} condition(s), {len(actions)} action(s)"
    if services:
        text += f"; calls {', '.join(sorted(set(services)))}"
    if entities:
        text += f"; uses {', '.join(sorted(entities))}"
    return text


def _rejection(problems: list[str], verb: str) -> dict:
    return _cfg_result(False, f"I cannot {verb} this automation: " + "; ".join(problems) + ".")


def _proposal_result(change: PendingAutomationChange, conversation_id: str) -> dict:
    confirmation_store.put(conversation_id, change)
    return _cfg_result(
        True,
        f"{change.summary} {change.question}",
        change.entity_id or "",
        voice_followup=True,
        metadata={"pending_confirmation": change.kind},
    )


def _no_confirmation_channel() -> dict:
    return _cfg_result(False, "Automation changes need a confirmation step, which is not available for this request.")


async def _handle_automation_config_action(
    action_name: str,
    action: dict,
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    conversation_id: str | None = None,
) -> dict:
    if action_name == "get_automation_config":
        return await _get_automation_config(
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            action=action,
        )
    if not conversation_id:
        return _no_confirmation_channel()
    if action_name == "create_automation":
        return await _propose_create(action, entity_query, entity_index, agent_id, conversation_id)
    if action_name == "update_automation":
        return await _propose_update(
            action,
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            conversation_id,
            span_collector=span_collector,
        )
    if action_name == "delete_automation":
        return await _propose_delete(
            action,
            entity_query,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            conversation_id,
            span_collector=span_collector,
        )
    return _cfg_result(False, f"Unknown config action: {action_name}")


async def _propose_create(
    action: dict,
    entity_query: str,
    entity_index: Any,
    agent_id: str | None,
    conversation_id: str,
) -> dict:
    params = action.get("parameters") or {}
    config = params.get("config")
    if not isinstance(config, dict) or not config:
        return _cfg_result(False, "Invalid automation configuration.")
    config = copy.deepcopy(config)
    config.pop("id", None)
    alias = str(config.get("alias") or entity_query or "AgentHub Automation").strip()
    config["alias"] = alias
    if not _section(config, "triggers") or not _section(config, "actions"):
        return _cfg_result(False, "An automation needs at least one trigger and one action.")

    problems = await _validate_automation_config(config, entity_index, agent_id)
    if problems:
        return _rejection(problems, "create")

    change = PendingAutomationChange(
        kind="create",
        alias=alias,
        summary=f"New automation '{alias}': {_describe_config(config)}.",
        question=_SAVE_QUESTION,
        config=config,
        agent_id=agent_id or "automation-agent",
    )
    return _proposal_result(change, conversation_id)


def _extract_patch(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[Any]], list[str]]:
    """Split update parameters into ``set`` (replace a key) and ``add`` (append to a section).

    ``config`` is accepted for compatibility and treated like ``set``: each
    provided top-level key replaces only that key of the existing config.
    """
    set_map: dict[str, Any] = {}
    add_map: dict[str, list[Any]] = {}
    ignored: list[str] = []
    for source_key in ("config", "set"):
        source = params.get(source_key)
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            canonical = _SECTION_ALIASES.get(key)
            if canonical:
                set_map[canonical] = value if isinstance(value, list) else [value]
            elif key in _SCALAR_KEYS:
                set_map[key] = value
            elif key != "id":
                ignored.append(key)
    add = params.get("add")
    if isinstance(add, dict):
        for key, value in add.items():
            canonical = _SECTION_ALIASES.get(key)
            if not canonical:
                ignored.append(key)
                continue
            items = value if isinstance(value, list) else [value]
            add_map.setdefault(canonical, []).extend(items)
    return set_map, add_map, ignored


def _apply_patch(existing: dict[str, Any], set_map: dict[str, Any], add_map: dict[str, list[Any]]) -> dict[str, Any]:
    merged = copy.deepcopy(existing)
    for key, value in set_map.items():
        if key in _LEGACY_SECTION_KEY:
            target = _section_key(merged, key)
            other = _LEGACY_SECTION_KEY[key] if target == key else key
            merged.pop(other, None)
            merged[target] = copy.deepcopy(value)
        else:
            merged[key] = value
    for key, items in add_map.items():
        target = _section_key(merged, key)
        merged[target] = _section(merged, key) + copy.deepcopy(items)
    if "id" in existing:
        merged["id"] = existing["id"]
    return merged


def _describe_patch(set_map: dict[str, Any], add_map: dict[str, list[Any]]) -> str:
    parts: list[str] = []
    for key, value in set_map.items():
        if key in _LEGACY_SECTION_KEY:
            parts.append(f"replace {key} with {len(value)} item(s)")
        else:
            parts.append(f"set {key} to '{value}'")
    for key, items in add_map.items():
        parts.append(f"add {len(items)} {key[:-1] if len(items) == 1 else key}")
    entities, services, _ = _collect_references(_patch_config(set_map, add_map))
    text = ", ".join(parts)
    if services:
        text += f"; calls {', '.join(sorted(set(services)))}"
    if entities:
        text += f"; uses {', '.join(sorted(entities))}"
    return text


def _patch_config(set_map: dict[str, Any], add_map: dict[str, list[Any]]) -> dict[str, Any]:
    """The new parts of an update, shaped as a config for validation."""
    patch: dict[str, Any] = {k: v for k, v in set_map.items() if k in _LEGACY_SECTION_KEY}
    for key, items in add_map.items():
        patch[key] = list(patch.get(key, [])) + list(items)
    return patch


async def _propose_update(
    action: dict,
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    conversation_id: str,
    span_collector=None,
) -> dict:
    resolution = await _resolve_automation_entity(
        entity_query,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector=span_collector,
        action=action,
    )
    if not resolution["success"]:
        return resolution
    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    config_id = await _resolve_config_id_from_entity(entity_id, ha_client)
    if not config_id:
        return _cfg_result(False, f"Could not find an editable configuration for '{friendly_name}'.", entity_id)

    set_map, add_map, ignored = _extract_patch(action.get("parameters") or {})
    if not set_map and not add_map:
        return _cfg_result(False, f"No changes were specified for '{friendly_name}'.", entity_id)

    try:
        existing = await ha_client.get_automation_config(config_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Failed to read automation config %s", config_id, exc_info=True)
        return _cfg_result(False, f"Failed to read the current automation: {exc}", entity_id)
    if not isinstance(existing, dict) or not existing:
        return _cfg_result(False, f"Could not retrieve the configuration of '{friendly_name}'.", entity_id)

    merged = _apply_patch(existing, set_map, add_map)
    if merged == existing:
        return _cfg_result(False, f"That would not change '{friendly_name}'.", entity_id)
    if any(key in set_map and not set_map[key] for key in ("triggers", "actions")):
        return _cfg_result(False, "An automation needs at least one trigger and one action.", entity_id)

    patch_config = _patch_config(set_map, add_map)
    problems = await _validate_automation_config(patch_config, entity_index, agent_id)
    if problems:
        return _rejection(problems, "update")

    summary = f"Update '{friendly_name}': {_describe_patch(set_map, add_map)}."
    if ignored:
        summary += f" Ignored unsupported fields: {', '.join(sorted(set(ignored)))}."
    change = PendingAutomationChange(
        kind="update",
        alias=str(merged.get("alias") or friendly_name),
        summary=summary,
        question=_SAVE_QUESTION,
        config=merged,
        config_id=config_id,
        entity_id=entity_id,
        base_config=existing,
        patch=patch_config,
        agent_id=agent_id or "automation-agent",
    )
    return _proposal_result(change, conversation_id)


async def _propose_delete(
    action: dict,
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    conversation_id: str,
    span_collector=None,
) -> dict:
    resolution = await _resolve_automation_entity(
        entity_query,
        ha_client,
        entity_index,
        entity_matcher,
        agent_id,
        span_collector=span_collector,
        action=action,
    )
    if not resolution["success"]:
        return resolution
    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    config_id = await _resolve_config_id_from_entity(entity_id, ha_client)
    if not config_id:
        return _cfg_result(False, f"Could not find an editable configuration for '{friendly_name}'.", entity_id)
    change = PendingAutomationChange(
        kind="delete",
        alias=friendly_name,
        summary=f"This permanently deletes the automation '{friendly_name}'.",
        question=_DELETE_QUESTION,
        config_id=config_id,
        entity_id=entity_id,
        agent_id=agent_id or "automation-agent",
    )
    return _proposal_result(change, conversation_id)


async def apply_pending_automation_change(
    change: PendingAutomationChange,
    ha_client: Any,
    entity_index: Any,
    *,
    agent_id: str | None = None,
) -> dict:
    """Write a confirmed change to Home Assistant (called only after a confirmation)."""
    if ha_client is None:
        return _cfg_result(False, "The smart home connection is currently unavailable.", change.entity_id)

    if change.kind == "create":
        config = change.config or {}
        problems = await _validate_automation_config(config, entity_index, agent_id)
        if problems:
            return _rejection(problems, "create")
        try:
            automation_id = await _ensure_unique_automation_id(ha_client, change.alias)
            await ha_client.save_automation_config(automation_id, config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Failed to create automation: %s", exc, exc_info=True)
            return _cfg_result(False, f"Failed to create automation: {exc}", "")
        return _cfg_result(True, f"Done, automation '{change.alias}' has been created.", automation_id)

    if change.kind == "update":
        problems = await _validate_automation_config(change.patch, entity_index, agent_id)
        if problems:
            return _rejection(problems, "update")
        try:
            current = await ha_client.get_automation_config(change.config_id)
            if current != change.base_config:
                return _cfg_result(
                    False,
                    f"The automation '{change.alias}' changed in the meantime, so I did not save. Please ask again.",
                    change.entity_id,
                )
            await ha_client.save_automation_config(change.config_id, change.config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Failed to update automation %s: %s", change.config_id, exc, exc_info=True)
            return _cfg_result(False, f"Failed to update automation: {exc}", change.entity_id)
        return _cfg_result(True, f"Done, automation '{change.alias}' has been updated.", change.entity_id)

    if change.kind == "delete":
        try:
            await ha_client.delete_automation_config(change.config_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Failed to delete automation %s: %s", change.config_id, exc, exc_info=True)
            return _cfg_result(False, f"Failed to delete automation: {exc}", change.entity_id)
        return _cfg_result(True, f"Done, automation '{change.alias}' has been deleted.", change.entity_id)

    return _cfg_result(False, f"Unknown automation change: {change.kind}", change.entity_id)


async def _get_automation_config(
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
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
            # Read of live config: never replayed from the action cache.
            "cacheable": False,
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
    action: dict | None = None,
) -> dict:
    """Shared entity resolver for update/delete/get_config. Returns a dict with success/entity_id/friendly_name/speech keys."""
    # LLM-picked entity_id is validation input only (Directive 4): the
    # same fail-closed gate as the shared helper, applied inline.
    entity_id_direct = await _validate_direct_entity_id(
        action.get("entity_id") if action else None,
        _validate_domain,
        agent_id=agent_id,
        entity_index=entity_index,
        allowed_domains=_ACTION_DOMAINS,
    )
    if entity_id_direct:
        friendly_name = _synthesize_direct_entity_metadata(entity_id_direct, entity_index).get(
            "top_friendly_name", entity_id_direct
        )
        return {"success": True, "entity_id": entity_id_direct, "friendly_name": friendly_name, "speech": None}

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
