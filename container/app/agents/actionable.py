"""Base class for agents that parse LLM output into HA actions."""

from __future__ import annotations

import asyncio
import logging
import re

from app.agents.action_executor import parse_action
from app.agents.base import BaseAgent
from app.models.agent import ActionExecuted, AgentError, AgentErrorCode, AgentTask, TaskResult

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

    def __init__(self, ha_client=None, entity_index=None, entity_matcher=None) -> None:
        super().__init__(ha_client=ha_client, entity_index=entity_index)
        self._entity_matcher = entity_matcher

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        """Execute the parsed action. Subclasses must override."""
        raise NotImplementedError

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
        except Exception:
            logger.exception("LLM call failed for %s", agent_id)
            return self._error_result(
                AgentErrorCode.LLM_ERROR,
                "The language model could not complete this request. Please try again.",
            )

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
                metadata = result.get("metadata") or {}
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
