"""Shared action parsing, execution, and verification for domain agents."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.db.repositories.settings import _settings_float
from app.entity.deterministic_resolver import (
    filter_matches_by_domain,  # noqa: F401  -- re-exported for test compat
    resolve_entity_deterministic_first,
)
from app.entity.visibility import _index_has_async_get_by_id, entity_is_visible
from app.ha_client.rest import mark_verified_ha_service_call

logger = logging.getLogger(__name__)

_READ_ONLY_ACTION_PREFIXES: tuple[str, ...] = ("query_", "list_", "get_")

# Per-request visible-entries snapshot (P2 resolver efficiency). Set by
# ActionableAgent after its pre-LLM resolution pass so the post-LLM
# ``resolve_and_validate_entity`` call reuses the same per-request,
# visibility-filtered snapshot instead of re-listing the whole index.
# Directive 5 is preserved: the snapshot is always computed fresh per
# request (per agent) and the exact-id path keeps its own visibility
# check. Value shape: ``(allowed_domains, agent_id, entries)``.
_request_visible_entries: contextvars.ContextVar[tuple[frozenset[str] | None, str | None, list[Any]] | None] = (
    contextvars.ContextVar("action_executor_visible_entries", default=None)
)


def set_request_visible_entries(
    allowed_domains: frozenset[str] | None,
    agent_id: str | None,
    entries: list[Any] | None,
) -> contextvars.Token:
    """Publish the per-request visible-entries snapshot (``None`` clears)."""
    value = (allowed_domains, agent_id, entries) if entries else None
    return _request_visible_entries.set(value)


def reset_request_visible_entries(token: contextvars.Token) -> None:
    """Restore the snapshot published before ``token`` was created."""
    _request_visible_entries.reset(token)


# Per-request closed-contract candidate-id gate (ENTITY_RESOLUTION_REWORK).
# Published by ActionableAgent after its pre-LLM keyword recall: the set of
# entity_ids the agent LLM may legitimately emit (recalled candidates plus
# last_entities anaphora hints). ``None`` means "no gate" (legacy/direct
# callers); an empty set means the recall found nothing and EVERY
# LLM-supplied entity_id is rejected fail-closed.
_request_candidate_ids: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "action_executor_candidate_ids", default=None
)


def set_request_candidate_ids(
    candidate_ids: set[str] | frozenset[str] | None,
) -> contextvars.Token:
    """Publish the per-request candidate-id gate (``None`` clears)."""
    value = frozenset(candidate_ids) if candidate_ids is not None else None
    return _request_candidate_ids.set(value)


def reset_request_candidate_ids(token: contextvars.Token) -> None:
    """Restore the gate published before ``token`` was created."""
    _request_candidate_ids.reset(token)


def is_read_only_action(action_name: str) -> bool:
    return action_name.lower().startswith(_READ_ONLY_ACTION_PREFIXES)


async def _validate_direct_entity_id(
    entity_id: str | None,
    validate_domain_fn,
    *,
    agent_id: str | None = None,
    entity_index: Any | None = None,
    allowed_domains: frozenset[str] | None = None,
) -> str | None:
    """Validate an LLM-supplied direct entity_id (fail-closed on every stage).

    Gate order: domain validator -> per-action allowed domains -> index
    existence -> per-agent visibility. A check that errors or cannot
    confirm the id rejects it; the caller then falls back to
    deterministic-first resolution (Directive 4: an LLM-proposed
    entity_id is validation input, never a trusted selection).

    ``allowed_domains``: optional PER-ACTION domain set (e.g. light's
    ``_ACTION_DOMAINS_LIGHT``). Write paths must pass their per-action
    set, not the broad read-side ``_ALLOWED_DOMAINS``, so e.g.
    ``set_brightness`` on ``switch.*`` is rejected.
    """
    if not entity_id:
        return None
    if not validate_domain_fn(entity_id):
        logger.warning("Direct entity_id %s rejected by domain validator", entity_id)
        return None
    if allowed_domains is not None:
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        if domain not in allowed_domains:
            logger.warning(
                "Direct entity_id %s rejected: domain '%s' not in per-action allowed domains %s",
                entity_id,
                domain,
                sorted(allowed_domains),
            )
            return None
    if entity_index is not None:
        try:
            if _index_has_async_get_by_id(entity_index):
                entry = await entity_index.get_by_id_async(entity_id)
            else:
                entry = entity_index.get_by_id(entity_id)
        except Exception:
            logger.warning(
                "Existence check failed for direct entity_id %s; rejecting (fail-closed)",
                entity_id,
                exc_info=True,
            )
            return None
        if entry is None:
            logger.warning(
                "Direct entity_id %s not found in entity index; rejecting (fail-closed)",
                entity_id,
            )
            return None
    if agent_id:
        try:
            visible = await entity_is_visible(agent_id, entity_id, entity_index)
        except Exception:
            logger.warning(
                "Visibility check failed for direct entity_id %s; rejecting (fail-closed)",
                entity_id,
                exc_info=True,
            )
            return None
        if not visible:
            logger.warning("Direct entity_id %s not visible to agent %s", entity_id, agent_id)
            return None
    return entity_id


def _synthesize_direct_entity_metadata(entity_id: str, entity_index: Any | None = None) -> dict[str, Any]:
    """Build resolution metadata when the LLM supplied entity_id directly."""
    friendly_name = entity_id
    if entity_index is not None and hasattr(entity_index, "get_by_id"):
        with contextlib.suppress(Exception):
            entry = entity_index.get_by_id(entity_id)
            if entry:
                friendly_name = getattr(entry, "friendly_name", None) or entity_id
    return {
        "query": entity_id,
        "normalized_query": entity_id,
        "resolution_path": "llm_entity_id",
        "match_count": 1,
        "top_entity_id": entity_id,
        "top_friendly_name": friendly_name,
        "candidate_entities": [],
    }


async def resolve_and_validate_entity(
    entity_query: str,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None,
    allowed_domains: frozenset[str],
    validate_domain_fn,
    *,
    preferred_area_id: str | None = None,
    enable_strip_device_noun: bool = False,
    enable_area_fallback: bool = False,
    preferred_domain: str | None = None,
    span_collector=None,
    require_matcher: bool = False,
    visible_entries: list[Any] | None = None,
    direct_entity_id: str | None = None,
    allowed_entity_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Resolve an entity query deterministically and validate its domain.

    Encapsulates the common entity resolution block used by every domain
    executor: init dict -> resolve_entity_deterministic_first -> domain
    validation -> not-found fallback.

    ``visible_entries``: optional per-request, visibility-filtered
    candidate snapshot forwarded to the resolver. When omitted, the
    snapshot published by ``set_request_visible_entries`` (same request,
    same agent) is reused -- but only when its domain coverage is a
    superset of ``allowed_domains``; otherwise a fresh listing is used.

    ``direct_entity_id``: optional entity_id proposed by the agent LLM.
    It is treated strictly as validation INPUT (Directive 4): it is
    accepted only after passing ``_validate_direct_entity_id`` with the
    caller's per-action ``allowed_domains`` (domain + per-action domain +
    index existence + visibility, all fail-closed). When validation
    fails, resolution falls back to the normal deterministic-first
    pipeline -- UNLESS ``allowed_entity_ids`` is active (see below).

    ``allowed_entity_ids``: optional closed-contract candidate set
    (ENTITY_RESOLUTION_REWORK). When omitted, the set published by
    ``set_request_candidate_ids`` (same request) is used; ``None`` there
    means no gate. When the gate is active, an LLM-supplied
    ``direct_entity_id`` is accepted iff it is in the set AND passes
    ``_validate_direct_entity_id``; otherwise the call returns a
    not-found result immediately (fail-closed, no matcher re-run), which
    triggers the agent's clarifying-question path. The free-form
    ``entity`` path is unaffected and keeps deterministic-first
    resolution.

    Returns a dict with keys:
        entity_id: resolved and validated entity_id (None if not found)
        friendly_name: friendly name of the resolved entity
        resolution: the full resolution dict
        not_found_result: only present when entity_id is None; return this
            directly after adding any caller-specific extra keys
            (e.g. ``cacheable``, ``voice_followup``).
    """
    from app.analytics.tracer import _optional_span

    if allowed_entity_ids is None:
        allowed_entity_ids = _request_candidate_ids.get()

    if direct_entity_id:
        async with _optional_span(span_collector, "entity_validate", agent_id=agent_id) as ev_span:
            ev_span["metadata"]["entity_id"] = direct_entity_id
            validated_direct: str | None = None
            if allowed_entity_ids is not None and direct_entity_id not in allowed_entity_ids:
                logger.warning(
                    "LLM-supplied entity_id '%s' not in the recalled candidate set; rejecting (fail-closed)",
                    direct_entity_id,
                )
            else:
                validated_direct = await _validate_direct_entity_id(
                    direct_entity_id,
                    validate_domain_fn,
                    agent_id=agent_id,
                    entity_index=entity_index,
                    allowed_domains=allowed_domains,
                )
            if validated_direct:
                ev_span["metadata"]["resolution_path"] = "llm_entity_id"
                metadata = _synthesize_direct_entity_metadata(validated_direct, entity_index)
                friendly = metadata["top_friendly_name"]
                return {
                    "entity_id": validated_direct,
                    "friendly_name": friendly,
                    "resolution": {
                        "entity_id": validated_direct,
                        "friendly_name": friendly,
                        "speech": None,
                        "metadata": metadata,
                    },
                    "not_found_result": None,
                }
            if allowed_entity_ids is not None:
                # Closed candidate contract: an LLM-picked id that is not in the
                # recalled candidate set (or failed validation) is a
                # hallucination -- reject fail-closed without a matcher re-run.
                # The not-found speech triggers the clarifying-question path.
                ev_span["metadata"]["resolution_path"] = "rejected_entity_id"
                resolution = {
                    "entity_id": None,
                    "friendly_name": entity_query,
                    "speech": None,
                    "metadata": {
                        "query": entity_query,
                        "match_count": 0,
                        "resolution_path": "rejected_entity_id",
                        "rejected_entity_id": direct_entity_id,
                    },
                }
                return {
                    "entity_id": None,
                    "friendly_name": entity_query,
                    "resolution": resolution,
                    "not_found_result": {
                        "success": False,
                        "entity_id": None,
                        "new_state": None,
                        "speech": f"Could not find an entity matching '{entity_query}'.",
                        "metadata": resolution["metadata"],
                    },
                }
            ev_span["metadata"]["resolution_path"] = "fallback_deterministic"
        logger.warning(
            "LLM-supplied direct entity_id '%s' failed validation; falling back to deterministic resolution",
            direct_entity_id,
        )

    if visible_entries is None:
        snapshot = _request_visible_entries.get()
        if snapshot is not None:
            snap_domains, snap_agent_id, snap_entries = snapshot
            domains_covered = (
                snap_domains is None or allowed_domains is None or set(allowed_domains) <= set(snap_domains)
            )
            if snap_agent_id == agent_id and snap_entries and domains_covered:
                if allowed_domains:
                    visible_entries = [
                        entry for entry in snap_entries if getattr(entry, "domain", None) in allowed_domains
                    ]
                else:
                    visible_entries = list(snap_entries)

    resolution = {
        "entity_id": None,
        "friendly_name": entity_query,
        "speech": None,
        "metadata": {"query": entity_query, "match_count": 0, "resolution_path": "not_attempted"},
    }

    if require_matcher:
        can_resolve = entity_matcher is not None
    else:
        can_resolve = entity_index is not None or entity_matcher is not None

    try:
        if can_resolve:
            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                kwargs: dict[str, Any] = {
                    "agent_id": agent_id,
                    "allowed_domains": allowed_domains,
                    "preferred_area_id": preferred_area_id,
                }
                if enable_strip_device_noun:
                    kwargs["enable_strip_device_noun"] = True
                if enable_area_fallback:
                    kwargs["enable_area_fallback"] = True
                if preferred_domain:
                    kwargs["preferred_domain"] = preferred_domain
                if visible_entries is not None:
                    kwargs["visible_entries"] = visible_entries
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    **kwargs,
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"]
    if entity_id and not validate_domain_fn(entity_id):
        logger.warning("Resolved entity %s not in allowed domains", entity_id)
        entity_id = None

    if not entity_id:
        not_found = {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution["speech"] or f"Could not find an entity matching '{entity_query}'.",
            "metadata": resolution.get("metadata"),
        }
        return {
            "entity_id": None,
            "friendly_name": friendly_name,
            "resolution": resolution,
            "not_found_result": not_found,
        }

    return {
        "entity_id": entity_id,
        "friendly_name": friendly_name,
        "resolution": resolution,
        "not_found_result": None,
    }


def _ensure_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


# Regex to find JSON blocks in LLM output (fenced)
_JSON_FENCE_RE = re.compile(r"```json\s*\n?(.*?)\n?\s*```", re.DOTALL)
# FLOW-LOW-1: smaller models occasionally emit an unlabelled ```...```
# fence around the action JSON. Accept those as a secondary match so we
# do not fall through to the looser raw-decode scanner, which is
# noticeably more permissive and can misparse surrounding prose.
_PLAIN_FENCE_RE = re.compile(r"```\s*\n?(.*?)\n?\s*```", re.DOTALL)


# P2-6 (FLOW-PARSE-1): unified action schema.
# ``parse_action`` accepts an LLM payload only when it conforms to this
# minimal contract: a non-empty ``action`` string, plus *either* a
# device target (``entity`` / ``entity_id``) *or* an explicit read-only
# action that does not require one (``list_lights``). Anything else is
# treated as a parse miss and the caller falls through to the next
# regex / fallback path so a malformed JSON block in one fence cannot
# poison parsing of a valid block in a later fence.
_ACTIONS_WITHOUT_ENTITY: frozenset[str] = frozenset(
    {
        # Light / switch / sensor read paths
        "list_lights",
        # Climate / scene / security / media / music / automation list paths
        "list_climate",
        "list_automations",
        "list_security",
        "list_media_players",
        "list_music_players",
        "list_scenes",
        "query_weather",
        "query_weather_forecast",
        # Timer agent list/query paths that aggregate across entities
        "list_timers",
        "list_alarms",
        # Lists agent list/query paths
        "list_lists",
        # Automation CRUD actions that do not require a pre-existing entity
        "create_automation",
    }
)


class ActionCondition(BaseModel):
    """Condition checked before executing a state-changing action.

    Allows agents to emit context-dependent actions (e.g. "turn on the
    light only if it is off"). When the condition fails the action is
    skipped rather than executed.
    """

    model_config = {"extra": "allow"}

    entity: str = Field(..., min_length=1)
    state: str | None = Field(None)
    attribute: str | None = Field(None)
    operator: str = Field("eq")


class ActionPayload(BaseModel):
    """Validated structured action emitted by domain LLM prompts.

    Backwards compatibility:
      * ``entity`` is the historical key (free-text device label that the
        entity matcher resolves). ``entity_id`` is accepted as a synonym
        for the few callers that already speak HA-native ids.
      * Extra keys are preserved (``model_config["extra"] = "allow"``)
        so legacy fields like ``parameters`` flow through to the
        downstream service-data builder unchanged.
    """

    model_config = {"extra": "allow"}

    action: str = Field(..., min_length=1)
    entity: str | None = None
    entity_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    condition: ActionCondition | None = None


def _validate_action_dict(candidate: Any) -> dict | None:
    """Return ``candidate`` iff it satisfies :class:`ActionPayload`.

    The original, untouched dict is returned (not the pydantic model)
    so existing callers that index with ``action.get("entity")`` /
    ``action.get("parameters")`` keep working unchanged. ``None``
    means "not a usable action -- try the next parse path".
    """
    if not isinstance(candidate, dict):
        return None
    try:
        validated = ActionPayload.model_validate(candidate)
    except ValidationError:
        return None

    action_name = validated.action.strip().lower()
    if not action_name:
        return None

    has_entity = bool((validated.entity or "").strip()) or bool((validated.entity_id or "").strip())
    if not has_entity and action_name not in _ACTIONS_WITHOUT_ENTITY:
        return None

    # If a condition is present, validate its shape independently.
    if "condition" in candidate:
        try:
            ActionCondition.model_validate(candidate["condition"])
        except ValidationError:
            return None

    return candidate


def _try_parse_json_with_action(text: str) -> dict | None:
    """Try to parse a JSON object containing an 'action' key from text.

    COR-10: uses ``json.JSONDecoder().raw_decode`` so that braces inside
    string literals (e.g. ``"description": "use {placeholder}"``) do not
    trip up a hand-rolled brace counter. We scan from each ``{`` position
    and let the decoder report where the object ends.

    P2-6: every candidate object that decodes successfully is validated
    against :class:`ActionPayload` before being returned. Decoded
    objects that contain ``"action"`` but fail schema validation are
    skipped -- the scanner keeps walking so a later, well-formed
    object in the same blob can still win.
    """
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            return None
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        if isinstance(obj, dict) and "action" in obj:
            validated = _validate_action_dict(obj)
            if validated is not None:
                return validated
        idx = end


def _collect_actions_from_text(text: str) -> list[dict]:
    """Collect every valid action dict decodable from ``text``.

    Multi-action companion to :func:`_try_parse_json_with_action`: the
    same ``json.JSONDecoder().raw_decode`` scan from each ``{`` position,
    but gathers EVERY dict that contains ``"action"`` and passes
    :func:`_validate_action_dict` instead of returning the first.
    """
    decoder = json.JSONDecoder()
    actions: list[dict] = []
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            return actions
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        if isinstance(obj, dict) and "action" in obj:
            validated = _validate_action_dict(obj)
            if validated is not None:
                actions.append(validated)
        idx = end


# Multi-action turns (one fenced JSON block per requested action, per the
# domain prompts) are capped so a runaway LLM response cannot flood the
# home with service calls. The light prompt (prompts/light.txt) states the
# same limit and asks the user to narrow a larger request down.
_MAX_ACTIONS_PER_TURN = 8


def _action_dedupe_key(action: dict) -> tuple:
    """Order-preserving dedupe key covering the full action payload."""
    return (
        str(action.get("action") or ""),
        str(action.get("entity") or ""),
        str(action.get("entity_id") or ""),
        json.dumps(action.get("parameters") or {}, sort_keys=True, default=str),
        json.dumps(action.get("condition"), sort_keys=True, default=str),
    )


def parse_actions(llm_response: str) -> list[dict]:
    """Extract all structured action dicts from an LLM response (see :func:`parse_actions_capped`)."""
    actions, _dropped = parse_actions_capped(llm_response)
    return actions


def parse_actions_capped(llm_response: str) -> tuple[list[dict], int]:
    """Extract all structured action dicts from an LLM response, in order.

    Returns ``(actions, dropped)``: ``dropped`` counts the distinct action
    blocks beyond ``_MAX_ACTIONS_PER_TURN`` that will NOT be executed, so
    the caller can tell the user that not everything was done.

    Multi-action turns: the domain prompts instruct the LLM to emit one
    fenced JSON block per action, so every block that decodes and
    validates is collected. Exact duplicates (same action, entity,
    entity_id, parameters and condition) are removed order-preserving
    and the result is capped at ``_MAX_ACTIONS_PER_TURN``.

    Stage precedence is identical to the former single-action parser:
    labelled ```json fences win over unlabelled ``` fences, and a
    raw-text scan only runs when neither fence stage yields an action.
    """
    # FLOW-LOW-1 / P2-6: same precedence and per-candidate validation as
    # the single-action path -- a malformed fence never poisons a
    # well-formed block in a later fence.
    actions: list[dict] = []
    for regex in (_JSON_FENCE_RE, _PLAIN_FENCE_RE):
        for match in regex.finditer(llm_response):
            actions.extend(_collect_actions_from_text(match.group(1)))
        if actions:
            break
    else:
        actions = _collect_actions_from_text(llm_response)

    seen: set[tuple] = set()
    deduped: list[dict] = []
    for action in actions:
        key = _action_dedupe_key(action)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)

    dropped = max(0, len(deduped) - _MAX_ACTIONS_PER_TURN)
    if dropped:
        logger.warning(
            "parse_actions: truncating %d action blocks to _MAX_ACTIONS_PER_TURN=%d",
            len(deduped),
            _MAX_ACTIONS_PER_TURN,
        )
        deduped = deduped[:_MAX_ACTIONS_PER_TURN]
    return deduped, dropped


def find_rejected_action_objects(llm_response: str) -> list[dict]:
    """Return JSON objects with an ``"action"`` key that FAILED action validation.

    Companion to :func:`parse_actions` for the parse-miss path: an LLM
    response such as ``{"action": "turn_on", "entity": null}`` plus "Done,
    the light is on" contains an action attempt that never executed. The
    caller must not speak the surrounding prose as if it had succeeded.
    """
    decoder = json.JSONDecoder()
    rejected: list[dict] = []
    idx = 0
    while True:
        start = llm_response.find("{", idx)
        if start == -1:
            return rejected
        try:
            obj, end = decoder.raw_decode(llm_response, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        if isinstance(obj, dict) and "action" in obj and _validate_action_dict(obj) is None:
            rejected.append(obj)
        idx = end


def parse_action(llm_response: str) -> dict | None:
    """Extract the first structured action dict from an LLM response.

    Thin wrapper over :func:`parse_actions` for callers that only act on
    a single action block. Returns ``None`` when no valid action is found.

    Expected format:
        {"action": "turn_on", "entity": "kitchen light", "parameters": {}}
    """
    actions = parse_actions(llm_response)
    return actions[0] if actions else None


# Post-call verification outcomes (``call_service_with_verification``).
VERIFY_REACHED = "reached"  # observed state equals the expected target
VERIFY_IN_PROGRESS = "in_progress"  # transitional state on the way to the target
VERIFY_MISMATCH = "mismatch"  # known terminal state that contradicts the target
VERIFY_UNVERIFIED = "unverified"  # nothing observed, or not (yet) confirmed within the verify window
VERIFY_ERROR = "error"  # the service call itself raised

# HA states that mean "the command is still being carried out". Reported as
# in progress, never as success or failure.
TRANSITIONAL_STATES: frozenset[str] = frozenset(
    {
        "opening",
        "closing",
        "locking",
        "unlocking",
        "arming",
        "disarming",
        "pending",
        "buffering",
        "starting",
    }
)

# Terminal states that satisfy an expected target although they differ from
# it literally (players that report "off"/"standby"/"paused" after stop, a
# TV that reports "standby" after turn_off, a vacuum that is already
# "docked" when asked to return). Executors pass the pre-call state as
# ``previous_state``, so a missing equivalent would turn into a failure.
_EQUIVALENT_TARGET_STATES: dict[str, frozenset[str]] = {
    "idle": frozenset({"off", "standby", "docked", "paused"}),
    "off": frozenset({"standby"}),
    "returning": frozenset({"docked"}),
}


# Climate HVAC modes: a thermostat may report another mode (often the old
# one) for a moment while a mode change settles.
_HVAC_MODES: frozenset[str] = frozenset({"off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"})

# Plausible intermediate states per expected target. A device may pass
# through these on its way to the target (a vacuum reports "idle" or
# "returning" right after start, a player "on"/"idle" before "playing", a
# panel "disarmed" while switching arm modes). They are never treated as a
# contradiction of the pre-call ``previous_state``: the outcome stays
# ``unverified`` (hedged speech, not cached), never a failure.
_INTERMEDIATE_STATES_BY_TARGET: dict[str, frozenset[str]] = {
    # vacuum start / clean_spot
    "cleaning": frozenset({"idle", "returning", "paused"}),
    # vacuum return_to_base (docked is an equivalent target state)
    "returning": frozenset({"idle", "paused", "cleaning"}),
    # media/music play
    "playing": frozenset({"idle", "on", "standby", "paused"}),
    # media/vacuum pause
    "paused": frozenset({"idle", "on", "standby", "returning"}),
    # media/vacuum stop (off/standby/docked/paused are equivalents)
    "idle": frozenset({"on", "returning"}),
    # turn_off of players/TVs that settle via idle/on
    "off": frozenset({"idle", "on"}),
    # covers that report a non-standard "stopped" mid-travel
    "open": frozenset({"stopped"}),
    "closed": frozenset({"stopped"}),
    # locks that unlatch ("open") on unlock
    "unlocked": frozenset({"open"}),
    # alarm panels that disarm before switching arm modes
    "armed_home": frozenset({"disarmed"}),
    "armed_away": frozenset({"disarmed"}),
    "armed_night": frozenset({"disarmed"}),
}


def _is_intermediate_state(expected_state: str, observed_state: str) -> bool:
    """True when ``observed_state`` may be a step on the way to ``expected_state``."""
    if observed_state in _INTERMEDIATE_STATES_BY_TARGET.get(expected_state, frozenset()):
        return True
    # Climate mode changes: any other HVAC mode may be the old mode still
    # being reported (or the device's own interpretation, e.g. auto/heat_cool).
    return expected_state in _HVAC_MODES and observed_state in _HVAC_MODES


# Terminal fault states that contradict any commanded target. Only these
# (or an opt-in pre-call comparison, see ``classify_verification_outcome``)
# turn a non-target observation into a failure; any other non-target state
# may simply be the pre-call state of a device that reports late.
FAULT_STATES: frozenset[str] = frozenset({"jammed", "problem", "error", "fault"})


def unverified_speech(friendly_name: str) -> str:
    """Hedged wording for a command whose new state was not confirmed in time."""
    return f"I sent the command to {friendly_name}, but it has not confirmed the new state yet."


# Per-request verification records (``call_service_with_verification``
# appends one dict per call). ActionableAgent opens a fresh list around each
# executor call so it can hedge the speech of, and keep out of the action
# cache, actions whose new state was not confirmed -- without every
# executor having to forward the verification outcome.
_request_verify_records: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "action_executor_verify_records", default=None
)


def start_verify_records() -> tuple[contextvars.Token, list[dict[str, Any]]]:
    """Begin collecting verification records for one executor call."""
    records: list[dict[str, Any]] = []
    return _request_verify_records.set(records), records


def reset_verify_records(token: contextvars.Token) -> None:
    """Stop collecting (restore the previous collector)."""
    _request_verify_records.reset(token)


class StateVerificationError(Exception):
    """The service call ran, but the entity did not reach the expected state."""

    def __init__(self, entity_id: str, expected_state: str | None, observed_state: str | None) -> None:
        self.entity_id = entity_id
        self.expected_state = expected_state
        self.observed_state = observed_state
        super().__init__(f"the device reports '{observed_state}' instead of '{expected_state}'")


def classify_verification_outcome(
    expected_state: str | None,
    observed_state: str | None,
    *,
    previous_state: str | None = None,
    strict: bool = False,
) -> str:
    """Classify an observed post-call state against the expected target.

    - ``reached``: the target (or an equivalent terminal state).
    - ``in_progress``: a transitional state (``TRANSITIONAL_STATES``).
    - ``mismatch``: a known fault state (``FAULT_STATES``, e.g. ``jammed``);
      or, when the caller passes the pre-call ``previous_state``, a
      non-target state the entity moved to (it changed and settled
      elsewhere); or any non-target state when ``strict`` is set (opt-in).
    - ``unverified``: nothing observed, or a non-target state that may be
      the unchanged pre-call state of a device that reports later than
      the verify window (Zigbee, cloud integrations), or a plausible
      intermediate state on the way to the target
      (``_INTERMEDIATE_STATES_BY_TARGET``, HVAC mode changes) even when it
      differs from ``previous_state``. Not a failure.
    """
    if observed_state is None:
        return VERIFY_UNVERIFIED
    if expected_state is None:
        return VERIFY_REACHED
    if observed_state == expected_state or observed_state in _EQUIVALENT_TARGET_STATES.get(expected_state, ()):
        return VERIFY_REACHED
    if observed_state in TRANSITIONAL_STATES:
        return VERIFY_IN_PROGRESS
    if observed_state in FAULT_STATES or strict:
        return VERIFY_MISMATCH
    if _is_intermediate_state(expected_state, observed_state):
        return VERIFY_UNVERIFIED
    if previous_state is not None and observed_state != previous_state:
        return VERIFY_MISMATCH
    return VERIFY_UNVERIFIED


async def call_service_with_verification(
    ha_client: Any,
    domain: str,
    service: str,
    entity_id: str,
    *,
    service_data: dict | None = None,
    expected_state: str | None = None,
    ws_timeout: float | None = None,
    poll_interval: float | None = None,
    poll_max: float | None = None,
    previous_state: str | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Shared primitive for domain executors: REST ``call_service`` + WS verify.

    FLOW-VERIFY-SHARED (0.18.5): every domain executor used to call
    ``ha_client.call_service`` followed by a fixed ``asyncio.sleep(0.3)``
    and a single ``get_state``. On async-bus aktors (KNX/ABB/Zigbee2MQTT,
    Matter over Thread, slow cloud integrations…) the ``state_changed``
    event fires *after* the REST call returns, so the poll routinely
    captured the *previous* state. Callers then produced stale speech
    like "light is off" right after a successful ``turn_on``.

    This helper registers a WebSocket state-change waiter *before* the
    REST call via ``ha_client.expect_state``, runs the call inside the
    context, and merges evidence from three sources (in priority order):

    1. HA's synchronous ``call_service`` response (a list of changed
       state objects -- authoritative when non-empty).
    2. The state observed by the WS waiter / polling fallback that
       ``expect_state`` sets up.
    3. ``None`` (verification inconclusive -- callers should fall back
       to intent-first speech using ``expected_state`` when available).

    Args:
        ha_client: HARestClient / HAWebSocketClient composite.
        domain: HA service domain (e.g. ``"light"``).
        service: HA service name (e.g. ``"turn_on"``).
        entity_id: Target entity id.
        service_data: Optional service payload; ``None``/empty is fine.
        expected_state: Deterministic target state (``"on"``/``"locked"``
            /``"armed_home"``/…). When ``None`` the WS waiter fires on
            *any* state change (``toggle``-like semantics).
        ws_timeout / poll_interval / poll_max: Override the corresponding
            ``state_verify.*`` settings; ``None`` means "read from
            SettingsRepository defaults".
        previous_state: Optional pre-call state. When given, a non-target
            state that differs from it counts as a contradiction.
        strict: Opt-in: any non-target, non-transitional state is a
            contradiction (no executor uses this today).

    Returns:
        Dict with:
            success: False when the call raised OR the entity ended in a
                contradicting terminal state (``outcome == "mismatch"``).
                An ``unverified`` outcome stays ``success=True``.
            call_succeeded: False iff an exception was raised during the call.
            outcome: one of ``VERIFY_REACHED`` / ``VERIFY_IN_PROGRESS`` /
                ``VERIFY_MISMATCH`` / ``VERIFY_UNVERIFIED`` / ``VERIFY_ERROR``
                (see :func:`classify_verification_outcome`).
            entity_id: echoed for convenience.
            call_result: raw REST response (``list``/``dict``/``None``).
            observed_state: merged state from REST / WS / poll.
            verified: True iff the outcome is ``VERIFY_REACHED``. Use this to
                decide between observed-state speech and intent-first speech.
            cacheable: False for a mismatch, or an ``unverified`` outcome of an
                action with an ``expected_state``.
            error: the exception (call failure) or a
                :class:`StateVerificationError` (mismatch), else ``None``.
    """
    if ws_timeout is None:
        ws_timeout = await _settings_float(
            "state_verify.ws_timeout_sec",
            default=1.5,
        )
    if poll_interval is None:
        poll_interval = await _settings_float(
            "state_verify.poll_interval_sec",
            default=0.25,
        )
    if poll_max is None:
        poll_max = await _settings_float(
            "state_verify.poll_max_sec",
            default=1.0,
        )

    call_result: Any = None
    observer: dict[str, Any] = {}
    expect_state_fn = getattr(ha_client, "expect_state", None)

    async def _call_service() -> Any:
        # The double-execution guard (app.ha_client.action_marker) is flagged
        # inside ha_client.call_service, so every HA write is covered.
        with mark_verified_ha_service_call("action-executor"):
            return await ha_client.call_service(
                domain,
                service,
                entity_id,
                service_data or None,
            )

    try:
        if expect_state_fn is None:
            call_result = await _call_service()
        else:
            try:
                cm = expect_state_fn(
                    entity_id,
                    expected=expected_state,
                    timeout=ws_timeout,
                    poll_interval=poll_interval,
                    poll_max=poll_max,
                )
                aenter = getattr(cm, "__aenter__", None)
                aexit = getattr(cm, "__aexit__", None)
            except TypeError:
                cm = None
                aenter = aexit = None
            if callable(aenter) and callable(aexit):
                async with cm as obs:
                    observer = obs if isinstance(obs, dict) else {}
                    call_result = await _call_service()
            else:
                # ``expect_state`` is mocked with a non-CM return (legacy
                # tests) -- fall back to the simple call path; the caller
                # still gets the REST response, just without WS verification.
                call_result = await _call_service()
    except Exception as exc:
        logger.error(
            "Service call failed: %s/%s on %s",
            domain,
            service,
            entity_id,
            exc_info=True,
        )
        return {
            "success": False,
            "call_succeeded": False,
            "outcome": VERIFY_ERROR,
            "entity_id": entity_id,
            "call_result": None,
            "observed_state": None,
            "verified": False,
            "error": exc,
        }

    observed = _extract_state_from_call_result(call_result, entity_id)
    observer_state = observer.get("new_state") if observer else None
    if observed is None:
        observed = observer_state
    elif expected_state and observed != expected_state and observer_state == expected_state:
        # The synchronous REST snapshot can catch an intermediate state
        # ("locking"); the WS waiter saw the target state afterwards.
        observed = observer_state

    outcome = classify_verification_outcome(expected_state, observed, previous_state=previous_state, strict=strict)
    verified = outcome == VERIFY_REACHED
    records = _request_verify_records.get()
    if records is not None:
        records.append(
            {"entity_id": entity_id, "outcome": outcome, "expected_state": expected_state, "observed_state": observed}
        )

    if outcome == VERIFY_UNVERIFIED and expected_state:
        logger.info(
            "State verify unconfirmed for %s: expected=%s observed=%s (device may report late)",
            entity_id,
            expected_state,
            observed,
        )

    if outcome == VERIFY_MISMATCH:
        # The command ran, but the device ended in a contradicting terminal
        # state: a lock that reports "jammed", or (with ``previous_state``)
        # a state it moved to that is not the target. Reported as a
        # failure so executors never claim success.
        logger.warning(
            "State verify mismatch for %s: expected=%s observed=%s",
            entity_id,
            expected_state,
            observed,
        )
        return {
            "success": False,
            "call_succeeded": True,
            "outcome": outcome,
            "entity_id": entity_id,
            "call_result": call_result,
            "observed_state": observed,
            "verified": False,
            "cacheable": False,
            "error": StateVerificationError(entity_id, expected_state, observed),
        }

    return {
        "success": True,
        "call_succeeded": True,
        "outcome": outcome,
        "entity_id": entity_id,
        "call_result": call_result,
        "observed_state": observed,
        "verified": verified,
        "cacheable": outcome != VERIFY_UNVERIFIED or expected_state is None,
        "error": None,
    }


def build_verified_speech(
    *,
    friendly_name: str,
    action_name: str,
    expected_state: str | None,
    observed_state: str | None,
    verified: bool,
    action_phrases: dict[str, str] | None = None,
) -> str:
    """Intent-first speech helper for domain executors.

    FLOW-VERIFY-SHARED (0.18.5): mirrors ``_build_action_speech`` but
    parameterized over a small per-domain phrase map so each executor
    can localize its action-to-verb mapping (``lock`` -> "locked",
    ``alarm_arm_home`` -> "armed in home mode", ``start_timer`` ->
    "started", …).

    Priority:
      1. ``verified`` with an ``expected_state``: "is now <expected>".
      2. An expected target that was NOT confirmed: a transitional state is
         spoken as in progress, a known fault state as a failure, and
         anything else (unchanged pre-call state, nothing observed) with
         the hedged :func:`unverified_speech` -- never as "Done".
      3. No expected target (toggle, or attribute-only actions such as fan
         speed or volume, which often change no state): the
         ``action_phrases`` entry, else "is now <observed>", else the
         humanized action name.
    """
    phrases = action_phrases or {}
    if expected_state:
        if verified:
            return f"Done, {friendly_name} is now {expected_state}."
        outcome = classify_verification_outcome(expected_state, observed_state)
        if outcome == VERIFY_REACHED:
            return f"Done, {friendly_name} is now {expected_state}."
        if outcome == VERIFY_IN_PROGRESS and observed_state:
            return f"OK, {friendly_name} is {observed_state.replace('_', ' ')} now."
        if outcome == VERIFY_MISMATCH and observed_state:
            return (
                f"The command was sent, but {friendly_name} reports {observed_state.replace('_', ' ')} "
                f"instead of {expected_state.replace('_', ' ')}."
            )
        return unverified_speech(friendly_name)
    if action_name in phrases:
        return f"Done, {friendly_name} {phrases[action_name]}."
    if observed_state:
        return f"Done, {friendly_name} is now {observed_state}."
    return f"Done, {friendly_name} {action_name.replace('_', ' ')}."


async def _evaluate_condition(
    condition: ActionCondition,
    ha_client: Any,
    entity_index: Any,
    entity_matcher: Any,
    agent_id: str | None = None,
    allowed_domains: frozenset[str] | None = None,
    preferred_area_id: str | None = None,
) -> tuple[bool, str | None, str | None, Exception | None]:
    """Evaluate a pre-action condition against the current HA state.

    Returns ``(passed, observed_value, resolved_entity_id, error)``.
    * ``passed`` is ``True`` when the condition is satisfied (or when
      evaluation cannot be performed and we choose to proceed).
    * ``observed_value`` is the state/attribute string that was compared.
    * ``resolved_entity_id`` is the HA entity id the condition referenced.
    * ``error`` is non-None when entity resolution or state lookup failed.
    """
    entity_query = condition.entity
    try:
        resolution = await resolve_entity_deterministic_first(
            entity_query,
            entity_index,
            entity_matcher,
            agent_id=agent_id,
            allowed_domains=allowed_domains,
            preferred_area_id=preferred_area_id,
            enable_strip_device_noun=True,
            enable_area_fallback=True,
        )
    except Exception as exc:
        return False, None, None, exc

    entity_id = resolution.get("entity_id")
    if not entity_id:
        return False, None, None, RuntimeError(f"Could not resolve condition entity '{entity_query}'")

    try:
        state_resp = await ha_client.get_state(entity_id)
    except Exception as exc:
        return False, None, entity_id, exc

    if not isinstance(state_resp, dict):
        return False, None, entity_id, RuntimeError(f"No state for {entity_id}")

    if condition.attribute:
        observed = state_resp.get("attributes", {}).get(condition.attribute)
        observed_str = str(observed) if observed is not None else None
    else:
        observed_str = _ensure_str(state_resp.get("state"))

    expected = (condition.state or "").strip()
    op = (condition.operator or "eq").strip().lower()

    if observed_str is None:
        # Cannot evaluate -- fail-safe: treat as not passed but surface error
        return (
            False,
            None,
            entity_id,
            RuntimeError(f"Missing {'attribute' if condition.attribute else 'state'} for {entity_id}"),
        )

    if op == "eq":
        passed = observed_str.lower() == expected.lower()
    elif op == "neq":
        passed = observed_str.lower() != expected.lower()
    else:
        # Unknown operator defaults to not-passed
        passed = False

    return passed, observed_str, entity_id, None


# ---------------------------------------------------------------------------
# FLOW-VERIFY-1: helpers for post-action state verification and speech.
# ---------------------------------------------------------------------------


def _extract_state_from_call_result(
    call_result: Any,
    entity_id: str,
) -> str | None:
    """Pick the target entity's state out of HA's ``call_service`` response.

    HA returns a JSON list of states it considered changed. We look for our
    exact entity_id and return its state string; anything else is ignored
    because a state reported on a *different* entity tells us nothing about
    the one we actually commanded.
    """
    if not isinstance(call_result, list):
        return None
    for entry in call_result:
        if not isinstance(entry, dict):
            continue
        if entry.get("entity_id") != entity_id:
            continue
        state = entry.get("state")
        if isinstance(state, str):
            return state
    return None
