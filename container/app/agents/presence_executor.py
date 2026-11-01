"""Presence queries: read-only answers from ``person.*`` and ``device_tracker.*`` state.

The executor never calls a Home Assistant service. It reads the live state of
the person (or tracker) entities visible to the calling agent and answers
"who is home", "how many people are home", "is <person> home" and "where is
<person>". Only the presence state (``home``, ``not_home``, a zone name) is
spoken; GPS coordinates and other tracker attributes never leave the executor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agents.action_executor import resolve_and_validate_entity
from app.entity.visibility import entity_is_visible

logger = logging.getLogger(__name__)

# Single-entity queries accept persons and device trackers; the person
# domain wins ties so "Laura" resolves to ``person.laura`` rather than her
# phone's tracker.
_ALLOWED_DOMAINS: frozenset[str] = frozenset({"person", "device_tracker"})
_PREFERRED_DOMAIN = "person"
# Lists and counts use persons only: a person aggregates its trackers, so
# counting trackers too would count people twice.
_LIST_DOMAIN_PREFIX = "person."

STATE_HOME = "home"
STATE_NOT_HOME = "not_home"
_UNKNOWN_STATES: frozenset[str] = frozenset({"", "unknown", "unavailable"})

PRESENCE_HOME = "home"
PRESENCE_AWAY = "away"
PRESENCE_ZONE = "zone"
PRESENCE_UNKNOWN = "unknown"

# Entity-candidate declaration (read by the agent's ``@agent`` call, see
# ``ActionableAgent._entity_actions``): ``query_presence`` acts on one
# person or tracker, ``list_presence`` summarizes every visible person.
ENTITY_ACTIONS: frozenset[str] = frozenset({"query_presence"})
ENTITY_FREE_ACTIONS: frozenset[str] = frozenset({"list_presence"})


def _validate_domain(entity_id: str) -> bool:
    """Check that entity_id belongs to an allowed domain for this executor."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    return domain in _ALLOWED_DOMAINS


def classify_presence(state: Any) -> tuple[str, str | None]:
    """Map a person/tracker state to ``(presence, zone_name)``.

    ``home`` and ``not_home`` are Home Assistant's fixed states; any other
    known state is the name of the zone the person is in.
    """
    value = state.strip() if isinstance(state, str) else ""
    if value.lower() in _UNKNOWN_STATES:
        return PRESENCE_UNKNOWN, None
    if value == STATE_HOME:
        return PRESENCE_HOME, None
    if value == STATE_NOT_HOME:
        return PRESENCE_AWAY, None
    return PRESENCE_ZONE, value


def _display_name(entity_id: str, state_resp: dict[str, Any]) -> str:
    attrs = state_resp.get("attributes") or {}
    name = attrs.get("friendly_name") if isinstance(attrs, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return entity_id


def _join_names(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def describe_presence(name: str, state: Any) -> str:
    """One sentence for a single person or tracker."""
    presence, zone = classify_presence(state)
    if presence == PRESENCE_HOME:
        return f"{name} is at home."
    if presence == PRESENCE_AWAY:
        return f"{name} is not at home."
    if presence == PRESENCE_ZONE:
        return f"{name} is not at home, but in the zone {zone}."
    return f"The location of {name} is unknown."


def summarize_presence(people: list[tuple[str, Any]]) -> str:
    """Summary sentence for every visible person: who is home, who is away, how many."""
    total = len(people)
    if total == 0:
        return "No person entities are available."

    home: list[str] = []
    away: list[str] = []
    unknown: list[str] = []
    for name, state in sorted(people, key=lambda item: item[0].casefold()):
        presence, zone = classify_presence(state)
        if presence == PRESENCE_HOME:
            home.append(name)
        elif presence == PRESENCE_AWAY:
            away.append(name)
        elif presence == PRESENCE_ZONE:
            away.append(f"{name} (zone {zone})")
        else:
            unknown.append(name)

    if total == 1:
        return describe_presence(*people[0])

    parts: list[str] = []
    if len(home) == total:
        parts.append(f"Everyone is at home, {total} of {total} people: {_join_names(home)}.")
    elif not home:
        parts.append(f"Nobody is at home, 0 of {total} people.")
    else:
        verb = "is" if len(home) == 1 else "are"
        parts.append(f"{len(home)} of {total} people {verb} at home: {_join_names(home)}.")
    if away:
        parts.append(f"Not at home: {_join_names(away)}.")
    if unknown:
        parts.append(f"Location unknown: {_join_names(unknown)}.")
    return " ".join(parts)


async def _list_presence(ha_client: Any, agent_id: str | None, entity_index: Any) -> dict:
    try:
        states = await ha_client.get_states()
    except Exception:
        logger.error("Failed to fetch states for list_presence", exc_info=True)
        return {
            "success": False,
            "entity_id": "",
            "new_state": None,
            "speech": "Sorry, I could not read who is at home.",
            "cacheable": False,
        }

    person_states = [
        s for s in states or [] if isinstance(s, dict) and str(s.get("entity_id", "")).startswith(_LIST_DOMAIN_PREFIX)
    ]
    if agent_id:
        visibility = await asyncio.gather(
            *[entity_is_visible(agent_id, s.get("entity_id", ""), entity_index) for s in person_states]
        )
        person_states = [s for s, ok in zip(person_states, visibility, strict=True) if ok]

    people = [(_display_name(s.get("entity_id", ""), s), s.get("state")) for s in person_states]
    home_count = sum(1 for _name, state in people if classify_presence(state)[0] == PRESENCE_HOME)
    return {
        "success": True,
        "entity_id": "",
        "new_state": None,
        "speech": summarize_presence(people),
        "cacheable": False,
        "metadata": {"presence_home_count": home_count, "presence_person_count": len(people)},
    }


async def _query_presence(
    entity_query: str,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    span_collector=None,
    *,
    action: dict | None = None,
) -> dict:
    resolved = await resolve_and_validate_entity(
        entity_query,
        entity_index,
        entity_matcher,
        agent_id,
        _ALLOWED_DOMAINS,
        _validate_domain,
        preferred_domain=_PREFERRED_DOMAIN,
        span_collector=span_collector,
        direct_entity_id=action.get("entity_id") if action else None,
    )
    if resolved["not_found_result"] is not None:
        result = dict(resolved["not_found_result"])
        resolution_path = str((result.get("metadata") or {}).get("resolution_path") or "")
        if not resolution_path.endswith("_ambiguous"):
            # Same answer for an unknown and a hidden person: visibility
            # rules must not reveal that a hidden person exists.
            label = (entity_query or "").strip() or "that person"
            result["speech"] = f"I could not find a person named '{label}'."
        result["cacheable"] = False
        return result

    entity_id = resolved["entity_id"]
    try:
        state_resp = await ha_client.get_state(entity_id)
    except Exception:
        logger.error("Presence state query failed for %s", entity_id, exc_info=True)
        state_resp = None
    if not isinstance(state_resp, dict):
        return {
            "success": False,
            "entity_id": entity_id,
            "new_state": None,
            "speech": f"Sorry, I could not read where {resolved['friendly_name'] or entity_id} is.",
            "cacheable": False,
            "metadata": resolved["resolution"].get("metadata", {}),
        }

    state = state_resp.get("state")
    name = _display_name(entity_id, state_resp)
    return {
        "success": True,
        "entity_id": entity_id,
        "new_state": state,
        "speech": describe_presence(name, state),
        "cacheable": False,
        "metadata": resolved["resolution"].get("metadata", {}),
    }


async def execute_presence_action(
    action: dict,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None = None,
    span_collector=None,
) -> dict:
    """Answer a read-only presence question.

    Args:
        action: Parsed action dict with "action", "entity" and optional "entity_id".
        ha_client: HARestClient instance.
        entity_index: EntityIndex instance.
        entity_matcher: EntityMatcher instance.
        agent_id: Calling agent; its visibility rules filter every person and tracker.

    Returns:
        dict with "success", "entity_id", "new_state", "speech" and ``cacheable=False``.
    """
    action_name = str(action.get("action", "")).lower()
    if action_name == "list_presence":
        return await _list_presence(ha_client, agent_id, entity_index)
    if action_name == "query_presence":
        return await _query_presence(
            action.get("entity", "") or "",
            ha_client,
            entity_index,
            entity_matcher,
            agent_id,
            span_collector=span_collector,
            action=action,
        )
    return {
        "success": False,
        "entity_id": None,
        "new_state": None,
        "speech": f"Unknown action: {action_name}",
        "cacheable": False,
    }
