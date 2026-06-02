"""Orchestrator agent for intent classification and task dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.a2a.protocol import JsonRpcRequest
from app.agents.agent_registry import CachedAgentRegistry
from app.agents.base import BaseAgent
from app.agents.cache_orchestrator import CacheOrchestrator
from app.agents.cancel_speech import generate_cancel_speech
from app.agents.classification_engine import ClassificationEngine, _RecoverableClassificationError
from app.agents.conversation_manager import ConversationManager
from app.agents.decorator import agent
from app.agents.dispatch_manager import DispatchManager
from app.agents.language_detect import detect_user_language
from app.agents.sanitize import strip_markdown, strip_parenthetical_asides
from app.agents.task_pipeline import PipelineDirector
from app.analytics.collector import track_request
from app.analytics.tracer import _optional_span
from app.cache.cache_manager import ActionReplayOutcome, RoutingSkipOutcome
from app.db.repository import SettingsRepository
from app.ha_client.home_context import home_context_provider
from app.models.agent import AgentCard, AgentTask, TaskContext

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
_CANCEL_INTERACTION_AGENT = "cancel-interaction"


_CANNED_TIMEOUT_SPEECH = "I couldn't process that request in time."
_CANNED_GENERAL_ERROR_SPEECH = "I couldn't process that request right now."


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
    collected_speech: list[str] = None  # type: ignore[assignment]
    stream_directive: str | None = None
    stream_reason: str | None = None
    action_executed: Any = None
    stream_error: Any = None
    stream_voice_followup: bool = False

    def __post_init__(self) -> None:
        if self.collected_speech is None:
            self.collected_speech = []

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
    ) -> None:
        super().__init__(ha_client=ha_client, entity_index=entity_index)
        self._dispatcher = dispatcher
        self._cache_manager = cache_manager
        self._filler_agent = filler_agent
        self._event_bus = event_bus
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
        )
        self._classification_engine = ClassificationEngine(
            agent_registry=self._agent_registry,
            cache_manager=cache_manager,
            call_llm=self._call_llm,
            load_prompt_async=self._load_prompt_async,
            get_turns=self._conversation_manager.get_turns,
            wrap_user_input=self._wrap_user_input,
            append_conversation_turn_messages=self._append_conversation_turn_messages,
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
            self._default_timeout = int(val or "5")
        except (ValueError, TypeError):
            logger.debug("Invalid a2a.default_timeout value, using default", exc_info=True)
        try:
            val = await SettingsRepository.get_value("a2a.max_iterations", "3")
            self._max_iterations = int(val or "3")
        except (ValueError, TypeError):
            logger.debug("Invalid a2a.max_iterations value, using default", exc_info=True)
        try:
            val = await SettingsRepository.get_value("a2a.max_dispatch_timeout", "60")
            self._max_dispatch_timeout = float(val or "60")
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
            self._mediation_temperature = float(val or "0.3")
        except (ValueError, TypeError):
            self._mediation_temperature = 0.3
        try:
            val = await SettingsRepository.get_value("mediation.max_tokens", "2048")
            self._mediation_max_tokens = int(val or "2048")
        except (ValueError, TypeError):
            self._mediation_max_tokens = 2048
        logger.info(
            "Mediation config: model=%s temperature=%.1f max_tokens=%d",
            self._mediation_model or "(orchestrator default)",
            self._mediation_temperature,
            self._mediation_max_tokens,
        )

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
        classifications: list[tuple[str, str, float]],
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
                source=incoming_context.source if incoming_context else "api",
                language=content_language,
                sequential_send=True,
                injection_detected=incoming_context.injection_detected if incoming_context else False,
            )

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
            enabled_raw = await SettingsRepository.get_value("orchestrator.organic_followup_enabled", "false")
            enabled = (enabled_raw or "false").lower() == "true"
            if not enabled:
                return speech, False
            raw_p = await SettingsRepository.get_value("orchestrator.organic_followup_probability", "0.08")
            p = float(raw_p or "0.08")
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
        target_agent: str | None = None,
        mediated_followup: bool = False,
    ) -> tuple[str, bool]:
        """Extend speech for organic follow-up; combine agent + organic mic-open flags."""
        if has_error or target_agent == _CANCEL_INTERACTION_AGENT:
            return speech, bool(agent_requested)
        speech_out, organic = await self._organic_voice_followup_offer(ctx, language, False, speech)
        # Hybrid: use mediated flag if available, otherwise ask LLM
        if mediated_followup:
            llm_followup = True
        else:
            llm_followup = await self._detect_followup_needed_llm(speech_out, language)
        return speech_out, bool(agent_requested or organic or llm_followup)

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
        return await self._cache_orchestrator.try_cache_replay(
            task=task,
            user_text=user_text,
            language=language,
            requesting_agent_id=requesting_agent_id,
            span_collector=span_collector,
            check_visibility=self._cached_action_is_still_visible,
            exec_cached_action=self._execute_cached_action,
        )

    @staticmethod
    def _build_synthetic_classifications(
        routing: RoutingSkipOutcome,
    ) -> list[tuple[str, str, float | None]]:
        return CacheOrchestrator.build_synthetic_classifications(routing)

    async def _cached_action_is_still_visible(self, agent_id: str, entity_id: str) -> bool:
        return await self._cache_orchestrator.cached_action_is_still_visible(agent_id, entity_id)

    async def _finalize_action_replay_hit(
        self,
        hit: ActionReplayOutcome,
        conversation_id: str,
        user_text: str,
        span_collector,
        *,
        task: AgentTask | None = None,
    ) -> dict[str, Any]:
        return await self._cache_orchestrator.finalize_action_replay_hit(
            hit,
            conversation_id,
            user_text,
            span_collector,
            task=task,
        )

    @staticmethod
    def _is_readonly_action_result(action_executed) -> bool:
        return CacheOrchestrator._is_readonly_action_result(action_executed)

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
        task: AgentTask | None = None,
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

    @staticmethod
    def _is_actionable_routing_agent(target_agent: str) -> bool:
        return CacheOrchestrator.is_actionable_routing_agent(target_agent)

    @staticmethod
    def _bool_setting_default(default: bool) -> str:
        return CacheOrchestrator.bool_setting_default(default)

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
            for s in span_collector.get_spans():
                if s.get("span_name") == "classify":
                    classify_duration = s.get("duration_ms")
                    break
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
        return CacheOrchestrator.legacy_pipeline_enabled()

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

    async def _run_pipeline_prelude(
        self,
        task: AgentTask,
        *,
        pre_classified: tuple[list[tuple[str, str, float | None]], bool] | None = None,
        classify_reason: str | None = None,
        allow_classify_cache_lookup: bool = False,
        extended_metadata: bool = False,
        publish_events: bool = False,
    ) -> PipelinePreludeResult:
        """Shared prelude: resolve language, check background/cache, classify.

        Encapsulates the logic that was duplicated between
        :meth:`_handle_task_impl` and :meth:`_handle_task_stream_impl`.
        When ``early_exit`` is not ``None`` the caller must short-circuit.
        """
        user_text = task.user_text or task.description

        conversation_id, detected_language, lang_turns = await self._pipeline_resolve_conversation_and_language(task)
        span_collector = task.span_collector

        if self._is_background_turn(task):
            result = await self._handle_background_turn(task)
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

        cache_replay = await self._pipeline_director.run_cache_replay(
            task,
            user_text,
            detected_language,
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
                detected_language=detected_language,
                lang_turns=lang_turns,
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

        used_origin_context = bool(task and task.context and (task.context.area_id or task.context.device_id))

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
                routing_skip=cache_replay.routing_skip,
                compound_bypass=cache_replay.compound_bypass,
                extended_metadata=extended_metadata,
                classify_reason=classify_reason,
                allow_classify_cache_lookup=allow_classify_cache_lookup,
            )
        except _RecoverableClassificationError as exc:
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
            ret_span["metadata"]["agent_response"] = speech
            # Fetch calendar reminder so the mediation LLM can weave it in naturally
            reminder_text: str | None = None
            if self._calendar_injector is not None and not has_error:
                try:
                    reminder_text = await self._calendar_injector.inject_reminders(
                        utterance=task.description,
                        device_id=task.context.device_id if task.context else None,
                        area_id=task.context.area_id if task.context else None,
                        user_id=task.context.user_id if task.context else None,
                        language=(task.context.language if task.context else "en") or "en",
                    )
                except Exception:
                    logger.debug("Calendar reminder injection failed", exc_info=True)

            should_mediate = target_agent != _CANCEL_INTERACTION_AGENT and (
                not has_error or not skip_mediation_on_error
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
                )
            elif reminder_text:
                # No mediation path -- append reminder directly as fallback
                separator = " " if speech and speech[-1] in ".!?" else ". "
                speech = f"{speech}{separator}{reminder_text}" if speech else reminder_text
            speech, voice_followup_effective = await self._merge_voice_followup_and_organic(
                speech,
                agent_requested=voice_followup_requested,
                ctx=task.context,
                language=language,
                has_error=has_error,
                target_agent=target_agent,
                mediated_followup=mediated_followup,
            )
            ret_span["metadata"]["final_response"] = speech
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
                    original_response_text=original_speech,
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
                    voice_followup=voice_followup_effective,
                )
        return speech, voice_followup_effective

    async def _run_pipeline(
        self,
        task: AgentTask,
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

    async def handle_task(
        self,
        task: AgentTask,
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

    def handle_task_stream(self, task: AgentTask) -> AsyncGenerator[dict[str, Any], None]:
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
        _pre_classified: tuple[list[tuple[str, str, float | None]], bool] | None = None,
        _classify_reason: str | None = None,
        _allow_classify_cache_lookup: bool | None = None,
    ) -> dict[str, Any]:
        """Thin wrapper around the shared TaskPipeline phases."""
        user_text = task.user_text or task.description
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
        turns = await self._get_turns(conversation_id)
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
        )
        response["conversation_id"] = conversation_id
        return response

    async def _handle_task_stream_impl(self, task: AgentTask) -> AsyncGenerator[dict[str, Any], None]:
        user_text = task.user_text or task.description
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
            exit_type = ee.pop("_exit_type", "")
            if exit_type == "classification_error":
                yield {
                    "token": "",
                    "done": True,
                    "conversation_id": prelude.conversation_id,
                    "mediated_speech": strip_markdown(ee.get("speech", "")),
                    "error": ee["error"],
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
                    final_chunk["error"] = ee["error"]
                yield final_chunk
            else:
                yield {
                    "token": ee["speech"],
                    "done": True,
                    "conversation_id": prelude.conversation_id,
                    "mediated_speech": ee["speech"],
                    "sanitized": True,
                }
            return

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

        if len(classifications) == 1 and target_agent == _CANCEL_INTERACTION_AGENT:
            async with _optional_span(span_collector, "dispatch", agent_id=_CANCEL_INTERACTION_AGENT) as span:
                full_speech = await generate_cancel_speech(detected_language, user_text)
                latency_ms = (time.perf_counter() - t0_request) * 1000
                span["metadata"]["latency_ms"] = latency_ms
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
                    target_agent=_CANCEL_INTERACTION_AGENT,
                    mediated_followup=False,
                )
                ret_span["metadata"]["final_response"] = full_speech
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
                        content_agent_for_filler or "",
                        language,
                    )
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
        agent_task = AgentTask(
            description=condensed_task,
            user_text=user_text,
            conversation_id=conversation_id,
            context=context,
        )

        # 3. Dispatch via A2A message/stream
        request = JsonRpcRequest(
            method="message/stream",
            params={
                "agent_id": target_agent,
                "task": agent_task,
                "_span_collector": span_collector,
            },
            id=conversation_id or "orchestrator-stream",
        )

        t0_dispatch = time.perf_counter()
        sc = StreamingContext()
        use_filler = await self._should_send_filler(target_agent)
        filler_threshold_ms = await self._get_filler_threshold_ms() if use_filler else 1000
        # P3-10: per-request filler-decision log; debug.
        logger.debug("Filler decision for %s: use_filler=%s", target_agent, use_filler)

        async def _process_chunk(chunk):
            """Process a single stream chunk: collect speech and detect actions."""
            chunk_result = chunk if isinstance(chunk, dict) else {}
            token = chunk_result.get("token", "")
            done = chunk_result.get("done", False)
            error = chunk_result.get("error")
            if error:
                logger.warning("Agent streaming error: %s", error)
                sc.stream_error = error
            if token:
                sc.append_speech(token)
            if done and chunk_result.get("action_executed"):
                sc.action_executed = chunk_result["action_executed"]
            if done and chunk_result.get("voice_followup"):
                sc.stream_voice_followup = True
            if done and chunk_result.get("directive"):
                sc.stream_directive = chunk_result["directive"]
                sc.stream_reason = chunk_result.get("reason")
            return token

        async def _stream_with_filler(stream_iter, span=None):
            """Race the first agent token against the filler threshold.

            Uses an asyncio.Queue to decouple the async generator reader
            from the consumer, so cancellation on timeout does not corrupt
            the generator state.
            """

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
                    sc.filler_start_ms = (time.perf_counter() - t0_request) * 1000
                    filler_text = await self._invoke_filler_agent(user_text, target_agent, language)
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
                    await _process_chunk(first_chunk)

                # Drain remaining chunks from queue
                while True:
                    item = await queue.get()
                    if item is _sentinel:
                        break
                    await _process_chunk(item)
            finally:
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(reader_task, timeout=5.0)

        async with _optional_span(span_collector, "dispatch", agent_id=target_agent) as span:
            async for token_dict in _stream_with_filler(self._dispatcher.dispatch_stream(request), span):
                yield token_dict
            span["metadata"]["token_count"] = len(sc.collected_speech)
            span["metadata"]["agent_response"] = "".join(sc.collected_speech)
            if sc.filler_sent:
                span["metadata"]["filler_sent"] = True
            span["metadata"]["non_filler_tokens_buffered_until_terminal"] = True

        latency_ms = (time.perf_counter() - t0_dispatch) * 1000
        await track_request(target_agent, cache_hit=False, latency_ms=latency_ms)

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
            final_chunk = {
                "token": "",
                "done": True,
                "conversation_id": conversation_id,
                "directive": sc.stream_directive,
                "routed_to": target_agent,
            }
            if sc.stream_reason is not None:
                final_chunk["reason"] = sc.stream_reason
            yield final_chunk
            return

        # 4. Store conversation turn and create trace summary
        full_speech = "".join(sc.collected_speech)
        if sc.stream_error is not None and target_agent == _FALLBACK_AGENT:
            if not full_speech.strip():
                full_speech = _CANNED_GENERAL_ERROR_SPEECH
            # For the fallback general-agent path, return a single user-facing
            # response instead of surfacing a transport-level stream error.
            sc.stream_error = None
        has_error = sc.stream_error is not None
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
            classifications=[(target_agent, condensed_task, confidence)],
            voice_followup_requested=sc.stream_voice_followup,
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
            "routed_to": target_agent,
            "sanitized": True,
        }
        if sc.stream_error:
            final_chunk["error"] = sc.stream_error
        if vf_eff:
            final_chunk["voice_followup"] = True
        if sc.action_executed:
            final_chunk["action_executed"] = sc.action_executed
        yield final_chunk

    async def _should_send_filler(self, target_agent: str) -> bool:
        """Check if filler is enabled and the target agent is expected to be slow."""
        try:
            val = await SettingsRepository.get_value("filler.enabled", "false")
            enabled = (val or "false").lower() == "true"
        except (ValueError, TypeError):
            enabled = False
        if not enabled:
            return False
        card = await self._agent_registry.get_agent_card(target_agent)
        if card is None:
            return False
        return card.expected_latency == "high"

    async def _get_filler_threshold_ms(self) -> int:
        """Read filler threshold from DB (live, not cached)."""
        try:
            val = await SettingsRepository.get_value("filler.threshold_ms", "1000")
            return int(val or "1000")
        except (ValueError, TypeError):
            return 1000

    async def _invoke_filler_agent(self, user_text: str, target_agent: str, language: str) -> str | None:
        """Call the filler-agent via the A2A dispatcher to generate a filler phrase.

        Returns the filler text or None if generation fails.
        """
        try:
            context = TaskContext(language=language)
            filler_task = AgentTask(
                description=f"generate_filler:{target_agent}",
                user_text=user_text,
                context=context,
            )
            request = JsonRpcRequest(
                method="message/send",
                params={
                    "agent_id": "filler-agent",
                    "task": filler_task,
                },
                id=f"filler-{uuid.uuid4().hex[:8]}",
            )
            response = await self._dispatcher.dispatch(request)
            result_data = self._dispatch_manager.normalize_agent_result(response)
            speech = result_data.get("speech", "")
            if not speech or not speech.strip():
                logger.warning("Filler agent returned empty speech; no filler will be spoken")
                return None
            return speech.strip()
        except Exception:
            logger.warning("Filler agent invocation failed", exc_info=True)
            return None

    async def _execute_cached_action(self, cached_action) -> dict[str, Any] | None:
        return await self._cache_orchestrator.execute_cached_action(cached_action)

    @staticmethod
    def _cancel_interaction_description_line() -> str:
        return ClassificationEngine.cancel_interaction_description_line()

    @staticmethod
    def _is_background_turn(task: AgentTask) -> bool:
        ctx = task.context
        return bool(ctx and ctx.source == "background" and ctx.background_event is not None)

    async def _handle_background_turn(self, task: AgentTask) -> dict[str, Any]:
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
        from app.agents.background_actions import handle_background_event

        return await handle_background_event(
            event,
            context=ctx,
            ha_client=self._ha_client,
            entity_index=self._entity_index,
            gateway=self._dispatcher,
        )

    async def _repair_send_agent_classifications(
        self,
        user_text: str,
        *,
        conversation_id: str | None,
        span_collector=None,
        language: str = "en",
    ) -> list[tuple[str, str, float | None]]:
        return await self._classification_engine.repair_send_agent_classifications(
            user_text,
            conversation_id=conversation_id,
            span_collector=span_collector,
            language=language,
        )

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
        return await self._classification_engine.sanitize_or_repair_classifications(
            classifications,
            user_text=user_text,
            conversation_id=conversation_id,
            span_collector=span_collector,
            language=language,
            allow_repair=allow_repair,
            require_send_partner=require_send_partner,
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

    async def _get_conversation_context_turn_limit(self) -> int:
        return await self._conversation_manager._get_conversation_context_turn_limit()

    async def _get_turns(self, conversation_id: str | None) -> list[dict[str, Any]]:
        return await self._conversation_manager.get_turns(conversation_id)

    async def _store_turn(
        self, conversation_id: str | None, user_text: str, assistant_text: str, agent_id: str | None = None
    ) -> None:
        await self._conversation_manager.store_turn(conversation_id, user_text, assistant_text, agent_id)

    def _evict_stale_conversations(self) -> None:
        self._conversation_manager._evict_stale_conversations()

    async def _merge_responses(
        self,
        agent_responses: list[tuple[str, str, bool]],
        user_text: str,
        span_collector=None,
        reminder_text: str | None = None,
    ) -> str:
        """Merge multiple agent responses into a single natural answer via LLM.

        Always calls LLM regardless of personality settings.
        Includes personality prompt if configured.
        If reminder_text is given, the LLM weaves it in naturally.
        Falls back to bracket-prefixed format on failure.
        """
        if not agent_responses:
            return "I couldn't process that request."

        # Only one response: return it directly (append reminder as fallback)
        if len(agent_responses) == 1:
            speech = agent_responses[0][1] or "I couldn't process that request."
            if reminder_text:
                separator = " " if speech and speech[-1] in ".!?" else ". "
                return f"{speech}{separator}{reminder_text}" if speech else reminder_text
            return speech

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
            personality: str | None = ""
            with contextlib.suppress(Exception):
                personality = await SettingsRepository.get_value("personality.prompt", "")

            system_content = await self._load_prompt_async("merge")
            personality_text = personality.strip() if personality and personality.strip() else ""
            system_content = system_content.replace("{personality}", personality_text).strip()

            user_content = f"User asked:\n{self._wrap_user_input(user_text)}\n\nAgent responses:\n{agent_summary}\n\n"
            if reminder_text:
                user_content += f"Reminder to weave in: {reminder_text}\n\n"
            user_content += "Combine into one natural response:"

            messages = [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ]

            overrides: dict[str, Any] = {
                "temperature": self._mediation_temperature,
                "max_tokens": self._mediation_max_tokens,
            }
            if self._mediation_model:
                overrides["model"] = self._mediation_model
            result = await self._call_llm(messages, span_collector=span_collector, **overrides)
            return result.strip() if result and result.strip() else self._format_fallback(agent_responses)
        except Exception:
            logger.warning("Multi-agent response merge failed, using fallback format", exc_info=True)
            fallback = self._format_fallback(agent_responses)
            if reminder_text:
                separator = " " if fallback and fallback[-1] in ".!?" else ". "
                return f"{fallback}{separator}{reminder_text}" if fallback else reminder_text
            return fallback

    @staticmethod
    def _format_fallback(agent_responses: list[tuple[str, str, bool]]) -> str:
        """Fallback formatting when LLM merge fails."""
        parts = [f"[{aid}] {sp}" for aid, sp, _ in agent_responses if sp and sp.strip()]
        return "\n\n".join(parts) if parts else "I couldn't process that request."

    async def _detect_followup_needed_llm(self, speech: str, language: str, span_collector=None) -> bool:
        """Lightweight LLM call to detect if speech contains a question requiring user response."""
        if not speech or not speech.strip():
            return False
        messages = [
            {
                "role": "system",
                "content": (
                    "You decide whether a smart home assistant response asks the user a question "
                    "that requires an answer. Reply ONLY with 'yes' or 'no'."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Language: {language}\n"
                    f"Assistant response: '{speech.strip()}'\n"
                    "Does this ask a question requiring user input? (yes/no)"
                ),
            },
        ]
        try:
            result = await self._call_llm(messages, temperature=0.0, max_tokens=4)
            return result and result.strip().lower().startswith("y")
        except Exception:
            logger.debug("Follow-up detection LLM call failed", exc_info=True)
            return False

    async def _mediate_response(
        self,
        agent_speech: str,
        user_text: str,
        agent_id: str,
        language: str = "en",
        span_collector=None,
        reminder_text: str | None = None,
    ) -> tuple[str, bool]:
        """Optionally mediate the domain agent response with personality.

        When personality.prompt is non-empty, passes the agent speech through
        a lightweight LLM call to apply the configured personality.
        If reminder_text is given, the LLM weaves it in naturally.
        Falls back to the original speech (+ appended reminder) on any failure.

        Returns:
            Tuple of (mediated_speech, followup_needed).
        """
        try:
            personality = await SettingsRepository.get_value("personality.prompt", "")
            if not (personality or "").strip():
                if reminder_text:
                    separator = " " if agent_speech and agent_speech[-1] in ".!?" else ". "
                    return (f"{agent_speech}{separator}{reminder_text}" if agent_speech else reminder_text), False
                return agent_speech, False
        except Exception:
            logger.debug("Failed to load personality prompt, using original speech", exc_info=True)
            if reminder_text:
                separator = " " if agent_speech and agent_speech[-1] in ".!?" else ". "
                return (f"{agent_speech}{separator}{reminder_text}" if agent_speech else reminder_text), False
            return agent_speech, False

        if not agent_speech or not agent_speech.strip():
            return agent_speech, False

        try:
            system_prompt = await self._load_prompt_async("mediate")
            personality_text = personality.strip() if personality and personality.strip() else ""
            system_prompt = system_prompt.replace("{personality}", personality_text)
            system_prompt = system_prompt.replace("{language}", language or "en").strip()
            user_content = (
                f"User asked:\n{self._wrap_user_input(user_text)}\nAgent ({agent_id}) responded: {agent_speech}"
            )
            if reminder_text:
                user_content += f"\nReminder to weave in: {reminder_text}"
            user_content += f"\n\nRephrase in {language}:"
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            overrides: dict[str, Any] = {
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
            mediated = strip_parenthetical_asides(result) if result and result.strip() else agent_speech
            followup = False
            if isinstance(mediated, str) and mediated.endswith("[FOLLOWUP]"):
                mediated = mediated[: -len("[FOLLOWUP]")].rstrip()
                followup = True
            return mediated, followup
        except Exception:
            logger.warning("Response mediation failed, using original speech", exc_info=True)
            if reminder_text:
                separator = " " if agent_speech and agent_speech[-1] in ".!?" else ". "
                return (f"{agent_speech}{separator}{reminder_text}" if agent_speech else reminder_text), False
            return agent_speech, False

    @staticmethod
    def _strip_seq_rule(prompt: str) -> str:
        return ClassificationEngine.strip_seq_rule(prompt)
