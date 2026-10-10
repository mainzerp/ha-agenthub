"""Pipeline phase strategy interfaces and default implementations.

Defines the Strategy pattern interfaces for the four pipeline phases
and provides default implementations extracted from PipelineDirector.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from app.agents.cache_orchestrator import CacheOrchestrator
from app.agents.classification_engine import ClassificationEngine
from app.agents.compound_utterance import looks_compound
from app.agents.conversation_manager import ConversationManager, extract_resolved_entities
from app.agents.dispatch_manager import DispatchManager, canned_error_code
from app.agents.sanitize import strip_markdown
from app.agents.task_pipeline import CacheReplayResult, DispatchResult
from app.cache.cache_manager import CacheManager
from app.models.agent import IngressTask, source_allows_voice_followup

__all__ = [
    "CacheReplayStrategy",
    "ClassificationStrategy",
    "DefaultCacheReplayStrategy",
    "DefaultClassificationStrategy",
    "DefaultDispatchStrategy",
    "DefaultFinalizationStrategy",
    "DispatchStrategy",
    "FinalizationStrategy",
]

logger = logging.getLogger(__name__)

# Fan-out cap for one multi-intent turn. Intents beyond the cap (lowest
# confidence first) are not dispatched; they are reported to the user via
# the merge step instead of being dropped silently.
MAX_PARALLEL_INTENTS = 5
# ``failed_agents`` reason for intents skipped by the fan-out cap.
INTENT_LIMIT_REASON = "intent_limit"


class CacheReplayStrategy(ABC):
    """Abstract strategy for the cache replay pipeline phase."""

    @abstractmethod
    async def execute(
        self,
        task: IngressTask,
        user_text: str,
        language: str,
        span_collector,
        *,
        skip_lookup: bool = False,
    ) -> CacheReplayResult: ...


class ClassificationStrategy(ABC):
    """Abstract strategy for the intent classification pipeline phase."""

    @abstractmethod
    async def execute(
        self,
        task: IngressTask,
        user_text: str,
        language: str,
        span_collector,
        *,
        pre_classified: tuple[list[tuple[str, str, float | None]], bool] | None = None,
        routing_skip: Any | None = None,
        compound_bypass: bool = False,
        extended_metadata: bool = False,
        classify_reason: str | None = None,
        allow_classify_cache_lookup: bool = False,
        prefetched_turns: list[dict[str, Any]] | None = None,
        pending_question: str | None = None,
    ) -> tuple[list[tuple[str, str, float | None]], bool, str, str, float | None]: ...


class DispatchStrategy(ABC):
    """Abstract strategy for the dispatch pipeline phase."""

    @abstractmethod
    async def execute(
        self,
        task: IngressTask,
        classifications: list[tuple[str, str, float | None]],
        user_text: str,
        conversation_id: str,
        turns: list[dict[str, Any]],
        span_collector,
        language: str,
        incoming_context,
    ) -> DispatchResult: ...


class FinalizationStrategy(ABC):
    """Abstract strategy for the finalization pipeline phase."""

    @abstractmethod
    async def execute(
        self,
        task: IngressTask,
        dispatch_result: DispatchResult,
        user_text: str,
        language: str,
        conversation_id: str,
        turns: list[dict[str, Any]],
        span_collector,
        classifications: list[tuple[str, str, float | None]],
        voice_followup_requested: bool,
        used_origin_context: bool,
        confidence: float | None = None,
        condensed_task: str = "",
        routing_entry_id: str | None = None,
    ) -> dict[str, Any]: ...


class DefaultCacheReplayStrategy(CacheReplayStrategy):
    """Default cache replay: lookup action-cache and routing-cache."""

    def __init__(
        self,
        cache_manager: CacheManager | None = None,
        cache_orchestrator: CacheOrchestrator | None = None,
    ) -> None:
        self._cache_manager = cache_manager
        self._cache_orchestrator = cache_orchestrator

    async def execute(
        self,
        task: IngressTask,
        user_text: str,
        language: str,
        span_collector,
        *,
        skip_lookup: bool = False,
    ) -> CacheReplayResult:
        compound_bypass = False
        action_replay = None
        routing_skip = None

        if not skip_lookup and self._cache_manager:
            if await self._cache_orchestrator._get_bool_setting_impl(
                "cache.compound_utterance_bypass", True
            ) and looks_compound(user_text):
                compound_bypass = True
                logger.debug("Skipping cache lookup for structurally compound utterance: %r", user_text[:80])
            else:
                action_replay, routing_skip = await self._cache_orchestrator.try_cache_replay(
                    task=task,
                    user_text=user_text,
                    language=language,
                    span_collector=span_collector,
                )

        return CacheReplayResult(
            action_replay=action_replay,
            routing_skip=routing_skip,
            compound_bypass=compound_bypass,
        )


class DefaultClassificationStrategy(ClassificationStrategy):
    """Default classification: classify intent via LLM or cached result."""

    def __init__(
        self,
        classification_engine: ClassificationEngine,
        pipeline_record_classify_span: Callable | None = None,
        call_llm: Callable | None = None,
        load_prompt_async: Callable | None = None,
        get_turns: Callable | None = None,
    ) -> None:
        self._classification_engine = classification_engine
        self._pipeline_record_classify_span = pipeline_record_classify_span
        self._call_llm = call_llm
        self._load_prompt_async = load_prompt_async
        self._get_turns = get_turns

    async def execute(
        self,
        task: IngressTask,
        user_text: str,
        language: str,
        span_collector,
        *,
        pre_classified: tuple[list[tuple[str, str, float | None]], bool] | None = None,
        routing_skip: Any | None = None,
        compound_bypass: bool = False,
        extended_metadata: bool = False,
        classify_reason: str | None = None,
        allow_classify_cache_lookup: bool = False,
        prefetched_turns: list[dict[str, Any]] | None = None,
        pending_question: str | None = None,
    ) -> tuple[list[tuple[str, str, float | None]], bool, str, str, float | None]:
        from app.analytics.tracer import _optional_span

        next_classify_extra: dict[str, object] = {}
        synthetic_preclassified = False

        if compound_bypass:
            next_classify_extra["compound_bypass"] = True
            next_classify_extra["compound_bypass_reason"] = "multi_sentence"
        if routing_skip is not None:
            pre_classified = (CacheOrchestrator.build_synthetic_classifications(routing_skip), True)
            synthetic_preclassified = True
            next_classify_extra["reason"] = "routing_cache_skip"
        if classify_reason:
            next_classify_extra["reason"] = classify_reason

        if pre_classified is not None:
            classifications, routing_cached = pre_classified
            target_agent, condensed_task, confidence = classifications[0]
            if synthetic_preclassified:
                async with _optional_span(span_collector, "classify", agent_id="orchestrator") as span:
                    self._pipeline_record_classify_span(
                        span,
                        classifications,
                        user_text,
                        condensed_task,
                        confidence,
                        routing_cached,
                        extended_metadata=extended_metadata,
                        extra_metadata=next_classify_extra or None,
                    )
        else:
            async with _optional_span(span_collector, "classify", agent_id="orchestrator") as span:
                classifications, routing_cached = await self._classification_engine.classify(
                    user_text,
                    cache_result=None,
                    conversation_id=task.conversation_id,
                    span_collector=span_collector,
                    language=language,
                    allow_cache_lookup=allow_classify_cache_lookup,
                    call_llm=self._call_llm,
                    load_prompt_async=self._load_prompt_async,
                    get_turns=self._get_turns,
                    prefetched_turns=prefetched_turns,
                    pending_question=pending_question,
                )
                target_agent, condensed_task, confidence = classifications[0]
                self._pipeline_record_classify_span(
                    span,
                    classifications,
                    user_text,
                    condensed_task,
                    confidence,
                    routing_cached,
                    extended_metadata=extended_metadata,
                    extra_metadata=next_classify_extra or None,
                )

        return classifications, routing_cached, target_agent, condensed_task, confidence


class DefaultDispatchStrategy(DispatchStrategy):
    """Default dispatch: route to one or more agents and collect results."""

    def __init__(
        self,
        dispatch_manager: DispatchManager,
        handle_sequential_send: Callable | None = None,
    ) -> None:
        self._dispatch_manager = dispatch_manager
        self._handle_sequential_send = handle_sequential_send

    async def execute(
        self,
        task: IngressTask,
        classifications: list[tuple[str, str, float | None]],
        user_text: str,
        conversation_id: str,
        turns: list[dict[str, Any]],
        span_collector,
        language: str,
        incoming_context,
    ) -> DispatchResult:
        is_sequential_send = any(a == "send-agent" for a, _, _ in classifications) and any(
            a != "send-agent" for a, _, _ in classifications
        )

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
                incoming_context,
                resolved_language=language,
            )
            action_executed = (result or {}).get("action_executed")
            has_error = bool((result or {}).get("error"))
            agent_error = (result or {}).get("error")
            agent_voice_followup = bool((result or {}).get("voice_followup"))
            return DispatchResult(
                classifications=classifications,
                target_agent=classifications[0][0],
                routed_to=routed_to,
                speech=speech,
                action_executed=action_executed,
                has_error=has_error,
                agent_error=agent_error,
                agent_voice_followup=agent_voice_followup,
                is_sequential_send=True,
            )

        if len(classifications) == 1:
            target_agent = classifications[0][0]
            condensed_task = classifications[0][1]
            agent_id, speech, result = await self._dispatch_manager.dispatch_single(
                target_agent,
                condensed_task,
                user_text,
                conversation_id,
                turns,
                span_collector,
                incoming_context=incoming_context,
                resolved_language=language,
            )
            action_executed = (result or {}).get("action_executed")
            routed_to = agent_id
            directive = (result or {}).get("directive")
            directive_reason = (result or {}).get("reason")
            agent_error = (result or {}).get("error")
            has_error = agent_error is not None
            agent_voice_followup = bool((result or {}).get("voice_followup"))
            return DispatchResult(
                classifications=classifications,
                target_agent=target_agent,
                routed_to=routed_to,
                speech=speech,
                action_executed=action_executed,
                has_error=has_error,
                agent_error=agent_error,
                agent_voice_followup=agent_voice_followup,
                directive=directive,
                directive_reason=directive_reason,
            )

        # Multi-agent dispatch
        import asyncio

        # Classifications arrive sorted by confidence; the lowest-confidence
        # intents over the cap are reported as not executed.
        for aid, _ctask, _conf in classifications[MAX_PARALLEL_INTENTS:]:
            logger.warning("Intent for %s exceeds the per-turn cap of %d; not dispatched", aid, MAX_PARALLEL_INTENTS)
            failed_agents.append((aid, INTENT_LIMIT_REASON))
        dispatched = classifications[:MAX_PARALLEL_INTENTS]

        dispatch_coros = [
            self._dispatch_manager.dispatch_single(
                aid,
                ctask,
                user_text,
                conversation_id,
                turns,
                span_collector,
                incoming_context=incoming_context,
                resolved_language=language,
            )
            for aid, ctask, _ in dispatched
        ]
        dispatch_results = await asyncio.gather(*dispatch_coros, return_exceptions=True)

        agent_responses: list[tuple[str, str, bool]] = []
        action_executed = None
        routed_agents: list[str] = []
        for idx, dr in enumerate(dispatch_results):
            agent_id_for_idx = dispatched[idx][0]
            if isinstance(dr, Exception):
                logger.warning("Multi-agent dispatch error for %s: %s", agent_id_for_idx, dr)
                failed_agents.append((agent_id_for_idx, str(dr)))
                continue
            aid, sp, res = dr  # type: ignore[misc]
            res_dict = res or {}
            res_error = res_dict.get("error") if isinstance(res_dict, dict) else None
            # Structured failure detection: canned dispatch outcomes carry
            # ``error.canned`` (DispatchManager.canned_result); no comparison
            # against English speech literals.
            if res is None or res_error:
                if res_error:
                    reason = res_error.get("code", "agent_error") if isinstance(res_error, dict) else "agent_error"
                else:
                    reason = "no_result"
                logger.warning("Multi-agent dispatch reported error for %s: %s", agent_id_for_idx, reason)
                failed_agents.append((agent_id_for_idx, reason))
                continue
            routed_agents.append(aid)
            acted = bool(res and res.get("action_executed"))
            agent_responses.append((aid, sp, acted))
            if res and res.get("action_executed") and action_executed is None:
                action_executed = res["action_executed"]
            if res and res.get("voice_followup"):
                agent_voice_followup = True

        target_agent = routed_agents[0] if routed_agents else "general-agent"
        routed_to = ", ".join(routed_agents) if routed_agents else "general-agent"
        speech = ""
        # M-13: a partial failure (some agents answered) is not a turn error --
        # only a total failure marks the turn erroneous (which would suppress
        # reminders and attach an error to the final response).
        has_error = bool(failed_agents) and not agent_responses

        return DispatchResult(
            classifications=classifications,
            target_agent=target_agent,
            routed_to=routed_to,
            speech=speech,
            action_executed=action_executed,
            has_error=has_error,
            failed_agents=failed_agents,
            agent_responses=agent_responses,
            agent_voice_followup=agent_voice_followup,
        )


class DefaultFinalizationStrategy(FinalizationStrategy):
    """Default finalization: merge, mediate, store turn, create trace."""

    def __init__(
        self,
        cache_orchestrator: CacheOrchestrator | None = None,
        calendar_injector: Any | None = None,
        conversation_manager: ConversationManager | None = None,
        merge_responses: Callable | None = None,
        merge_voice_followup_and_organic: Callable | None = None,
        finalize_single_agent_response: Callable | None = None,
        create_trace: Callable | None = None,
    ) -> None:
        self._cache_orchestrator = cache_orchestrator
        self._calendar_injector = calendar_injector
        self._conversation_manager = conversation_manager
        self._merge_responses = merge_responses
        # Accepted for PipelineDirector constructor compatibility only; the
        # follow-up flags are merged inline (the orchestrator no-op is gone).
        del merge_voice_followup_and_organic
        self._finalize_single_agent_response = finalize_single_agent_response
        self._create_trace = create_trace

    async def execute(
        self,
        task: IngressTask,
        dispatch_result: DispatchResult,
        user_text: str,
        language: str,
        conversation_id: str,
        turns: list[dict[str, Any]],
        span_collector,
        classifications: list[tuple[str, str, float | None]],
        voice_followup_requested: bool,
        used_origin_context: bool,
        confidence: float | None = None,
        condensed_task: str = "",
        routing_entry_id: str | None = None,
    ) -> dict[str, Any]:
        has_error = dispatch_result.has_error
        target_agent = dispatch_result.target_agent
        routed_to = dispatch_result.routed_to
        speech = dispatch_result.speech
        action_executed = dispatch_result.action_executed
        agent_error = dispatch_result.agent_error
        failed_agents = dispatch_result.failed_agents
        agent_responses = dispatch_result.agent_responses
        agent_voice_followup = dispatch_result.agent_voice_followup
        is_sequential_send = dispatch_result.is_sequential_send

        voice_followup_effective = False

        if len(classifications) > 1 and not is_sequential_send:
            # Multi-agent finalization
            original_speech = speech
            from app.analytics.tracer import _optional_span

            skipped_ids = {aid for aid, reason in failed_agents if reason == INTENT_LIMIT_REASON}
            unreachable = [aid for aid, reason in failed_agents if reason != INTENT_LIMIT_REASON]
            skipped_tasks = [f"{aid}: {ctask}" for aid, ctask, _ in classifications if aid in skipped_ids]
            async with _optional_span(span_collector, "return", agent_id="orchestrator") as ret_span:
                ret_span["metadata"]["from_agent"] = routed_to
                if skipped_tasks:
                    ret_span["metadata"]["intents_skipped"] = sorted(skipped_ids)
                if not agent_responses and failed_agents:
                    # All agents failed: the merge step localizes the system
                    # line into the turn language (English on LLM failure).
                    speech, _ = await self._merge_responses(
                        [],
                        user_text,
                        span_collector=span_collector,
                        failed_agents=unreachable or sorted(skipped_ids),
                        language=language,
                    )
                    mediated_followup = False
                else:
                    reminder_text = None
                    if self._calendar_injector is not None and not has_error:
                        try:
                            reminder_text = await self._calendar_injector.inject_reminders(
                                utterance=task.description if task else None,
                                device_id=task.context.device_id if task.context else None,
                                area_id=task.context.area_id if task.context else None,
                                user_id=task.context.user_id if task.context else None,
                                language=language or "en",
                            )
                        except Exception:
                            logger.debug("Calendar reminder injection failed", exc_info=True)

                    speech, mediated_followup = await self._merge_responses(
                        agent_responses,
                        user_text,
                        span_collector=span_collector,
                        reminder_text=reminder_text,
                        failed_agents=unreachable or None,
                        language=language,
                        skipped_tasks=skipped_tasks or None,
                    )

                ret_span["metadata"]["agent_response"] = speech
                followup_question = bool(agent_voice_followup or mediated_followup)
                # Follow-up signal (multi-agent): a merged answer that asks the
                # user something records the pending question exactly like the
                # single-agent funnel (_finalize_post_mediation) does. A
                # comma-joined ``routed_to`` is never pinned; the next turn
                # still gets the follow-up hint during classification.
                if followup_question and self._conversation_manager is not None:
                    self._conversation_manager.set_pending_question(conversation_id, speech, routed_to)
                source = task.context.source if task and task.context else None
                voice_followup_effective = followup_question and source_allows_voice_followup(source)
                ret_span["metadata"]["final_response"] = speech
                ret_span["metadata"]["mediated"] = (speech != original_speech) or len(classifications) > 1
                ret_span["metadata"]["voice_followup"] = voice_followup_effective
                ret_span["metadata"]["cache_stored_response"] = False
                ret_span["metadata"]["cache_stored_routing"] = False
                # ENTITY_RES_REDESIGN Phase 6: remember the acted-on entity
                # (success path only) as an anaphora recency hint.
                resolved_entities = await extract_resolved_entities(action_executed)
                await self._conversation_manager.store_turn(
                    conversation_id,
                    user_text,
                    speech,
                    agent_id=routed_to,
                    resolved_entities=resolved_entities,
                    user_id=task.context.user_id if task and task.context else None,
                    language=language,
                    source=task.context.source if task and task.context else None,
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
                        task_context=task.context if task else None,
                        voice_followup=voice_followup_effective,
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
                language=language,
                turns=turns,
                classifications=classifications,
                voice_followup_requested=agent_voice_followup,
                routed_to=routed_to,
                mediation_agent=mediation_agent,
                # Error turns are mediated too, so canned system lines reach
                # the user in the turn language and personality.
                skip_mediation_on_error=False,
                skip_response_cache=is_sequential_send,
                used_origin_context=used_origin_context,
                routing_entry_id=routing_entry_id,
                canned_code=canned_error_code(agent_error),
            )

        response = {
            "speech": strip_markdown(speech),
            "routed_to": routed_to,
            "action_executed": action_executed,
            "voice_followup": voice_followup_effective,
            "sanitized": True,
        }
        if has_error:
            # Phase-1 contract: response errors are plain strings.
            code = "unknown"
            if isinstance(agent_error, dict):
                code = str(agent_error.get("code") or "unknown")
            response["error"] = code
        if failed_agents:
            response["partial_failure"] = {
                "failed_agents": [{"agent_id": aid, "error": msg} for aid, msg in failed_agents],
            }
        return response
