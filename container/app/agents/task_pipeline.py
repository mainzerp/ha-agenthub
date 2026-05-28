"""Shared task pipeline phases extracted from OrchestratorAgent.

This mixin provides the four high-level pipeline phases so that both
``_handle_task_impl`` and ``_handle_task_stream_impl`` can reuse them
without duplicating the ~150 lines of shared prelude / finalisation
choreography.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.compound_utterance import looks_compound
from app.agents.sanitize import strip_markdown
from app.models.agent import AgentTask


@dataclass
class DispatchResult:
    """Result bag produced by the dispatch phase."""

    classifications: list[tuple[str, str, float | None]]
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


class TaskPipeline:
    """Mixin - expects ``self`` to be an OrchestratorAgent (or compatible)."""

    # ------------------------------------------------------------------
    # Phase 0: cache replay (before classification)
    # ------------------------------------------------------------------

    async def _run_cache_replay(
        self,
        task: AgentTask,
        user_text: str,
        language: str,
        span_collector,
        *,
        skip_lookup: bool = False,
    ) -> tuple[Any | None, Any | None, bool]:
        """Try action-cache replay and routing-cache skip.

        Returns ``(action_replay, routing_skip, compound_bypass)``.
        When ``action_replay`` is not ``None`` the caller should short-
        circuit and return the replay result directly.

        Context-dependent (conditional) tasks bypass the action cache
        because they are marked ``cacheable=False`` by the executor.
        """
        compound_bypass = False
        action_replay = None
        routing_skip = None

        if not skip_lookup and self._cache_manager:  # type: ignore[attr-defined]
            if await self._get_bool_setting("cache.compound_utterance_bypass", True) and looks_compound(user_text):  # type: ignore[attr-defined]
                compound_bypass = True
                import logging

                logger = logging.getLogger(__name__)
                logger.debug("Skipping cache lookup for structurally compound utterance: %r", user_text[:80])
            else:
                action_replay, routing_skip = await self._try_cache_replay(  # type: ignore[attr-defined]
                    task=task,
                    user_text=user_text,
                    language=language,
                    span_collector=span_collector,
                )

        return action_replay, routing_skip, compound_bypass

    # ------------------------------------------------------------------
    # Phase 1: intent classification
    # ------------------------------------------------------------------

    async def _run_classification(
        self,
        task: AgentTask,
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
    ) -> tuple[list[tuple[str, str, float | None]], bool, str, str, float | None]:
        """Classify intent and return the canonical 5-tuple.

        Returns
            (classifications, routing_cached, target_agent, condensed_task, confidence)

        Raises
            _RecoverableClassificationError on unrecoverable parse failures.
        """
        from app.analytics.tracer import _optional_span

        next_classify_extra: dict[str, object] = {}
        synthetic_preclassified = False

        if compound_bypass:
            next_classify_extra["compound_bypass"] = True
            next_classify_extra["compound_bypass_reason"] = "multi_sentence"
        if routing_skip is not None:
            pre_classified = (self._build_synthetic_classifications(routing_skip), True)  # type: ignore[attr-defined]
            synthetic_preclassified = True
            next_classify_extra["reason"] = "routing_cache_skip"
        if classify_reason:
            next_classify_extra["reason"] = classify_reason

        if pre_classified is not None:
            classifications, routing_cached = pre_classified
            target_agent, condensed_task, confidence = classifications[0]
            if synthetic_preclassified:
                async with _optional_span(span_collector, "classify", agent_id="orchestrator") as span:
                    self._pipeline_record_classify_span(  # type: ignore[attr-defined]
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
            try:
                async with _optional_span(span_collector, "classify", agent_id="orchestrator") as span:
                    classifications, routing_cached = await self._classify(  # type: ignore[attr-defined]
                        user_text,
                        cache_result=None,
                        conversation_id=task.conversation_id,
                        span_collector=span_collector,
                        language=language,
                        allow_cache_lookup=allow_classify_cache_lookup,
                    )
                    target_agent, condensed_task, confidence = classifications[0]
                    self._pipeline_record_classify_span(  # type: ignore[attr-defined]
                        span,
                        classifications,
                        user_text,
                        condensed_task,
                        confidence,
                        routing_cached,
                        extended_metadata=extended_metadata,
                        extra_metadata=next_classify_extra or None,
                    )
            except Exception:
                # _classify may raise _RecoverableClassificationError - let it propagate
                raise

        return classifications, routing_cached, target_agent, condensed_task, confidence

    # ------------------------------------------------------------------
    # Phase 2: dispatch (non-streaming only)
    # ------------------------------------------------------------------

    async def _run_dispatch(
        self,
        task: AgentTask,
        classifications: list[tuple[str, str, float | None]],
        user_text: str,
        conversation_id: str,
        turns: list[dict[str, Any]],
        span_collector,
        language: str,
        incoming_context,
    ) -> DispatchResult:
        """Dispatch to one or more agents and return a :class:`DispatchResult`.

        Handles sequential-send, single-agent, and multi-agent dispatch.
        """
        import logging

        logger = logging.getLogger(__name__)

        is_sequential_send = any(a == "send-agent" for a, _, _ in classifications) and any(
            a != "send-agent" for a, _, _ in classifications
        )

        failed_agents: list[tuple[str, str]] = []
        agent_error = None
        agent_voice_followup = False
        directive = None
        directive_reason = None

        if is_sequential_send:
            routed_to, speech, result = await self._handle_sequential_send(  # type: ignore[attr-defined]
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
            agent_id, speech, result = await self._dispatch_single(  # type: ignore[attr-defined]
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

        dispatch_coros = [
            self._dispatch_single(  # type: ignore[attr-defined]
                aid,
                ctask,
                user_text,
                conversation_id,
                turns,
                span_collector,
                incoming_context=incoming_context,
                resolved_language=language,
            )
            for aid, ctask, _ in classifications
        ]
        dispatch_results = await asyncio.gather(*dispatch_coros, return_exceptions=True)

        agent_responses: list[tuple[str, str, bool]] = []
        action_executed = None
        routed_agents: list[str] = []
        for idx, dr in enumerate(dispatch_results):
            agent_id_for_idx = classifications[idx][0]
            if isinstance(dr, Exception):
                logger.warning("Multi-agent dispatch error for %s: %s", agent_id_for_idx, dr)
                failed_agents.append((agent_id_for_idx, str(dr)))
                continue
            aid, sp, res = dr  # type: ignore[misc]
            res_dict = res or {}
            res_error = res_dict.get("error") if isinstance(res_dict, dict) else None
            if (
                res is None
                or res_error
                or sp
                in (
                    "I couldn't process that request in time.",
                    "I couldn't process that request right now.",
                )
            ):
                if res_error:
                    reason = res_error.get("code", "canned_error") if isinstance(res_error, dict) else "canned_error"
                elif res is None:
                    reason = "timeout"
                else:
                    reason = "canned_speech"
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
        has_error = len(failed_agents) > 0

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

    # ------------------------------------------------------------------
    # Phase 3: finalisation (non-streaming only)
    # ------------------------------------------------------------------

    async def _run_finalization(
        self,
        task: AgentTask,
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
    ) -> dict[str, Any]:
        """Run finalisation: merge, mediate, store turn, trace."""
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
            # Multi-agent finalization (inline; merge step has no streaming counterpart).
            original_speech = speech
            from app.analytics.tracer import _optional_span

            async with _optional_span(span_collector, "return", agent_id="orchestrator") as ret_span:
                ret_span["metadata"]["from_agent"] = routed_to
                if not agent_responses and failed_agents:
                    speech = "I'm sorry, I couldn't complete that request. All agents encountered errors."
                else:
                    # Fetch calendar reminder before merge so the LLM can weave it in naturally
                    reminder_text = None
                    if self._calendar_injector is not None and not has_error:  # type: ignore[attr-defined]
                        try:
                            reminder_text = await self._calendar_injector.inject_reminders(  # type: ignore[attr-defined]
                                utterance=task.description if task else None,
                                device_id=task.context.device_id if task.context else None,
                                area_id=task.context.area_id if task.context else None,
                                user_id=task.context.user_id if task.context else None,
                                language=language or "en",
                            )
                        except Exception:
                            import logging

                            logger = logging.getLogger(__name__)
                            logger.debug("Calendar reminder injection failed", exc_info=True)

                    speech = await self._merge_responses(  # type: ignore[attr-defined]
                        agent_responses, user_text, span_collector=span_collector, reminder_text=reminder_text
                    )
                    if failed_agents:
                        failed_names = ", ".join(aid for aid, _ in failed_agents)
                        speech += f"\n\n(Note: {failed_names} could not be reached.)"

                ret_span["metadata"]["agent_response"] = speech
                speech, voice_followup_effective = await self._merge_voice_followup_and_organic(  # type: ignore[attr-defined]
                    speech,
                    agent_requested=agent_voice_followup,
                    ctx=task.context if task else None,
                    language=language,
                    has_error=has_error,
                    target_agent=target_agent,
                    mediated_followup=False,
                )
                ret_span["metadata"]["final_response"] = speech
                ret_span["metadata"]["mediated"] = (speech != original_speech) or len(classifications) > 1
                ret_span["metadata"]["voice_followup"] = voice_followup_effective
                ret_span["metadata"]["cache_stored_response"] = False
                ret_span["metadata"]["cache_stored_routing"] = False
                await self._store_turn(conversation_id, user_text, speech, agent_id=routed_to)  # type: ignore[attr-defined]
                if span_collector:
                    await self._create_trace(  # type: ignore[attr-defined]
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
            speech, voice_followup_effective = await self._finalize_single_agent_response(  # type: ignore[attr-defined]
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
                skip_mediation_on_error=True,
                skip_response_cache=is_sequential_send,
                used_origin_context=used_origin_context,
            )

        response = {
            "speech": strip_markdown(speech),
            "routed_to": routed_to,
            "action_executed": action_executed,
            "voice_followup": voice_followup_effective,
            "sanitized": True,
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
        return response
