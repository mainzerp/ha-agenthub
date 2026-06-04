"""Shared task pipeline phases as a standalone PipelineDirector.

This class provides the four high-level pipeline phases so that both
``_handle_task_impl`` and ``_handle_task_stream_impl`` can reuse them
without duplicating the ~150 lines of shared prelude / finalisation
choreography.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.agents.cache_orchestrator import CacheOrchestrator
from app.agents.classification_engine import ClassificationEngine
from app.agents.conversation_manager import ConversationManager
from app.agents.dispatch_manager import DispatchManager
from app.cache.cache_manager import ActionReplayOutcome, CacheManager, RoutingSkipOutcome
from app.models.agent import AgentTask

if TYPE_CHECKING:
    from app.agents.pipeline_strategies import (
        CacheReplayStrategy,
        ClassificationStrategy,
        DispatchStrategy,
        FinalizationStrategy,
    )


@dataclass
class CacheReplayResult:
    """Result bag produced by the cache replay phase."""

    action_replay: ActionReplayOutcome | None = None
    routing_skip: RoutingSkipOutcome | None = None
    compound_bypass: bool = False


@dataclass
class DispatchResult:
    """Result bag produced by the dispatch phase."""

    classifications: list[tuple[str, str, float | None, list[str]]]
    target_agent: str
    routed_to: str
    speech: str
    action_executed: Any = None
    has_error: bool = False
    agent_error: Any = None
    agent_voice_followup: bool = False
    is_sequential_send: bool = False
    directive: str | None = None
    directive_reason: str | None = None
    failed_agents: list[tuple[str, str]] = field(default_factory=list)
    agent_responses: list[tuple[str, str, bool]] = field(default_factory=list)


class PipelineDirector:
    """Standalone pipeline coordinator - accepts all dependencies via constructor."""

    def __init__(
        self,
        cache_manager: CacheManager | None = None,
        calendar_injector: Any | None = None,
        cache_orchestrator: CacheOrchestrator | None = None,
        classification_engine: ClassificationEngine | None = None,
        dispatch_manager: DispatchManager | None = None,
        conversation_manager: ConversationManager | None = None,
        call_llm: Callable | None = None,
        load_prompt_async: Callable | None = None,
        get_turns: Callable | None = None,
        pipeline_record_classify_span: Callable | None = None,
        handle_sequential_send: Callable | None = None,
        merge_responses: Callable | None = None,
        merge_voice_followup_and_organic: Callable | None = None,
        create_trace: Callable | None = None,
        finalize_single_agent_response: Callable | None = None,
        cache_replay_strategy: CacheReplayStrategy | None = None,
        classification_strategy: ClassificationStrategy | None = None,
        dispatch_strategy: DispatchStrategy | None = None,
        finalization_strategy: FinalizationStrategy | None = None,
    ) -> None:
        self._cache_manager = cache_manager
        self._calendar_injector = calendar_injector
        self._cache_orchestrator = cache_orchestrator
        self._classification_engine = classification_engine
        self._dispatch_manager = dispatch_manager
        self._conversation_manager = conversation_manager
        self._call_llm = call_llm
        self._load_prompt_async = load_prompt_async
        self._get_turns = get_turns
        self._pipeline_record_classify_span = pipeline_record_classify_span
        self._handle_sequential_send = handle_sequential_send
        self._merge_responses = merge_responses
        self._merge_voice_followup_and_organic = merge_voice_followup_and_organic
        self._create_trace = create_trace
        self._finalize_single_agent_response = finalize_single_agent_response

        from app.agents.pipeline_strategies import (
            DefaultCacheReplayStrategy,
            DefaultClassificationStrategy,
            DefaultDispatchStrategy,
            DefaultFinalizationStrategy,
        )

        if cache_replay_strategy is None:
            cache_replay_strategy = DefaultCacheReplayStrategy(
                cache_manager=cache_manager,
                cache_orchestrator=cache_orchestrator,
            )
        self._cache_replay_strategy = cache_replay_strategy

        if classification_strategy is None:
            classification_strategy = DefaultClassificationStrategy(
                classification_engine=classification_engine,
                pipeline_record_classify_span=pipeline_record_classify_span,
                call_llm=call_llm,
                load_prompt_async=load_prompt_async,
                get_turns=get_turns,
            )
        self._classification_strategy = classification_strategy

        if dispatch_strategy is None:
            dispatch_strategy = DefaultDispatchStrategy(
                dispatch_manager=dispatch_manager,
                handle_sequential_send=handle_sequential_send,
            )
        self._dispatch_strategy = dispatch_strategy

        if finalization_strategy is None:
            finalization_strategy = DefaultFinalizationStrategy(
                cache_orchestrator=cache_orchestrator,
                calendar_injector=calendar_injector,
                conversation_manager=conversation_manager,
                merge_responses=merge_responses,
                merge_voice_followup_and_organic=merge_voice_followup_and_organic,
                finalize_single_agent_response=finalize_single_agent_response,
                create_trace=create_trace,
            )
        self._finalization_strategy = finalization_strategy

    def set_cache_replay_strategy(self, strategy: CacheReplayStrategy) -> None:
        self._cache_replay_strategy = strategy

    def set_classification_strategy(self, strategy: ClassificationStrategy) -> None:
        self._classification_strategy = strategy

    def set_dispatch_strategy(self, strategy: DispatchStrategy) -> None:
        self._dispatch_strategy = strategy

    def set_finalization_strategy(self, strategy: FinalizationStrategy) -> None:
        self._finalization_strategy = strategy

    # ------------------------------------------------------------------
    # Phase 0: cache replay (before classification)
    # ------------------------------------------------------------------

    async def run_cache_replay(
        self,
        task: AgentTask,
        user_text: str,
        language: str,
        span_collector,
        *,
        skip_lookup: bool = False,
    ) -> CacheReplayResult:
        if self._cache_manager is None:
            return CacheReplayResult()
        return await self._cache_replay_strategy.execute(
            task=task,
            user_text=user_text,
            language=language,
            span_collector=span_collector,
            skip_lookup=skip_lookup,
        )

    # ------------------------------------------------------------------
    # Phase 1: intent classification
    # ------------------------------------------------------------------

    async def run_classification(
        self,
        task: AgentTask,
        user_text: str,
        language: str,
        span_collector,
        *,
        pre_classified: tuple[list[tuple[str, str, float | None, list[str]]], bool] | None = None,
        routing_skip: Any | None = None,
        compound_bypass: bool = False,
        extended_metadata: bool = False,
        classify_reason: str | None = None,
        allow_classify_cache_lookup: bool = False,
    ) -> tuple[list[tuple[str, str, float | None, list[str]]], bool, str, str, float | None]:
        return await self._classification_strategy.execute(
            task=task,
            user_text=user_text,
            language=language,
            span_collector=span_collector,
            pre_classified=pre_classified,
            routing_skip=routing_skip,
            compound_bypass=compound_bypass,
            extended_metadata=extended_metadata,
            classify_reason=classify_reason,
            allow_classify_cache_lookup=allow_classify_cache_lookup,
        )

    # ------------------------------------------------------------------
    # Phase 2: dispatch (non-streaming only)
    # ------------------------------------------------------------------

    async def run_dispatch(
        self,
        task: AgentTask,
        classifications: list[tuple[str, str, float | None, list[str]]],
        user_text: str,
        conversation_id: str,
        turns: list[dict[str, Any]],
        span_collector,
        language: str,
        incoming_context,
    ) -> DispatchResult:
        return await self._dispatch_strategy.execute(
            task=task,
            classifications=classifications,
            user_text=user_text,
            conversation_id=conversation_id,
            turns=turns,
            span_collector=span_collector,
            language=language,
            incoming_context=incoming_context,
        )

    # ------------------------------------------------------------------
    # Phase 3: finalisation (non-streaming only)
    # ------------------------------------------------------------------

    async def run_finalization(
        self,
        task: AgentTask,
        dispatch_result: DispatchResult,
        user_text: str,
        language: str,
        conversation_id: str,
        turns: list[dict[str, Any]],
        span_collector,
        classifications: list[tuple[str, str, float | None, list[str]]],
        voice_followup_requested: bool,
        used_origin_context: bool,
        confidence: float | None = None,
        condensed_task: str = "",
    ) -> dict[str, Any]:
        return await self._finalization_strategy.execute(
            task=task,
            dispatch_result=dispatch_result,
            user_text=user_text,
            language=language,
            conversation_id=conversation_id,
            turns=turns,
            span_collector=span_collector,
            classifications=classifications,
            voice_followup_requested=voice_followup_requested,
            used_origin_context=used_origin_context,
            confidence=confidence,
            condensed_task=condensed_task,
        )
