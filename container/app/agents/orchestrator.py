"""Orchestrator agent for intent classification and task dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from app.a2a._request import build_send_request, build_stream_request
from app.a2a.protocol import JsonRpcRequest
from app.agents.agent_registry import CachedAgentRegistry
from app.agents.base import BaseAgent
from app.agents.cache_orchestrator import CacheOrchestrator
from app.agents.cancel_speech import generate_cancel_speech
from app.agents.classification_engine import ClassificationEngine, _RecoverableClassificationError
from app.agents.conversation_manager import ConversationManager, extract_resolved_entities
from app.agents.decorator import agent
from app.agents.dispatch_manager import DispatchManager
from app.agents.filler_coordinator import FillerCoordinator
from app.agents.language_detect import detect_user_language
from app.agents.mediation import MediationService, MediationStreamError, _strip_followup_tag
from app.agents.sanitize import strip_markdown, strip_parenthetical_asides
from app.agents.task_pipeline import PipelineDirector
from app.analytics.collector import track_request, track_request_background
from app.analytics.tracer import _optional_span
from app.cache.cache_manager import ActionReplayOutcome, RoutingSkipOutcome
from app.db.repository import SettingsRepository
from app.ha_client.home_context import populate_task_context_home_context
from app.memory import get_memory_service
from app.models.agent import (
    CANCEL_INTERACTION_AGENT,
    FALLBACK_AGENT,
    AgentCard,
    BackgroundTask,
    DispatchTask,
    IngressTask,
    LastEntity,
    TaskContext,
)

logger = logging.getLogger(__name__)

_CANNED_TIMEOUT_SPEECH = "I couldn't process that request in time."
_CANNED_GENERAL_ERROR_SPEECH = "I couldn't process that request right now."

# Trailing mediation marker (prompt contract, prompts/mediate.txt): the LLM
# appends it to signal a voice follow-up. While relaying streamed mediation
# tokens, a len(_FOLLOWUP_TAG)-char holdback guarantees no part of a trailing
# marker is ever emitted to the client/TTS, even when split across tokens.
_FOLLOWUP_TAG = "[FOLLOWUP]"

_PERSONALITY_CACHE_TTL_SEC: float = 300.0


def _stringify_error(err: Any) -> str | None:
    """Normalize a chunk ``error`` value to ``str | None`` (Phase-1 contract).

    ``StreamToken.error`` is ``str | None``; early-exit and background paths
    historically attached ``{"code", "message", "recoverable"}`` dicts,
    which crashed pydantic validation inside every streaming generator.
    Dict payloads are logged at debug in full and reduced to their
    ``message`` (fallback ``code``); the structured early-exit dict stays
    available in ``prelude.early_exit`` for logs/traces.
    """
    if err is None:
        return None
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        logger.debug("Stringifying dict-shaped chunk error: %s", err)
        message = err.get("message") or err.get("code")
        return str(message) if message else "unknown error"
    return str(err)


async def _cancel_filler_future(filler_future: asyncio.Task | None) -> None:
    """Cancel a t=0 filler task that is no longer needed and retrieve its outcome.

    P1: filler generation starts at dispatch time; when the agent answers
    before the threshold (or the turn exits early) the filler task is
    cancelled here. The await after ``cancel()`` is suppression-scoped so a
    mid-flight filler dispatch unwinds cleanly, and an already-finished
    task's exception is retrieved so it never logs "exception was never
    retrieved".
    """
    if filler_future is None:
        return
    if not filler_future.done():
        filler_future.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await filler_future
    elif not filler_future.cancelled():
        filler_future.exception()


def _consume_memory_task_result(memory_task: asyncio.Task, state: dict[str, Any] | None = None) -> None:
    """Done-callback for abandoned memory searches: consume/log the outcome.

    When a trace span state is attached, mark the span as abandoned and
    resolve it so the ``memory_retrieval`` span closes as soon as the late
    search finishes.
    """
    if state is not None:
        state["metadata"].update({"matches_attached": False, "abandoned": True})
        state["resolved"].set()
    if memory_task.cancelled():
        return
    exc = memory_task.exception()
    if exc is not None:
        logger.debug("Session-memory search failed", exc_info=exc)


async def _trace_memory_retrieval(
    search_task: asyncio.Task,
    span_collector: Any,
    state: dict[str, Any],
) -> None:
    """Record the ``memory_retrieval`` span for a concurrent memory search.

    Opens when the search task starts (concurrent with ``classify``) and
    closes when ``_resolve_memory_context`` (or the abandon callback) sets
    ``state["resolved"]``. Only suffix-safe / innocuous metadata keys.
    """
    async with _optional_span(span_collector, "memory_retrieval", agent_id="orchestrator") as span:
        try:
            matches = await asyncio.shield(search_task)
        except asyncio.CancelledError:
            span["metadata"]["cancelled"] = True
            raise
        # search exceptions propagate -> start_span records status="error"
        top = matches[0] if matches else None
        span["metadata"]["match_count"] = len(matches) if matches else 0
        if top is not None:
            similarity = getattr(top, "similarity", None) if not isinstance(top, dict) else top.get("similarity")
            if similarity is not None:
                span["metadata"]["top_similarity"] = round(float(similarity), 4)
            conversation_id = (
                getattr(top, "conversation_id", None) if not isinstance(top, dict) else top.get("conversation_id")
            )
            if conversation_id:
                span["metadata"]["matched_conversation_id"] = str(conversation_id)
        await state["resolved"].wait()
        span["metadata"].update(state["metadata"])


async def _resolve_memory_context(
    task: IngressTask,
    memory_task: asyncio.Task | None,
    memory_service: Any,
    span_state: dict[str, Any] | None = None,
) -> None:
    """Resolve the session-memory prelude task into ``task.context.memory_context``.

    Wait behavior follows the ``memory.wait_mode`` setting:
    ``best_effort`` takes the result only when the search already
    finished during classification -- zero added latency; a still-pending
    task keeps running with a done-callback that consumes its outcome.
    ``blocking`` (default) waits up to ``memory.wait_timeout_ms`` and
    proceeds with ``memory_context=None`` on timeout. Failures never
    block dispatch.

    ``span_state`` (when given) feeds the ``memory_retrieval`` trace span:
    each outcome records its wait metadata and signals ``resolved``; for
    abandoned (still-pending) searches the signal is deferred to the
    done-callback so the span closes when the late search finishes.
    """
    if memory_task is None or memory_service is None:
        return
    try:
        wait_mode, wait_timeout_ms = await memory_service.wait_config()
        matches: Any = None
        if wait_mode == "blocking":
            try:
                matches = await asyncio.wait_for(asyncio.shield(memory_task), timeout=wait_timeout_ms / 1000)
            except TimeoutError:
                if not memory_task.done():
                    # Abandoned: the done-callback resolves the span state
                    # when the late search finishes.
                    memory_task.add_done_callback(lambda t: _consume_memory_task_result(t, span_state))
                    if span_state is not None:
                        span_state["metadata"].update(
                            {"wait_mode": wait_mode, "timed_out": True, "matches_attached": False}
                        )
                elif span_state is not None:
                    span_state["metadata"].update(
                        {"wait_mode": wait_mode, "timed_out": True, "matches_attached": False}
                    )
                    span_state["resolved"].set()
        elif memory_task.done():
            matches = memory_task.result()
        else:
            # Abandoned best-effort search: the done-callback resolves the
            # span state when the late search finishes.
            memory_task.add_done_callback(lambda t: _consume_memory_task_result(t, span_state))
        if span_state is not None and not span_state["resolved"].is_set() and memory_task.done():
            span_state["metadata"].update(
                {"wait_mode": wait_mode, "timed_out": False, "matches_attached": bool(matches)}
            )
            span_state["resolved"].set()
        if matches and task.context is not None:
            task.context.memory_context = [asdict(m) if is_dataclass(m) else dict(m) for m in matches]
            top = task.context.memory_context[0]
            logger.info(
                "Session memory: %d match(es) attached (top session=%s sim=%.3f)",
                len(matches),
                top.get("conversation_id", "?"),
                float(top.get("similarity") or 0.0),
            )
    except Exception:
        logger.debug("Session-memory resolution failed", exc_info=True)


@dataclass
class PipelinePreludeResult:
    """Shared prelude result for streaming and non-streaming pipelines.

    Fields are computed once and consumed by both :meth:`_handle_task_impl`
    and :meth:`_handle_task_stream_impl`. ``early_exit`` signals that the
    caller should short-circuit; its dict carries the logic result so each
    caller formats it for its own output channel.
    """

    conversation_id: str
    detected_language: str
    lang_turns: list
    span_collector: Any
    classifications: list[tuple[str, str, float | None]]
    routing_cached: bool
    target_agent: str
    condensed_task: str
    confidence: float | None
    used_origin_context: bool
    early_exit: dict[str, Any] | None = None
    # ENTITY_RESOLUTION_REWORK (R-B): entry id of the served routing-cache
    # hit, so a failed cached-agent turn can invalidate the entry at
    # finalization time. None when the turn was not routing-cached.
    routing_entry_id: str | None = None


@dataclass
class StreamingContext:
    """Encapsulates streaming-specific state for a single request.

    Fields correspond to the local variables in ``_handle_task_stream_impl``
    that track filler state, collected speech, stream errors, and progress.
    """

    filler_sent: bool = False
    filler_text_sent: str = ""
    filler_start_ms: float = 0.0
    filler_end_ms: float = 0.0
    filler_generated: bool = False
    filler_send_ms: float = 0.0
    collected_speech: list[str] = field(default_factory=list)
    stream_directive: str | None = None
    stream_reason: str | None = None
    action_executed: Any = None
    stream_error: str | None = None
    stream_voice_followup: bool = False
    # P0 first-frame latency: True once at least one agent token was relayed
    # downstream as a non-terminal chunk (mediation inactive for the turn).
    relayed_tokens: bool = False

    def __post_init__(self) -> None:
        pass

    def reset_buffer(self) -> None:
        self.collected_speech.clear()

    def append_speech(self, token: str) -> None:
        if token:
            self.collected_speech.append(token)


@agent(
    agent_id="orchestrator",
    name="Orchestrator",
    description="Routes user requests to the appropriate specialized agent.",
    skills=["intent_classification", "task_routing"],
    needs_entity_matcher=False,
    factory=lambda app, filler: OrchestratorAgent(
        dispatcher=app.state.dispatcher,
        registry=app.state.registry,
        cache_manager=getattr(app.state, "cache_manager", None),
        ha_client=getattr(app.state, "ha_client", None),
        entity_index=getattr(app.state, "entity_index", None),
        entity_matcher=getattr(app.state, "entity_matcher", None),
        filler_agent=filler,
    ),
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
        agent_registry: CachedAgentRegistry | None = None,
        event_bus=None,
        entity_matcher=None,
    ) -> None:
        super().__init__(ha_client=ha_client, entity_index=entity_index)
        self._dispatcher = dispatcher
        self._cache_manager = cache_manager
        self._filler_agent = filler_agent
        self._event_bus = event_bus
        # Unused since ENTITY_RESOLUTION_REWORK removed the orchestrator's
        # ingress matcher pass (agents do their own keyword recall). The
        # constructor param stays for factory/test compatibility.
        self._entity_matcher = entity_matcher
        self._default_timeout: int = 5
        self._max_iterations: int = 3
        self._mediation_model: str | None = None
        self._mediation_temperature: float = 0.3
        self._mediation_max_tokens: int = 2048
        self._max_dispatch_timeout: float = 60.0
        self._calendar_injector = None
        if ha_client is not None and entity_index is not None:
            from app.agents.calendar_injector import CalendarReminderInjector

            self._calendar_injector = CalendarReminderInjector(ha_client, entity_index, llm_call=self._call_llm)

        self._registry_value = registry
        self._agent_registry = agent_registry or CachedAgentRegistry(
            registry=registry,
            default_timeout=self._default_timeout,
            max_dispatch_timeout=self._max_dispatch_timeout,
        )

        # Decomposed module instances
        self._conversation_manager = ConversationManager()
        self._dispatch_manager = DispatchManager(
            dispatcher=dispatcher,
            agent_registry=self._agent_registry,
            ha_client=ha_client,
            call_llm=self._call_llm,
            load_prompt_async=self._load_prompt_async,
            resolve_dispatch_timeout=self._resolve_dispatch_timeout,
            wrap_user_input=self._wrap_user_input,
            mediation_model=self._mediation_model,
            mediation_temperature=self._mediation_temperature,
            mediation_max_tokens=self._mediation_max_tokens,
            settings_repo=SettingsRepository,
        )
        self._classification_engine = ClassificationEngine(
            agent_registry=self._agent_registry,
            cache_manager=cache_manager,
            call_llm=self._call_llm,
            load_prompt_async=self._load_prompt_async,
            get_turns=self._conversation_manager.get_turns,
            wrap_user_input=self._wrap_user_input,
            append_conversation_turn_messages=self._append_conversation_turn_messages,
            entity_index=entity_index,
        )
        self._cache_orchestrator = CacheOrchestrator(
            cache_manager=cache_manager,
            entity_index=entity_index,
            ha_client=ha_client,
            agent_registry=self._agent_registry,
            calendar_injector=self._calendar_injector,
            get_turns=self._conversation_manager.get_turns,
            store_turn=self._conversation_manager.store_turn,
            merge_voice_followup_and_organic=self._merge_voice_followup_and_organic,
            create_trace=self._create_trace,
        )
        self._pipeline_director = PipelineDirector(
            cache_manager=cache_manager,
            calendar_injector=self._calendar_injector,
            cache_orchestrator=self._cache_orchestrator,
            classification_engine=self._classification_engine,
            dispatch_manager=self._dispatch_manager,
            conversation_manager=self._conversation_manager,
            call_llm=self._call_llm,
            load_prompt_async=self._load_prompt_async,
            get_turns=self._get_turns,
            pipeline_record_classify_span=self._pipeline_record_classify_span,
            handle_sequential_send=self._handle_sequential_send,
            merge_responses=self._merge_responses,
            merge_voice_followup_and_organic=self._merge_voice_followup_and_organic,
            create_trace=self._create_trace,
            finalize_single_agent_response=self._finalize_single_agent_response,
        )

    @property
    def _mediation(self) -> MediationService:
        """Lazily-created MediationService.

        Laziness (rather than eager construction in ``__init__``) preserves the
        ``OrchestratorAgent.__new__(...)`` construction pattern used by some unit
        tests: those build a bare instance and set only the ``_mediation_*``
        attributes, then call ``_mediate_response``. An eager ``__init__``
        assignment would be absent in that case and raise ``AttributeError``.
        The service holds no own state, so creating it on first access is free.
        """
        cached = self.__dict__.get("_mediation_service")
        if cached is None:
            cached = MediationService(self)
            self.__dict__["_mediation_service"] = cached
        return cached

    @property
    def _filler_coord(self) -> FillerCoordinator:
        """Lazily-created FillerCoordinator (see :attr:`_mediation`)."""
        cached = self.__dict__.get("_filler_coord_service")
        if cached is None:
            cached = FillerCoordinator(
                settings_repo=SettingsRepository,
                agent_registry=self._agent_registry,
                dispatcher=self._dispatcher,
                dispatch_manager=self._dispatch_manager,
            )
            self.__dict__["_filler_coord_service"] = cached
        return cached

    def apply_pipeline_strategies(self, strategies: dict[str, Any]) -> None:
        """Apply strategy overrides from PluginContext to PipelineDirector.

        ``strategies`` is a dict keyed by phase name
        (``"cache_replay"``, ``"classification"``, ``"dispatch"``,
        ``"finalization"``). Values are strategy instances conforming to
        the corresponding ABC in :mod:`pipeline_strategies`.
        """
        _setters: dict[str, Any] = {
            "cache_replay": self._pipeline_director.set_cache_replay_strategy,
            "classification": self._pipeline_director.set_classification_strategy,
            "dispatch": self._pipeline_director.set_dispatch_strategy,
            "finalization": self._pipeline_director.set_finalization_strategy,
        }
        for phase, strategy in strategies.items():
            setter = _setters.get(phase)
            if setter is not None:
                setter(strategy)
            else:
                logger.warning("Unknown pipeline strategy phase: %s", phase)

    @property
    def _registry(self):
        return self._registry_value

    @_registry.setter
    def _registry(self, value):
        self._registry_value = value
        if hasattr(self, "_agent_registry") and self._agent_registry is not None:
            self._agent_registry._registry = value

    async def initialize(self) -> None:
        """Load reliability config from DB. Call during startup."""
        await self._load_reliability_config()
        await self._load_mediation_config()

    async def _load_reliability_config(self) -> None:
        """Read timeout and max_iterations from settings."""
        try:
            val = await SettingsRepository.get_value("a2a.default_timeout", "5")
            self._default_timeout = int(val) if val is not None else 5
        except (ValueError, TypeError):
            logger.debug("Invalid a2a.default_timeout value, using default", exc_info=True)
        try:
            val = await SettingsRepository.get_value("a2a.max_iterations", "3")
            self._max_iterations = int(val) if val is not None else 3
        except (ValueError, TypeError):
            logger.debug("Invalid a2a.max_iterations value, using default", exc_info=True)
        try:
            val = await SettingsRepository.get_value("a2a.max_dispatch_timeout", "60")
            self._max_dispatch_timeout = float(val) if val is not None else 60.0
        except (ValueError, TypeError):
            logger.debug("Invalid a2a.max_dispatch_timeout value, using default", exc_info=True)
        # P2-2: invalidate per-agent cache so changes to settings or
        # AgentCard.timeout_sec are picked up on the next dispatch.
        self._agent_registry.set_default_timeout(self._default_timeout)
        self._agent_registry.set_max_dispatch_timeout(self._max_dispatch_timeout)
        self._agent_registry.invalidate_caches()
        logger.info(
            "Orchestrator reliability config: timeout=%ds max_iterations=%d max_dispatch_timeout=%.1fs",
            self._default_timeout,
            self._max_iterations,
            self._max_dispatch_timeout,
        )

    async def _resolve_dispatch_timeout(self, agent_id: str) -> float:
        """Return the dispatch timeout (seconds) for ``agent_id``.

        P2-2 (FLOW-TIMEOUT-1): delegates to :class:`CachedAgentRegistry`.
        """
        return await self._agent_registry.resolve_dispatch_timeout(
            agent_id,
            default_timeout=self._default_timeout,
            settings_repo=SettingsRepository,
        )

    async def _load_mediation_config(self) -> None:
        """Read mediation/merge override params from settings."""
        try:
            val = await SettingsRepository.get_value("mediation.model", "")
            self._mediation_model = val if val else None
        except (ValueError, TypeError):
            self._mediation_model = None
        try:
            val = await SettingsRepository.get_value("mediation.temperature", "0.3")
            self._mediation_temperature = float(val) if val is not None else 0.3
        except (ValueError, TypeError):
            self._mediation_temperature = 0.3
        try:
            val = await SettingsRepository.get_value("mediation.max_tokens", "2048")
            self._mediation_max_tokens = int(val) if val is not None else 2048
        except (ValueError, TypeError):
            self._mediation_max_tokens = 2048
        logger.info(
            "Mediation config: model=%s temperature=%.1f max_tokens=%d",
            self._mediation_model or "(orchestrator default)",
            self._mediation_temperature,
            self._mediation_max_tokens,
        )

    async def _get_personality_cached(self) -> str:
        """Return cached personality prompt with 300-second TTL."""
        now_ = time.monotonic()
        cache_ts = getattr(self, "_personality_cache_ts", None)
        if cache_ts is not None and now_ - cache_ts < _PERSONALITY_CACHE_TTL_SEC:
            return getattr(self, "_personality_cache_value", "")
        try:
            personality = await SettingsRepository.get_value("personality.prompt", "")
        except asyncio.CancelledError:
            raise
        except Exception:
            personality = ""
        self._personality_cache_ts = now_
        self._personality_cache_value = personality
        return personality

    async def _get_known_agents(self) -> set[str]:
        return await self._classification_engine._get_known_agents()

    async def _resolve_language(
        self, user_text: str, context_language: str | None = None, turns: list[dict[str, Any]] | None = None
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

    async def _dispatch_fallback(
        self,
        request: JsonRpcRequest,
        target_agent: str,
        span_collector,
        reason: str,
    ) -> tuple[str, Any] | None:
        return await self._dispatch_manager.dispatch_fallback(request, target_agent, span_collector, reason)

    async def _dispatch_single(
        self,
        target_agent: str,
        condensed_task: str,
        user_text: str,
        conversation_id: str | None,
        turns: list[dict[str, Any]],
        span_collector,
        incoming_context: TaskContext | None = None,
        skip_dispatch_span: bool = False,
        *,
        resolved_language: str | None = None,
    ) -> tuple[str, str, dict[str, Any] | None]:
        return await self._dispatch_manager.dispatch_single(
            target_agent,
            condensed_task,
            user_text,
            conversation_id,
            turns,
            span_collector,
            incoming_context=incoming_context,
            skip_dispatch_span=skip_dispatch_span,
            resolved_language=resolved_language,
        )

    async def _handle_sequential_send(
        self,
        classifications: list[tuple[str, str, float | None]],
        user_text: str,
        conversation_id: str,
        turns: list[dict[str, Any]],
        span_collector,
        incoming_context,
        *,
        resolved_language: str | None = None,
    ) -> tuple[str, str, dict[str, Any] | None]:
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

        _content_result: dict[str, Any] | None = None
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
                user_id=incoming_context.user_id if incoming_context else None,
                source=incoming_context.source if incoming_context else "api",
                language=content_language,
                sequential_send=True,
                injection_detected=incoming_context.injection_detected if incoming_context else False,
                # Session memory: matches resolved by the prelude overlap task.
                memory_context=incoming_context.memory_context if incoming_context else None,
                # Phase 6: anaphora recency hints ride the content leg like
                # the rest of the conversation context.
                last_entities=list(incoming_context.last_entities) if incoming_context else [],
            )

            if self._ha_client:
                await populate_task_context_home_context(content_context, self._ha_client)
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
                span["metadata"]["agent_response"] = content_speech or ""
                span["metadata"]["condensed_task"] = content_task
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
            span["metadata"]["agent_response"] = send_speech or ""
            span["metadata"]["condensed_task"] = augmented_task

        routed_to = f"{content_agent_id}, send-agent"

        merged_result = dict(send_result) if send_result else {}
        if _content_result and _content_result.get("voice_followup"):
            merged_result["voice_followup"] = True

        return routed_to, send_speech, merged_result

    def _merge_voice_followup_and_organic(
        self,
        speech: str,
        *,
        agent_requested: bool,
        mediated_followup: bool = False,
    ) -> tuple[str, bool]:
        """Merge agent-requested and mediated followup flags."""
        return speech, bool(agent_requested or mediated_followup)

    # ------------------------------------------------------------------
    # Shared helpers to reduce duplication between handle_task / handle_task_stream
    # ------------------------------------------------------------------

    async def _try_cache_replay(
        self,
        *,
        task: IngressTask | None = None,
        user_text: str,
        language: str = "en",
        requesting_agent_id: str = "orchestrator",
        span_collector=None,
    ) -> tuple[ActionReplayOutcome | None, RoutingSkipOutcome | None]:
        return await self._cache_orchestrator.try_cache_replay(
            task=task,
            user_text=user_text,
            language=language,
            requesting_agent_id=requesting_agent_id,
            span_collector=span_collector,
            check_visibility=self._cached_action_is_still_visible,
            exec_cached_action=self._execute_cached_action,
        )

    async def _cached_action_is_still_visible(self, agent_id: str, entity_id: str) -> bool:
        return await self._cache_orchestrator.cached_action_is_still_visible(agent_id, entity_id)

    async def _finalize_action_replay_hit(
        self,
        hit: ActionReplayOutcome,
        conversation_id: str,
        user_text: str,
        span_collector,
        *,
        task: IngressTask | None = None,
    ) -> dict[str, Any]:
        return await self._cache_orchestrator.finalize_action_replay_hit(
            hit,
            conversation_id,
            user_text,
            span_collector,
            task=task,
        )

    async def _store_after_dispatch(
        self,
        *,
        user_text: str,
        language: str,
        target_agent: str,
        condensed_task: str,
        confidence: float | None,
        speech: str,
        original_response_text: str = "",
        action_executed,
        has_error: bool,
        task: IngressTask | None = None,
        merged_multi_agent: bool = False,
        used_origin_context: bool = False,
    ) -> tuple[bool, bool]:
        return await self._cache_orchestrator.store_after_dispatch(
            user_text=user_text,
            language=language,
            target_agent=target_agent,
            condensed_task=condensed_task,
            confidence=confidence,
            speech=speech,
            original_response_text=original_response_text,
            action_executed=action_executed,
            has_error=has_error,
            task=task,
            merged_multi_agent=merged_multi_agent,
            used_origin_context=used_origin_context,
        )

    async def _get_bool_setting(self, key: str, default: bool) -> bool:
        return await self._cache_orchestrator._get_bool_setting_impl(key, default)

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
        turns: list[dict[str, Any]],
        *,
        task_context: TaskContext | None = None,
        voice_followup: bool = False,
    ) -> None:
        """Create a trace summary from span data.

        FLOW-CTX-1 (0.18.6): ``task_context`` carries device/area
        identity so the trace row can record which satellite spoke.
        """
        try:
            from app.analytics.tracer import create_trace_summary

            classify_duration = None
            cache_hit_type = None
            for s in span_collector.get_spans():
                if s.get("span_name") == "classify" and classify_duration is None:
                    classify_duration = s.get("duration_ms")
                if s.get("span_name") == "cache_lookup":
                    cache_hit_type = (s.get("metadata") or {}).get("hit_type")
            agents = list({s.get("agent_id") for s in span_collector.get_spans() if s.get("agent_id")})
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
                voice_followup=voice_followup,
                cache_hit_type=cache_hit_type,
            )
        except asyncio.CancelledError:
            raise
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
        return CacheOrchestrator.legacy_pipeline_enabled()

    def _pipeline_resolve_conversation_id(self, task: IngressTask | BackgroundTask) -> tuple[str, str]:
        """Cheap prelude half: conversation_id (with uuid fallback) and the
        request/context language. No I/O.

        P3 prelude reorder: language auto-detection (langdetect) and the
        conversation-turn prefetch only run after a cache miss, so the
        action-cache-hit path skips both.
        """
        conversation_id = task.conversation_id
        if not conversation_id:
            conversation_id = str(uuid.uuid4())
            logger.debug("No conversation_id from HA, generated fallback: %s", conversation_id)
        context_language = (task.context.language if task.context else None) or "en"
        return conversation_id, context_language

    async def _pipeline_resolve_conversation_and_language(
        self, task: IngressTask | BackgroundTask
    ) -> tuple[str, str, list]:
        """Full language resolution: prefetch the conversation turns and run
        language detection (settings override > auto-detect > turns-detect >
        fallback).

        Shared prelude between :meth:`_handle_task_impl` and
        :meth:`_handle_task_stream_impl`. P3: invoked only after a cache
        miss -- the returned ``lang_turns`` are then threaded into
        ``classify`` and reused for the dispatch context, so the turn list
        is fetched exactly once per turn.
        """
        conversation_id, context_language = self._pipeline_resolve_conversation_id(task)
        if self._is_background_turn(task):
            return conversation_id, context_language, []
        # DP-4: background turns returned above; only IngressTask reaches text reads.
        task = cast(IngressTask, task)
        user_text = task.description
        lang_turns = await self._get_turns(conversation_id)
        # ENTITY_RES_REDESIGN Phase 6: attach anaphora recency hints (most
        # recent first) to the task context so every dispatch-envelope
        # build downstream can copy them onto the agent-bound TaskContext.
        if task.context is not None:
            last_entities = await self._conversation_manager.get_last_entities(conversation_id)
            task.context.last_entities = [LastEntity(**e) for e in last_entities]
        detected_language = await self._resolve_language(user_text, context_language, turns=lang_turns)
        return conversation_id, detected_language, lang_turns

    async def _explicit_cache_language(self, context_language: str) -> str:
        """Language for the pre-detection cache-replay lookup (P3).

        Cache keys embed the language (``make_text_id`` hashes
        ``(normalized_text, language)``). The replay lookup runs BEFORE
        auto-detection now, so it uses the explicit language: the manual
        ``language`` setting when the operator pinned one, else the
        request/context language. Entries stored under an auto-detected
        language that differs from the explicit one are unreachable by
        this lookup -- an accepted cold-cache cost in exchange for
        skipping langdetect and the turn prefetch on cache hits.
        """
        setting = ""
        try:
            setting = await SettingsRepository.get_value("language", "auto")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Degrade to the request language -- the post-miss
            # `_resolve_language` performs the authoritative read anyway.
            logger.debug("language setting read failed, using request language", exc_info=True)
        if setting and setting != "auto":
            return setting
        return context_language or "en"

    async def _run_pipeline_prelude(
        self,
        task: IngressTask | BackgroundTask,
        *,
        pre_classified: tuple[list[tuple[str, str, float | None]], bool] | None = None,
        classify_reason: str | None = None,
        allow_classify_cache_lookup: bool = False,
        extended_metadata: bool = False,
        publish_events: bool = False,
    ) -> PipelinePreludeResult:
        """Shared prelude: check background/cache, then resolve language and classify.

        Encapsulates the logic that was duplicated between
        :meth:`_handle_task_impl` and :meth:`_handle_task_stream_impl`.
        When ``early_exit`` is not ``None`` the caller must short-circuit.

        P3 prelude reorder: the cache replay runs BEFORE language
        detection / turn prefetch, so action-cache hits skip ``langdetect``
        and the turn fetch. The replay lookup uses the explicit language
        (settings override or request language -- cache keys embed the
        language, see :meth:`_explicit_cache_language`).
        """
        conversation_id, context_language = self._pipeline_resolve_conversation_id(task)
        span_collector = task.span_collector

        if self._is_background_turn(task):
            result = await self._handle_background_turn(task)
            return PipelinePreludeResult(
                conversation_id=conversation_id,
                detected_language=context_language,
                lang_turns=[],
                span_collector=span_collector,
                classifications=[],
                routing_cached=False,
                target_agent="orchestrator",
                condensed_task="",
                confidence=None,
                used_origin_context=False,
                early_exit={
                    "_exit_type": "background_turn",
                    "speech": result.get("speech", ""),
                    "routed_to": "orchestrator",
                    "action_executed": result.get("action_executed"),
                    "voice_followup": False,
                    "error": result.get("error"),
                },
            )

        # DP-4: background turns early-exited above; only IngressTask reaches text reads.
        task = cast(IngressTask, task)
        user_text = task.description

        cache_language = await self._explicit_cache_language(context_language)
        cache_replay = await self._pipeline_director.run_cache_replay(
            task,
            user_text,
            cache_language,
            span_collector,
            skip_lookup=pre_classified is not None,
        )
        if cache_replay.action_replay is not None:
            replay = await self._finalize_action_replay_hit(
                cache_replay.action_replay,
                conversation_id,
                user_text,
                span_collector,
                task=task,
            )
            return PipelinePreludeResult(
                conversation_id=conversation_id,
                detected_language=context_language,
                lang_turns=[],
                span_collector=span_collector,
                classifications=[],
                routing_cached=False,
                target_agent="",
                condensed_task="",
                confidence=None,
                used_origin_context=False,
                early_exit={
                    "_exit_type": "cache_replay",
                    **replay,
                },
            )

        # Cache miss: pay language detection and the (single) turn prefetch.
        conversation_id, detected_language, lang_turns = await self._pipeline_resolve_conversation_and_language(task)

        used_origin_context = bool(task and task.context and (task.context.area_id or task.context.device_id))

        # Routing-cache hit: classification is skipped for this agent; the
        # served entry id is threaded onto the prelude result so a FAILED
        # cached-agent turn can invalidate the entry at finalization (R-B).
        routing_skip = cache_replay.routing_skip

        # Session memory: overlap the memory search with the classification
        # LLM call on the cache-miss path (action-cache replays early-exit
        # above and never pay the embed cost -- D6). Read-only; failures and
        # slow searches never block dispatch (see _resolve_memory_context).
        # The memory_retrieval span task opens with the search and closes
        # when _resolve_memory_context (or its abandon done-callback)
        # resolves the span state. Flush boundary: an abandoned best-effort
        # search finishing after the collector flushed produces no span
        # (silently dropped), by design.
        memory_task: asyncio.Task | None = None
        memory_span_state: dict[str, Any] | None = None
        memory_span_task: asyncio.Task | None = None
        memory_service = get_memory_service()
        if memory_service is not None and await memory_service.is_enabled():
            memory_task = asyncio.create_task(
                memory_service.search(
                    user_text,
                    task.context.user_id if task.context else None,
                    current_conversation_id=conversation_id,
                )
            )
            memory_span_state = {"resolved": asyncio.Event(), "metadata": {}}
            memory_span_task = asyncio.create_task(
                _trace_memory_retrieval(memory_task, span_collector, memory_span_state)
            )

        if publish_events and self._event_bus is not None:
            await self._event_bus.publish(
                "pipeline.pre_classify", {"task": task, "user_text": user_text, "language": detected_language}
            )

        try:
            (
                classifications,
                routing_cached,
                target_agent,
                condensed_task,
                confidence,
            ) = await self._pipeline_director.run_classification(
                task,
                user_text,
                detected_language,
                span_collector,
                pre_classified=pre_classified,
                routing_skip=routing_skip,
                compound_bypass=cache_replay.compound_bypass,
                extended_metadata=extended_metadata,
                classify_reason=classify_reason,
                allow_classify_cache_lookup=allow_classify_cache_lookup,
                prefetched_turns=lang_turns,
            )
        except _RecoverableClassificationError as exc:
            # Never abandon the detached memory overlap task: read-only,
            # safe to cancel.
            # Cancelling it makes the span task's shield await raise
            # CancelledError, closing the memory_retrieval span with
            # cancelled=True; the span task then finishes on its own.
            if memory_task is not None and not memory_task.done():
                memory_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await memory_task
            # Defensive: consume the span task too (normally it already
            # finished when memory_task unwound).
            if memory_span_task is not None and not memory_span_task.done():
                memory_span_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await memory_span_task
            return PipelinePreludeResult(
                conversation_id=conversation_id,
                detected_language=detected_language,
                lang_turns=lang_turns,
                span_collector=span_collector,
                classifications=[],
                routing_cached=False,
                target_agent="orchestrator",
                condensed_task="",
                confidence=None,
                used_origin_context=used_origin_context,
                early_exit={
                    "_exit_type": "classification_error",
                    "speech": exc.message,
                    "routed_to": "orchestrator",
                    "action_executed": None,
                    "voice_followup": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "recoverable": True,
                    },
                },
            )

        await _resolve_memory_context(task, memory_task, memory_service, span_state=memory_span_state)

        logger.debug(
            "Routed to %s (%s): %s (conversation=%s)",
            target_agent,
            f"{confidence * 100:.0f}%" if confidence is not None else "unknown",
            condensed_task[:80],
            conversation_id,
        )

        if publish_events and self._event_bus is not None:
            await self._event_bus.publish(
                "pipeline.post_classify",
                {
                    "task": task,
                    "classifications": classifications,
                    "target_agent": target_agent,
                    "condensed_task": condensed_task,
                    "confidence": confidence,
                },
            )

        return PipelinePreludeResult(
            conversation_id=conversation_id,
            detected_language=detected_language,
            lang_turns=lang_turns,
            span_collector=span_collector,
            classifications=classifications,
            routing_cached=routing_cached,
            target_agent=target_agent,
            condensed_task=condensed_task,
            confidence=confidence,
            used_origin_context=used_origin_context,
            routing_entry_id=(routing_skip.entry_id or None) if routing_skip is not None else None,
        )

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
        extra_metadata: dict[str, Any] | None = None,
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
        span["metadata"]["user_input"] = user_text
        span["metadata"]["condensed_task"] = condensed_task
        span["metadata"]["confidence"] = confidence
        span["metadata"]["routing_cached"] = routing_cached
        span["metadata"]["multi_agent"] = len(classifications) > 1
        if extended_metadata and len(classifications) > 1:
            span["metadata"]["all_classifications"] = {
                a: {"task": t[:300], "confidence": c} for a, t, c in classifications
            }
        if extra_metadata:
            span["metadata"].update(extra_metadata)

    async def _prepare_mediation_inputs(
        self,
        task: IngressTask,
        has_error: bool,
        language: str,
    ) -> tuple[str | None, bool]:
        """Return (reminder_text, allow_organic_followup)."""
        reminder_text: str | None = None
        if self._calendar_injector is not None and not has_error:
            try:
                reminder_text = await self._calendar_injector.inject_reminders(
                    utterance=task.description,
                    device_id=task.context.device_id if task.context else None,
                    area_id=task.context.area_id if task.context else None,
                    user_id=task.context.user_id if task.context else None,
                    language=(task.context.language if task.context else language) or language,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Calendar reminder injection failed", exc_info=True)

        allow_organic_followup = False
        if task.context and task.context.source == "ha" and not has_error:
            try:
                enabled_raw = await SettingsRepository.get_value("orchestrator.organic_followup_enabled", "false")
                if (enabled_raw or "false").lower() == "true":
                    raw_p = await SettingsRepository.get_value("orchestrator.organic_followup_probability", "0.08")
                    p = float(raw_p or "0.08")
                    allow_organic_followup = random.random() < p
            except (TypeError, ValueError):
                pass

        return reminder_text, allow_organic_followup

    async def _finalize_post_mediation(
        self,
        *,
        task: IngressTask,
        user_text: str,
        target_agent: str,
        confidence: float | None,
        condensed_task: str,
        mediated_speech: str,
        original_speech: str,
        action_executed,
        has_error: bool,
        span_collector,
        conversation_id: str,
        language: str,
        turns: list,
        classifications: list[tuple[str, str, float | None]],
        voice_followup_requested: bool,
        mediated_followup: bool = False,
        routed_to: str | None = None,
        skip_response_cache: bool = False,
        used_origin_context: bool = False,
        routing_entry_id: str | None = None,
        ret_span: dict | None = None,
    ) -> tuple[str, bool]:
        """Run post-mediation finalization: merge voice followup, store cache/turn/trace."""
        if routed_to is None:
            routed_to = target_agent
        cache_stored_action = False
        cache_stored_routing = False
        cache_stored_response = False

        speech, voice_followup_effective = self._merge_voice_followup_and_organic(
            mediated_speech,
            agent_requested=voice_followup_requested,
            mediated_followup=mediated_followup,
        )
        if ret_span is not None:
            ret_span["metadata"]["final_response"] = speech
            ret_span["metadata"]["mediated"] = speech != original_speech
            ret_span["metadata"]["voice_followup"] = voice_followup_effective
        # R-B (ENTITY_RESOLUTION_REWORK): the served routing-cache entry sent
        # this turn to the cached agent; when that turn failed (no action or
        # a failed action) the entry is poison -- invalidate it so the next
        # identical phrasing re-classifies via LLM. ``has_error`` is too
        # narrow here; the action_executed signal is the reliable one.
        # This MUST run before _store_after_dispatch: a poisoned turn may not
        # store a fresh routing row for the same phrasing, otherwise the cache
        # re-poisons itself in the very turn that invalidated the served entry
        # (observed live: misrouted command -> refusal statement -> routing
        # re-stored -> same misroute on the next attempt).
        served_entry_poisoned = False
        if routing_entry_id:
            ae = action_executed.model_dump() if hasattr(action_executed, "model_dump") else action_executed
            if not isinstance(ae, dict) or not ae.get("success"):
                served_entry_poisoned = True
                await self._cache_orchestrator.invalidate_served_routing(
                    routing_entry_id,
                    reason="cached_agent_turn_failed",
                )
                if ret_span is not None:
                    ret_span["metadata"]["routing_cache_invalidated"] = True
        if not skip_response_cache and target_agent != CANCEL_INTERACTION_AGENT and not served_entry_poisoned:
            cache_stored_action, cache_stored_routing = await self._store_after_dispatch(
                user_text=user_text,
                language=language,
                target_agent=target_agent,
                condensed_task=condensed_task,
                confidence=confidence,
                speech=speech,
                original_response_text=original_speech,
                action_executed=action_executed,
                has_error=has_error,
                task=task,
                merged_multi_agent=False,
                used_origin_context=used_origin_context,
            )
            cache_stored_response = cache_stored_action
        if ret_span is not None:
            ret_span["metadata"]["cache_stored_action"] = cache_stored_action
            ret_span["metadata"]["cache_stored_response"] = cache_stored_response
            ret_span["metadata"]["cache_stored_routing"] = cache_stored_routing
        # ENTITY_RES_REDESIGN Phase 6: remember the acted-on entity (success
        # path only) as an anaphora recency hint for later turns.
        resolved_entities = await extract_resolved_entities(action_executed, getattr(self, "_entity_index", None))
        await self._store_turn(
            conversation_id,
            user_text,
            speech,
            agent_id=routed_to,
            resolved_entities=resolved_entities,
            user_id=task.context.user_id if task.context else None,
            language=language,
            source=task.context.source if task.context else None,
        )
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
                voice_followup=voice_followup_effective,
            )
        return speech, voice_followup_effective

    async def _finalize_single_agent_response(
        self,
        *,
        task: IngressTask,
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
        routing_entry_id: str | None = None,
        mediation_inputs: tuple[str | None, bool] | None = None,
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

        Mediation runs whenever the configured personality is set OR a
        calendar reminder applies, so personality is applied to ALL
        system responses (including deterministic executor
        confirmations). ``mediation_inputs`` lets the streaming caller
        pass its pre-dispatch probe result (has_error already applied)
        instead of re-querying the calendar injector.
        """
        if routed_to is None:
            routed_to = target_agent
        if mediation_agent is None:
            mediation_agent = target_agent
        original_speech = speech
        async with _optional_span(span_collector, "return", agent_id="orchestrator") as ret_span:
            ret_span["metadata"]["from_agent"] = routed_to
            ret_span["metadata"]["agent_response"] = speech

            if mediation_inputs is not None:
                # Precomputed by the caller (streaming path probes the
                # reminder before dispatch so the token-relay decision can
                # be made at the first chunk); has_error already applied.
                reminder_text, allow_organic_followup = mediation_inputs
            else:
                reminder_text, allow_organic_followup = await self._prepare_mediation_inputs(
                    task, has_error=has_error, language=language
                )

            # Mediation runs whenever a personality is configured OR a
            # reminder must be woven in -- personality applies to every
            # system response again (deterministic executor confirmations
            # included).
            personality = await self._get_personality_cached()
            should_mediate = (
                target_agent != CANCEL_INTERACTION_AGENT
                and (not has_error or not skip_mediation_on_error)
                and (bool(personality.strip()) or bool(reminder_text))
            )
            mediated_followup = False
            if should_mediate:
                speech, mediated_followup = await self._mediate_response(
                    speech,
                    user_text,
                    mediation_agent,
                    language=language,
                    span_collector=span_collector,
                    reminder_text=reminder_text,
                    allow_organic_followup=allow_organic_followup,
                )
            elif reminder_text:
                # No mediation path -- append reminder directly as fallback
                separator = " " if speech and speech[-1] in ".!?" else ". "
                speech = f"{speech}{separator}{reminder_text}" if speech else reminder_text

            return await self._finalize_post_mediation(
                task=task,
                user_text=user_text,
                target_agent=target_agent,
                confidence=confidence,
                condensed_task=condensed_task,
                mediated_speech=speech,
                original_speech=original_speech,
                action_executed=action_executed,
                has_error=has_error,
                span_collector=span_collector,
                conversation_id=conversation_id,
                language=language,
                turns=turns,
                classifications=classifications,
                voice_followup_requested=voice_followup_requested,
                mediated_followup=mediated_followup,
                routed_to=routed_to,
                skip_response_cache=skip_response_cache,
                used_origin_context=used_origin_context,
                routing_entry_id=routing_entry_id,
                ret_span=ret_span,
            )

    async def _run_pipeline(
        self,
        task: IngressTask | BackgroundTask,
        *,
        streaming: bool,
        _pre_classified: tuple[list[tuple[str, str, float | None]], bool] | None = None,
        _classify_reason: str | None = None,
        _allow_classify_cache_lookup: bool | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
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

    async def handle_task(  # type: ignore[override]  # FLOW_REDEF DP-1: ingress boundary accepts IngressTask | BackgroundTask
        self,
        task: IngressTask | BackgroundTask,
        *,
        _pre_classified: tuple[list[tuple[str, str, float | None]], bool] | None = None,
        _classify_reason: str | None = None,
        _allow_classify_cache_lookup: bool | None = None,
    ) -> dict[str, Any]:
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
        final: dict[str, Any] | None = None
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

    def handle_task_stream(self, task: IngressTask | BackgroundTask) -> AsyncGenerator[dict[str, Any], None]:  # type: ignore[override]  # FLOW_REDEF DP-1: ingress boundary accepts IngressTask | BackgroundTask
        """Public streaming entry point.

        Returns the unified pipeline iterator directly. Honors
        ``ORCHESTRATOR_LEGACY_PIPELINE=1`` for emergency rollback.
        """
        if self._legacy_pipeline_enabled():
            return self._handle_task_stream_impl(task)
        return self._run_pipeline(task, streaming=True)

    async def _handle_task_impl(
        self,
        task: IngressTask | BackgroundTask,
        *,
        _pre_classified: tuple[list[tuple[str, str, float | None]], bool] | None = None,
        _classify_reason: str | None = None,
        _allow_classify_cache_lookup: bool | None = None,
    ) -> dict[str, Any]:
        """Thin wrapper around the shared TaskPipeline phases."""
        prelude = await self._run_pipeline_prelude(
            task,
            pre_classified=_pre_classified,
            classify_reason=_classify_reason,
            allow_classify_cache_lookup=_allow_classify_cache_lookup
            if _allow_classify_cache_lookup is not None
            else False,
            extended_metadata=True,
            publish_events=self._event_bus is not None,
        )
        if prelude.early_exit is not None:
            response = dict(prelude.early_exit)
            response.pop("_exit_type", None)
            response["conversation_id"] = prelude.conversation_id
            return response
        # DP-4: background turns early-exited in the prelude; only IngressTask reaches text reads.
        task = cast(IngressTask, task)
        user_text = task.description

        conversation_id = prelude.conversation_id
        detected_language = prelude.detected_language
        span_collector = prelude.span_collector
        classifications = prelude.classifications
        _routing_cached = prelude.routing_cached
        target_agent = prelude.target_agent
        condensed_task = prelude.condensed_task
        confidence = prelude.confidence
        used_origin_context = prelude.used_origin_context

        # Phase 2: dispatch
        # P3: reuse the prelude's turn snapshot instead of a second fetch --
        # nothing stores a turn between the prelude and dispatch.
        turns = list(prelude.lang_turns)
        if self._event_bus is not None:
            await self._event_bus.publish(
                "pipeline.pre_dispatch",
                {"task": task, "classifications": classifications, "target_agent": target_agent},
            )
        dispatch_result = await self._pipeline_director.run_dispatch(
            task,
            classifications,
            user_text,
            conversation_id,
            turns,
            span_collector,
            detected_language,
            task.context,
        )
        if self._event_bus is not None:
            await self._event_bus.publish(
                "pipeline.post_dispatch",
                {"task": task, "dispatch_result": dispatch_result},
            )

        if dispatch_result.directive:
            # M-12: directive turns are real turns -- persist the turn and
            # trace before returning (no cache store).
            resolved_entities = await extract_resolved_entities(
                dispatch_result.action_executed, getattr(self, "_entity_index", None)
            )
            await self._store_turn(
                conversation_id,
                user_text,
                dispatch_result.speech,
                agent_id=dispatch_result.routed_to,
                resolved_entities=resolved_entities,
            )
            if span_collector:
                await self._create_trace(
                    span_collector,
                    conversation_id,
                    user_text,
                    dispatch_result.speech,
                    dispatch_result.target_agent,
                    confidence,
                    condensed_task,
                    classifications,
                    turns,
                    task_context=task.context,
                    voice_followup=False,
                )
            return {
                "speech": dispatch_result.speech,
                "conversation_id": conversation_id,
                "routed_to": dispatch_result.routed_to,
                "action_executed": None,
                "voice_followup": False,
                "directive": dispatch_result.directive,
                "reason": dispatch_result.directive_reason,
            }

        # Phase 3: finalization
        response = await self._pipeline_director.run_finalization(
            task,
            dispatch_result,
            user_text,
            detected_language,
            conversation_id,
            turns,
            span_collector,
            classifications,
            dispatch_result.agent_voice_followup,
            used_origin_context,
            confidence=confidence,
            condensed_task=condensed_task,
            routing_entry_id=prelude.routing_entry_id,
        )
        response["conversation_id"] = conversation_id
        return response

    async def _handle_task_stream_impl(
        self, task: IngressTask | BackgroundTask
    ) -> AsyncGenerator[dict[str, Any], None]:
        span_collector = task.span_collector
        t0_request = time.perf_counter()
        t0_request_utc = datetime.now(UTC)

        prelude = await self._run_pipeline_prelude(
            task,
            extended_metadata=False,
            publish_events=False,
        )
        if prelude.early_exit is not None:
            ee = prelude.early_exit
            exit_type = ee.get("_exit_type", "")
            if exit_type == "classification_error":
                yield {
                    "token": "",
                    "done": True,
                    "conversation_id": prelude.conversation_id,
                    "mediated_speech": strip_markdown(ee.get("speech", "")),
                    "error": _stringify_error(ee["error"]),
                }
            elif exit_type == "background_turn":
                final_chunk: dict[str, Any] = {
                    "token": "",
                    "done": True,
                    "conversation_id": prelude.conversation_id,
                    "mediated_speech": strip_markdown(ee.get("speech", "")),
                    "routed_to": ee.get("routed_to", "orchestrator"),
                    "sanitized": True,
                }
                if ee.get("action_executed"):
                    final_chunk["action_executed"] = ee["action_executed"]
                if ee.get("error"):
                    final_chunk["error"] = _stringify_error(ee["error"])
                yield final_chunk
            else:
                # cache_replay exit: forward routing/action metadata so the
                # streaming done frame carries the same bridge fields as the
                # single-agent path (M-7).
                final_chunk = {
                    "token": ee["speech"],
                    "done": True,
                    "conversation_id": prelude.conversation_id,
                    "mediated_speech": ee["speech"],
                    "routed_to": ee.get("routed_to", "orchestrator"),
                    "sanitized": True,
                }
                if ee.get("action_executed"):
                    final_chunk["action_executed"] = ee["action_executed"]
                if ee.get("voice_followup"):
                    final_chunk["voice_followup"] = True
                yield final_chunk
            return

        # DP-4: background turns early-exited in the prelude; only IngressTask reaches text reads.
        task = cast(IngressTask, task)
        user_text = task.description
        conversation_id = prelude.conversation_id
        detected_language = prelude.detected_language
        lang_turns = prelude.lang_turns
        span_collector = prelude.span_collector
        classifications = prelude.classifications
        routing_cached = prelude.routing_cached
        target_agent = prelude.target_agent
        condensed_task = prelude.condensed_task
        confidence = prelude.confidence
        used_origin_context = prelude.used_origin_context

        if len(classifications) == 1 and target_agent == CANCEL_INTERACTION_AGENT:
            async with _optional_span(span_collector, "dispatch", agent_id=CANCEL_INTERACTION_AGENT) as span:
                full_speech = await generate_cancel_speech(detected_language, user_text)
                latency_ms = (time.perf_counter() - t0_request) * 1000
                span["metadata"]["latency_ms"] = latency_ms
                await track_request(CANCEL_INTERACTION_AGENT, cache_hit=False, latency_ms=latency_ms)
            async with _optional_span(span_collector, "return", agent_id="orchestrator") as ret_span:
                ret_span["metadata"]["from_agent"] = target_agent
                ret_span["metadata"]["agent_response"] = full_speech
                full_speech, vf_eff = self._merge_voice_followup_and_organic(
                    full_speech,
                    agent_requested=False,
                    mediated_followup=False,
                )
                ret_span["metadata"]["final_response"] = full_speech
                ret_span["metadata"]["mediated"] = False
                ret_span["metadata"]["voice_followup"] = vf_eff
                ret_span["metadata"]["cache_stored_response"] = False
                ret_span["metadata"]["cache_stored_routing"] = False
                await self._store_turn(conversation_id, user_text, full_speech, agent_id=target_agent)
                if span_collector:
                    clf = classifications
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
                        voice_followup=vf_eff,
                    )
            mediated_text = strip_markdown(full_speech)
            final_chunk = {
                "token": "",
                "done": True,
                "conversation_id": conversation_id,
                "mediated_speech": mediated_text,
                "routed_to": target_agent,
                "sanitized": True,
            }
            if vf_eff:
                final_chunk["voice_followup"] = True
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
                task_future = asyncio.create_task(task_coro)
                # P1: kick off filler generation at dispatch time (t=0) so a
                # slow agent hears the filler at ~threshold instead of
                # threshold + filler-LLM latency. Cancelled when the agent
                # answers before the threshold fires.
                filler_future = asyncio.create_task(
                    self._invoke_filler_agent(user_text, content_agent_for_filler or "", language)
                )
                try:
                    elapsed = time.perf_counter() - t0_request
                    remaining = max(0, seq_filler_threshold_ms / 1000 - elapsed)

                    done_set, _ = await asyncio.wait({task_future}, timeout=remaining)
                    if done_set:
                        # handle_task completed before threshold -- no filler needed
                        result = task_future.result()
                        await _cancel_filler_future(filler_future)
                    else:
                        # Threshold exceeded -- filler generation already runs
                        # since t=0; await its (usually finished) result.
                        seq_filler_start_ms = (time.perf_counter() - t0_request) * 1000
                        filler_text = await filler_future
                        seq_filler_end_ms = (time.perf_counter() - t0_request) * 1000

                        if filler_text and not task_future.done():
                            seq_filler_generated = True
                            seq_filler_text = filler_text
                            seq_filler_send_ms = (time.perf_counter() - t0_request) * 1000
                            yield {
                                "filler_push": filler_text,
                                "done": False,
                                "conversation_id": conversation_id,
                            }
                            seq_filler_sent = True
                        elif filler_text:
                            seq_filler_generated = True
                            seq_filler_text = filler_text

                        result = await task_future
                except BaseException:
                    # Never abandon the detached futures: cancel them and
                    # await them so their exceptions are retrieved before the
                    # original failure/cancellation propagates.
                    await _cancel_filler_future(filler_future)
                    if not task_future.done():
                        task_future.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task_future
                    elif not task_future.cancelled():
                        task_future.exception()
                    raise
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
            # M-8: sequential-send terminal chunks carry the bridge metadata.
            if result.get("routed_to"):
                seq_final["routed_to"] = result["routed_to"]
            if result.get("action_executed"):
                seq_final["action_executed"] = result["action_executed"]
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
            # M-8: multi-agent terminal chunks carry the bridge metadata.
            if result.get("routed_to"):
                multi_final["routed_to"] = result["routed_to"]
            if result.get("action_executed"):
                multi_final["action_executed"] = result["action_executed"]
            if result.get("voice_followup"):
                multi_final["voice_followup"] = True
            yield multi_final
            return

        # 2. Build context and task (single agent streaming)
        # P3: reuse the prelude's turn snapshot instead of a second fetch.
        turns = list(lang_turns)
        language = detected_language
        context = TaskContext(conversation_turns=turns, language=language)
        if task.context:
            context.device_id = task.context.device_id
            context.area_id = task.context.area_id
            context.device_name = task.context.device_name
            context.area_name = task.context.area_name
            context.user_id = task.context.user_id
            context.source = task.context.source
            context.injection_detected = task.context.injection_detected
            # Session memory: matches resolved by the prelude overlap task.
            context.memory_context = task.context.memory_context
            # Phase 6: anaphora recency hints populated in the prelude.
            context.last_entities = list(task.context.last_entities)
        if self._ha_client:
            await populate_task_context_home_context(context, self._ha_client)

        # First-frame latency: resolve the personality up front (TTL-cached,
        # cheap) and probe the mediation inputs (calendar reminder + organic
        # followup roll) concurrently with the dispatch. The probe result
        # decides -- when the first agent chunk arrives -- whether agent
        # tokens relay straight through (mediation inactive: no personality
        # and no reminder) or stay buffered for the mediation LLM.
        personality = await self._get_personality_cached()
        mediation_inputs_task = asyncio.create_task(
            self._prepare_mediation_inputs(task, has_error=False, language=language)
        )

        agent_task = DispatchTask(
            description=condensed_task,
            conversation_id=conversation_id,
            context=context,
        )

        # 3. Dispatch via A2A message/stream
        request = build_stream_request(
            target_agent,
            agent_task,
            request_id=conversation_id or "orchestrator-stream",
            span_collector=span_collector,
        )

        t0_dispatch = time.perf_counter()
        sc = StreamingContext()
        use_filler = await self._should_send_filler(target_agent)
        filler_threshold_ms = await self._get_filler_threshold_ms() if use_filler else 1000
        # P3-10: per-request filler-decision log; debug.
        logger.debug("Filler decision for %s: use_filler=%s", target_agent, use_filler)

        # P1: start filler generation at dispatch time (t=0) so a slow agent
        # hears the filler at ~threshold instead of threshold + filler-LLM
        # latency. Cancelled as soon as the agent answers before the
        # threshold (or when the turn ends without the filler being sent).
        filler_task: asyncio.Task | None = None
        if use_filler:
            filler_task = asyncio.create_task(self._invoke_filler_agent(user_text, target_agent, language))

        # Relay state for the first-frame-latency path: when mediation is
        # inactive for the turn (no personality configured, no reminder
        # pending), agent tokens are yielded straight to the client instead
        # of being buffered until the terminal frame.
        relay_state: dict[str, Any] = {
            "decided": False,
            "enabled": False,
            "reminder_text": None,
            "allow_organic_followup": False,
        }

        async def _ensure_relay_decided() -> None:
            """Await the pre-dispatch mediation probe once and latch the relay decision."""
            if relay_state["decided"]:
                return
            relay_state["decided"] = True
            try:
                reminder_text, allow_organic_followup = await mediation_inputs_task
            except asyncio.CancelledError:
                raise
            except Exception:
                # Mediation must never break the turn: degrade to no-reminder
                # (tokens relay, the reminder is dropped for this turn).
                logger.debug("Mediation inputs probe failed; assuming no reminder", exc_info=True)
                reminder_text, allow_organic_followup = None, False
            relay_state["reminder_text"] = reminder_text
            relay_state["allow_organic_followup"] = allow_organic_followup
            # Mediation runs whenever a personality is configured OR a
            # reminder must be woven in. Only a mediation-inactive turn
            # (neither applies) relays agent tokens straight through.
            relay_state["enabled"] = not (reminder_text or personality.strip())

        def _discard_mediation_inputs() -> None:
            """Release the mediation probe on early exits (timeout/directive)."""
            if not mediation_inputs_task.done():
                mediation_inputs_task.cancel()
                return
            if not mediation_inputs_task.cancelled():
                # Retrieve the outcome so a probe failure never logs
                # "exception was never retrieved" on paths that ignore it.
                mediation_inputs_task.exception()

        async def _process_chunk(chunk):
            """Process a single stream chunk: collect speech, detect actions,
            and return a relay chunk when tokens stream straight through."""
            chunk_result = chunk if isinstance(chunk, dict) else {}
            token = chunk_result.get("token", "")
            done = chunk_result.get("done", False)
            error = chunk_result.get("error")
            if error:
                logger.warning("Agent streaming error: %s", error)
                sc.stream_error = _stringify_error(error)
            if token:
                sc.append_speech(token)
            if done and chunk_result.get("action_executed"):
                sc.action_executed = chunk_result["action_executed"]
            if done and chunk_result.get("voice_followup"):
                sc.stream_voice_followup = True
            if done and chunk_result.get("directive"):
                sc.stream_directive = chunk_result["directive"]
                sc.stream_reason = chunk_result.get("reason")
            await _ensure_relay_decided()
            if not relay_state["enabled"] or not token:
                return None
            # Directive turns speak through their own terminal frame, and
            # errored turns fall back to the buffered terminal speech.
            if sc.stream_directive or sc.stream_error:
                return None
            sc.relayed_tokens = True
            return {"token": token, "done": False, "conversation_id": conversation_id}

        async def _stream_with_filler(stream_iter, span=None):
            """Race the first agent token against the filler threshold.

            Uses an asyncio.Queue to decouple the async generator reader
            from the consumer, so cancellation on timeout does not corrupt
            the generator state.
            """

            if not use_filler:
                # No filler logic -- stream directly
                async for chunk in stream_iter:
                    relay_chunk = await _process_chunk(chunk)
                    if relay_chunk is not None:
                        yield relay_chunk
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
                    # Agent answered before the threshold -- cancel the t=0
                    # filler task; its result is no longer needed.
                    await _cancel_filler_future(filler_task)
                    if item is not _sentinel:
                        first_chunk = item
                except TimeoutError:
                    # Agent is slow -- the filler task started at t=0 is
                    # usually finished by now; await its result.
                    logger.debug("Threshold exceeded, generating filler for %s", target_agent)
                    sc.filler_start_ms = (time.perf_counter() - t0_request) * 1000
                    filler_text = await filler_task if filler_task is not None else None
                    sc.filler_end_ms = (time.perf_counter() - t0_request) * 1000
                    logger.debug("Filler generation result: %s", repr(filler_text[:80]) if filler_text else "None")
                    pre_first_chunk = None
                    if filler_text:
                        sc.filler_generated = True
                        sc.filler_text_sent = filler_text
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
                            sc.filler_send_ms = (time.perf_counter() - t0_request) * 1000
                            yield {
                                "filler_push": filler_text,
                                "done": False,
                                "conversation_id": conversation_id,
                            }
                            sc.filler_sent = True
                            logger.debug("Filler sent for %s: %s", target_agent, filler_text[:80])

                    if pre_first_chunk is not None:
                        item = pre_first_chunk
                    else:
                        item = await queue.get()
                    if item is _sentinel:
                        # Sentinel consumed early; nothing more to drain
                        return
                    first_chunk = item

                # Process first chunk
                if first_chunk is not None:
                    relay_chunk = await _process_chunk(first_chunk)
                    if relay_chunk is not None:
                        yield relay_chunk

                # Drain remaining chunks from queue
                while True:
                    item = await queue.get()
                    if item is _sentinel:
                        break
                    relay_chunk = await _process_chunk(item)
                    if relay_chunk is not None:
                        yield relay_chunk
            finally:
                # The turn ended (agent answered, stream failed, or the
                # dispatch timed out) -- make sure the t=0 filler task does
                # not outlive the turn.
                await _cancel_filler_future(filler_task)
                reader_task.cancel()
                try:
                    await stream_iter.aclose()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("stream_iter.aclose() raised during cleanup", exc_info=True)
                try:
                    await asyncio.wait_for(reader_task, timeout=5.0)
                except asyncio.CancelledError:
                    pass
                except TimeoutError:
                    pass
                except Exception:
                    logger.debug("reader_task cleanup raised", exc_info=True)

        # M-11: the streaming dispatch shares the per-agent timeout budget of
        # the non-streaming path (same registry resolution as
        # ``DispatchManager.resolve_dispatch_timeout``).
        stream_dispatch_timeout = await self._dispatch_manager.resolve_dispatch_timeout(target_agent)
        async with _optional_span(span_collector, "dispatch", agent_id=target_agent) as span:
            span["metadata"]["dispatch_timeout_sec"] = stream_dispatch_timeout
            _t_stream_start = time.perf_counter()
            try:
                async with asyncio.timeout(stream_dispatch_timeout):
                    async for token_dict in _stream_with_filler(self._dispatcher.dispatch_stream(request), span):
                        yield token_dict
            except TimeoutError:
                # M-11: streaming dispatch timed out -- mirror the
                # non-streaming fallback: a non-streaming send of the same
                # task to the fallback agent, then a terminal chunk with a
                # string error.
                logger.warning(
                    "Streaming dispatch to %s timed out after %.1fs, falling back",
                    target_agent,
                    stream_dispatch_timeout,
                )
                span["metadata"]["stream_timeout"] = True
                fallback_speech = ""
                if target_agent != FALLBACK_AGENT:
                    fb_request = build_send_request(
                        FALLBACK_AGENT,
                        agent_task,
                        request_id=conversation_id or "orchestrator-stream-fallback",
                        span_collector=span_collector,
                    )
                    fb_result = await self._dispatch_fallback(
                        fb_request, target_agent, span_collector, "stream_timeout"
                    )
                    if fb_result is not None:
                        _fb_agent, fb_response = fb_result
                        fb_data = DispatchManager.normalize_agent_result(fb_response, agent_id=FALLBACK_AGENT)
                        fallback_speech = fb_data.get("speech") or ""
                if not fallback_speech:
                    fallback_speech = _CANNED_TIMEOUT_SPEECH
                _discard_mediation_inputs()
                yield {
                    "token": "",
                    "done": True,
                    "conversation_id": conversation_id,
                    "mediated_speech": fallback_speech,
                    "routed_to": FALLBACK_AGENT,
                    "error": f"Streaming dispatch to {target_agent} timed out after {stream_dispatch_timeout:.1f}s.",
                    "sanitized": True,
                }
                return
            _t_stream_end = time.perf_counter()
            logger.info(
                "dispatch_stream agent=%s stream_inner=%.1fms",
                target_agent,
                (_t_stream_end - _t_stream_start) * 1000,
            )
            span["metadata"]["token_count"] = len(sc.collected_speech)
            span["metadata"]["agent_response"] = "".join(sc.collected_speech)
            if sc.filler_sent:
                span["metadata"]["filler_sent"] = True
            if sc.relayed_tokens:
                span["metadata"]["agent_tokens_relayed"] = True
            else:
                span["metadata"]["non_filler_tokens_buffered_until_terminal"] = True

        latency_ms = (time.perf_counter() - t0_dispatch) * 1000
        track_request_background(target_agent, cache_hit=False, latency_ms=latency_ms)

        # Record filler_generate span (always, if filler was generated -- even if not sent)
        if sc.filler_generated:
            async with _optional_span(span_collector, "filler_generate", agent_id="filler-agent") as fg_span:
                fg_span["metadata"]["threshold_ms"] = filler_threshold_ms
                fg_span["metadata"]["target_agent"] = target_agent
                fg_span["metadata"]["filler_text"] = sc.filler_text_sent
                fg_span["metadata"]["was_sent"] = sc.filler_sent
                if sc.filler_start_ms > 0:
                    actual_start = t0_request_utc + timedelta(milliseconds=sc.filler_start_ms)
                    fg_span["start_time"] = actual_start.isoformat()
                    fg_span["_override_duration_ms"] = round(sc.filler_end_ms - sc.filler_start_ms, 2)

        # Record filler_send span (only if filler was actually yielded to user)
        if sc.filler_sent:
            async with _optional_span(span_collector, "filler_send", agent_id="filler-agent") as fs_span:
                fs_span["metadata"]["target_agent"] = target_agent
                fs_span["metadata"]["filler_text"] = sc.filler_text_sent
                if sc.filler_send_ms > 0:
                    actual_start = t0_request_utc + timedelta(milliseconds=sc.filler_send_ms)
                    fs_span["start_time"] = actual_start.isoformat()
                    fs_span["_override_duration_ms"] = 0

        if sc.stream_directive:
            # M-12: directive turns are real turns -- persist the turn and
            # trace before yielding the terminal chunk (no cache store).
            directive_speech = "".join(sc.collected_speech)
            resolved_entities = await extract_resolved_entities(
                sc.action_executed, getattr(self, "_entity_index", None)
            )
            await self._store_turn(
                conversation_id, user_text, directive_speech, agent_id=target_agent, resolved_entities=resolved_entities
            )
            if span_collector:
                await self._create_trace(
                    span_collector,
                    conversation_id,
                    user_text,
                    directive_speech,
                    target_agent,
                    confidence,
                    condensed_task,
                    classifications,
                    turns,
                    task_context=task.context,
                    voice_followup=False,
                )
            final_chunk = {
                "token": "",
                "done": True,
                "conversation_id": conversation_id,
                "directive": sc.stream_directive,
                "routed_to": target_agent,
            }
            if sc.stream_reason is not None:
                final_chunk["reason"] = sc.stream_reason
            _discard_mediation_inputs()
            yield final_chunk
            return

        # 4. Store conversation turn and create trace summary
        # P0: relayed agent tokens are raw LLM stream output (pre-P0 the
        # agent speech arrived pre-stripped from complete()); strip the
        # assembled speech so stored turns / cache entries stay clean.
        full_speech = "".join(sc.collected_speech).strip()
        if sc.stream_error is not None and target_agent == FALLBACK_AGENT:
            if not full_speech.strip():
                full_speech = _CANNED_GENERAL_ERROR_SPEECH
            # For the fallback general-agent path, return a single user-facing
            # response instead of surfacing a transport-level stream error.
            sc.stream_error = None
        has_error = sc.stream_error is not None

        # Check if mediation streaming is enabled (default on since the
        # first-frame-latency rework; set to "false" to opt out).
        mediation_streaming_enabled_raw = await SettingsRepository.get_value(
            "orchestrator.mediation_streaming_enabled", "true"
        )
        mediation_streaming_enabled = (mediation_streaming_enabled_raw or "true").lower() == "true"

        # Consume the pre-dispatch mediation probe (started before dispatch,
        # so it is long finished by now). Mirror
        # _prepare_mediation_inputs(has_error=True): no reminder injection
        # and no organic followup on failed turns.
        await _ensure_relay_decided()
        reminder_text = relay_state["reminder_text"]
        allow_organic_followup = relay_state["allow_organic_followup"]
        if has_error:
            reminder_text, allow_organic_followup = None, False

        # Mediation runs whenever a personality is configured OR a reminder
        # must be woven in -- personality applies to every system response
        # again (deterministic executor confirmations included). Only a
        # mediation-inactive turn relays agent tokens straight through.
        should_mediate = target_agent != CANCEL_INTERACTION_AGENT and (bool(personality.strip()) or bool(reminder_text))

        tokens_were_streamed = sc.relayed_tokens
        # M-9: the streaming mediation branch requires a non-empty
        # personality. A reminder-only turn (no personality) falls through to
        # the blocking path, which appends the reminder to the agent's answer
        # instead of streaming the reminder as the sole token (replacing the
        # answer).
        use_streamed_mediation = (
            mediation_streaming_enabled and should_mediate and personality.strip() and full_speech.strip()
        )
        if use_streamed_mediation:
            # Stream mediated tokens to the client
            mediated_tokens: list[str] = []
            mediation_failed_partial = False
            # Hold back the trailing len(_FOLLOWUP_TAG) chars so a trailing
            # "[FOLLOWUP]" marker never leaks into the token frames (the tag
            # only ever appears as a suffix of the complete text); the
            # stripped remainder is flushed after the loop.
            pending = ""
            try:
                async for token in self._mediate_response_stream(
                    agent_speech=full_speech,
                    user_text=user_text,
                    agent_id=target_agent,
                    language=language,
                    span_collector=span_collector,
                    reminder_text=reminder_text,
                    allow_organic_followup=allow_organic_followup,
                ):
                    if token:
                        mediated_tokens.append(token)
                        pending += token
                        if len(pending) > len(_FOLLOWUP_TAG):
                            emit = pending[: -len(_FOLLOWUP_TAG)]
                            pending = pending[-len(_FOLLOWUP_TAG) :]
                            yield {
                                "token": emit,
                                "done": False,
                                "conversation_id": conversation_id,
                            }
            except MediationStreamError:
                if not mediated_tokens:
                    # M-10: nothing was spoken -- fall back to the blocking
                    # path so the full answer is delivered and stored.
                    use_streamed_mediation = False
                else:
                    # M-10: partial output was already spoken and cannot be
                    # retracted; post-mediation finalization below persists
                    # the ORIGINAL full speech so the turn store / response
                    # cache never record the truncation. The unflushed
                    # holdback is dropped: the spoken stream is already
                    # truncated and flushing could leak a partial tag.
                    mediation_failed_partial = True

        if use_streamed_mediation:
            # Flush the holdback remainder (never a tag fragment, thanks to
            # the window above) as one final non-done token frame so no
            # mediated text is lost.
            if not mediation_failed_partial:
                tail, _ = _strip_followup_tag(pending)
                if tail:
                    yield {
                        "token": tail,
                        "done": False,
                        "conversation_id": conversation_id,
                    }
            # Post-process the collected mediated text
            collected_mediated = "".join(mediated_tokens)
            mediated = strip_parenthetical_asides(collected_mediated) if collected_mediated.strip() else full_speech
            mediated, followup = _strip_followup_tag(mediated)

            if mediated_tokens:
                tokens_were_streamed = True
            if mediation_failed_partial:
                mediated = full_speech

            # Run post-mediation finalization
            full_speech, vf_eff = await self._finalize_post_mediation(
                task=task,
                user_text=user_text,
                target_agent=target_agent,
                confidence=confidence,
                condensed_task=condensed_task,
                mediated_speech=mediated,
                original_speech="".join(sc.collected_speech),
                action_executed=sc.action_executed,
                has_error=has_error,
                span_collector=span_collector,
                conversation_id=conversation_id,
                language=language,
                turns=turns,
                classifications=classifications,
                voice_followup_requested=sc.stream_voice_followup,
                mediated_followup=followup,
                routed_to=target_agent,
                skip_response_cache=False,
                used_origin_context=used_origin_context,
                routing_entry_id=prelude.routing_entry_id,
            )
        else:
            # Existing blocking path. The mediation probe was already
            # consumed above (has_error applied), so pass it through instead
            # of re-querying the calendar injector.
            full_speech, vf_eff = await self._finalize_single_agent_response(
                task=task,
                user_text=user_text,
                target_agent=target_agent,
                confidence=confidence,
                condensed_task=condensed_task,
                speech=full_speech,
                action_executed=sc.action_executed,
                has_error=has_error,
                span_collector=span_collector,
                conversation_id=conversation_id,
                language=language,
                turns=turns,
                classifications=classifications,
                voice_followup_requested=sc.stream_voice_followup,
                routed_to=target_agent,
                mediation_agent=target_agent,
                skip_mediation_on_error=False,
                used_origin_context=used_origin_context,
                routing_entry_id=prelude.routing_entry_id,
                mediation_inputs=(reminder_text, allow_organic_followup),
            )

        # Yield final done chunk; mediated_speech is only included when tokens
        # were not already streamed, avoiding duplicate TTS output.
        mediated_text = strip_markdown(full_speech)
        final_chunk: dict[str, Any] = {
            "token": "",
            "done": True,
            "conversation_id": conversation_id,
            "routed_to": target_agent,
            "sanitized": True,
        }
        if not tokens_were_streamed:
            final_chunk["mediated_speech"] = mediated_text
        if sc.stream_error:
            final_chunk["error"] = _stringify_error(sc.stream_error)
        if vf_eff:
            final_chunk["voice_followup"] = True
        if sc.action_executed:
            final_chunk["action_executed"] = sc.action_executed
        yield final_chunk

    async def _should_send_filler(self, target_agent: str) -> bool:
        """Check if filler is enabled and the target agent is expected to be slow.

        Delegates to :class:`~app.agents.filler_coordinator.FillerCoordinator`;
        kept as a thin wrapper so direct unit tests and the in-place race
        orchestration (which reads it off the instance) keep working unchanged.
        """
        return await self._filler_coord.should_send_filler(target_agent)

    async def _get_filler_threshold_ms(self) -> int:
        """Read filler threshold from DB (live, not cached).

        Delegates to :class:`~app.agents.filler_coordinator.FillerCoordinator`.
        """
        return await self._filler_coord.get_filler_threshold_ms()

    async def _invoke_filler_agent(self, user_text: str, target_agent: str, language: str) -> str | None:
        """Call the filler-agent via the A2A dispatcher to generate a filler phrase.

        Delegates to :class:`~app.agents.filler_coordinator.FillerCoordinator`.
        Returns the filler text or None if generation fails.
        """
        return await self._filler_coord.invoke_filler_agent(user_text, target_agent, language)

    async def _execute_cached_action(self, cached_action) -> dict[str, Any] | None:
        return await self._cache_orchestrator.execute_cached_action(cached_action)

    @staticmethod
    def _is_background_turn(task: IngressTask | BackgroundTask) -> bool:
        ctx = task.context
        return bool(ctx and ctx.source == "background" and ctx.background_event is not None)

    async def _handle_background_turn(self, task: IngressTask | BackgroundTask) -> dict[str, Any]:
        ctx = task.context
        event = ctx.background_event if ctx else None
        if event is None:
            logger.warning(
                "Background turn missing event payload (code=%s, recoverable=%s)",
                "parse_error",
                True,
            )
            return {
                "speech": "",
                "error": "Missing background event payload.",
            }
        from app.agents.background_actions import handle_background_event

        return await handle_background_event(
            event,
            context=ctx,
            ha_client=self._ha_client,
            entity_index=self._entity_index,
            gateway=self._dispatcher,
        )

    async def _build_agent_descriptions(self) -> str:
        return await self._classification_engine.build_agent_descriptions()

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
        return await self._classification_engine.classify(
            user_text,
            cache_result=cache_result,
            conversation_id=conversation_id,
            span_collector=span_collector,
            language=language,
            allow_cache_lookup=allow_cache_lookup,
            call_llm=self._call_llm,
            load_prompt_async=self._load_prompt_async,
            get_turns=self._get_turns,
        )

    async def _parse_classification(self, response: str, original_text: str) -> list[tuple[str, str, float | None]]:
        return await self._classification_engine.parse_classification(response, original_text)

    async def _get_turns(self, conversation_id: str | None) -> list[dict[str, Any]]:
        return await self._conversation_manager.get_turns(conversation_id)

    async def _store_turn(
        self,
        conversation_id: str | None,
        user_text: str,
        assistant_text: str,
        agent_id: str | None = None,
        resolved_entities: list[dict[str, Any]] | None = None,
        user_id: str | None = None,
        language: str | None = None,
        source: str | None = None,
    ) -> None:
        await self._conversation_manager.store_turn(
            conversation_id,
            user_text,
            assistant_text,
            agent_id,
            resolved_entities=resolved_entities,
            user_id=user_id,
            language=language,
            source=source,
        )

    def _evict_stale_conversations(self) -> None:
        self._conversation_manager._evict_stale_conversations()

    async def _merge_responses(
        self,
        agent_responses: list[tuple[str, str, bool]],
        user_text: str,
        span_collector=None,
        reminder_text: str | None = None,
        failed_agents: list[str] | None = None,
    ) -> tuple[str, bool]:
        """Merge multiple agent responses into a single natural answer.

        Delegates to :class:`~app.agents.mediation.MediationService`; kept as a
        thin wrapper so the ``PipelineDirector`` callback and the direct unit
        tests (``orch._merge_responses(...)``) keep working unchanged.
        """
        return await self._mediation.merge_responses(
            agent_responses,
            user_text,
            span_collector=span_collector,
            reminder_text=reminder_text,
            failed_agents=failed_agents,
        )

    @staticmethod
    def _format_fallback(agent_responses: list[tuple[str, str, bool]]) -> str:
        """Fallback formatting when LLM merge fails.

        Delegates to :class:`~app.agents.mediation.MediationService.format_fallback`;
        kept as a staticmethod so ``OrchestratorAgent._format_fallback(...)``
        (called by unit tests) keeps working unchanged.
        """
        return MediationService.format_fallback(agent_responses)

    async def _mediate_response(
        self,
        agent_speech: str,
        user_text: str,
        agent_id: str,
        language: str = "en",
        span_collector=None,
        reminder_text: str | None = None,
        allow_organic_followup: bool = False,
    ) -> tuple[str, bool]:
        """Optionally mediate the domain agent response with personality.

        Delegates to :class:`~app.agents.mediation.MediationService`; kept as a
        thin wrapper so direct unit tests (``orch._mediate_response(...)``) and
        internal call sites keep working unchanged. The service resolves its
        collaborators (personality cache, prompt loader, LLM client, mediation
        overrides) through this orchestrator at call time, preserving the
        ``patch.object(orch, ...)`` seams exercised by the tests.

        Returns:
            Tuple of (mediated_speech, followup_needed).
        """
        return await self._mediation.mediate_response(
            agent_speech,
            user_text,
            agent_id,
            language=language,
            span_collector=span_collector,
            reminder_text=reminder_text,
            allow_organic_followup=allow_organic_followup,
        )

    async def _mediate_response_stream(
        self,
        agent_speech: str,
        user_text: str,
        agent_id: str,
        language: str = "en",
        span_collector=None,
        reminder_text: str | None = None,
        allow_organic_followup: bool = False,
    ) -> AsyncGenerator[str, None]:
        """Streaming variant of _mediate_response.

        Delegates to :class:`~app.agents.mediation.MediationService`; kept as a
        thin wrapper so direct unit tests (``orch._mediate_response_stream(...)``)
        keep working unchanged. Yields mediated tokens as the LLM generates them.
        The caller must collect tokens and run post-processing
        (strip_parenthetical_asides, [FOLLOWUP] detection) on the complete text.
        This method does NOT return the followup flag.
        """
        async for token in self._mediation.mediate_response_stream(
            agent_speech,
            user_text,
            agent_id,
            language=language,
            span_collector=span_collector,
            reminder_text=reminder_text,
            allow_organic_followup=allow_organic_followup,
        ):
            yield token

    @staticmethod
    def _strip_seq_rule(prompt: str) -> str:
        return ClassificationEngine.strip_seq_rule(prompt)
