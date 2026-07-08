"""Base class for agents that parse LLM output into HA actions.

Also provides the config-driven :class:`_ConfigurableDomainAgent` and
the :class:`DomainAgent` type alias for external imports.
"""

from __future__ import annotations

import asyncio
import inspect as _inspect
import logging
import re
import time
from typing import Any

from app.agents.action_executor import parse_action
from app.agents.base import BaseAgent, _render_prompt_template, language_code_to_name
from app.agents.decorator import agent
from app.analytics.tracer import _optional_span
from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.models.agent import ActionExecuted, AgentCard, AgentError, AgentErrorCode, AgentTask, TaskContext, TaskResult

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```json\s*\n?.*?\n?\s*```", re.DOTALL)
_RAW_JSON_OBJ_RE = re.compile(r'\{[^{}]*"action"\s*:.*?\}', re.DOTALL)


def strip_json_blocks(text: str) -> str:
    """Remove JSON code fences and raw JSON action objects from text."""
    text = _JSON_FENCE_RE.sub("", text)
    text = _RAW_JSON_OBJ_RE.sub("", text)
    return text.strip() or "Sorry, I could not process that request."


class ActionableAgent(BaseAgent):
    """Base for domain agents that parse actions from LLM output and execute via HA.

    Subclasses must define:
        - agent_card (property)
        - _prompt_name (str): name of the prompt file (e.g., "light")
        - _do_execute(): async method that delegates to the domain-specific executor
    """

    _prompt_name: str = ""
    _clarify_on_not_found: bool = True
    _allowed_domains: frozenset[str] | None = None
    _supports_conditions: bool = False

    def __init__(self, ha_client=None, entity_index=None, entity_matcher=None) -> None:
        super().__init__(ha_client=ha_client, entity_index=entity_index)
        self._entity_matcher = entity_matcher
        self._current_task: AgentTask | None = None
        self._current_task_context: TaskContext | None = None

    async def _resolve_relevant_entities(self, task: AgentTask) -> list[tuple[str, str]]:
        """Resolve up to 3 unique entity mentions from verbatim_terms or description fallback.

        Returns a list of (entity_id, friendly_name) tuples.
        """
        terms = list(task.verbatim_terms or [])
        # Fallback: if orchestrator didn't populate verbatim_terms,
        # use the full condensed task as a single query
        if not terms and task.description:
            terms = [task.description]

        if not terms:
            return []

        agent_id = self.agent_card.agent_id
        resolved: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        cached_visible_entries: list[Any] | None = None

        for term in terms:
            if len(resolved) >= 3:
                break
            try:
                result = await resolve_entity_deterministic_first(
                    term,
                    self._entity_index,
                    self._entity_matcher,
                    agent_id,
                    allowed_domains=self._allowed_domains,
                    visible_entries=cached_visible_entries,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Entity resolution failed for term %r", term, exc_info=True)
                continue

            if cached_visible_entries is None:
                cached_visible_entries = result.get("_visible_entries")

            entity_id = result.get("entity_id")
            friendly_name = result.get("friendly_name")
            if entity_id and entity_id not in seen_ids:
                seen_ids.add(entity_id)
                resolved.append((entity_id, friendly_name or entity_id))

        return resolved

    async def _build_relevant_entity_state_context(self, resolved_entities: list[tuple[str, str]]) -> str | None:
        """Build a compact single-line string of current states for the given entities.

        Queries the entity index first, falling back to ha_client.get_state().
        Returns None if no states could be retrieved.
        """
        if not resolved_entities:
            return None

        lines: list[str] = []
        for entity_id, friendly_name in resolved_entities:
            state_value: str | None = None
            # Try entity index first
            if self._entity_index is not None:
                try:
                    entry = await self._entity_index.get_by_id_async(entity_id)
                    if entry is not None:
                        state_value = getattr(entry, "state", None)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("get_by_id_async failed for %s", entity_id, exc_info=True)
            # Fallback to HA client
            if state_value is None and self._ha_client is not None:
                try:
                    state_resp = await self._ha_client.get_state(entity_id)
                    if isinstance(state_resp, dict):
                        state_value = state_resp.get("state")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("ha_client.get_state failed for %s", entity_id, exc_info=True)
            if state_value is not None:
                lines.append(f"{friendly_name} ({entity_id}): {state_value}")

        if not lines:
            return None
        return ", ".join(lines)

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        """Execute the parsed action. Subclasses must override."""
        raise NotImplementedError

    def _extract_verbatim_terms(self) -> list[str]:
        """Read verbatim_terms from the current task context.

        Provides a single source of truth for domain agents that need
        the orchestrator-preserved original-language tokens for
        entity matching.
        """
        current_task = getattr(self, "_current_task", None)
        return list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []

    async def _generate_not_found_speech(self, entity_query: str, task: AgentTask, span_collector=None) -> str:
        """Ask the LLM to generate a language-appropriate clarifying question when an entity is not found."""
        language = (task.context.language if task.context else None) or "en"
        messages = [
            {
                "role": "system",
                "content": _render_prompt_template(
                    self._load_prompt("entity_not_found"), language=language_code_to_name(language)
                ),
            },
            {
                "role": "user",
                "content": (
                    f"The user asked: {self._wrap_user_input(task.description)}\n"
                    f'No device named "{entity_query}" was found. '
                    "Generate a brief clarifying question asking the user to specify which device they mean."
                ),
            },
        ]
        try:
            result = await self._call_llm(messages, span_collector=span_collector)
            return (
                result.strip()
                if result and result.strip()
                else f"I could not find '{entity_query}'. Which device did you mean?"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Not-found clarification LLM call failed", exc_info=True)
            return f"I could not find '{entity_query}'. Which device did you mean?"

    def _handle_parse_miss(self, task: AgentTask, response: str) -> TaskResult:
        """Return the fallback result when the LLM response has no valid action."""
        return TaskResult(speech=strip_json_blocks(response))

    async def handle_task(self, task: AgentTask) -> TaskResult:
        # FLOW-CTX-1 (0.18.6): expose the incoming TaskContext so
        # domain-specific ``_do_execute`` implementations can pick up
        # satellite area, device_id and request source without
        # plumbing an extra kwarg through every executor signature.
        # Cleared in ``finally`` to avoid leaking between overlapping
        # tasks (same agent instance, two concurrent requests).
        self._current_task_context = task.context
        # 0.23.0: domain executors (e.g. climate) read verbatim_terms
        # from the active task without an extra plumbing kwarg.
        self._current_task = task
        try:
            try:
                return await self._handle_task_inner(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled failure in %s", self.agent_card.agent_id)
                return self._error_result(
                    AgentErrorCode.INTERNAL,
                    "Sorry, something went wrong while handling that request.",
                )
        finally:
            self._current_task_context = None
            self._current_task = None

    async def _handle_task_inner(self, task: AgentTask) -> TaskResult:
        _t0 = time.perf_counter()
        agent_id = self.agent_card.agent_id
        span_collector = task.span_collector
        system_prompt = await self._load_prompt_async(self._prompt_name)

        # Inject language directive for non-English users (PREPEND so it sits
        # in front of the few-shot examples and is not overridden by them).
        language = None
        if task.context:
            language = task.context.language
        if language and language.lower() not in ("en", "english", ""):
            lang_directive = (
                f"CRITICAL LANGUAGE INSTRUCTION: The user's language is {language}.\n"
                f"Respond in {language}.\n"
                f"Copy entity, device, room, and scene names verbatim from the user's message.\n"
                f"NEVER translate entity names to English, regardless of what language the few-shot examples use.\n"
                f"If a few-shot example uses a different language than the user, copy the example's STRUCTURE but keep the USER's original entity names unchanged.\n\n"
            )
            system_prompt = lang_directive + system_prompt

        # Inject time/location context (append: data, not constraint rule)
        time_location = self._build_time_location_context(task.context)
        if time_location:
            system_prompt += f"\n\n{time_location}"

        # Generic state-aware output rules (Phase 3). The conditional block is
        # appended only for agents whose executor evaluates the condition field.
        system_prompt += (
            "\n\nOutput rules:\n"
            "- ALWAYS output a JSON action block when an action is determinable; otherwise respond with natural text only.\n"
            "- Execute the action the user explicitly requested, using only the actions documented in your prompt above.\n"
            "- The injected entity states above are for context only. Do NOT describe them in your response.\n"
            '- Only use toggle when the user explicitly says "toggle".'
        )
        if self._supports_conditions:
            system_prompt += (
                "\n\nConditional actions:\n"
                '- When the user says "if X, then Y", use the optional "condition" field.\n'
                "- The condition references another entity by name and an expected state.\n"
                '- Example JSON: {"action": "turn_on", "entity": "Keller", "condition": {"entity": "outdoor brightness", "state": "dark"}}'
            )

        # Inject relevant entity states (compact single-line format, after output rules)
        try:
            async with _optional_span(span_collector, "entity_resolution", agent_id=agent_id) as er_span:
                resolved_entities = await self._resolve_relevant_entities(task)
                _t1 = time.perf_counter()
                entity_state_context = await self._build_relevant_entity_state_context(resolved_entities)
                _t2 = time.perf_counter()
                er_span["metadata"]["resolved_count"] = len(resolved_entities)
                er_span["metadata"]["has_state_context"] = entity_state_context is not None
                er_span["metadata"]["resolve_ms"] = round((_t1 - _t0) * 1000, 1)
                er_span["metadata"]["state_fetch_ms"] = round((_t2 - _t1) * 1000, 1)
            if entity_state_context:
                system_prompt += f"\n\nContext: {entity_state_context}"
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Entity state injection failed for %s", agent_id, exc_info=True)

        messages = [{"role": "system", "content": system_prompt}]

        if task.context and task.context.conversation_turns:
            self._append_conversation_turn_messages(messages, task.context.conversation_turns)

        # The orchestrator condenses the user's request into a task written in
        # the user's own language. Agents receive only the distilled description,
        # not the raw user_text.
        user_content = self._wrap_user_input(task.description)

        messages.append({"role": "user", "content": user_content})

        try:
            if span_collector:
                async with span_collector.start_span("llm_call", agent_id=agent_id) as span:
                    response = await self._call_llm(messages, span_collector=span_collector)
                    span["metadata"]["model"] = agent_id
                    span["metadata"]["llm_response"] = response[:500] if response else ""
            else:
                response = await self._call_llm(messages)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("LLM call failed for %s: %s", agent_id, str(e)[:200])
            return self._error_result(
                AgentErrorCode.LLM_ERROR,
                "The language model could not complete this request. Please try again.",
            )

        _t3 = time.perf_counter()

        if not response:
            logger.warning("LLM returned empty response for %s task: %s", agent_id, task.description[:100])
            return self._error_result(
                AgentErrorCode.LLM_EMPTY_RESPONSE,
                "The language model did not return a response. Please try again.",
            )

        action = parse_action(response)

        # Path A: Action + HA client -> execute
        if action and self._ha_client:
            try:
                if span_collector:
                    async with span_collector.start_span("ha_action", agent_id=agent_id) as span:
                        result = await self._do_execute(
                            action,
                            self._ha_client,
                            self._entity_index,
                            self._entity_matcher,
                            agent_id=agent_id,
                            span_collector=span_collector,
                        )
                        span["metadata"]["action"] = action.get("action")
                        span["metadata"]["entity"] = action.get("entity")
                        span["metadata"]["success"] = result.get("success")
                        span["metadata"]["action_params"] = {
                            k: v for k, v in action.items() if k not in ("action", "entity")
                        }
                        span["metadata"]["result_speech"] = (result.get("speech") or "")[:500]
                else:
                    result = await self._do_execute(
                        action,
                        self._ha_client,
                        self._entity_index,
                        self._entity_matcher,
                        agent_id=agent_id,
                        span_collector=span_collector,
                    )

                _t4 = time.perf_counter()

                # Entity not found: replace hardcoded speech with LLM-generated clarifying question.
                # LOW-15: skip the generic LLM clarification when the resolver already produced a
                # targeted disambiguation speech ("Multiple entities match ..."), signalled by a
                # resolution_path ending in "_ambiguous". Otherwise the deterministic message would
                # be overwritten by a vague "which device did you mean?" question.
                resolution_path = (result.get("metadata") or {}).get("resolution_path") or ""
                is_ambiguous = resolution_path.endswith("_ambiguous")
                if (
                    self._clarify_on_not_found
                    and not result.get("success")
                    and result.get("entity_id") is None
                    and not result.get("error")
                    and not is_ambiguous
                ):
                    entity_query = action.get("entity", "")
                    result = {
                        **result,
                        "speech": await self._generate_not_found_speech(entity_query, task, span_collector),
                    }

                metadata = result.get("metadata") or {}
                _t5 = time.perf_counter()
                logger.info(
                    "dispatch_timing agent=%s pre_entities=%.1fms entities=%.1fms llm_parse=%.1fms ha_action=%.1fms post_action=%.1fms total=%.1fms",
                    agent_id,
                    (_t1 - _t0) * 1000,
                    (_t2 - _t1) * 1000,
                    (_t3 - _t2) * 1000,
                    (_t4 - _t3) * 1000,
                    (_t5 - _t4) * 1000,
                    (_t5 - _t0) * 1000,
                )
                if result.get("directive"):
                    return TaskResult(
                        speech=result.get("speech", ""),
                        directive=result.get("directive"),
                        reason=result.get("reason"),
                        metadata=metadata,
                        voice_followup=bool(result.get("voice_followup")),
                    )
                explicit_error = result.get("error")
                if explicit_error:
                    error = explicit_error
                    if not isinstance(error, AgentError):
                        error = AgentError.model_validate(explicit_error)
                    return TaskResult(
                        speech=result.get("speech", ""),
                        error=error,
                        metadata=metadata,
                        voice_followup=bool(result.get("voice_followup")),
                    )
                return TaskResult(
                    speech=result["speech"],
                    metadata=metadata,
                    voice_followup=bool(result.get("voice_followup")),
                    action_executed=ActionExecuted(
                        action=action.get("action", ""),
                        entity_id=result.get("entity_id") or "",
                        success=result.get("success", False),
                        new_state=result.get("new_state"),
                        cacheable=result.get("cacheable", True),
                        # P1-5: forward the action's structured parameters
                        # (brightness, color_temp, transition, ...) so the
                        # orchestrator can persist them on the response
                        # cache entry and replay the exact same call on
                        # the next hit. Executors may optionally override
                        # this by returning ``service_data`` on the result
                        # dict.
                        service_data=(
                            result.get("service_data")
                            if isinstance(result.get("service_data"), dict)
                            else (action.get("parameters") or {})
                        ),
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Action execution failed for %s action=%s", agent_id, action)
                entity = action.get("entity", "the device")
                return self._error_result(
                    AgentErrorCode.ACTION_FAILED,
                    f"Sorry, I could not execute the action on {entity}.",
                )

        # Path B: Action but no HA client
        if action and not self._ha_client:
            logger.warning("Action parsed but ha_client is None for %s: %s", agent_id, action)
            entity = action.get("entity", "the device")
            return self._error_result(
                AgentErrorCode.HA_UNAVAILABLE,
                f"I understood the request for {entity}, but the smart home connection is currently unavailable.",
                recoverable=False,
            )

        # Path C: No action (informational)
        return self._handle_parse_miss(task, response)


# ---------------------------------------------------------------------------
# Config-driven domain agent infrastructure (Part 2B)
# ---------------------------------------------------------------------------


class _ConfigurableDomainAgent(ActionableAgent):
    """Domain agent whose behaviour is driven by the @agent decorator metadata.

    Standard agents (light, climate, cover, vacuum, scene, security, media,
    music, automation) are instantiated through this class.  Agents that
    need unique logic (TimerAgent, ListsAgent, CalendarAgent) continue to
    use their own subclasses.
    """

    def __init__(self, ha_client=None, entity_index=None, entity_matcher=None) -> None:
        meta = getattr(self.__class__, "_agent_meta", {})
        self._prompt_name = meta.get("prompt_name", "")
        self._allowed_domains = meta.get("allowed_domains")
        super().__init__(ha_client=ha_client, entity_index=entity_index, entity_matcher=entity_matcher)

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        ctx = getattr(self, "_current_task_context", None)
        area_id = ctx.area_id if ctx else None
        verbatim_terms = self._extract_verbatim_terms()

        kwargs: dict[str, Any] = {
            "preferred_area_id": area_id,
            "task_context": ctx,
            "verbatim_terms": verbatim_terms,
        }

        meta = getattr(self.__class__, "_agent_meta", {})
        executor_module = meta.get("executor_module", "")
        executor_name = meta.get("executor_name", "")

        import importlib as _importlib

        t0 = time.perf_counter()
        executor_fn = getattr(_importlib.import_module(executor_module), executor_name)
        t1 = time.perf_counter()
        sig = _inspect.signature(executor_fn)
        filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
        t2 = time.perf_counter()
        result = await executor_fn(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id=agent_id,
            span_collector=span_collector,
            **filtered,
        )
        t3 = time.perf_counter()
        logger.debug(
            "_do_execute %s: import=%.1fms prep=%.1fms exec=%.1fms total=%.1fms",
            agent_id,
            (t1 - t0) * 1000,
            (t2 - t1) * 1000,
            (t3 - t2) * 1000,
            (t3 - t0) * 1000,
        )
        return result

    @property
    def agent_card(self) -> AgentCard:
        meta = getattr(self.__class__, "_agent_meta", {})
        card_kwargs: dict[str, Any] = {
            "agent_id": meta.get("agent_id", ""),
            "name": meta.get("name", ""),
            "description": meta.get("description", ""),
            "skills": meta.get("skills", []),
            "endpoint": meta.get("endpoint", ""),
        }
        expected_latency = meta.get("expected_latency")
        if expected_latency:
            card_kwargs["expected_latency"] = expected_latency
        timeout_sec = meta.get("timeout_sec")
        if timeout_sec is not None:
            card_kwargs["timeout_sec"] = timeout_sec
        return AgentCard(**card_kwargs)


# -- Domain agent classes (decorated, registered via @agent) ------------------


@agent(
    agent_id="light-agent",
    name="Light Agent",
    description=(
        "Controls and queries lights, switches, and illuminance sensors: on/off, toggle, "
        "brightness, color, color temperature. Reports light/switch status and light-level "
        "readings. Lists all lights and switches. Reads Home Assistant Recorder history for "
        "lights, switches, and illuminance sensors (e.g. how long a light was on yesterday)."
    ),
    skills=[
        "light_control",
        "switch_control",
        "brightness",
        "color",
        "toggle",
        "illuminance_sensor",
        "light_status",
        "light_query",
        "switch_status",
        "switch_query",
        "entity_history",
        "recorder_history",
    ],
    prompt_name="light",
    allowed_domains=frozenset({"light", "switch", "sensor"}),
    executor_module="app.agents.light_executor",
    executor_name="execute_light_action",
)
class LightAgent(_ConfigurableDomainAgent):
    _supports_conditions = True


@agent(
    agent_id="climate-agent",
    name="Climate Agent",
    description=(
        "Controls and queries climate/HVAC devices, fans, humidifiers, environmental sensors, "
        "and local weather conditions/forecasts. Set temperature, HVAC mode, fan speed, "
        "humidity, turn on/off. Control fans: speed, preset mode, oscillation, direction. "
        "Control humidifiers: target humidity, mode. Reads sensors: temperature, humidity, "
        "pressure, dew point, wind, precipitation. Queries weather entities for current "
        "conditions and forecasts."
    ),
    skills=[
        "temperature",
        "hvac_mode",
        "fan_speed",
        "humidity",
        "climate_on_off",
        "sensor_reading",
        "climate_status",
        "sensor_query",
        "weather_sensor",
        "current_weather",
        "weather_forecast",
        "entity_history",
        "recorder_history",
        "fan_control",
        "fan_speed",
        "fan_preset",
        "fan_oscillate",
        "fan_direction",
        "humidifier_control",
        "humidifier_humidity",
        "humidifier_mode",
    ],
    prompt_name="climate",
    allowed_domains=frozenset({"climate", "weather", "sensor"}),
    executor_module="app.agents.climate_executor",
    executor_name="execute_climate_action",
    db_gated=True,
)
class ClimateAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="cover-agent",
    name="Cover Agent",
    description=(
        "Controls and queries covers, blinds, curtains, shutters, garage doors, gates, "
        "awnings, and windows: open, close, stop, set position, and tilt control. "
        "Reports cover status including current position and tilt position. "
        "Lists all cover entities."
    ),
    skills=[
        "cover_control",
        "open",
        "close",
        "stop",
        "set_position",
        "tilt_control",
        "query_cover_state",
        "list_covers",
        "entity_history",
        "recorder_history",
    ],
    prompt_name="cover",
    allowed_domains=frozenset({"cover"}),
    executor_module="app.agents.cover_executor",
    executor_name="execute_cover_action",
)
class CoverAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="vacuum-agent",
    name="Vacuum Agent",
    description=(
        "Controls and queries robot vacuum cleaners: start cleaning, pause, stop, "
        "return to base, clean spot, locate, and set fan speed. Reports vacuum state "
        "including battery level, fan speed, and status. Lists all vacuum entities."
    ),
    skills=[
        "vacuum_control",
        "start",
        "pause",
        "stop",
        "return_to_base",
        "clean_spot",
        "set_fan_speed",
        "locate",
        "query_vacuum_state",
        "list_vacuums",
    ],
    prompt_name="vacuum",
    allowed_domains=frozenset({"vacuum"}),
    executor_module="app.agents.vacuum_executor",
    executor_name="execute_vacuum_action",
)
class VacuumAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="scene-agent",
    name="Scene Agent",
    description=(
        "Activates Home Assistant scenes with optional transition timing. "
        "Lists available scenes and checks if a scene exists."
    ),
    skills=["scene_activate", "scene_list", "scene_query"],
    prompt_name="scene",
    allowed_domains=frozenset({"scene"}),
    executor_module="app.agents.scene_executor",
    executor_name="execute_scene_action",
    db_gated=True,
)
class SceneAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="security-agent",
    name="Security Agent",
    description=(
        "Controls and queries locks, alarm panels, cameras, and security sensors "
        "(motion, door, window, doorbell, smoke, gas). Lock/unlock, arm/disarm, "
        "camera on/off. Reports status and lists all security devices. Reads Home "
        "Assistant Recorder history for those entities (e.g. door open events yesterday)."
    ),
    skills=[
        "lock_control",
        "alarm_control",
        "camera_control",
        "door_sensor",
        "window_sensor",
        "motion_sensor",
        "doorbell",
        "smoke_sensor",
        "security_status",
        "security_query",
        "entity_history",
        "recorder_history",
    ],
    prompt_name="security",
    allowed_domains=frozenset({"lock", "binary_sensor", "alarm_control_panel"}),
    executor_module="app.agents.security_executor",
    executor_name="execute_security_action",
    db_gated=True,
)
class SecurityAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="media-agent",
    name="Media Agent",
    description=(
        "Controls generic media players (TV, Chromecast, streaming devices): "
        "on/off, play/pause/stop, volume, mute, input/source selection. "
        "Reports playback status. Not for music library/Music Assistant -- use music-agent."
    ),
    skills=[
        "tv_control",
        "speaker_control",
        "casting",
        "playback",
        "volume_control",
        "mute",
        "source_selection",
        "media_status",
        "playback_query",
    ],
    prompt_name="media",
    allowed_domains=frozenset({"media_player"}),
    executor_module="app.agents.media_executor",
    executor_name="execute_media_action",
    db_gated=True,
)
class MediaAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="music-agent",
    name="Music Agent",
    description=(
        "Controls music playback via Music Assistant: play, pause, skip, volume, "
        "shuffle, repeat, library search, queue management, playlist/artist/album "
        "selection. Reports current track info and lists music players."
    ),
    skills=[
        "music_playback",
        "volume_control",
        "playlist_selection",
        "library_search",
        "queue_management",
        "shuffle",
        "repeat",
        "music_status",
        "playback_query",
    ],
    prompt_name="music",
    allowed_domains=frozenset({"media_player"}),
    executor_module="app.agents.music_executor",
    executor_name="execute_music_action",
)
class MusicAgent(_ConfigurableDomainAgent):
    pass


@agent(
    agent_id="automation-agent",
    name="Automation Agent",
    description=(
        "Enables, disables, triggers, creates, updates, deletes, and queries "
        "Home Assistant automations. Reports status (enabled/disabled, last triggered time). "
        "Lists all automations."
    ),
    skills=[
        "automation_enable",
        "automation_disable",
        "automation_trigger",
        "automation_status",
        "automation_query",
        "automation_create",
        "automation_update",
        "automation_delete",
        "automation_config",
    ],
    prompt_name="automation",
    allowed_domains=frozenset({"automation", "script"}),
    executor_module="app.agents.automation_executor",
    executor_name="execute_automation_action",
    db_gated=True,
)
class AutomationAgent(_ConfigurableDomainAgent):
    pass


DomainAgent = _ConfigurableDomainAgent
