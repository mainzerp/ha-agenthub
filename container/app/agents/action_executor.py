"""Shared action parsing, execution, and verification for domain agents."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.analytics.tracer import _optional_span
from app.db.repositories.settings import _settings_float
from app.entity.deterministic_resolver import (
    filter_matches_by_domain,  # noqa: F401  -- re-exported for test compat
    resolve_entity_deterministic_first,
)
from app.ha_client.history_query import execute_recorder_history_query
from app.ha_client.rest import allow_internal_ha_service_calls, mark_verified_ha_service_call
from app.models.agent import TaskContext

logger = logging.getLogger(__name__)

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
    }
)


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


async def execute_action(
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

    resolution = {
        "entity_id": None,
        "friendly_name": entity_query,
        "speech": None,
        "metadata": {"query": entity_query, "match_count": 0, "resolution_path": "not_attempted"},
    }
    try:
        if entity_matcher:
            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    agent_id,
                    allowed_domains=_ACTION_DOMAINS_LIGHT.get(action_name, _ALLOWED_DOMAINS),
                    preferred_area_id=preferred_area_id,
                    enable_strip_device_noun=True,
                    enable_area_fallback=True,
                    preferred_domain="light",
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
            "voice_followup": True,
        }

    # Extract domain from entity_id
    domain = entity_id.split(".")[0] if "." in entity_id else "light"

    # Build service data
    service_data = _build_service_data(action)

    # FLOW-VERIFY-1 / FLOW-VERIFY-SHARED (0.18.5): delegate the
    # call_service + WS-waiter dance to the shared helper.
    expected_state = _EXPECTED_STATE_BY_DOMAIN_ACTION.get((domain, action_name))
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
    return {
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
) -> dict:
    resolution = {
        "entity_id": None,
        "friendly_name": entity_query,
        "speech": None,
        "metadata": {"query": entity_query, "match_count": 0, "resolution_path": "not_attempted"},
    }
    try:
        if entity_matcher:
            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    agent_id,
                    allowed_domains=_ALLOWED_DOMAINS,
                    preferred_area_id=preferred_area_id,
                    enable_strip_device_noun=True,
                    enable_area_fallback=True,
                    preferred_domain="light",
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
            "voice_followup": True,
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
        speech = _format_light_state(entity_id, state_resp)
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
            "speech": f"Failed to query light status: {exc}",
            "cacheable": False,
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
    resolution = {
        "entity_id": None,
        "friendly_name": entity_query,
        "speech": None,
        "metadata": {"query": entity_query, "match_count": 0, "resolution_path": "not_attempted"},
    }
    try:
        if entity_matcher:
            async with _optional_span(span_collector, "entity_match", agent_id=agent_id) as em_span:
                resolution = await resolve_entity_deterministic_first(
                    entity_query,
                    entity_index,
                    entity_matcher,
                    agent_id,
                    allowed_domains=_ALLOWED_DOMAINS,
                    preferred_area_id=preferred_area_id,
                    enable_strip_device_noun=True,
                    enable_area_fallback=True,
                    preferred_domain="light",
                )
                em_span["metadata"] = resolution["metadata"]
    except Exception:
        logger.warning("Entity resolution failed for '%s'", entity_query, exc_info=True)

    entity_id = resolution["entity_id"]
    friendly_name = resolution["friendly_name"] or entity_query

    if entity_id and not _validate_domain(entity_id):
        logger.warning("Resolved entity %s not in allowed domains %s", entity_id, _ALLOWED_DOMAINS)
        entity_id = None

    if not entity_id:
        return {
            "success": False,
            "entity_id": None,
            "new_state": None,
            "speech": resolution["speech"] or f"Could not find a visible entity matching '{entity_query}'.",
            "cacheable": False,
            "voice_followup": True,
        }

    return await execute_recorder_history_query(
        entity_id,
        friendly_name,
        parameters,
        ha_client,
        allowed_domains=_ALLOWED_DOMAINS,
        task_context=task_context,
    )


async def _list_lights(ha_client: Any) -> dict:
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

    lights_on = []
    lights_off = []
    switches_on = []
    switches_off = []
    for s in states:
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
        )
    if action_name == "list_lights":
        return await _list_lights(ha_client)
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
