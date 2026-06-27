"""Mediation service extracted from OrchestratorAgent.

Owns the response-mediation logic: the blocking ``mediate_response``,
the streaming ``mediate_response_stream`` (a near-duplicate of the blocking
variant that yields tokens instead of returning a tuple), the multi-agent
``merge_responses`` and the ``format_fallback`` helper.

Behaviour is identical to the pre-extraction code on
:class:`~app.agents.orchestrator.OrchestratorAgent`. The service holds a
back-reference to its owning orchestrator and resolves every collaborator
(``_get_personality_cached``, ``_load_prompt_async``, ``_call_llm``,
``_call_llm_stream``, ``_wrap_user_input`` and the ``_mediation_*`` overrides)
through it at call time. This deliberately preserves the
``patch.object(orch, "_get_personality_cached")`` / ``patch.object(orch,
"_call_llm_stream")`` seams exercised by the test-suite: because lookup happens
on the orchestrator instance at call time, instance-attribute mocks still take
effect.

The personality cache (``_get_personality_cached``) and
``_prepare_mediation_inputs`` remain on the orchestrator: the cache state lives
on the orchestrator instance (tests set/inspect ``orch._personality_cache_ts``
and ``orch._personality_cache_value``) and both helpers read the orchestrator's
``SettingsRepository`` (patched as ``app.agents.orchestrator.SettingsRepository``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

from app.agents.sanitize import strip_parenthetical_asides
from app.analytics.tracer import _optional_span

logger = logging.getLogger(__name__)


class MediationService:
    """Applies personality / reminders to domain-agent responses."""

    def __init__(self, orch) -> None:
        self._orch = orch

    # ------------------------------------------------------------------
    # Multi-agent merge
    # ------------------------------------------------------------------

    async def merge_responses(
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
            personality = await self._orch._get_personality_cached()

            system_content = await self._orch._load_prompt_async("merge")
            personality_text = personality.strip() if personality and personality.strip() else ""
            system_content = system_content.replace("{personality}", personality_text).strip()

            user_content = (
                f"User asked:\n{self._orch._wrap_user_input(user_text)}\n\nAgent responses:\n{agent_summary}\n\n"
            )
            if reminder_text:
                user_content += f"Reminder to weave in: {reminder_text}\n\n"
            user_content += "Combine into one natural response:"

            messages = [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ]

            overrides: dict[str, Any] = {
                "temperature": self._orch._mediation_temperature,
                "max_tokens": self._orch._mediation_max_tokens,
            }
            if self._orch._mediation_model:
                overrides["model"] = self._orch._mediation_model
            result = await self._orch._call_llm(messages, span_collector=span_collector, **overrides)
            return result.strip() if result and result.strip() else self.format_fallback(agent_responses)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Multi-agent response merge failed, using fallback format", exc_info=True)
            fallback = self.format_fallback(agent_responses)
            if reminder_text:
                separator = " " if fallback and fallback[-1] in ".!?" else ". "
                return f"{fallback}{separator}{reminder_text}" if fallback else reminder_text
            return fallback

    @staticmethod
    def format_fallback(agent_responses: list[tuple[str, str, bool]]) -> str:
        """Fallback formatting when LLM merge fails."""
        parts = [f"[{aid}] {sp}" for aid, sp, _ in agent_responses if sp and sp.strip()]
        return "\n\n".join(parts) if parts else "I couldn't process that request."

    # ------------------------------------------------------------------
    # Single-agent mediation
    # ------------------------------------------------------------------

    async def mediate_response(
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

        When personality.prompt is non-empty, passes the agent speech through
        a lightweight LLM call to apply the configured personality.
        If reminder_text is given, the LLM weaves it in naturally.
        Falls back to the original speech (+ appended reminder) on any failure.

        Returns:
            Tuple of (mediated_speech, followup_needed).
        """
        personality = await self._orch._get_personality_cached()
        if not personality.strip():
            if reminder_text:
                separator = " " if agent_speech and agent_speech[-1] in ".!?" else ". "
                return (f"{agent_speech}{separator}{reminder_text}" if agent_speech else reminder_text), False
            return agent_speech, False

        if not agent_speech or not agent_speech.strip():
            return agent_speech, False

        try:
            system_prompt = await self._orch._load_prompt_async("mediate")
            personality_text = personality.strip() if personality and personality.strip() else ""
            system_prompt = system_prompt.replace("{personality}", personality_text)
            system_prompt = system_prompt.replace("{language}", language or "en")
            system_prompt = system_prompt.replace(
                "{organic_followup_hint}",
                "You may add a natural follow-up question at the end. Append [FOLLOWUP] if you do."
                if allow_organic_followup
                else "Do not add any follow-up questions.",
            ).strip()
            user_content = (
                f"User asked:\n{self._orch._wrap_user_input(user_text)}\nAgent ({agent_id}) responded: {agent_speech}"
            )
            if reminder_text:
                user_content += f"\nReminder to weave in: {reminder_text}"
            user_content += f"\n\nRephrase in {language}:"
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            overrides: dict[str, Any] = {
                "temperature": self._orch._mediation_temperature,
                "max_tokens": self._orch._mediation_max_tokens,
            }
            if self._orch._mediation_model:
                overrides["model"] = self._orch._mediation_model
            async with _optional_span(span_collector, "mediation", agent_id="orchestrator") as span:
                result = await self._orch._call_llm(messages, span_collector=span_collector, **overrides)
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
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Response mediation failed, using original speech", exc_info=True)
            if reminder_text:
                separator = " " if agent_speech and agent_speech[-1] in ".!?" else ". "
                return (f"{agent_speech}{separator}{reminder_text}" if agent_speech else reminder_text), False
            return agent_speech, False

    async def mediate_response_stream(
        self,
        agent_speech: str,
        user_text: str,
        agent_id: str,
        language: str = "en",
        span_collector=None,
        reminder_text: str | None = None,
        allow_organic_followup: bool = False,
    ) -> AsyncGenerator[str, None]:
        """Streaming variant of mediate_response.

        Yields mediated tokens as the LLM generates them.
        The caller must collect tokens and run post-processing
        (strip_parenthetical_asides, [FOLLOWUP] detection) on the
        complete text. This method does NOT return the followup flag.
        """
        personality = await self._orch._get_personality_cached()
        if not personality.strip():
            # No personality -- nothing to stream mediate.
            # If a reminder exists, yield it as the only token so the caller
            # can append it to the speech (matching the blocking path behaviour).
            if reminder_text:
                yield reminder_text
            return

        if not agent_speech or not agent_speech.strip():
            return

        try:
            system_prompt = await self._orch._load_prompt_async("mediate")
            personality_text = personality.strip() if personality and personality.strip() else ""
            system_prompt = system_prompt.replace("{personality}", personality_text)
            system_prompt = system_prompt.replace("{language}", language or "en")
            system_prompt = system_prompt.replace(
                "{organic_followup_hint}",
                "You may add a natural follow-up question at the end. Append [FOLLOWUP] if you do."
                if allow_organic_followup
                else "Do not add any follow-up questions.",
            ).strip()
            user_content = (
                f"User asked:\n{self._orch._wrap_user_input(user_text)}\nAgent ({agent_id}) responded: {agent_speech}"
            )
            if reminder_text:
                user_content += f"\nReminder to weave in: {reminder_text}"
            user_content += f"\n\nRephrase in {language}:"
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            overrides: dict[str, Any] = {
                "temperature": self._orch._mediation_temperature,
                "max_tokens": self._orch._mediation_max_tokens,
            }
            if self._orch._mediation_model:
                overrides["model"] = self._orch._mediation_model
            async with _optional_span(span_collector, "mediation", agent_id="orchestrator") as span:
                span["metadata"]["personality_active"] = True
                span["metadata"]["language"] = language or "en"
                span["metadata"]["original_length"] = len(agent_speech)
                span["metadata"]["streamed"] = True
                async for token in self._orch._call_llm_stream(messages, span_collector=span_collector, **overrides):
                    if token:
                        yield token
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Response mediation stream failed, caller should fall back to _mediate_response", exc_info=True
            )
            return
