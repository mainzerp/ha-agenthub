"""Orchestrator agent for intent classification and task dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

from app.a2a.protocol import JsonRpcRequest
from app.agents.base import BaseAgent
from app.agents.cancel_speech import generate_cancel_speech
from app.agents.compound_utterance import looks_compound
from app.agents.language_detect import detect_user_language
from app.agents.sanitize import strip_markdown
from app.analytics.collector import track_agent_timeout, track_request
from app.analytics.tracer import _optional_span
from app.cache.cache_manager import ActionReplayOutcome, RoutingSkipOutcome
from app.db.repository import ConversationRepository, SettingsRepository
from app.entity.deterministic_resolver import resolve_entity_deterministic_first
from app.entity.visibility import entity_is_visible
from app.ha_client.home_context import home_context_provider
from app.models.agent import AgentCard, AgentTask, TaskContext
from app.models.cache import ActionCacheEntry, CachedAction

logger = logging.getLogger(__name__)

# Conversation context setting defaults
_DEFAULT_CONVERSATION_CONTEXT_TURNS = 3
_MIN_CONVERSATION_CONTEXT_TURNS = 1
_MAX_CONVERSATION_CONTEXT_TURNS = 20

# Conversation memory limits
_MAX_CONVERSATIONS = 1000
_CONVERSATION_TTL_SECONDS = 1800  # 30 minutes

# Fallback agent when classification fails
_FALLBACK_AGENT = "general-agent"
# Virtual agent: classification LLM routes here when the user only dismisses the chat/voice turn.
_CANCEL_INTERACTION_AGENT = "cancel-interaction"
_INTERNAL_ONLY_AGENTS: frozenset[str] = frozenset({"orchestrator", "rewrite-agent", "filler-agent"})


class _RecoverableClassificationError(RuntimeError):
    def __init__(self, message: str, *, code: str = "parse_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _sanitize_condensed(
    condensed: str,
    fragment_re: re.Pattern[str] | None,
    original_line: str,
) -> str:
    """Strip embedded ``<known-agent> (NN%):`` fragments from a condensed task.

    The classification LLM occasionally repeats its own header inside the
    condensed task body (e.g. ``"climate-agent (96%): living room
    temperatureclimate-agent (96%): living room temperature"``). Reject
    those fragments and collapse verbatim back-to-back repetitions so the
    routed agent sees a clean, single-statement task.
    """
    if not condensed or fragment_re is None:
        return condensed
    original = condensed

    parts = fragment_re.split(condensed)
    text_segments = [parts[0], *parts[2::2]]
    text_segments = [seg.strip(" ;|,-") for seg in text_segments if seg and seg.strip()]

    seen: list[str] = []
    for seg in text_segments:
        if seg not in seen:
            seen.append(seg)

    if seen:
        cleaned = seen[0]
        half = len(cleaned) // 2
        while half > 0 and cleaned[:half] == cleaned[half : 2 * half] and cleaned[half:].startswith(cleaned[:half]):
            cleaned = cleaned[:half].rstrip()
            half = len(cleaned) // 2
    else:
        cleaned = condensed

    if cleaned != original:
        logger.warning(
            "Sanitized embedded classification fragments from condensed task: %s -> %s",
            repr(original_line[:200]),
            repr(cleaned[:200]),
        )
    return cleaned


_CANNED_TIMEOUT_SPEECH = "I couldn't process that request in time."
_CANNED_GENERAL_ERROR_SPEECH = "I couldn't process that request right now."

_CACHED_SERVICE_DATA_KEYS: frozenset[str] = frozenset(
    {
        "brightness",
        "brightness_pct",
        "color_name",
        "color_temp",
        "color_temp_kelvin",
        "rgb_color",
        "hs_color",
        "xy_color",
        "transition",
        "effect",
        "volume_level",
        "source",
        "media_content_id",
        "media_content_type",
        "temperature",
        "hvac_mode",
        "preset_mode",
        "fan_mode",
        "swing_mode",
        "position",
        "percentage",
    }
)


class OrchestratorAgent(BaseAgent):
    """Classifies user intent and dispatches to specialized agents via A2A."""

    def __init__(
        self,
        dispatcher,
        registry=None,
        cache_manager=None,
        ha_client=None,
        entity_index=None,
        filler_agent=None,
    ) -> None:
        super().__init__(ha_client=ha_client, entity_index=entity_index)
        self._dispatcher = dispatcher
        self._registry = registry
        self._cache_manager = cache_manager
        self._filler_agent = filler_agent
        self._conversations: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self._default_timeout: int = 5
        self._max_iterations: int = 3
        self._mediation_model: str | None = None
        self._mediation_temperature: float = 0.3
        self._mediation_max_tokens: int = 2048
        self._per_agent_timeout_cache: dict[str, float] = {}
        self._max_dispatch_timeout: float = 60.0
        self._known_agents_cache: tuple[float, set[str]] | None = None
        self._known_agents_ttl: float = 5.0

    async def initialize(self) -> None:
        """Load reliability config from DB. Call during startup."""
        await self._load_reliability_config()
        await self._load_mediation_config()

    async def _load_reliability_config(self) -> None:
        """Read timeout and max_iterations from settings."""
        try:
            val = await SettingsRepository.get_value("a2a.default_timeout", "5")
            self._default_timeout = int(val)
        except (ValueError, TypeError):
            pass
        try:
            val = await SettingsRepository.get_value("a2a.max_iterations", "3")
            self._max_iterations = int(val)
        except (ValueError, TypeError):
            pass
        try:
            val = await SettingsRepository.get_value("a2a.max_dispatch_timeout", "60")
            self._max_dispatch_timeout = float(val)
        except (ValueError, TypeError):
            pass
        # P2-2: invalidate per-agent cache so changes to settings or
        # AgentCard.timeout_sec are picked up on the next dispatch.
        self._per_agent_timeout_cache.clear()
        # P3-7: also invalidate the known-agents memo so admin reloads
        # immediately observe newly registered/unregistered agents.
        self._known_agents_cache = None
        logger.info(
            "Orchestrator reliability config: timeout=%ds max_iterations=%d max_dispatch_timeout=%.1fs",
            self._default_timeout,
            self._max_iterations,
            self._max_dispatch_timeout,
        )

    async def _resolve_dispatch_timeout(self, agent_id: str) -> float:
        """Return the dispatch timeout (seconds) for ``agent_id``.

        P2-2 (FLOW-TIMEOUT-1): resolution priority --
            1. ``agent.dispatch_timeout.<agent_id>`` settings key
               (operator override, persisted in SQLite).
            2. ``AgentCard.timeout_sec`` from the registry (per-agent
               default declared by the agent module itself).
            3. ``self._default_timeout`` (orchestrator-wide fallback).

        Result is capped at ``self._max_dispatch_timeout`` to protect
        against misconfiguration. Cached per agent_id for the lifetime
        of the orchestrator instance; ``initialize()`` clears the cache.
        """
        cached = self._per_agent_timeout_cache.get(agent_id)
        if cached is not None:
            return cached

        resolved: float | None = None
        # 1. Settings override.
        try:
            raw = await SettingsRepository.get_value(
                f"agent.dispatch_timeout.{agent_id}",
                "",
            )
            if raw:
                resolved = float(raw)
        except (ValueError, TypeError):
            resolved = None

        # 2. AgentCard.timeout_sec from the registry.
        if resolved is None and self._registry is not None:
            try:
                cards = await self._registry.list_agents()
                for card in cards:
                    if getattr(card, "agent_id", None) != agent_id:
                        continue
                    card_timeout = getattr(card, "timeout_sec", None)
                    if card_timeout is not None:
                        resolved = float(card_timeout)
                    break
            except Exception:
                logger.debug(
                    "Per-agent timeout: registry lookup failed for %s",
                    agent_id,
                    exc_info=True,
                )

        # 3. Orchestrator default.
        if resolved is None or resolved <= 0:
            resolved = float(self._default_timeout)

        # Cap to defend against misconfiguration.
        if resolved > self._max_dispatch_timeout:
            resolved = float(self._max_dispatch_timeout)

        self._per_agent_timeout_cache[agent_id] = resolved
        return resolved

    async def _load_mediation_config(self) -> None:
        """Read mediation/merge override params from settings."""
        try:
            val = await SettingsRepository.get_value("mediation.model", "")
            self._mediation_model = val if val else None
        except (ValueError, TypeError):
            self._mediation_model = None
        try:
            val = await SettingsRepository.get_value("mediation.temperature", "0.3")
            self._mediation_temperature = float(val)
        except (ValueError, TypeError):
            self._mediation_temperature = 0.3
        try:
            val = await SettingsRepository.get_value("mediation.max_tokens", "2048")
            self._mediation_max_tokens = int(val)
        except (ValueError, TypeError):
            self._mediation_max_tokens = 2048
        logger.info(
            "Mediation config: model=%s temperature=%.1f max_tokens=%d",
            self._mediation_model or "(orchestrator default)",
            self._mediation_temperature,
            self._mediation_max_tokens,
        )

    async def _get_known_agents(self) -> set[str]:
        """Return set of currently registered agent IDs (excluding orchestrator).

        P3-7: result is memoised for ``_known_agents_ttl`` seconds so the
        hot classification path does not fan out a registry query on every
        request. The set is rebuilt on the next call once the TTL expires;
        ``initialize()`` clears the cache so admin reloads are not
        starved by it.
        """
        if not self._registry:
            return {"light-agent", "music-agent", "general-agent", _CANCEL_INTERACTION_AGENT}
        now = time.monotonic()
        cached = self._known_agents_cache
        if cached is not None and (now - cached[0]) < self._known_agents_ttl:
            return set(cached[1])
        cards = await self._registry.list_agents()
        agents = {card.agent_id for card in cards if card.agent_id != "orchestrator"} | {_CANCEL_INTERACTION_AGENT}
        self._known_agents_cache = (now, set(agents))
        return agents

    async def _resolve_language(
        self, user_text: str, context_language: str | None = None, turns: list[dict] | None = None
    ) -> str:
        """Resolve effective language: DB setting > auto-detect > turns-detect > fallback."""
        setting = await SettingsRepository.get_value("language", "auto")
        if setting and setting != "auto":
            return setting  # Manual override from settings
        # Auto-detect from user text
        detected = detect_user_language(user_text, fallback="")
        if detected:
            return detected
        # Low confidence on short text - try with recent conversation context
        if turns:
            user_turns = [t.get("content", "") for t in turns if t.get("role") == "user"]
            if user_turns:
                combined = " ".join(user_turns[-3:]) + " " + user_text
                detected = detect_user_language(combined, fallback="")
                if detected:
                    return detected
        return context_language or "en"

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="orchestrator",
            name="Orchestrator",
            description="Routes user requests to the appropriate specialized agent.",
            skills=["intent_classification", "task_routing"],
            endpoint="local://orchestrator",
        )

    async def _dispatch_single(
        self,
        target_agent: str,
        condensed_task: str,
        user_text: str,
        conversation_id: str | None,
        turns: list[dict],
        span_collector,
        incoming_context: TaskContext | None = None,
        skip_dispatch_span: bool = False,
        *,
        resolved_language: str | None = None,
    ) -> tuple[str, str, dict | None]:
        """Dispatch a single task to one agent and return (agent_id, speech, result_dict)."""
        t_dispatch = time.perf_counter()
        context = TaskContext(conversation_turns=turns)
        if incoming_context:
            context.device_id = incoming_context.device_id
            context.area_id = incoming_context.area_id
            # FLOW-CTX-1 (0.18.6): propagate human-readable names and
            # origin so downstream agents + traces don't have to
            # reach back up the stack for them.
            context.device_name = incoming_context.device_name
            context.area_name = incoming_context.area_name
            context.source = incoming_context.source
            context.language = incoming_context.language
            context.native_plain_timer_eligible = incoming_context.native_plain_timer_eligible
            context.injection_detected = incoming_context.injection_detected
        # FLOW-HIGH-3: Prefer the orchestrator-resolved language (from
        # _resolve_language / detect_user_language) over whatever the
        # incoming request carried. The streaming path already does this;
        # the non-streaming path used to silently keep the original
        # context language, which is usually "en" from HA.
        if resolved_language:
            context.language = resolved_language

        if target_agent == _CANCEL_INTERACTION_AGENT:
            speech = await generate_cancel_speech(context.language, user_text)
            await track_request(
                _CANCEL_INTERACTION_AGENT,
                cache_hit=False,
                latency_ms=(time.perf_counter() - t_dispatch) * 1000,
            )
            return _CANCEL_INTERACTION_AGENT, speech, {"speech": speech, "action_executed": None}

        # Populate home location/time context
        if self._ha_client:
            try:
                from zoneinfo import ZoneInfo

                home_ctx = await home_context_provider.get(self._ha_client)
                context.timezone = home_ctx.timezone
                context.location_name = home_ctx.location_name
                try:
                    tz = ZoneInfo(home_ctx.timezone)
                    now = datetime.now(tz)
                    context.local_time = now.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    context.local_time = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
            except Exception:
                logger.debug("Failed to populate home context", exc_info=True)

        verbatim_terms = self._extract_verbatim_terms(user_text)
        agent_task = AgentTask(
            description=self._append_original_suffix(condensed_task, verbatim_terms),
            user_text=user_text,
            conversation_id=conversation_id,
            context=context,
            verbatim_terms=verbatim_terms,
        )
        request = JsonRpcRequest(
            method="message/send",
            params={
                "agent_id": target_agent,
                "task": agent_task.model_dump(),
                "_span_collector": span_collector,
            },
            id=conversation_id or "orchestrator-dispatch",
        )
        try:
            t0 = time.perf_counter()
            noop_span = {"metadata": {}}
            dispatch_ctx = (
                contextlib.nullcontext(noop_span)
                if skip_dispatch_span
                else _optional_span(span_collector, "dispatch", agent_id=target_agent)
            )
            # P2-2 (FLOW-TIMEOUT-1): use the per-agent timeout instead
            # of the global 5s default so long-running agents
            # (general/web search, MCP-tool reasoning) are not killed
            # mid-call.
            dispatch_timeout = await self._resolve_dispatch_timeout(target_agent)
            async with dispatch_ctx as span:
                response = await asyncio.wait_for(
                    self._dispatcher.dispatch(request),
                    timeout=dispatch_timeout,
                )
                latency_ms = (time.perf_counter() - t0) * 1000
                span["metadata"]["latency_ms"] = round(latency_ms, 1)
                span["metadata"]["dispatch_timeout_sec"] = dispatch_timeout
                result_data = response.result or {}
                span["metadata"]["agent_response"] = (result_data.get("speech") or "")[:500]
                span["metadata"]["condensed_task"] = condensed_task[:500]
            # P3-10: per-request hot-path log; debug.
            logger.debug("Agent %s responded in %.1fms", target_agent, latency_ms)
            await track_request(target_agent, cache_hit=False, latency_ms=latency_ms)
        except TimeoutError:
            logger.warning(
                "Agent %s timed out after %.1fs, falling back",
                target_agent,
                dispatch_timeout,
            )
            await track_agent_timeout(target_agent, dispatch_timeout)
            if target_agent != _FALLBACK_AGENT:
                request.params["agent_id"] = _FALLBACK_AGENT
                try:
                    # FLOW-HIGH-2: emit a distinct dispatch_fallback span
                    # and track_request for the fallback agent so the
                    # trace UI shows the real hop and analytics counts
                    # the actual handler.
                    t_fb = time.perf_counter()
                    fb_timeout = await self._resolve_dispatch_timeout(_FALLBACK_AGENT)
                    async with _optional_span(
                        span_collector,
                        "dispatch_fallback",
                        agent_id=_FALLBACK_AGENT,
                    ) as fb_span:
                        response = await asyncio.wait_for(
                            self._dispatcher.dispatch(request),
                            timeout=fb_timeout,
                        )
                        fb_latency_ms = (time.perf_counter() - t_fb) * 1000
                        fb_span["metadata"]["latency_ms"] = round(fb_latency_ms, 1)
                        fb_span["metadata"]["from_agent"] = target_agent
                        fb_span["metadata"]["reason"] = "timeout"
                        fb_span["metadata"]["dispatch_timeout_sec"] = fb_timeout
                        fb_result_data = response.result or {}
                        fb_span["metadata"]["agent_response"] = (fb_result_data.get("speech") or "")[:500]
                    await track_request(_FALLBACK_AGENT, cache_hit=False, latency_ms=fb_latency_ms)
                    target_agent = _FALLBACK_AGENT
                except TimeoutError:
                    await track_agent_timeout(_FALLBACK_AGENT, fb_timeout)
                    return target_agent, _CANNED_TIMEOUT_SPEECH, None
            else:
                return target_agent, _CANNED_TIMEOUT_SPEECH, None

        if response.error:
            logger.warning(
                "Agent %s error: %s -- falling back to %s",
                target_agent,
                response.error.message,
                _FALLBACK_AGENT,
            )
            if target_agent != _FALLBACK_AGENT:
                request.params["agent_id"] = _FALLBACK_AGENT
                try:
                    # FLOW-HIGH-2: emit a distinct dispatch_fallback span
                    # for the error-fallback path too and attribute the
                    # request to the actual fallback agent.
                    t_fb = time.perf_counter()
                    fb_timeout = await self._resolve_dispatch_timeout(_FALLBACK_AGENT)
                    async with _optional_span(
                        span_collector,
                        "dispatch_fallback",
                        agent_id=_FALLBACK_AGENT,
                    ) as fb_span:
                        response = await asyncio.wait_for(
                            self._dispatcher.dispatch(request),
                            timeout=fb_timeout,
                        )
                        fb_latency_ms = (time.perf_counter() - t_fb) * 1000
                        fb_span["metadata"]["latency_ms"] = round(fb_latency_ms, 1)
                        fb_span["metadata"]["from_agent"] = target_agent
                        fb_span["metadata"]["reason"] = "agent_error"
                        fb_span["metadata"]["dispatch_timeout_sec"] = fb_timeout
                        fb_result_data = response.result or {}
                        fb_span["metadata"]["agent_response"] = (fb_result_data.get("speech") or "")[:500]
                    await track_request(_FALLBACK_AGENT, cache_hit=False, latency_ms=fb_latency_ms)
                    target_agent = _FALLBACK_AGENT
                except TimeoutError:
                    await track_agent_timeout(_FALLBACK_AGENT, fb_timeout)
                    return target_agent, _CANNED_TIMEOUT_SPEECH, None
            else:
                # FLOW-HIGH-1: original target IS general-agent and errored.
                # Without this branch, response.result is typically empty
                # and the caller sees empty speech and no error info.
                # Surface a structured error + canned speech so downstream
                # code (multi-agent merge, response-cache gating, trace
                # summary) can react instead of returning blank output.
                error_code = (response.error.message or "unknown")[:64]
                return (
                    _FALLBACK_AGENT,
                    _CANNED_GENERAL_ERROR_SPEECH,
                    {
                        "speech": _CANNED_GENERAL_ERROR_SPEECH,
                        "error": {
                            "code": error_code,
                            "recoverable": True,
                        },
                    },
                )

        result = response.result or {}
        speech = result.get("speech", "")

        # Log structured errors from agents for observability
        error = result.get("error")
        if error:
            error_code = error.get("code", "unknown")
            logger.info(
                "Agent %s returned error: %s (recoverable=%s)",
                target_agent,
                error_code,
                error.get("recoverable", True),
            )

        return target_agent, speech, result

    async def _handle_sequential_send(
        self,
        classifications: list[tuple[str, str, float]],
        user_text: str,
        conversation_id: str,
        turns: list[dict],
        span_collector,
        incoming_context,
        *,
        resolved_language: str | None = None,
    ) -> tuple[str, str, dict | None]:
        """Handle sequential dispatch: content agent -> send agent.

        Returns (routed_to, speech, result_dict) like _dispatch_single.
        """
        content_agents = [(a, t, c) for a, t, c in classifications if a != "send-agent"]
        send_classification = next(((a, t, c) for a, t, c in classifications if a == "send-agent"), None)

        if not send_classification:
            logger.warning("_handle_sequential_send called without send-agent classification")
            return await self._dispatch_single(
                classifications[0][0],
                classifications[0][1],
                user_text,
                conversation_id,
                turns,
                span_collector,
                incoming_context=incoming_context,
                resolved_language=resolved_language,
            )

        _send_agent_id, send_task_text, _send_confidence = send_classification

        # Step 1: Dispatch content agent(s)
        _content_result: dict | None = None
        content_dispatched = False
        if content_agents:
            content_aid, content_task, _ = content_agents[0]
            content_dispatched = True
            content_language = resolved_language or (incoming_context.language if incoming_context else None) or "en"
            content_context = TaskContext(
                conversation_turns=turns,
                device_id=incoming_context.device_id if incoming_context else None,
                area_id=incoming_context.area_id if incoming_context else None,
                device_name=incoming_context.device_name if incoming_context else None,
                area_name=incoming_context.area_name if incoming_context else None,
                source=incoming_context.source if incoming_context else "api",
                language=content_language,
                sequential_send=True,
                injection_detected=incoming_context.injection_detected if incoming_context else False,
            )

            # Populate home location/time context
            if self._ha_client:
                try:
                    from zoneinfo import ZoneInfo

                    home_ctx = await home_context_provider.get(self._ha_client)
                    content_context.timezone = home_ctx.timezone
                    content_context.location_name = home_ctx.location_name
                    try:
                        tz = ZoneInfo(home_ctx.timezone)
                        now = datetime.now(tz)
                        content_context.local_time = now.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        content_context.local_time = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    logger.debug("Failed to populate home context for sequential send", exc_info=True)
            async with _optional_span(span_collector, "dispatch_content", agent_id=content_aid) as span:
                content_agent_id, content_speech, _content_result = await self._dispatch_single(
                    content_aid,
                    content_task,
                    user_text,
                    conversation_id,
                    turns,
                    span_collector,
                    incoming_context=content_context,
                    skip_dispatch_span=True,
                    resolved_language=resolved_language,
                )
                span["metadata"]["content_agent"] = content_agent_id
                span["metadata"]["content_length"] = len(content_speech or "")
                span["metadata"]["agent_response"] = (content_speech or "")[:500]
                span["metadata"]["condensed_task"] = content_task[:500]
        else:
            content_speech = turns[-1].get("content", "") if turns else ""
            content_agent_id = "conversation-history"

        if not content_speech:
            return (
                "send-agent",
                "No content available to send.",
                {
                    "speech": "No content available to send.",
                    "error": {
                        "code": "parse_error",
                        "recoverable": True,
                    },
                },
            )

        # FLOW-CRIT-3: When the content agent dispatched but failed
        # (timeout -> canned string + result=None, or returned a
        # structured error/partial_failure), we must NOT pipe that
        # canned text into the send-agent. Doing so would push messages
        # like "I couldn't process that request in time." into Telegram /
        # TTS as if the user had asked to send them.
        if content_dispatched:
            result_dict = _content_result or {}
            content_failed = (
                _content_result is None or bool(result_dict.get("error")) or bool(result_dict.get("partial_failure"))
            )
            if content_failed:
                fallback_speech = "I could not prepare the content to send."
                return (
                    "send-agent",
                    fallback_speech,
                    {
                        "speech": fallback_speech,
                        "error": {
                            "code": "content_unavailable",
                            "recoverable": True,
                        },
                    },
                )

        # Step 2: Build augmented task for send-agent with the content
        from app.agents.send import _CONTENT_SEPARATOR

        augmented_task = f"{send_task_text}{_CONTENT_SEPARATOR}{content_speech}"

        async with _optional_span(span_collector, "dispatch_send", agent_id="send-agent") as span:
            _send_aid, send_speech, send_result = await self._dispatch_single(
                "send-agent",
                augmented_task,
                user_text,
                conversation_id,
                turns,
                span_collector,
                incoming_context=incoming_context,
                skip_dispatch_span=True,
                resolved_language=resolved_language,
            )
            span["metadata"]["send_target"] = send_task_text
            span["metadata"]["content_from"] = content_agent_id
            span["metadata"]["agent_response"] = (send_speech or "")[:500]
            span["metadata"]["condensed_task"] = augmented_task[:500]

        routed_to = f"{content_agent_id}, send-agent"

        merged_result = dict(send_result) if send_result else {}
        if _content_result and _content_result.get("voice_followup"):
            merged_result["voice_followup"] = True

        return routed_to, send_speech, merged_result

    def _schedule_ha_voice_followup_if_requested(self, task: AgentTask, effective: bool) -> None:
        """Re-open Assist STT on the user's device (HA voice requests only)."""
        if not effective or not self._ha_client:
            return
        ctx = task.context
        if not ctx or ctx.source != "ha":
            logger.debug("Voice follow-up skipped (requires source=ha)")
            return
        if not ctx.area_id and not ctx.device_id:
            logger.debug("Voice follow-up skipped (need area_id and/or device_id, e.g. Companion has device_id only)")
            return
        from app.agents.background_actions import spawn_voice_followup_after_conversation

        spawn_voice_followup_after_conversation(
            self._ha_client,
            area_id=ctx.area_id,
            origin_device_id=ctx.device_id,
            entity_index=self._entity_index,
        )

    async def _organic_voice_followup_offer(
        self,
        ctx: TaskContext | None,
        language: str,
        has_error: bool,
        speech: str,
    ) -> tuple[str, bool]:
        """Optionally append a short closing question for satellite sessions."""
        if has_error or not speech or not speech.strip() or not ctx or ctx.source != "ha":
            return speech, False
        try:
            enabled = (
                await SettingsRepository.get_value("orchestrator.organic_followup_enabled", "false")
            ).lower() == "true"
            if not enabled:
                return speech, False
            raw_p = await SettingsRepository.get_value("orchestrator.organic_followup_probability", "0.08")
            p = float(raw_p)
        except (TypeError, ValueError):
            p = 0.08
        if random.random() >= p:
            return speech, False
        lang_key = "de" if (language or "en").lower().startswith("de") else "en"
        suffix = {
            "de": " Darf es noch etwas sein?",
            "en": " Is there anything else I can help with?",
        }[lang_key]
        return speech + suffix, True

    async def _merge_voice_followup_and_organic(
        self,
        speech: str,
        *,
        agent_requested: bool,
        ctx: TaskContext | None,
        language: str,
        has_error: bool,
    ) -> tuple[str, bool]:
        """Extend speech for organic follow-up; combine agent + organic mic-open flags."""
        if has_error:
            return speech, bool(agent_requested)
        speech_out, organic = await self._organic_voice_followup_offer(ctx, language, False, speech)
        return speech_out, bool(agent_requested or organic)

    # ------------------------------------------------------------------
    # Shared helpers to reduce duplication between handle_task / handle_task_stream
    # ------------------------------------------------------------------

    async def _try_cache_replay(
        self,
        *,
        task: AgentTask | None = None,
        user_text: str,
        language: str = "en",
        requesting_agent_id: str = "orchestrator",
        span_collector=None,
    ) -> tuple[ActionReplayOutcome | None, RoutingSkipOutcome | None]:
        """Try action replay first, then routing skip, before live classify."""
        if not self._cache_manager:
            return None, None
        if not await self._get_bool_setting("cache.enabled", True):
            return None, None

        async def _resolve_entity(current_text: str, target_agent: str) -> str | None:
            if not self._entity_index:
                return None
            context = getattr(task, "context", None)
            resolution = await resolve_entity_deterministic_first(
                current_text,
                self._entity_index,
                None,
                target_agent,
                preferred_area_id=getattr(context, "area_id", None),
                verbatim_terms=list(getattr(task, "verbatim_terms", []) or []),
            )
            return resolution.get("entity_id")

        async with _optional_span(span_collector, "cache_lookup", agent_id="orchestrator") as cache_span:
            action_hit = await self._cache_manager.try_replay_action(
                query_text=user_text,
                language=language,
                requesting_agent_id=requesting_agent_id,
                resolve_entity=_resolve_entity,
                check_visibility=self._cached_action_is_still_visible,
                execute_cached_action=self._execute_cached_action,
            )
            if action_hit is not None:
                cache_span["metadata"]["hit_type"] = "action_hit"
                cache_span["metadata"]["similarity"] = action_hit.similarity
                cache_span["metadata"]["cached_agent_id"] = action_hit.agent_id
                cache_span["metadata"]["cache_tier"] = "action"
                return action_hit, None

            routing_hit = await self._cache_manager.try_routing_skip(
                query_text=user_text,
                language=language,
            )
            if routing_hit is not None:
                cache_span["metadata"]["hit_type"] = "routing_hit"
                cache_span["metadata"]["similarity"] = routing_hit.similarity
                cache_span["metadata"]["cached_agent_id"] = routing_hit.agent_id
                cache_span["metadata"]["cache_tier"] = "routing"
                return None, routing_hit

            cache_span["metadata"]["hit_type"] = "miss"
            cache_span["metadata"]["cache_tier"] = "both_miss"
            return None, None

    @staticmethod
    def _build_synthetic_classifications(
        routing: RoutingSkipOutcome,
    ) -> list[tuple[str, str, float | None]]:
        return [(routing.agent_id, routing.condensed_task, 1.0)]

    async def _cached_action_is_still_visible(self, agent_id: str, entity_id: str) -> bool:
        """Re-evaluate per-agent visibility rules for a cached action's entity.

        Mirrors the include/exclude semantics enforced by
        ``EntityMatcher._apply_visibility_rules`` for the live path.
        Fail-closed: any error is treated as "not visible" so a cached
        action cannot fire when visibility cannot be evaluated.
        """
        if not entity_id:
            return False
        try:
            return await entity_is_visible(
                agent_id,
                entity_id,
                self._entity_index,
                fail_closed_on_metadata_gap=True,
            )
        except Exception:
            logger.debug(
                "Visibility evaluation failed for agent=%s entity=%s; treating as not visible",
                agent_id,
                entity_id,
                exc_info=True,
            )
            return False

    async def _finalize_action_replay_hit(
        self,
        hit: ActionReplayOutcome,
        conversation_id: str,
        user_text: str,
        span_collector,
        *,
        task: AgentTask | None = None,
    ) -> dict:
        """Finalize a successful action-cache full hit."""
        target_agent = hit.agent_id or "unknown"
        task_context = getattr(task, "context", None) if task is not None else None
        speech = hit.response_text or ""
        if self._cache_manager:
            speech = await self._cache_manager.apply_rewrite(hit)

        if hit.cached_action:
            async with _optional_span(span_collector, "ha_action", agent_id=target_agent) as ha_span:
                ha_span["metadata"]["action"] = hit.cached_action.service
                ha_span["metadata"]["entity"] = hit.cached_action.entity_id
                ha_span["metadata"]["success"] = hit.replay_result is not None
                ha_span["metadata"]["cached"] = True
        if hit.rewrite_applied:
            async with _optional_span(span_collector, "rewrite", agent_id="rewrite-agent") as rw_span:
                rw_span["metadata"]["original_text"] = (hit.original_response_text or "")[:500]
                rw_span["metadata"]["rewritten_text"] = speech[:500]
                rw_span["metadata"]["latency_ms"] = hit.rewrite_latency_ms
                rw_span["metadata"]["success"] = True
                if hit.rewrite_latency_ms is not None:
                    rw_span["duration_ms"] = round(hit.rewrite_latency_ms, 2)

        async with _optional_span(span_collector, "return", agent_id="orchestrator") as ret_span:
            ret_span["metadata"]["from_agent"] = target_agent
            ret_span["metadata"]["agent_response"] = speech[:500]
            ret_span["metadata"]["final_response"] = speech[:500]
            ret_span["metadata"]["mediated"] = bool(hit.rewrite_applied)
            ret_span["metadata"]["action_cache_hit"] = True
            ret_span["metadata"]["response_cache_hit"] = False
            ret_span["metadata"]["sanitized"] = False
            prior_turns = await self._get_turns(conversation_id)
            await self._store_turn(conversation_id, user_text, speech, agent_id=target_agent)
            if span_collector:
                try:
                    from app.analytics.tracer import create_trace_summary

                    await create_trace_summary(
                        trace_id=span_collector.trace_id,
                        conversation_id=conversation_id,
                        user_input=user_text,
                        final_response=speech,
                        routing_agent=target_agent,
                        routing_confidence=1.0,
                        routing_duration_ms=None,
                        condensed_task=user_text,
                        agents=["orchestrator", target_agent],
                        source=getattr(span_collector, "source", "api"),
                        conversation_turns=prior_turns,
                        device_id=getattr(task_context, "device_id", None),
                        area_id=getattr(task_context, "area_id", None),
                        device_name=getattr(task_context, "device_name", None),
                        area_name=getattr(task_context, "area_name", None),
                    )
                except Exception:
                    logger.warning("Failed to create trace summary", exc_info=True)

        return {
            "speech": speech,
            "routed_to": target_agent,
            "action_executed": hit.replay_result,
            "sanitized": False,
            "voice_followup": False,
        }

    @staticmethod
    def _is_readonly_action_result(action_executed) -> bool:
        if not isinstance(action_executed, dict):
            return False
        action_name = str(action_executed.get("action") or "").strip().lower()
        if not action_name:
            service_name = str(action_executed.get("service") or "").strip().lower()
            action_name = service_name.split("/", 1)[1] if "/" in service_name else service_name
        return action_name.startswith(("query_", "list_"))

    async def _store_after_dispatch(
        self,
        *,
        user_text: str,
        language: str,
        target_agent: str,
        condensed_task: str,
        confidence: float | None,
        speech: str,
        action_executed,
        has_error: bool,
        task: AgentTask | None = None,
        merged_multi_agent: bool = False,
        used_origin_context: bool = False,
    ) -> tuple[bool, bool]:
        """Store either an action-cache row or a routing-cache row, never both."""
        if merged_multi_agent or not self._cache_manager or not speech or has_error:
            return False, False
        if self._legacy_pipeline_enabled():
            return False, False
        if not await self._get_bool_setting("cache.enabled", True):
            return False, False
        if target_agent in (_CANCEL_INTERACTION_AGENT, "send-agent") or target_agent in _INTERNAL_ONLY_AGENTS:
            return False, False

        entity_ids: list[str] = []
        if isinstance(action_executed, dict):
            raw_entity_ids = action_executed.get("entity_ids")
            if isinstance(raw_entity_ids, list):
                entity_ids.extend(str(item) for item in raw_entity_ids if item)
            entity_id = str(action_executed.get("entity_id") or "").strip()
            if entity_id:
                entity_ids.append(entity_id)
            entity_ids = list(dict.fromkeys(entity_ids))

        confidence_value = confidence if confidence is not None else 0.0
        readonly_action = self._is_readonly_action_result(action_executed)
        if isinstance(action_executed, dict) and action_executed.get("success"):
            if readonly_action:
                try:
                    await self._cache_manager.store_routing_async(
                        user_text,
                        target_agent,
                        confidence_value,
                        condensed_task,
                        language=language,
                        entity_ids=entity_ids,
                    )
                    return False, True
                except Exception:
                    logger.warning("Failed to store routing decision", exc_info=True)
                    return False, False

            if action_executed.get("cacheable", True):
                entity_id = str(action_executed.get("entity_id") or "").strip()
                action_name = str(action_executed.get("action") or "").strip().lower()
                if entity_id and action_name:
                    raw_service_data = action_executed.get("service_data") or {}
                    cached_service_data: dict = {}
                    if isinstance(raw_service_data, dict):
                        for key in _CACHED_SERVICE_DATA_KEYS:
                            if key in raw_service_data:
                                cached_service_data[key] = raw_service_data[key]
                    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
                    cached_action = CachedAction(
                        service=f"{domain}/{action_name}" if domain else action_name,
                        entity_id=entity_id,
                        service_data=cached_service_data,
                    )
                    entry = ActionCacheEntry(
                        query_text=user_text,
                        language=language,
                        agent_id=target_agent,
                        condensed_task=condensed_task,
                        confidence=confidence_value,
                        response_text=speech,
                        cached_action=cached_action,
                        entity_ids=entity_ids,
                        origin_area_id=(task.context.area_id if used_origin_context and task and task.context else None),
                        origin_device_id=(
                            task.context.device_id if used_origin_context and task and task.context else None
                        ),
                        executed_at=datetime.now(UTC).isoformat(),
                    )
                    try:
                        await self._cache_manager.store_action_async(entry)
                        return True, False
                    except Exception:
                        logger.warning("Failed to store action cache entry", exc_info=True)
                        return False, False
                return False, False

            return False, False

        if self._is_actionable_routing_agent(target_agent):
            return False, False

        try:
            await self._cache_manager.store_routing_async(
                user_text,
                target_agent,
                confidence_value,
                condensed_task,
                language=language,
                entity_ids=entity_ids,
            )
            return False, True
        except Exception:
            logger.warning("Failed to store routing decision", exc_info=True)
            return False, False

    @staticmethod
    def _is_actionable_routing_agent(target_agent: str) -> bool:
        return (
            target_agent not in (_FALLBACK_AGENT, _CANCEL_INTERACTION_AGENT, "send-agent")
            and target_agent not in _INTERNAL_ONLY_AGENTS
        )

    @staticmethod
    def _bool_setting_default(default: bool) -> str:
        return "true" if default else "false"

    async def _get_bool_setting(self, key: str, default: bool) -> bool:
        try:
            legacy_key = {
                "cache.compound_utterance_bypass": "routing.compound_utterance_bypass",
            }.get(key)
            raw = await SettingsRepository.get_value(key, None)
            if raw is None and legacy_key is not None:
                raw = await SettingsRepository.get_value(legacy_key, None)
        except Exception:
            return default
        if raw is None:
            return default
        normalized = str(raw).strip().lower()
        if not normalized:
            return default
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        return default

    async def _create_trace(
        self,
        span_collector,
        conversation_id: str,
        user_text: str,
        speech: str,
        target_agent: str,
        confidence: float | None,
        condensed_task: str,
        classifications: list[tuple[str, str, float | None]],
        turns: list[dict],
        *,
        task_context: TaskContext | None = None,
    ) -> None:
        """Create a trace summary from span data.

        FLOW-CTX-1 (0.18.6): ``task_context`` carries device/area
        identity so the trace row can record which satellite spoke.
        """
        try:
            from app.analytics.tracer import create_trace_summary

            classify_duration = None
            for s in span_collector._spans:
                if s.get("span_name") == "classify":
                    classify_duration = s.get("duration_ms")
                    break
            agents = list({s.get("agent_id") for s in span_collector._spans if s.get("agent_id")})
            if "orchestrator" not in agents:
                agents.insert(0, "orchestrator")
            await create_trace_summary(
                trace_id=span_collector.trace_id,
                conversation_id=conversation_id,
                user_input=user_text,
                final_response=speech,
                routing_agent=target_agent,
                routing_confidence=confidence,
                routing_duration_ms=classify_duration,
                condensed_task=condensed_task,
                agents=agents,
                source=getattr(span_collector, "source", "api"),
                agent_instructions={aid: ctask for aid, ctask, _ in classifications}
                if len(classifications) > 1
                else None,
                conversation_turns=turns,
                device_id=getattr(task_context, "device_id", None),
                area_id=getattr(task_context, "area_id", None),
                device_name=getattr(task_context, "device_name", None),
                area_name=getattr(task_context, "area_name", None),
            )
        except Exception:
            logger.warning("Failed to create trace summary", exc_info=True)

    # ---------------------------------------------------------------
    # P1-1 (0.18.x): Unified pipeline entry point.
    #
    # ``handle_task`` and ``handle_task_stream`` are kept as the
    # public surface (BaseAgent contract / A2A transport entry).
    # Both delegate to ``_run_pipeline`` which selects between the
    # non-streaming and streaming impls. The actual pipeline bodies
    # live in ``_handle_task_impl`` and ``_handle_task_stream_impl``
    # and remain behavior-identical to the pre-refactor code so
    # that the streaming token sequence, multi-agent merge order,
    # cache-hit short-circuits, sequential-send filler timing,
    # cancel-interaction shortcut and FLOW-XXX fixes all stay in
    # the exact same call sites.
    #
    # The ``ORCHESTRATOR_LEGACY_PIPELINE=1`` environment variable
    # bypasses ``_run_pipeline`` and calls the impls directly. This
    # exists as a rollback lever in case a follow-up refactor
    # (deeper deduplication of the ~80% shared choreography)
    # introduces a regression -- production can be flipped back
    # without a code revert.
    # ---------------------------------------------------------------

    @staticmethod
    def _legacy_pipeline_enabled() -> bool:
        return os.environ.get("ORCHESTRATOR_LEGACY_PIPELINE") == "1"

    async def _pipeline_resolve_conversation_and_language(self, task: AgentTask) -> tuple[str, str, list]:
        """Resolve conversation_id (with uuid fallback), the effective
        language for this turn, and prefetch the conversation turns
        used by language detection.

        Shared prelude between :meth:`_handle_task_impl` and
        :meth:`_handle_task_stream_impl`. Behaviour-preserving
        extraction; both sites previously inlined an identical 7-line
        block.
        """
        user_text = task.user_text or task.description
        conversation_id = task.conversation_id
        if not conversation_id:
            conversation_id = str(uuid.uuid4())
            logger.debug("No conversation_id from HA, generated fallback: %s", conversation_id)
        context_language = (task.context.language if task.context else None) or "en"
        if self._is_background_turn(task):
            return conversation_id, context_language, []
        lang_turns = await self._get_turns(conversation_id)
        detected_language = await self._resolve_language(user_text, context_language, turns=lang_turns)
        return conversation_id, detected_language, lang_turns

    @staticmethod
    def _pipeline_record_classify_span(
        span,
        classifications: list[tuple[str, str, float | None]],
        user_text: str,
        condensed_task: str,
        confidence: float | None,
        routing_cached: bool,
        *,
        extended_metadata: bool = False,
        extra_metadata: dict | None = None,
    ) -> None:
        """Populate the ``classify`` span metadata block.

        Both pipeline impls record the same six base keys; only the
        non-streaming impl additionally records ``all_classifications``
        when more than one classification is returned. ``extended_metadata``
        opts into that extra key. Default ``False`` preserves the
        existing streaming behaviour exactly. Behaviour-preserving
        helper extracted in P1-1 iter 3.
        """
        span["metadata"]["target_agent"] = ", ".join(a for a, _, _ in classifications)
        span["metadata"]["user_input"] = user_text[:500]
        span["metadata"]["condensed_task"] = condensed_task[:500]
        span["metadata"]["confidence"] = confidence
        span["metadata"]["routing_cached"] = routing_cached
        span["metadata"]["multi_agent"] = len(classifications) > 1
        if extended_metadata and len(classifications) > 1:
            span["metadata"]["all_classifications"] = {
                a: {"task": t[:300], "confidence": c} for a, t, c in classifications
            }
        if extra_metadata:
            span["metadata"].update(extra_metadata)

    async def _finalize_single_agent_response(
        self,
        *,
        task: AgentTask,
        user_text: str,
        target_agent: str,
        confidence: float | None,
        condensed_task: str,
        speech: str,
        action_executed,
        has_error: bool,
        span_collector,
        conversation_id: str,
        language: str,
        turns: list,
        classifications: list[tuple[str, str, float | None]],
        voice_followup_requested: bool,
        routed_to: str | None = None,
        mediation_agent: str | None = None,
        skip_mediation_on_error: bool = True,
        skip_response_cache: bool = False,
        used_origin_context: bool = False,
    ) -> tuple[str, bool]:
        """Run the shared single-agent / sequential-send finalization
        block: open the ``return`` span, mediate the agent speech,
        merge organic / requested voice-followup, store the response
        cache, persist the turn and emit the trace summary. Returns
        ``(final_speech, voice_followup_effective)``.

        Both pipeline impls (single-agent path) executed an almost
        identical sequence here; the only intentional differences were
        (a) the non-streaming pipeline skips mediation when the agent
        already reported an error (``skip_mediation_on_error=True``)
        while the streaming pipeline always mediated, and (b) the
        ``from_agent`` / ``_store_turn`` agent_id tag uses the
        comma-joined ``routed_to`` for sequential-send while streaming
        uses the bare ``target_agent``. Both knobs are explicit
        parameters so callers preserve their prior behaviour exactly.
        Behaviour-preserving helper extracted in P1-1 iter 3.
        """
        if routed_to is None:
            routed_to = target_agent
        if mediation_agent is None:
            mediation_agent = target_agent
        original_speech = speech
        cache_stored_action = False
        cache_stored_routing = False
        async with _optional_span(span_collector, "return", agent_id="orchestrator") as ret_span:
            ret_span["metadata"]["from_agent"] = routed_to
            ret_span["metadata"]["agent_response"] = speech[:500]
            should_mediate = target_agent != _CANCEL_INTERACTION_AGENT and (
                not has_error or not skip_mediation_on_error
            )
            if should_mediate:
                speech = await self._mediate_response(
                    speech,
                    user_text,
                    mediation_agent,
                    language=language,
                    span_collector=span_collector,
                )
            speech, voice_followup_effective = await self._merge_voice_followup_and_organic(
                speech,
                agent_requested=voice_followup_requested,
                ctx=task.context,
                language=language,
                has_error=has_error,
            )
            ret_span["metadata"]["final_response"] = speech[:500]
            ret_span["metadata"]["mediated"] = speech != original_speech
            ret_span["metadata"]["voice_followup"] = voice_followup_effective
            if not skip_response_cache and target_agent != _CANCEL_INTERACTION_AGENT:
                cache_stored_action, cache_stored_routing = await self._store_after_dispatch(
                    user_text=user_text,
                    language=language,
                    target_agent=target_agent,
                    condensed_task=condensed_task,
                    confidence=confidence,
                    speech=speech,
                    action_executed=action_executed,
                    has_error=has_error,
                    task=task,
                    merged_multi_agent=False,
                    used_origin_context=used_origin_context,
                )
            ret_span["metadata"]["cache_stored_action"] = cache_stored_action
            ret_span["metadata"]["cache_stored_response"] = cache_stored_action
            ret_span["metadata"]["cache_stored_routing"] = cache_stored_routing
            await self._store_turn(conversation_id, user_text, speech, agent_id=routed_to)
            if span_collector:
                await self._create_trace(
                    span_collector,
                    conversation_id,
                    user_text,
                    speech,
                    target_agent,
                    confidence,
                    condensed_task,
                    classifications,
                    turns,
                    task_context=task.context,
                )
        return speech, voice_followup_effective

    async def _run_pipeline(
        self,
        task: AgentTask,
        *,
        streaming: bool,
        _pre_classified: tuple[list[tuple[str, str, float]], bool] | None = None,
        _classify_reason: str | None = None,
        _allow_classify_cache_lookup: bool | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Unified pipeline entry.

        When ``streaming`` is ``True`` this yields the same
        ``token``/``done`` chunks as :meth:`_handle_task_stream_impl`.
        When ``streaming`` is ``False`` this yields exactly one
        terminal chunk of the form ``{"done": True, "payload": dict}``
        where ``payload`` is the dict that the non-streaming
        :meth:`_handle_task_impl` would return.

        Note (P3-9, obsolete): the original plan called for
        consolidating the streaming and non-streaming dispatch paths
        into a single coroutine. After P1-1 iterations 1-3 the shared
        helpers (``_dispatch_single``, ``_handle_sequential_send``,
        ``_classify``, ``_finalize_single_agent_response``,
        ``_create_trace``, ``_store_after_dispatch``) already cover all
        non-streaming-specific logic. The streaming impl additionally
        delegates multi-agent and sequential-send back to ``handle_task``
        instead of re-implementing them. The remaining differences are
        the genuine streaming primitives (token-by-token relay and the
        filler/queue race), which P1-1 documented as a "genuine
        architectural difference". P3-9 is therefore considered done by
        P1-1 and intentionally not deduplicated further.
        """
        if streaming:
            async for chunk in self._handle_task_stream_impl(task):
                yield chunk
            return
        result = await self._handle_task_impl(
            task,
            _pre_classified=_pre_classified,
            _classify_reason=_classify_reason,
            _allow_classify_cache_lookup=_allow_classify_cache_lookup,
        )
        yield {"done": True, "payload": result}

    async def handle_task(
        self,
        task: AgentTask,
        *,
        _pre_classified: tuple[list[tuple[str, str, float]], bool] | None = None,
        _classify_reason: str | None = None,
        _allow_classify_cache_lookup: bool | None = None,
    ) -> dict:
        """Public non-streaming entry point.

        Wraps :meth:`_run_pipeline` and unpacks the terminal chunk.
        Honors ``ORCHESTRATOR_LEGACY_PIPELINE=1`` for emergency
        rollback to the direct impl call.
        """
        if self._legacy_pipeline_enabled():
            return await self._handle_task_impl(
                task,
                _pre_classified=_pre_classified,
                _classify_reason=_classify_reason,
                _allow_classify_cache_lookup=_allow_classify_cache_lookup,
            )
        final: dict | None = None
        async for chunk in self._run_pipeline(
            task,
            streaming=False,
            _pre_classified=_pre_classified,
            _classify_reason=_classify_reason,
            _allow_classify_cache_lookup=_allow_classify_cache_lookup,
        ):
            if chunk.get("done"):
                final = chunk
        if final is None or "payload" not in final:
            # Defensive: the non-streaming branch always yields a
            # terminal chunk with ``payload``. Reaching this means
            # something replaced the pipeline at runtime; fall back
            # to a direct impl call rather than returning ``None``.
            return await self._handle_task_impl(
                task,
                _pre_classified=_pre_classified,
                _classify_reason=_classify_reason,
                _allow_classify_cache_lookup=_allow_classify_cache_lookup,
            )
        return final["payload"]

    def handle_task_stream(self, task: AgentTask) -> AsyncGenerator[dict, None]:
        """Public streaming entry point.

        Returns the unified pipeline iterator directly. Honors
        ``ORCHESTRATOR_LEGACY_PIPELINE=1`` for emergency rollback.
        """
        if self._legacy_pipeline_enabled():
            return self._handle_task_stream_impl(task)
        return self._run_pipeline(task, streaming=True)

    async def _handle_task_impl(
        self,
        task: AgentTask,
        *,
        _pre_classified: tuple[list[tuple[str, str, float]], bool] | None = None,
        _classify_reason: str | None = None,
        _allow_classify_cache_lookup: bool | None = None,
    ) -> dict:
        user_text = task.user_text or task.description
        conversation_id, detected_language, _lang_turns = await self._pipeline_resolve_conversation_and_language(task)

        # Get span collector from task context if available
        span_collector = task.span_collector

        if self._is_background_turn(task):
            result = await self._handle_background_turn(task)
            return {
                "speech": result.get("speech", ""),
                "conversation_id": conversation_id,
                "routed_to": "orchestrator",
                "action_executed": result.get("action_executed"),
                "voice_followup": False,
                "error": result.get("error"),
            }

        # 0. Routing cache lookup (before classify)
        compound_bypass = False
        action_replay = None
        routing_skip = None
        if _pre_classified is None and self._cache_manager:
            if await self._get_bool_setting("cache.compound_utterance_bypass", True) and looks_compound(user_text):
                compound_bypass = True
                logger.debug("Skipping cache lookup for structurally compound utterance: %r", user_text[:80])
            else:
                action_replay, routing_skip = await self._try_cache_replay(
                    task=task,
                    user_text=user_text,
                    language=detected_language,
                    span_collector=span_collector,
                )
        if action_replay is not None:
            replay = await self._finalize_action_replay_hit(
                action_replay,
                conversation_id,
                user_text,
                span_collector,
                task=task,
            )
            replay["conversation_id"] = conversation_id
            self._schedule_ha_voice_followup_if_requested(task, False)
            return replay

        pre_classified = _pre_classified
        synthetic_preclassified = False
        allow_classify_cache_lookup = _allow_classify_cache_lookup if _allow_classify_cache_lookup is not None else False
        next_classify_extra: dict[str, object] = {}
        used_origin_context = False
        if compound_bypass:
            next_classify_extra["compound_bypass"] = True
            next_classify_extra["compound_bypass_reason"] = "multi_sentence"
        if routing_skip is not None:
            pre_classified = (self._build_synthetic_classifications(routing_skip), True)
            synthetic_preclassified = True
            next_classify_extra["reason"] = "routing_cache_skip"
        if _classify_reason:
            next_classify_extra["reason"] = _classify_reason

        agent_responses: list[tuple[str, str, bool]] = []
        while True:
            # 1. Classify intent (skip if pre-classified by handle_task_stream)
            if pre_classified is not None:
                classifications, routing_cached = pre_classified
                target_agent, condensed_task, confidence = classifications[0]
                pre_classified = None
                if synthetic_preclassified:
                    async with _optional_span(span_collector, "classify", agent_id="orchestrator") as span:
                        self._pipeline_record_classify_span(
                            span,
                            classifications,
                            user_text,
                            condensed_task,
                            confidence,
                            routing_cached,
                            extended_metadata=True,
                            extra_metadata=next_classify_extra or None,
                        )
                    synthetic_preclassified = False
            else:
                try:
                    async with _optional_span(span_collector, "classify", agent_id="orchestrator") as span:
                        classifications, routing_cached = await self._classify(
                            user_text,
                            cache_result=None,
                            conversation_id=conversation_id,
                            span_collector=span_collector,
                            language=detected_language,
                            allow_cache_lookup=allow_classify_cache_lookup,
                        )
                        target_agent, condensed_task, confidence = classifications[0]
                        self._pipeline_record_classify_span(
                            span,
                            classifications,
                            user_text,
                            condensed_task,
                            confidence,
                            routing_cached,
                            extended_metadata=True,
                            extra_metadata=next_classify_extra or None,
                        )
                except _RecoverableClassificationError as exc:
                    return {
                        "speech": exc.message,
                        "conversation_id": conversation_id,
                        "routed_to": "orchestrator",
                        "action_executed": None,
                        "voice_followup": False,
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            "recoverable": True,
                        },
                    }
            next_classify_extra = {}
            allow_classify_cache_lookup = False

            # P3-10: per-request routing log; debug.
            logger.debug(
                "Routed to %s (%s): %s (conversation=%s)",
                target_agent,
                f"{confidence * 100:.0f}%" if confidence is not None else "unknown",
                condensed_task[:80],
                conversation_id,
            )

            # 2. Build context with conversation turns
            turns = await self._get_turns(conversation_id)

            # Check for sequential send dispatch
            is_sequential_send = any(a == "send-agent" for a, _, _ in classifications) and any(
                a != "send-agent" for a, _, _ in classifications
            )

            # 3-4. Dispatch
            incoming_context = task.context
            failed_agents: list[tuple[str, str]] = []
            agent_error = None
            agent_voice_followup = False
            directive = None
            directive_reason = None
            if is_sequential_send:
                routed_to, speech, result = await self._handle_sequential_send(
                    classifications,
                    user_text,
                    conversation_id,
                    turns,
                    span_collector,
                    incoming_context=incoming_context,
                    resolved_language=detected_language,
                )
                action_executed = (result or {}).get("action_executed")
                has_error = bool((result or {}).get("error"))
                agent_error = (result or {}).get("error")
                agent_voice_followup = bool((result or {}).get("voice_followup"))
            elif len(classifications) == 1:
                agent_id, speech, result = await self._dispatch_single(
                    target_agent,
                    condensed_task,
                    user_text,
                    conversation_id,
                    turns,
                    span_collector,
                    incoming_context=incoming_context,
                    resolved_language=detected_language,
                )
                action_executed = (result or {}).get("action_executed")
                routed_to = agent_id
                directive = (result or {}).get("directive")
                directive_reason = (result or {}).get("reason")
                if directive:
                    return {
                        "speech": result.get("speech", ""),
                        "conversation_id": conversation_id,
                        "routed_to": routed_to,
                        "action_executed": None,
                        "voice_followup": False,
                        "directive": directive,
                        "reason": directive_reason,
                    }

                agent_error = (result or {}).get("error")
                has_error = agent_error is not None
                agent_voice_followup = bool((result or {}).get("voice_followup"))
            else:
                dispatch_coros = [
                    self._dispatch_single(
                        aid,
                        ctask,
                        user_text,
                        conversation_id,
                        turns,
                        span_collector,
                        incoming_context=incoming_context,
                        resolved_language=detected_language,
                    )
                    for aid, ctask, _ in classifications
                ]
                dispatch_results = await asyncio.gather(*dispatch_coros, return_exceptions=True)

                agent_responses = []
                action_executed = None
                routed_agents: list[str] = []
                for idx, dr in enumerate(dispatch_results):
                    agent_id_for_idx = classifications[idx][0]
                    if isinstance(dr, Exception):
                        logger.warning("Multi-agent dispatch error for %s: %s", agent_id_for_idx, dr)
                        failed_agents.append((agent_id_for_idx, str(dr)))
                        continue
                    aid, sp, res = dr
                    res_dict = res or {}
                    res_error = res_dict.get("error") if isinstance(res_dict, dict) else None
                    if res is None or res_error or sp in (_CANNED_TIMEOUT_SPEECH, _CANNED_GENERAL_ERROR_SPEECH):
                        if res_error:
                            reason = (
                                res_error.get("code", "canned_error") if isinstance(res_error, dict) else "canned_error"
                            )
                        elif res is None:
                            reason = "timeout"
                        else:
                            reason = "canned_speech"
                        logger.warning(
                            "Multi-agent dispatch reported error for %s: %s",
                            agent_id_for_idx,
                            reason,
                        )
                        failed_agents.append((agent_id_for_idx, reason))
                        continue
                    routed_agents.append(aid)
                    acted = bool(res and res.get("action_executed"))
                    agent_responses.append((aid, sp, acted))
                    if res and res.get("action_executed") and action_executed is None:
                        action_executed = res["action_executed"]
                    if res and res.get("voice_followup"):
                        agent_voice_followup = True

                target_agent = routed_agents[0] if routed_agents else _FALLBACK_AGENT
                routed_to = ", ".join(routed_agents) if routed_agents else _FALLBACK_AGENT
                speech = ""
                has_error = len(failed_agents) > 0
            break

        # 5. Mediate response, store turn, trace
        voice_followup_effective = False
        if len(classifications) > 1 and not is_sequential_send:
            # --- Multi-agent finalization (inline; merge step has no
            # streaming counterpart and must run before mediation skip).
            original_speech = speech
            async with _optional_span(span_collector, "return", agent_id="orchestrator") as ret_span:
                ret_span["metadata"]["from_agent"] = routed_to
                if not agent_responses and failed_agents:
                    speech = "I'm sorry, I couldn't complete that request. All agents encountered errors."
                else:
                    speech = await self._merge_responses(agent_responses, user_text, span_collector=span_collector)
                    if failed_agents:
                        failed_names = ", ".join(aid for aid, _ in failed_agents)
                        speech += f"\n\n(Note: {failed_names} could not be reached.)"
                result = {"speech": speech}
                ret_span["metadata"]["agent_response"] = speech[:500]
                speech, voice_followup_effective = await self._merge_voice_followup_and_organic(
                    speech,
                    agent_requested=agent_voice_followup,
                    ctx=incoming_context,
                    language=detected_language,
                    has_error=has_error,
                )
                ret_span["metadata"]["final_response"] = speech[:500]
                ret_span["metadata"]["mediated"] = (speech != original_speech) or len(classifications) > 1
                ret_span["metadata"]["voice_followup"] = voice_followup_effective
                ret_span["metadata"]["cache_stored_response"] = False
                ret_span["metadata"]["cache_stored_routing"] = False
                await self._store_turn(conversation_id, user_text, speech, agent_id=routed_to)
                if span_collector:
                    await self._create_trace(
                        span_collector,
                        conversation_id,
                        user_text,
                        speech,
                        target_agent,
                        confidence,
                        condensed_task,
                        classifications,
                        turns,
                        task_context=task.context,
                    )
        else:
            mediation_agent = "send-agent" if is_sequential_send else target_agent
            speech, voice_followup_effective = await self._finalize_single_agent_response(
                task=task,
                user_text=user_text,
                target_agent=target_agent,
                confidence=confidence,
                condensed_task=condensed_task,
                speech=speech,
                action_executed=action_executed,
                has_error=has_error,
                span_collector=span_collector,
                conversation_id=conversation_id,
                language=detected_language,
                turns=turns,
                classifications=classifications,
                voice_followup_requested=agent_voice_followup,
                routed_to=routed_to,
                mediation_agent=mediation_agent,
                skip_mediation_on_error=True,
                skip_response_cache=is_sequential_send,
                used_origin_context=used_origin_context,
            )

        response = {
            "speech": strip_markdown(speech),
            "conversation_id": conversation_id,
            "routed_to": routed_to,
            "action_executed": action_executed,
            "voice_followup": voice_followup_effective,
        }
        if has_error:
            response["error"] = {
                "code": agent_error.get("code", "unknown") if agent_error else "unknown",
                "recoverable": agent_error.get("recoverable", True) if agent_error else True,
            }
        if failed_agents:
            response["partial_failure"] = {
                "failed_agents": [{"agent_id": aid, "error": msg} for aid, msg in failed_agents],
            }
        self._schedule_ha_voice_followup_if_requested(task, voice_followup_effective)
        return response

    async def _handle_task_stream_impl(self, task: AgentTask) -> AsyncGenerator[dict, None]:
        user_text = task.user_text or task.description
        conversation_id, detected_language, lang_turns = await self._pipeline_resolve_conversation_and_language(task)

        span_collector = task.span_collector
        t0_request = time.perf_counter()  # Wall-clock start for filler threshold
        t0_request_utc = datetime.now(UTC)  # Absolute UTC for span timestamp overrides

        # 0. Cache replay / routing skip (before classify)
        compound_bypass = False
        action_replay = None
        routing_skip = None
        if self._is_background_turn(task):
            result = await self._handle_background_turn(task)
            final_chunk = {
                "token": "",
                "done": True,
                "conversation_id": conversation_id,
                "mediated_speech": strip_markdown(result.get("speech", "")),
            }
            if result.get("error"):
                final_chunk["error"] = result["error"]
            yield final_chunk
            return
        if self._cache_manager:
            if await self._get_bool_setting("cache.compound_utterance_bypass", True) and looks_compound(user_text):
                compound_bypass = True
                logger.debug("Skipping cache lookup for structurally compound utterance: %r", user_text[:80])
            else:
                action_replay, routing_skip = await self._try_cache_replay(
                    task=task,
                    user_text=user_text,
                    language=detected_language,
                    span_collector=span_collector,
                )
        if action_replay is not None:
            replay = await self._finalize_action_replay_hit(
                action_replay,
                conversation_id,
                user_text,
                span_collector,
                task=task,
            )
            yield {
                "token": replay["speech"],
                "done": True,
                "conversation_id": conversation_id,
                "mediated_speech": replay["speech"],
                "sanitized": False,
            }
            return

        # 1. Classify (non-streaming -- fast via Groq)
        classify_extra: dict[str, object] = {}
        used_origin_context = False
        if compound_bypass:
            classify_extra["compound_bypass"] = True
            classify_extra["compound_bypass_reason"] = "multi_sentence"
        try:
            if routing_skip is not None:
                classifications = self._build_synthetic_classifications(routing_skip)
                routing_cached = True
                target_agent, condensed_task, confidence = classifications[0]
                async with _optional_span(span_collector, "classify", agent_id="orchestrator") as span:
                    self._pipeline_record_classify_span(
                        span,
                        classifications,
                        user_text,
                        condensed_task,
                        confidence,
                        routing_cached,
                        extended_metadata=False,
                        extra_metadata={**classify_extra, "reason": "routing_cache_skip"} if classify_extra else {"reason": "routing_cache_skip"},
                    )
            else:
                async with _optional_span(span_collector, "classify", agent_id="orchestrator") as span:
                    classifications, routing_cached = await self._classify(
                        user_text,
                        cache_result=None,
                        conversation_id=conversation_id,
                        span_collector=span_collector,
                        language=detected_language,
                        allow_cache_lookup=False,
                    )
                    target_agent, condensed_task, confidence = classifications[0]
                    self._pipeline_record_classify_span(
                        span,
                        classifications,
                        user_text,
                        condensed_task,
                        confidence,
                        routing_cached,
                        extended_metadata=False,
                        extra_metadata=classify_extra or None,
                    )
        except _RecoverableClassificationError as exc:
            yield {
                "token": "",
                "done": True,
                "conversation_id": conversation_id,
                "mediated_speech": strip_markdown(exc.message),
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "recoverable": True,
                },
            }
            return
        # P3-10: per-request streaming routing log; debug.
        logger.debug(
            "Stream routed to %s: %s (conversation=%s)",
            target_agent,
            condensed_task[:80],
            conversation_id,
        )

        if len(classifications) == 1 and target_agent == _CANCEL_INTERACTION_AGENT:
            full_speech = await generate_cancel_speech(detected_language, user_text)
            latency_ms = (time.perf_counter() - t0_request) * 1000
            await track_request(_CANCEL_INTERACTION_AGENT, cache_hit=False, latency_ms=latency_ms)
            async with _optional_span(span_collector, "return", agent_id="orchestrator") as ret_span:
                ret_span["metadata"]["from_agent"] = target_agent
                ret_span["metadata"]["agent_response"] = full_speech
                full_speech, vf_eff = await self._merge_voice_followup_and_organic(
                    full_speech,
                    agent_requested=False,
                    ctx=task.context,
                    language=detected_language,
                    has_error=False,
                )
                ret_span["metadata"]["final_response"] = full_speech[:500]
                ret_span["metadata"]["mediated"] = False
                ret_span["metadata"]["voice_followup"] = vf_eff
                ret_span["metadata"]["cache_stored_response"] = False
                ret_span["metadata"]["cache_stored_routing"] = False
                await self._store_turn(conversation_id, user_text, full_speech, agent_id=target_agent)
                if span_collector:
                    clf = [(target_agent, condensed_task, confidence)]
                    await self._create_trace(
                        span_collector,
                        conversation_id,
                        user_text,
                        full_speech,
                        target_agent,
                        confidence,
                        condensed_task,
                        clf,
                        lang_turns,
                        task_context=task.context,
                    )
            mediated_text = strip_markdown(full_speech)
            final_chunk = {
                "token": "",
                "done": True,
                "conversation_id": conversation_id,
                "mediated_speech": mediated_text,
            }
            if vf_eff:
                final_chunk["voice_followup"] = True
            self._schedule_ha_voice_followup_if_requested(task, vf_eff)
            yield final_chunk
            return

        # Multi-agent: yield progress marker, then fall back to non-streaming handle_task
        is_sequential_send = any(a == "send-agent" for a, _, _ in classifications) and any(
            a != "send-agent" for a, _, _ in classifications
        )

        # Sequential send: fall back to non-streaming, with filler support
        if is_sequential_send:
            yield {
                "token": "",
                "done": False,
                "conversation_id": conversation_id,
                "status": "sequential_send",
            }

            # Determine which content agent to check for filler
            content_agent_ids = [a for a, _, _ in classifications if a != "send-agent"]
            content_agent_for_filler = content_agent_ids[0] if content_agent_ids else None
            seq_use_filler = (
                await self._should_send_filler(content_agent_for_filler) if content_agent_for_filler else False
            )
            language = detected_language

            seq_filler_sent = False
            seq_filler_text = ""
            seq_filler_start_ms = 0.0
            seq_filler_end_ms = 0.0
            seq_filler_generated = False
            seq_filler_send_ms = 0.0
            seq_filler_threshold_ms = 1000

            if seq_use_filler:
                seq_filler_threshold_ms = await self._get_filler_threshold_ms()
                # Race handle_task against filler threshold
                task_coro = self.handle_task(task, _pre_classified=(classifications, routing_cached))
                task_future = asyncio.ensure_future(task_coro)

                elapsed = time.perf_counter() - t0_request
                remaining = max(0, seq_filler_threshold_ms / 1000 - elapsed)

                done_set, _ = await asyncio.wait({task_future}, timeout=remaining)
                if done_set:
                    # handle_task completed before threshold -- no filler needed
                    result = task_future.result()
                else:
                    # Threshold exceeded -- generate filler
                    seq_filler_start_ms = (time.perf_counter() - t0_request) * 1000
                    filler_text = await self._invoke_filler_agent(
                        user_text,
                        content_agent_for_filler,
                        language,
                    )
                    seq_filler_end_ms = (time.perf_counter() - t0_request) * 1000

                    if filler_text and not task_future.done():
                        seq_filler_generated = True
                        seq_filler_text = filler_text
                        seq_filler_send_ms = (time.perf_counter() - t0_request) * 1000
                        yield {
                            "token": filler_text,
                            "done": False,
                            "is_filler": True,
                            "conversation_id": conversation_id,
                        }
                        seq_filler_sent = True
                    elif filler_text:
                        seq_filler_generated = True
                        seq_filler_text = filler_text

                    result = await task_future
            else:
                result = await self.handle_task(task, _pre_classified=(classifications, routing_cached))

            # Record filler_generate span
            if seq_filler_generated:
                async with _optional_span(span_collector, "filler_generate", agent_id="filler-agent") as fg_span:
                    fg_span["metadata"]["threshold_ms"] = seq_filler_threshold_ms
                    fg_span["metadata"]["target_agent"] = content_agent_for_filler
                    fg_span["metadata"]["filler_text"] = seq_filler_text
                    fg_span["metadata"]["sequential_send"] = True
                    fg_span["metadata"]["was_sent"] = seq_filler_sent
                    if seq_filler_start_ms > 0:
                        actual_start = t0_request_utc + timedelta(milliseconds=seq_filler_start_ms)
                        fg_span["start_time"] = actual_start.isoformat()
                        fg_span["_override_duration_ms"] = round(
                            seq_filler_end_ms - seq_filler_start_ms,
                            2,
                        )

            # Record filler_send span
            if seq_filler_sent:
                async with _optional_span(span_collector, "filler_send", agent_id="filler-agent") as fs_span:
                    fs_span["metadata"]["target_agent"] = content_agent_for_filler
                    fs_span["metadata"]["filler_text"] = seq_filler_text
                    fs_span["metadata"]["sequential_send"] = True
                    if seq_filler_send_ms > 0:
                        actual_start = t0_request_utc + timedelta(milliseconds=seq_filler_send_ms)
                        fs_span["start_time"] = actual_start.isoformat()
                        fs_span["_override_duration_ms"] = 0

            seq_final = {
                "token": result["speech"],
                "done": True,
                "conversation_id": conversation_id,
                "mediated_speech": result["speech"],
            }
            if result.get("voice_followup"):
                seq_final["voice_followup"] = True
            yield seq_final
            return

        if len(classifications) > 1:
            yield {
                "token": "",
                "done": False,
                "conversation_id": conversation_id,
                "status": "multi_agent",
                "agents": [a for a, _, _ in classifications],
            }
            result = await self.handle_task(task, _pre_classified=(classifications, routing_cached))
            multi_final = {
                "token": result["speech"],
                "done": True,
                "conversation_id": conversation_id,
                "mediated_speech": result["speech"],
            }
            if result.get("voice_followup"):
                multi_final["voice_followup"] = True
            yield multi_final
            return

        # 2. Build context and task (single agent streaming)
        turns = await self._get_turns(conversation_id)
        language = detected_language
        context = TaskContext(conversation_turns=turns, language=language)
        if task.context:
            context.device_id = task.context.device_id
            context.area_id = task.context.area_id
            context.device_name = task.context.device_name
            context.area_name = task.context.area_name
            context.source = task.context.source
            context.native_plain_timer_eligible = task.context.native_plain_timer_eligible
            context.injection_detected = task.context.injection_detected
        if self._ha_client:
            try:
                from zoneinfo import ZoneInfo

                home_ctx = await home_context_provider.get(self._ha_client)
                context.timezone = home_ctx.timezone
                context.location_name = home_ctx.location_name
                try:
                    tz = ZoneInfo(home_ctx.timezone)
                    now = datetime.now(tz)
                    context.local_time = now.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    context.local_time = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
            except Exception:
                logger.debug("Failed to populate home context for streaming", exc_info=True)
        verbatim_terms = self._extract_verbatim_terms(user_text)
        agent_task = AgentTask(
            description=self._append_original_suffix(condensed_task, verbatim_terms),
            user_text=user_text,
            conversation_id=conversation_id,
            context=context,
            verbatim_terms=verbatim_terms,
        )

        # 3. Dispatch via A2A message/stream
        request = JsonRpcRequest(
            method="message/stream",
            params={
                "agent_id": target_agent,
                "task": agent_task.model_dump(),
                "_span_collector": span_collector,
            },
            id=conversation_id or "orchestrator-stream",
        )

        t0_dispatch = time.perf_counter()
        collected_speech = []
        action_executed = None
        stream_error = None
        stream_voice_followup = False
        stream_directive = None
        stream_reason = None
        use_filler = await self._should_send_filler(target_agent)
        filler_threshold_ms = await self._get_filler_threshold_ms() if use_filler else 1000
        filler_sent = False
        filler_text_sent = ""
        filler_start_ms = 0.0
        filler_end_ms = 0.0
        filler_generated = False
        filler_send_ms = 0.0
        # P3-10: per-request filler-decision log; debug.
        logger.debug("Filler decision for %s: use_filler=%s", target_agent, use_filler)

        async def _process_chunk(chunk):
            """Process a single stream chunk: collect speech and detect actions."""
            nonlocal action_executed, stream_error, stream_voice_followup, stream_directive, stream_reason
            token = chunk.result.get("token", "")
            done = chunk.result.get("done", False)
            error = chunk.result.get("error")
            if error:
                logger.warning("Agent streaming error: %s", error)
                stream_error = error
            if token:
                collected_speech.append(token)
            if done and chunk.result.get("action_executed"):
                action_executed = chunk.result["action_executed"]
            if done and chunk.result.get("voice_followup"):
                stream_voice_followup = True
            if done and chunk.result.get("directive"):
                stream_directive = chunk.result["directive"]
                stream_reason = chunk.result.get("reason")
            return token

        async def _stream_with_filler(stream_iter, span=None):
            """Race the first agent token against the filler threshold.

            Uses an asyncio.Queue to decouple the async generator reader
            from the consumer, so cancellation on timeout does not corrupt
            the generator state.
            """
            nonlocal filler_sent, filler_text_sent, filler_start_ms, filler_end_ms, filler_generated, filler_send_ms

            if not use_filler:
                # No filler logic -- stream directly
                async for chunk in stream_iter:
                    await _process_chunk(chunk)
                return

            # Queue-based approach: reader task fills queue, main loop consumes
            queue: asyncio.Queue = asyncio.Queue()
            _sentinel = object()

            async def _reader():
                try:
                    async for chunk in stream_iter:
                        await queue.put(chunk)
                finally:
                    await queue.put(_sentinel)

            reader_task = asyncio.create_task(_reader())

            try:
                # Wait for first chunk or threshold (accounting for time already spent on classify)
                first_chunk = None
                elapsed_since_request = time.perf_counter() - t0_request
                remaining_threshold = max(0, filler_threshold_ms / 1000 - elapsed_since_request)
                # P3-10: per-request filler timing detail; debug.
                logger.debug(
                    "Filler remaining threshold: %.1fms (elapsed %.0fms)",
                    remaining_threshold * 1000,
                    elapsed_since_request * 1000,
                )
                try:
                    item = await asyncio.wait_for(
                        queue.get(),
                        timeout=remaining_threshold,
                    )
                    logger.debug("First chunk arrived before threshold")
                    if item is not _sentinel:
                        first_chunk = item
                except TimeoutError:
                    # Agent is slow -- generate and yield filler
                    logger.debug("Threshold exceeded, generating filler for %s", target_agent)
                    filler_start_ms = (time.perf_counter() - t0_request) * 1000
                    filler_text = await self._invoke_filler_agent(user_text, target_agent, language)
                    filler_end_ms = (time.perf_counter() - t0_request) * 1000
                    logger.debug("Filler generation result: %s", repr(filler_text[:80]) if filler_text else "None")
                    pre_first_chunk = None
                    if filler_text:
                        filler_generated = True
                        filler_text_sent = filler_text
                        # FLOW-MED-3: atomic probe for an already-queued
                        # chunk. ``queue.empty()`` is a racy snapshot:
                        # a chunk can be put between the check and
                        # the ``yield`` that sends the filler. Use
                        # ``get_nowait`` which either atomically pops
                        # the head or raises :class:`QueueEmpty` in
                        # one step, eliminating the race.
                        try:
                            pre_first_chunk = queue.get_nowait()
                            logger.debug("Agent responded during filler generation, skipping filler")
                        except asyncio.QueueEmpty:
                            pre_first_chunk = None

                        if pre_first_chunk is None:
                            filler_send_ms = (time.perf_counter() - t0_request) * 1000
                            yield {
                                "token": filler_text,
                                "done": False,
                                "is_filler": True,
                                "conversation_id": None,
                            }
                            filler_sent = True
                            logger.debug("Filler sent for %s: %s", target_agent, filler_text[:80])

                    if pre_first_chunk is not None:
                        item = pre_first_chunk
                    else:
                        item = await queue.get()
                    if item is not _sentinel:
                        first_chunk = item

                # Process first chunk
                if first_chunk is not None:
                    await _process_chunk(first_chunk)

                # Drain remaining chunks from queue
                while True:
                    item = await queue.get()
                    if item is _sentinel:
                        break
                    await _process_chunk(item)
            finally:
                await reader_task

        async with _optional_span(span_collector, "dispatch", agent_id=target_agent) as span:
            async for token_dict in _stream_with_filler(self._dispatcher.dispatch_stream(request), span):
                yield token_dict
            span["metadata"]["token_count"] = len(collected_speech)
            span["metadata"]["agent_response"] = "".join(collected_speech)[:500]
            if filler_sent:
                span["metadata"]["filler_sent"] = True
            span["metadata"]["non_filler_tokens_buffered_until_terminal"] = True

        latency_ms = (time.perf_counter() - t0_dispatch) * 1000
        await track_request(target_agent, cache_hit=False, latency_ms=latency_ms)

        # Record filler_generate span (always, if filler was generated -- even if not sent)
        if filler_generated:
            async with _optional_span(span_collector, "filler_generate", agent_id="filler-agent") as fg_span:
                fg_span["metadata"]["threshold_ms"] = filler_threshold_ms
                fg_span["metadata"]["target_agent"] = target_agent
                fg_span["metadata"]["filler_text"] = filler_text_sent
                fg_span["metadata"]["was_sent"] = filler_sent
                if filler_start_ms > 0:
                    actual_start = t0_request_utc + timedelta(milliseconds=filler_start_ms)
                    fg_span["start_time"] = actual_start.isoformat()
                    fg_span["_override_duration_ms"] = round(filler_end_ms - filler_start_ms, 2)

        # Record filler_send span (only if filler was actually yielded to user)
        if filler_sent:
            async with _optional_span(span_collector, "filler_send", agent_id="filler-agent") as fs_span:
                fs_span["metadata"]["target_agent"] = target_agent
                fs_span["metadata"]["filler_text"] = filler_text_sent
                if filler_send_ms > 0:
                    actual_start = t0_request_utc + timedelta(milliseconds=filler_send_ms)
                    fs_span["start_time"] = actual_start.isoformat()
                    fs_span["_override_duration_ms"] = 0

        if stream_directive:
            final_chunk = {
                "token": "",
                "done": True,
                "conversation_id": conversation_id,
                "directive": stream_directive,
            }
            if stream_reason is not None:
                final_chunk["reason"] = stream_reason
            yield final_chunk
            return

        # 4. Store conversation turn and create trace summary
        full_speech = "".join(collected_speech)
        if stream_error is not None and target_agent == _FALLBACK_AGENT:
            if not full_speech.strip():
                full_speech = _CANNED_GENERAL_ERROR_SPEECH
            # For the fallback general-agent path, return a single user-facing
            # response instead of surfacing a transport-level stream error.
            stream_error = None
        has_error = stream_error is not None
        full_speech, vf_eff = await self._finalize_single_agent_response(
            task=task,
            user_text=user_text,
            target_agent=target_agent,
            confidence=confidence,
            condensed_task=condensed_task,
            speech=full_speech,
            action_executed=action_executed,
            has_error=has_error,
            span_collector=span_collector,
            conversation_id=conversation_id,
            language=language,
            turns=turns,
            classifications=[(target_agent, condensed_task, confidence)],
            voice_followup_requested=stream_voice_followup,
            routed_to=target_agent,
            mediation_agent=target_agent,
            skip_mediation_on_error=False,
            used_origin_context=used_origin_context,
        )

        # Yield final done chunk with mediated_speech (always included)
        mediated_text = strip_markdown(full_speech)
        final_chunk = {
            "token": "",
            "done": True,
            "conversation_id": conversation_id,
            "mediated_speech": mediated_text,
        }
        if stream_error:
            final_chunk["error"] = stream_error
        if vf_eff:
            final_chunk["voice_followup"] = True
        self._schedule_ha_voice_followup_if_requested(task, vf_eff)
        yield final_chunk

    async def _should_send_filler(self, target_agent: str) -> bool:
        """Check if filler is enabled and the target agent is expected to be slow."""
        try:
            val = await SettingsRepository.get_value("filler.enabled", "false")
            enabled = val.lower() == "true"
        except (ValueError, TypeError):
            enabled = False
        if not enabled:
            return False
        if not self._registry:
            return False
        cards = await self._registry.list_agents()
        for card in cards:
            if card.agent_id == target_agent:
                return card.expected_latency == "high"
        return False

    async def _get_filler_threshold_ms(self) -> int:
        """Read filler threshold from DB (live, not cached)."""
        try:
            val = await SettingsRepository.get_value("filler.threshold_ms", "1000")
            return int(val)
        except (ValueError, TypeError):
            return 1000

    async def _invoke_filler_agent(self, user_text: str, target_agent: str, language: str) -> str | None:
        """Call the filler-agent directly to generate a filler phrase.

        Returns the filler text or None if generation fails.
        """
        if not self._filler_agent:
            return None
        try:
            context = TaskContext(language=language)
            filler_task = AgentTask(
                description=f"generate_filler:{target_agent}",
                user_text=user_text,
                context=context,
            )
            result = await self._filler_agent.handle_task(filler_task)
            speech = result.speech if hasattr(result, "speech") else (result or {}).get("speech", "")
            return speech.strip() if speech else None
        except Exception:
            logger.warning("Filler agent invocation failed", exc_info=True)
            return None

    async def _execute_cached_action(self, cached_action) -> dict | None:
        """Execute a cached action via HA client. Returns action result or None.

        FLOW-CRIT-2 / FLOW-VERIFY-2:
        HA's REST ``call_service`` returns the list of states it observed
        changing. For async-bus aktors (KNX, ABB, Zigbee2MQTT…) the
        ``state_changed`` event fires *after* the REST call returns, so
        ``call_service`` responds with ``[]`` even on a successful
        command. The previous implementation treated an empty response
        as a silent no-op and fell through to live dispatch -- meaning
        every repeated action on a slow bus still ran the full agent
        pipeline, defeating the response cache.

        We now mirror the live ``action_executor.execute_action`` path:
        register a WebSocket state waiter *before* the REST call via
        ``ha_client.expect_state``. When the REST response is empty we
        consult the observer; if it saw the entity reach the expected
        target state (or any state change, for toggles), the replay is
        confirmed and we return the observed state. Only a true timeout
        + no REST evidence counts as failure.
        """
        if not self._ha_client or not cached_action:
            return None

        service = cached_action.service or ""
        if "/" in service:
            domain, action = service.split("/", 1)
        else:
            domain, action = service, ""
        entity_id = cached_action.entity_id or ""
        service_data = cached_action.service_data or None

        if not domain or not action or not entity_id:
            return None

        # Import here to avoid a circular import at module load time
        # (action_executor -> orchestrator via analytics/spans).
        from app.agents.action_executor import (
            _EXPECTED_STATE_BY_ACTION,
            _extract_state_from_call_result,
            call_service_with_verification,
        )

        expected_state = _EXPECTED_STATE_BY_ACTION.get(action)

        # FLOW-VERIFY-SHARED (0.18.5): delegate the REST+WS dance to the
        # shared helper so the orchestrator and domain executors agree on
        # how empty REST responses are treated.
        verify = await call_service_with_verification(
            self._ha_client,
            domain,
            action,
            entity_id,
            service_data=service_data,
            expected_state=expected_state,
        )
        if not verify["success"]:
            logger.warning(
                "Cached action execution failed",
                exc_info=verify.get("error") is not None,
            )
            return None

        call_result = verify["call_result"]
        if call_result is None:
            return None

        # Non-empty REST response is authoritative: success.
        non_empty = bool(call_result) and not (isinstance(call_result, (list, dict)) and len(call_result) == 0)
        if non_empty:
            return {
                "success": True,
                "entity_id": entity_id,
                "action": action,
                "state": _extract_state_from_call_result(
                    call_result,
                    entity_id,
                ),
                "source": "call_service",
            }

        # Empty REST response -- consult the WS / poll observer. For
        # async-bus aktors this is the common path.
        observed_state = verify["observed_state"]
        if expected_state:
            # Targeted action (turn_on/turn_off/set_*): require the
            # observed state to match the intent. A stale mismatch
            # (observed=off after turn_on) means HA accepted but the
            # bus did not follow through; fall through to live
            # dispatch so the user gets a truthful response.
            if observed_state == expected_state:
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "action": action,
                    "state": observed_state,
                    "source": "ws_observer",
                }
            logger.info(
                "Cached action %s on %s: empty REST, observer saw %r (expected %r); falling through to live dispatch",
                service,
                entity_id,
                observed_state,
                expected_state,
            )
            return None

        # Untargeted actions (toggle and similar): any observed state
        # change after the call counts as confirmation.
        if observed_state is not None:
            return {
                "success": True,
                "entity_id": entity_id,
                "action": action,
                "state": observed_state,
                "source": "ws_observer",
            }

        logger.info(
            "Cached action %s on %s: empty REST, no observer evidence; falling through to live dispatch",
            service,
            entity_id,
        )
        return None

    @staticmethod
    def _cancel_interaction_description_line() -> str:
        return (
            "- cancel-interaction: User dismisses or aborts ONLY the current voice/chat turn "
            "(nevermind, forget it, scratch that, no thanks, stop as in stop talking, "
            "German e.g. abbrechen/egal/schon gut when meaning dismiss—not device control). "
            "NOT for canceling timers, alarms, or media playback—route those to timer-agent, "
            "music-agent, etc."
        )

    @staticmethod
    def _is_background_turn(task: AgentTask) -> bool:
        ctx = task.context
        return bool(ctx and ctx.source == "background" and ctx.background_event is not None)

    async def _handle_background_turn(self, task: AgentTask) -> dict:
        ctx = task.context
        event = ctx.background_event if ctx else None
        if event is None:
            return {
                "speech": "",
                "error": {
                    "code": "parse_error",
                    "message": "Missing background event payload.",
                    "recoverable": True,
                },
            }
        from app.a2a.orchestrator_gateway import OrchestratorGateway
        from app.agents.background_actions import handle_background_event

        return await handle_background_event(
            event,
            context=ctx,
            ha_client=self._ha_client,
            entity_index=self._entity_index,
            gateway=OrchestratorGateway(self._dispatcher),
        )

    async def _repair_send_agent_classifications(
        self,
        user_text: str,
        *,
        conversation_id: str | None,
        span_collector=None,
        language: str = "en",
    ) -> list[tuple[str, str, float | None]]:
        system_prompt_template = await self._load_prompt_async("orchestrator")
        agent_descriptions = await self._build_agent_descriptions()
        lang = (language or "").strip().lower()
        if lang and lang != "en":
            language_hint = (
                f"User language hint: the user message is in '{lang}'. "
                "Entity, room, device, and location names in the user input are "
                "ALREADY in the user's language and MUST be copied verbatim into "
                "the condensed task. Do not translate them to English."
            )
        else:
            language_hint = ""
        system_prompt = system_prompt_template.replace("{agent_descriptions}", agent_descriptions).replace(
            "{language_hint}",
            language_hint,
        )
        system_prompt += (
            "\n\nHard routing rules:\n"
            "- send-agent is a delivery-only second step and is NEVER valid on its own.\n"
            "- If delivery is requested, return exactly one non-send content agent first and send-agent second.\n"
            "- Never return orchestrator, filler-agent, or rewrite-agent."
        )
        messages = [{"role": "system", "content": system_prompt}]
        turns = await self._get_turns(conversation_id)
        if turns:
            self._append_conversation_turn_messages(messages, turns, max_content_length=300)
        messages.append(
            {
                "role": "user",
                "content": (
                    "Repair the routing result for this request. "
                    "If the user wants content delivered somewhere, return content-agent first and send-agent second.\n\n"
                    f"Request:\n{self._wrap_user_input(user_text)}"
                ),
            }
        )
        async with _optional_span(span_collector, "llm_call", agent_id="orchestrator") as llm_span:
            response = await self._call_llm(messages, span_collector=span_collector)
            llm_span["metadata"]["model"] = "orchestrator_repair"
            llm_span["metadata"]["routing_cached"] = False
        return await self._parse_classification(response, user_text)

    async def _sanitize_or_repair_classifications(
        self,
        classifications: list[tuple[str, str, float | None]],
        *,
        user_text: str,
        conversation_id: str | None,
        span_collector=None,
        language: str = "en",
        allow_repair: bool = True,
        require_send_partner: bool = False,
    ) -> list[tuple[str, str, float | None]]:
        filtered = [c for c in classifications if c[0] not in _INTERNAL_ONLY_AGENTS]
        if not filtered:
            raise _RecoverableClassificationError("I couldn't determine the right agent for that request.")

        send_entries = [c for c in filtered if c[0] == "send-agent"]
        content_entries = [c for c in filtered if c[0] != "send-agent"]

        if not send_entries:
            if require_send_partner:
                raise _RecoverableClassificationError("I couldn't determine what content to deliver.")
            return filtered

        if content_entries:
            return content_entries + send_entries

        if allow_repair:
            repaired = await self._repair_send_agent_classifications(
                user_text,
                conversation_id=conversation_id,
                span_collector=span_collector,
                language=language,
            )
            return await self._sanitize_or_repair_classifications(
                repaired,
                user_text=user_text,
                conversation_id=conversation_id,
                span_collector=span_collector,
                language=language,
                allow_repair=False,
                require_send_partner=True,
            )

        raise _RecoverableClassificationError("I couldn't determine what content to deliver.")

    async def _build_agent_descriptions(self) -> str:
        """Build agent list for classification prompt from registered AgentCards."""
        cancel_line = self._cancel_interaction_description_line()
        if not self._registry:
            return "- general-agent: fallback for general questions and unroutable requests\n" + cancel_line

        cards = await self._registry.list_agents()
        lines = []
        for card in cards:
            if card.agent_id in _INTERNAL_ONLY_AGENTS:
                continue
            skills_str = ", ".join(card.skills) if card.skills else ""
            if skills_str:
                lines.append(f"- {card.agent_id}: {card.description} (skills: {skills_str})")
            else:
                lines.append(f"- {card.agent_id}: {card.description}")
        if not lines:
            lines.append("- general-agent: fallback for general questions and unroutable requests")
        return "\n".join(lines) + "\n" + cancel_line

    async def _classify(
        self,
        user_text: str,
        *,
        cache_result=None,
        conversation_id: str | None = None,
        span_collector=None,
        language: str = "en",
        allow_cache_lookup: bool = True,
    ) -> tuple[list[tuple[str, str, float | None]], bool]:
        """Classify user intent and produce a condensed task.

        The condensed task is a clear, actionable English description of
        what the agent should do. All entity/device/room/location names
        from the user's original text are preserved EXACTLY (verbatim,
        never translated or normalized).

        Args:
            user_text: The raw user input.
            cache_result: Optional pre-computed CacheResult from handle_task.

        Returns:
            (classifications, routing_cached) where classifications is a list
            of (target_agent_id, condensed_task, confidence) tuples.
        """
        # Use pre-computed cache result if available (avoids double lookup)
        if cache_result is not None:
            if cache_result.hit_type == "routing_hit" and cache_result.agent_id:
                if cache_result.agent_id == "send-agent" or cache_result.agent_id in _INTERNAL_ONLY_AGENTS:
                    logger.debug(
                        "Ignoring invalid routing cache hit: %s for '%s'", cache_result.agent_id, user_text[:80]
                    )
                else:
                    # P3-10: per-request cache-hit log; debug.
                    logger.debug("Routing cache hit: %s for '%s'", cache_result.agent_id, user_text[:80])
                    condensed = user_text
                    return [(cache_result.agent_id, condensed, 1.0)], True
        elif allow_cache_lookup and self._cache_manager:
            # Fallback: no pre-computed result (e.g. called without handle_task)
            try:
                cache_result = await self._cache_manager.process(
                    user_text,
                    language=language,
                )
                if cache_result.hit_type == "routing_hit" and cache_result.agent_id:
                    if cache_result.agent_id == "send-agent" or cache_result.agent_id in _INTERNAL_ONLY_AGENTS:
                        logger.debug(
                            "Ignoring invalid routing cache hit: %s for '%s'",
                            cache_result.agent_id,
                            user_text[:80],
                        )
                    else:
                        # P3-10: per-request cache-hit log; debug.
                        logger.debug("Routing cache hit: %s for '%s'", cache_result.agent_id, user_text[:80])
                        condensed = user_text
                        return [(cache_result.agent_id, condensed, 1.0)], True
            except Exception:
                logger.warning("Routing cache check failed, proceeding with LLM", exc_info=True)

        system_prompt_template = await self._load_prompt_async("orchestrator")
        agent_descriptions = await self._build_agent_descriptions()
        lang = (language or "").strip().lower()
        if lang and lang != "en":
            language_hint = (
                f"User language hint: the user message is in '{lang}'. "
                "Entity, room, device, and location names in the user input are "
                "ALREADY in the user's language and MUST be copied verbatim into "
                "the condensed task. Do not translate them to English."
            )
        else:
            language_hint = ""
        system_prompt = system_prompt_template.replace("{agent_descriptions}", agent_descriptions).replace(
            "{language_hint}", language_hint
        )
        if "send-agent" not in agent_descriptions:
            system_prompt = self._strip_seq_rule(system_prompt)
        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # Inject recent conversation history as proper multi-turn messages
        turns = await self._get_turns(conversation_id)
        if turns:
            self._append_conversation_turn_messages(messages, turns, max_content_length=300)
        messages.append({"role": "user", "content": self._wrap_user_input(user_text)})

        try:
            async with _optional_span(span_collector, "llm_call", agent_id="orchestrator") as llm_span:
                response = await self._call_llm(messages, span_collector=span_collector)
                llm_span["metadata"]["model"] = "orchestrator"
                llm_span["metadata"]["routing_cached"] = False
            logger.debug("Classification LLM response for '%s': %s", user_text[:60], repr(response[:300]))
            classifications = await self._parse_classification(response, user_text)
            classifications = await self._sanitize_or_repair_classifications(
                classifications,
                user_text=user_text,
                conversation_id=conversation_id,
                span_collector=span_collector,
                language=language,
            )
            if self._cache_manager and self._legacy_pipeline_enabled() and len(classifications) == 1:
                target_agent, condensed, confidence = classifications[0]
                if target_agent not in (_FALLBACK_AGENT, _CANCEL_INTERACTION_AGENT, "send-agent"):
                    if confidence is not None:
                        try:
                            await self._cache_manager.store_routing_async(
                                user_text,
                                target_agent,
                                confidence,
                                condensed,
                                language=language,
                            )
                        except Exception:
                            logger.warning("Failed to store routing decision", exc_info=True)
            return classifications, False
        except _RecoverableClassificationError:
            raise
        except Exception:
            logger.exception("Intent classification failed, falling back to %s", _FALLBACK_AGENT)
            return [(_FALLBACK_AGENT, user_text, 0.0)], False

    async def _parse_classification(self, response: str, original_text: str) -> list[tuple[str, str, float | None]]:
        """Parse LLM classification response (single or multi-line).

        Expected format per line: "<agent-id> (<confidence>%): <condensed task>"
        Falls back to old format: "<agent-id>: <condensed task>"
        Falls back to general-agent if parsing fails.

        P1-4: lines without an explicit ``(<nn>%)`` confidence yield
        ``None`` so downstream gating can distinguish "the model told us
        85%" from "the model did not tell us anything and we guessed".
        The previous 0.8 default poisoned the routing cache with
        synthetic confidence that was then exposed in traces as if the
        LLM had produced it.

        Returns a list of ``(agent_id, condensed_task, confidence)``
        tuples; ``confidence`` is ``None`` when the model did not supply
        one.
        """
        response = response.strip()
        known_agents = await self._get_known_agents()
        results: list[tuple[str, str, float | None]] = []

        # Build the embedded-fragment matcher once per call. Sorting by
        # length first prevents shorter agent ids from matching inside
        # longer ones (``agent`` vs ``agent-light``).
        if known_agents:
            agent_alt = "|".join(re.escape(a) for a in sorted(known_agents, key=len, reverse=True))
            # No ``\b`` anchor: the malformed LLM output we target glues the
            # next agent id directly onto the previous task text without any
            # separator (``"...temperatureclimate-agent (96%): ..."``), so a
            # leading word boundary would never fire. Sorting by length
            # already prevents shorter ids from matching inside longer ones.
            fragment_re: re.Pattern[str] | None = re.compile(
                rf"({agent_alt})\s*(?:\(\s*\d+\s*%?\s*\))?\s*:\s*",
                re.IGNORECASE,
            )
        else:
            fragment_re = None

        lines = [line.strip() for line in response.split("\n") if line.strip()]
        for line in lines:
            # FLOW-LOW-3: tolerate ``[SEQ]`` prefixes that carry leading
            # whitespace after a list marker or a normalization pass. The
            # old ``startswith`` check fired only on the unindented form;
            # a model that emitted ``"  [SEQ] kitchen, turn on"`` would
            # slip through as a regular classification line. ``lstrip``
            # + ``removeprefix`` is idempotent when the prefix is absent.
            line = line.lstrip()
            line = line.removeprefix("[SEQ]").strip()
            # Try new format: "agent-id (85%): task text"
            confidence: float | None
            match = re.match(r"^([\w-]+)\s*\((\d+)%?\)\s*:\s*(.+)$", line, re.DOTALL)
            if match:
                agent_id = match.group(1).strip().lower()
                confidence = min(float(match.group(2)) / 100.0, 1.0)
                condensed = match.group(3).strip()
                condensed = _sanitize_condensed(condensed, fragment_re, line)
            else:
                # Fallback to old format: "agent-id: task text"
                if ":" not in line:
                    logger.warning("Could not parse classification line: %s", line[:100])
                    continue
                agent_id, _, condensed = line.partition(":")
                agent_id = agent_id.strip().lower()
                condensed = condensed.strip()
                condensed = _sanitize_condensed(condensed, fragment_re, line)
                # P1-4: leave confidence unset so callers can decide
                # whether to persist this routing decision.
                confidence = None

            if agent_id not in known_agents:
                logger.warning("Unknown agent '%s' in classification, skipping line", agent_id)
                continue

            if not condensed:
                condensed = original_text

            results.append((agent_id, condensed, confidence))

        if not results:
            return [(_FALLBACK_AGENT, original_text, 0.0)]

        # Deduplicate by agent_id: keep higher confidence, merge tasks
        seen: dict[str, tuple[str, float | None, list[str]]] = {}
        for agent_id, condensed, confidence in results:
            if agent_id in seen:
                existing_condensed, existing_conf, tasks = seen[agent_id]
                # Treat unknown confidence as 0 for the ordering decision
                # but preserve the original None in the stored tuple.
                existing_cmp = existing_conf if existing_conf is not None else -1.0
                current_cmp = confidence if confidence is not None else -1.0
                if current_cmp > existing_cmp:
                    tasks.append(existing_condensed)
                    seen[agent_id] = (condensed, confidence, tasks)
                else:
                    tasks.append(condensed)
            else:
                seen[agent_id] = (condensed, confidence, [])

        deduped: list[tuple[str, str, float | None]] = []
        for agent_id, (condensed, confidence, extra_tasks) in seen.items():
            if extra_tasks:
                condensed = condensed + " ; " + " ; ".join(extra_tasks)
            deduped.append((agent_id, condensed, confidence))

        # Sort by confidence desc (None treated as lowest), cap at 3
        deduped.sort(key=lambda x: x[2] if x[2] is not None else -1.0, reverse=True)
        return deduped[:3]

    async def _get_conversation_context_turn_limit(self) -> int:
        fallback = _DEFAULT_CONVERSATION_CONTEXT_TURNS
        try:
            raw_value = await SettingsRepository.get_value(
                "general.conversation_context_turns",
                str(fallback),
            )
            parsed = int(str(raw_value).strip())
        except Exception:
            logger.debug(
                "Failed to read general.conversation_context_turns; using default %d",
                fallback,
                exc_info=True,
            )
            return fallback
        return max(
            _MIN_CONVERSATION_CONTEXT_TURNS,
            min(_MAX_CONVERSATION_CONTEXT_TURNS, parsed),
        )

    async def _get_turns(self, conversation_id: str | None) -> list[dict]:
        """Get recent conversation turns for context.

        FLOW-MED-7: on in-memory miss, fall back to the DB so
        multi-worker deployments and post-restart replays still see
        conversation context. The result is cached back into
        ``_conversations`` so subsequent calls stay in-memory.
        """
        if not conversation_id:
            return []
        turn_limit = await self._get_conversation_context_turn_limit()
        max_messages = turn_limit * 2
        entry = self._conversations.get(conversation_id)
        if entry is not None:
            ts, turns = entry
            if time.monotonic() - ts <= _CONVERSATION_TTL_SECONDS:
                trimmed_turns = list(turns[-max_messages:]) if len(turns) > max_messages else list(turns)
                if len(trimmed_turns) != len(turns):
                    self._conversations[conversation_id] = (ts, trimmed_turns)
                return trimmed_turns
            self._conversations.pop(conversation_id, None)

        try:
            rows = await ConversationRepository.get_by_conversation_id(
                conversation_id,
            )
        except Exception:
            logger.debug(
                "DB fallback for conversation turns failed for %s",
                conversation_id,
                exc_info=True,
            )
            return []

        if not rows:
            return []

        turns: list[dict] = []
        for row in rows[-turn_limit:]:
            user_text = row.get("user_text") or ""
            if user_text:
                turns.append({"role": "user", "content": user_text})
            resp_text = row.get("response_text") or ""
            if resp_text:
                assistant_turn: dict = {"role": "assistant", "content": resp_text}
                agent_id = row.get("agent_id")
                if agent_id:
                    assistant_turn["agent_id"] = agent_id
                turns.append(assistant_turn)

        if turns:
            self._conversations[conversation_id] = (time.monotonic(), turns)
            self._evict_stale_conversations()
        return list(turns)

    async def _store_turn(
        self, conversation_id: str | None, user_text: str, assistant_text: str, agent_id: str | None = None
    ) -> None:
        """Store a conversation turn, keeping the configured number of exchanges."""
        if not conversation_id:
            return
        turn_limit = await self._get_conversation_context_turn_limit()
        self._evict_stale_conversations()
        now = time.monotonic()
        if conversation_id in self._conversations:
            self._conversations.move_to_end(conversation_id)
            _, turns = self._conversations[conversation_id]
        else:
            turns = []
        turns.append({"role": "user", "content": user_text})
        assistant_turn = {"role": "assistant", "content": assistant_text}
        if agent_id:
            assistant_turn["agent_id"] = agent_id
        turns.append(assistant_turn)
        max_messages = turn_limit * 2
        if len(turns) > max_messages:
            turns = turns[-max_messages:]
        self._conversations[conversation_id] = (now, turns)

        # Persist to DB for admin/analytics visibility
        try:
            await ConversationRepository.insert(
                conversation_id=conversation_id,
                user_text=user_text,
                agent_id=agent_id,
                response_text=assistant_text,
            )
        except Exception:
            logger.warning("Failed to persist conversation turn to DB", exc_info=True)

    def _evict_stale_conversations(self) -> None:
        """Remove conversations older than TTL and enforce max count."""
        now = time.monotonic()
        while self._conversations:
            oldest_key = next(iter(self._conversations))
            ts, _ = self._conversations[oldest_key]
            if now - ts > _CONVERSATION_TTL_SECONDS:
                self._conversations.pop(oldest_key)
            else:
                break
        while len(self._conversations) > _MAX_CONVERSATIONS:
            self._conversations.popitem(last=False)

    async def _merge_responses(
        self,
        agent_responses: list[tuple[str, str, bool]],
        user_text: str,
        span_collector=None,
    ) -> str:
        """Merge multiple agent responses into a single natural answer via LLM.

        Always calls LLM regardless of personality settings.
        Includes personality prompt if configured.
        Falls back to bracket-prefixed format on failure.
        """
        if not agent_responses:
            return "I couldn't process that request."

        # Only one response: return it directly
        if len(agent_responses) == 1:
            return agent_responses[0][1] or "I couldn't process that request."

        # Build structured summary of each agent response
        summary_parts = []
        for agent_id, speech, acted in agent_responses:
            status = "[action executed]" if acted else "[no action executed]"
            if speech and speech.strip():
                summary_parts.append(f"- {agent_id} {status}: {speech}")
            else:
                summary_parts.append(f"- {agent_id} {status}: (no response)")
        agent_summary = "\n".join(summary_parts)

        try:
            personality = ""
            with contextlib.suppress(Exception):
                personality = await SettingsRepository.get_value("personality.prompt", "")

            system_content = await self._load_prompt_async("merge")
            personality_text = personality.strip() if personality and personality.strip() else ""
            system_content = system_content.replace("{personality}", personality_text).strip()

            messages = [
                {"role": "system", "content": system_content},
                {
                    "role": "user",
                    "content": (
                        f"User asked:\n{self._wrap_user_input(user_text)}\n\n"
                        f"Agent responses:\n{agent_summary}\n\n"
                        "Combine into one natural response:"
                    ),
                },
            ]

            overrides = {
                "temperature": self._mediation_temperature,
                "max_tokens": self._mediation_max_tokens,
            }
            if self._mediation_model:
                overrides["model"] = self._mediation_model
            result = await self._call_llm(messages, span_collector=span_collector, **overrides)
            return result.strip() if result and result.strip() else self._format_fallback(agent_responses)
        except Exception:
            logger.warning("Multi-agent response merge failed, using fallback format", exc_info=True)
            return self._format_fallback(agent_responses)

    @staticmethod
    def _format_fallback(agent_responses: list[tuple[str, str, bool]]) -> str:
        """Fallback formatting when LLM merge fails."""
        parts = [f"[{aid}] {sp}" for aid, sp, _ in agent_responses if sp and sp.strip()]
        return "\n\n".join(parts) if parts else "I couldn't process that request."

    async def _mediate_response(
        self, agent_speech: str, user_text: str, agent_id: str, language: str = "en", span_collector=None
    ) -> str:
        """Optionally mediate the domain agent response with personality.

        When personality.prompt is non-empty, passes the agent speech through
        a lightweight LLM call to apply the configured personality.
        Falls back to the original speech on any failure.
        """
        try:
            personality = await SettingsRepository.get_value("personality.prompt", "")
            if not personality.strip():
                return agent_speech
        except Exception:
            return agent_speech

        if not agent_speech or not agent_speech.strip():
            return agent_speech

        try:
            system_prompt = await self._load_prompt_async("mediate")
            personality_text = personality.strip() if personality.strip() else ""
            system_prompt = system_prompt.replace("{personality}", personality_text)
            system_prompt = system_prompt.replace("{language}", language or "en").strip()
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"User asked:\n{self._wrap_user_input(user_text)}\n"
                        f"Agent ({agent_id}) responded: {agent_speech}\n\n"
                        f"Rephrase in {language}:"
                    ),
                },
            ]
            overrides = {
                "temperature": self._mediation_temperature,
                "max_tokens": self._mediation_max_tokens,
            }
            if self._mediation_model:
                overrides["model"] = self._mediation_model
            async with _optional_span(span_collector, "mediation", agent_id="orchestrator") as span:
                result = await self._call_llm(messages, span_collector=span_collector, **overrides)
                span["metadata"]["personality_active"] = True
                span["metadata"]["language"] = language or "en"
                span["metadata"]["original_length"] = len(agent_speech)
                span["metadata"]["mediated_length"] = len(result.strip()) if result else 0
            return result.strip() if result and result.strip() else agent_speech
        except Exception:
            logger.warning("Response mediation failed, using original speech", exc_info=True)
            return agent_speech

    @staticmethod
    def _strip_seq_rule(prompt: str) -> str:
        """Remove the sequential dispatch rule block when send-agent is unavailable."""
        start_marker = "Sequential dispatch rule:"
        end_marker = "Format:"
        start_idx = prompt.find(start_marker)
        end_idx = prompt.find(end_marker)
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            return prompt[:start_idx] + prompt[end_idx:]
        return prompt

    # ------------------------------------------------------------------
    # 0.23.0: language-agnostic verbatim-term extraction.
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_verbatim_terms(user_text: str) -> list[str]:
        """Pull likely entity / room tokens out of the user's original text.

        The heuristic is intentionally language-agnostic: it never
        consults a translation table. It picks up:

        - Tokens shaped like Home Assistant entity ids (``light.kitchen``).
        - Snake_case identifiers (``living_room``).
        - Quoted spans ("...", '...').
        - Mid-sentence Capitalized words (skip the leading word so that
          a normal sentence start does not get treated as a token).

        Returns an order-preserving deduped list, capped at 6 items.
        """
        if not user_text or not isinstance(user_text, str):
            return []
        text = user_text.strip()
        terms: list[str] = []
        seen: set[str] = set()

        def _add(term: str) -> None:
            term = term.strip().strip(".,;:!?\"'()[]{}")
            if not term or len(term) < 2 or len(term) > 60:
                return
            key = term.lower()
            if key in seen:
                return
            seen.add(key)
            terms.append(term)

        # Quoted spans first.
        for m in re.finditer(r'"([^"]{2,60})"|\'([^\']{2,60})\'', text):
            span = m.group(1) or m.group(2) or ""
            if span:
                _add(span)

        # HA-id shape and snake_case.
        for m in re.finditer(r"\b[a-z][a-z0-9_]*\.[a-z0-9_]+\b", text):
            _add(m.group(0))
        for m in re.finditer(r"\b[a-zA-Z]+(?:_[a-zA-Z0-9]+)+\b", text):
            _add(m.group(0))

        # Mid-sentence Capitalized words (skip first word of the text).
        words = re.findall(r"[^\s]+", text)
        for idx, w in enumerate(words):
            if idx == 0:
                continue
            cleaned = w.strip(".,;:!?\"'()[]{}")
            if not cleaned or len(cleaned) < 2:
                continue
            first = cleaned[0]
            # Match any Unicode uppercase letter (covers DE/FR/ES diacritics too).
            if first.isupper() and not cleaned.isupper() and any(c.isalpha() for c in cleaned[1:]):
                _add(cleaned)

        return terms[:6]

    @staticmethod
    def _append_original_suffix(condensed_task: str, verbatim_terms: list[str]) -> str:
        """Append ``[original: t1, t2]`` to a condensed task when terms are absent.

        Idempotent: never appends a second suffix and never duplicates a
        term that is already present (case-insensitive substring) in
        the condensed text.
        """
        if not verbatim_terms or not condensed_task:
            return condensed_task or ""
        if "[original:" in condensed_task:
            return condensed_task
        haystack = condensed_task.lower()
        missing: list[str] = []
        for term in verbatim_terms[:4]:
            if term and term.lower() not in haystack:
                missing.append(term)
        if not missing:
            return condensed_task
        return f"{condensed_task} [original: {', '.join(missing)}]"
