"""Shared action parsing, execution, and verification for domain agents."""

from __future__ import annotations

import contextlib
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
from app.ha_client.rest import mark_verified_ha_service_call

logger = logging.getLogger(__name__)

_READ_ONLY_ACTION_PREFIXES: tuple[str, ...] = ("query_", "list_", "get_")


def is_read_only_action(action_name: str) -> bool:
    return action_name.lower().startswith(_READ_ONLY_ACTION_PREFIXES)


def _validate_direct_entity_id(entity_id: str | None, validate_domain_fn) -> str | None:
    if not entity_id:
        return None
    if not validate_domain_fn(entity_id):
        logger.warning("Direct entity_id %s rejected by domain validator", entity_id)
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
    verbatim_terms: list[str] | None = None,
    enable_strip_device_noun: bool = False,
    enable_area_fallback: bool = False,
    preferred_domain: str | None = None,
    span_collector=None,
    require_matcher: bool = False,
) -> dict[str, Any]:
    """Resolve an entity query deterministically and validate its domain.

    Encapsulates the common entity resolution block used by every domain
    executor: init dict -> resolve_entity_deterministic_first -> domain
    validation -> not-found fallback.

    Returns a dict with keys:
        entity_id: resolved and validated entity_id (None if not found)
        friendly_name: friendly name of the resolved entity
        resolution: the full resolution dict
        not_found_result: only present when entity_id is None; return this
            directly after adding any caller-specific extra keys
            (e.g. ``cacheable``, ``voice_followup``).
    """
    from app.analytics.tracer import _optional_span

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
                if verbatim_terms is not None:
                    kwargs["verbatim_terms"] = verbatim_terms
                if enable_strip_device_noun:
                    kwargs["enable_strip_device_noun"] = True
                if enable_area_fallback:
                    kwargs["enable_area_fallback"] = True
                if preferred_domain:
                    kwargs["preferred_domain"] = preferred_domain
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


def parse_action(llm_response: str) -> dict | None:
    """Extract a structured action dict from an LLM response.

    Looks for JSON in ```json``` fences first, then falls back to raw JSON
    objects containing an "action" key.

    Expected format:
        {"action": "turn_on", "entity": "kitchen light", "parameters": {}}

    Returns None if no valid action block is found.
    """
    # FLOW-LOW-1: try labelled ```json fences first (preferred, most
    # specific), then fall back to unlabelled ``` fences before the raw
    # scanner. Ordering matters: a labelled fence MUST win over a plain
    # fence when both are present so we do not silently parse a prose
    # example block.
    #
    # P2-6 (FLOW-PARSE-1): each path runs the candidate JSON through
    # ``_validate_action_dict`` (via ``_try_parse_json_with_action``).
    # If a fence's contents fail validation we fall through to the
    # next regex instead of returning a malformed action -- a labelled
    # ```json fence containing a stub example no longer overrides a
    # well-formed plain-fence or inline action below it.
    for regex in (_JSON_FENCE_RE, _PLAIN_FENCE_RE):
        for match in regex.finditer(llm_response):
            result = _try_parse_json_with_action(match.group(1))
            if result:
                return result

    return _try_parse_json_with_action(llm_response)


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

    Returns:
        Dict with:
            success: False iff an exception was raised during the call.
            entity_id: echoed for convenience.
            call_result: raw REST response (``list``/``dict``/``None``).
            observed_state: merged state from REST / WS / poll.
            verified: True iff ``observed_state`` matches
                ``expected_state`` (or ``expected_state is None`` and
                *something* was observed). Use this to decide between
                observed-state speech and intent-first speech.
            error: the exception when ``success`` is False, else ``None``.
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
            "entity_id": entity_id,
            "call_result": None,
            "observed_state": None,
            "verified": False,
            "error": exc,
        }

    observed = _extract_state_from_call_result(call_result, entity_id)
    if observed is None and observer:
        observed = observer.get("new_state")

    verified = observed is not None if expected_state is None else observed == expected_state

    if expected_state and observed is not None and observed != expected_state:
        logger.info(
            "State verify mismatch for %s: expected=%s observed=%s",
            entity_id,
            expected_state,
            observed,
        )

    return {
        "success": True,
        "entity_id": entity_id,
        "call_result": call_result,
        "observed_state": observed,
        "verified": verified,
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
      1. If ``verified`` and we have an ``expected_state`` or
         ``observed_state``, speak the authoritative state.
      2. Otherwise use the ``action_phrases`` mapping if it carries the
         action name (intent-first wording even on stale observations).
      3. Fall back to the humanized action name (``set_hvac_mode`` ->
         ``set hvac mode``).
    """
    phrases = action_phrases or {}
    # 1. Deterministic verified target wins: "is now <expected>".
    if expected_state and verified:
        return f"Done, {friendly_name} is now {expected_state}."
    # 2. For non-state-changing actions (or stale observations), prefer
    #    an intent-first phrase when we have one -- never let an observed
    #    but contradictory state leak into speech.
    if action_name in phrases:
        return f"Done, {friendly_name} {phrases[action_name]}."
    # 3. We do have an expected target but verification was inconclusive
    #    (empty REST + no WS evidence). Stay on intent.
    if expected_state:
        return f"Done, {friendly_name} is now {expected_state}."
    # 4. No expected target, but something observed -- speak it.
    if observed_state:
        return f"Done, {friendly_name} is now {observed_state}."
    # 5. Last resort: humanize the action name.
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
