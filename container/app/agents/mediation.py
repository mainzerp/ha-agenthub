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
from contextlib import aclosing
from typing import Any

from app.agents.base import language_code_to_name
from app.agents.sanitize import _remove_asides, strip_markdown_markers, strip_parenthetical_asides
from app.analytics.tracer import _optional_span

logger = logging.getLogger(__name__)

_FOLLOWUP_TAG = "[FOLLOWUP]"

# English system lines; ``MediationService.localize_message`` renders them
# in the turn language (English text is the fallback when that LLM fails).
_NOTHING_PROCESSED_SPEECH = "I couldn't process that request."
_ALL_AGENTS_FAILED_SPEECH = "I'm sorry, I couldn't complete that request. All agents encountered errors."

# Upper bound for the localization LLM call: it runs on error paths that
# already waited for a failed agent, so it must not add a long stall.
_LOCALIZE_TIMEOUT_SEC = 6.0
_LOCALIZE_MAX_TOKENS = 256


def is_english_language(language: str | None) -> bool:
    """True for English or an unknown/empty language code (no localization)."""
    primary = (language or "en").strip().lower().split("-", 1)[0]
    return primary in ("", "en")


class MediationStreamError(Exception):
    """Raised when the mediation LLM stream fails (M-10).

    The orchestrator distinguishes zero-token failures (fall back to the
    blocking mediation path) from mid-stream failures (keep the spoken
    partial output but persist the original full speech).
    """


def _strip_followup_tag(text: str | None) -> tuple[str | None, bool]:
    """Strip a trailing [FOLLOWUP] tag; report whether one was present.

    Non-string input is returned unchanged with followup=False, matching the
    previous inline ``isinstance(mediated, str)`` guard used at the streaming
    post-process site. Trailing whitespace after the tag is tolerated.
    """
    if not isinstance(text, str):
        return text, False
    stripped = text.rstrip()
    if stripped.endswith(_FOLLOWUP_TAG):
        return stripped[: -len(_FOLLOWUP_TAG)].rstrip(), True
    return text, False


class StreamedSpeechFilter:
    """Incrementally emit mediated tokens with the same aside/[FOLLOWUP]
    cleanup as the collected text, plus TTS-safe Markdown marker removal
    (the terminal ``mediated_speech`` is Markdown-stripped as well)."""

    def __init__(self) -> None:
        self._raw = ""
        self._emitted_len = 0
        self._emitted_chars = 0

    @property
    def emitted_chars(self) -> int:
        """Number of characters actually returned for emission so far."""
        return self._emitted_chars

    def _take(self, text: str) -> str:
        # Leading whitespace is never the first thing emitted: a
        # whitespace-only token frame would count as streamed output.
        if not self._emitted_chars:
            text = text.lstrip()
        self._emitted_chars += len(text)
        return text

    def feed(self, token: str) -> str:
        """Buffer ``token``; return the text now safe to emit ("" if none).

        Text past the first ``(`` that no ``)`` closes yet is held back: it
        may still turn out to be an aside. The trailing ``len([FOLLOWUP])``
        chars of cleaned text are held back as well so a tag fragment or an
        undecided Markdown marker never leaks into a token frame.
        """
        self._raw += token
        last_close = self._raw.rfind(")")
        open_idx = self._raw.find("(", last_close + 1)
        stable = self._raw if open_idx == -1 else self._raw[:open_idx]
        cleaned = strip_markdown_markers(_remove_asides(stable))
        safe_end = len(cleaned.rstrip()) - len(_FOLLOWUP_TAG)
        if safe_end > self._emitted_len:
            emit = cleaned[self._emitted_len : safe_end]
            self._emitted_len = safe_end
            return self._take(emit)
        return ""

    def finish(self) -> tuple[str, bool]:
        """Flush the remaining emit-able tail; report [FOLLOWUP] presence.

        An unclosed ``(`` is not an aside (the cleanup needs a closing
        ``)``), so its text is emitted here, matching the collected-text
        path.
        """
        # Markers first, so a wrapped tag ("**[FOLLOWUP]**") is detected.
        stripped, followup = _strip_followup_tag(strip_markdown_markers(_remove_asides(self._raw)))
        tail = stripped[self._emitted_len :] if len(stripped) > self._emitted_len else ""
        self._emitted_len = max(self._emitted_len, len(stripped))
        return self._take(tail), followup


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
        failed_agents: list[str] | None = None,
        *,
        language: str | None = None,
        skipped_tasks: list[str] | None = None,
    ) -> tuple[str, bool]:
        """Merge multiple agent responses into a single natural answer via LLM.

        Always calls LLM regardless of personality settings.
        Includes personality prompt if configured.
        If reminder_text is given, the LLM weaves it in naturally.
        If failed_agents is given, the LLM briefly notes the unreachable
        agents in the user's language (replaces the old hardcoded English
        suffix; the LLM-free ``format_fallback`` stays note-less).
        If skipped_tasks is given (intents over the per-turn dispatch cap),
        the LLM tells the user those parts were not executed.
        System lines without agent output (nothing answered, all agents
        failed) are localized into ``language`` via :meth:`localize_message`.
        Falls back to bracket-prefixed format on failure.
        """
        if not agent_responses:
            canned = _ALL_AGENTS_FAILED_SPEECH if failed_agents else _NOTHING_PROCESSED_SPEECH
            return await self.localize_message(canned, language, span_collector=span_collector), False

        # Only one response and nothing failed or skipped: return it directly
        # (append reminder as fallback). When some agents failed, the
        # merge still goes through the LLM so the failure note lands in
        # the user's language.
        if len(agent_responses) == 1 and not failed_agents and not skipped_tasks:
            speech = agent_responses[0][1] or await self.localize_message(
                _NOTHING_PROCESSED_SPEECH, language, span_collector=span_collector
            )
            if reminder_text:
                separator = " " if speech and speech[-1] in ".!?" else ". "
                return (f"{speech}{separator}{reminder_text}" if speech else reminder_text), False
            return speech, False

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
            if failed_agents:
                unreachable = ", ".join(failed_agents)
                user_content += (
                    f"Unreachable agents: {unreachable}\n"
                    "Briefly note that these agents could not be reached, in the same language "
                    "as the user's question. Do not invent reasons.\n\n"
                )
            if skipped_tasks:
                user_content += (
                    "Not executed (too many requests in one message): " + "; ".join(skipped_tasks) + "\n"
                    "Briefly tell the user, in the same language as the user's question, that these parts "
                    "were not executed and can be asked again separately.\n\n"
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
            merged = result.strip() if result and result.strip() else self.format_fallback(agent_responses)
            merged, followup = _strip_followup_tag(merged)
            return merged, followup
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Multi-agent response merge failed, using fallback format", exc_info=True)
            fallback = self.format_fallback(agent_responses)
            if reminder_text:
                separator = " " if fallback and fallback[-1] in ".!?" else ". "
                return (f"{fallback}{separator}{reminder_text}" if fallback else reminder_text), False
            return fallback, False

    @staticmethod
    def format_fallback(agent_responses: list[tuple[str, str, bool]]) -> str:
        """Fallback formatting when LLM merge fails.

        LLM-free by design (the merge LLM just failed), so the empty-output
        line stays English.
        """
        parts = [f"[{aid}] {sp}" for aid, sp, _ in agent_responses if sp and sp.strip()]
        return "\n\n".join(parts) if parts else _NOTHING_PROCESSED_SPEECH

    # ------------------------------------------------------------------
    # System-line localization
    # ------------------------------------------------------------------

    async def localize_message(
        self,
        text: str,
        language: str | None,
        *,
        span_collector=None,
    ) -> str:
        """Render an orchestrator-generated English line in the turn language.

        Used for canned error/timeout/status lines that no agent produced,
        so no static translation table is needed. English (or unknown)
        languages return ``text`` unchanged without an LLM call; any LLM
        failure or timeout also returns the English ``text``.
        """
        if not text or not text.strip() or is_english_language(language):
            return text
        try:
            system_prompt = await self._orch._load_prompt_async("localize")
            system_prompt = system_prompt.replace("{language}", language_code_to_name(language)).strip()
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ]
            overrides: dict[str, Any] = {
                "temperature": self._orch._mediation_temperature,
                "max_tokens": _LOCALIZE_MAX_TOKENS,
            }
            if self._orch._mediation_model:
                overrides["model"] = self._orch._mediation_model
            async with _optional_span(span_collector, "localize", agent_id="orchestrator") as span:
                span["metadata"]["language"] = language
                result = await asyncio.wait_for(
                    self._orch._call_llm(messages, span_collector=span_collector, **overrides),
                    timeout=_LOCALIZE_TIMEOUT_SEC,
                )
            localized = strip_parenthetical_asides(result).strip() if isinstance(result, str) else ""
            return localized or text
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("System-line localization failed, using English text", exc_info=True)
            return text

    # ------------------------------------------------------------------
    # Single-agent mediation
    #
    # Invoked by the orchestrator whenever the configured personality is
    # set OR a calendar reminder must be woven into the answer -- the
    # personality applies to every system response (deterministic executor
    # confirmations included). [FOLLOWUP] detection and reminder weaving
    # live in this path.
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
            system_prompt = system_prompt.replace("{language}", language_code_to_name(language))
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
            mediated, followup = _strip_followup_tag(mediated)
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
            # No personality -- nothing to stream-mediate. The reminder-only
            # case is handled by the BLOCKING mediation path (M-9): the
            # orchestrator gates the streaming branch on a non-empty
            # personality, so the agent's answer is never replaced by a
            # lone reminder token.
            return

        if not agent_speech or not agent_speech.strip():
            return

        try:
            system_prompt = await self._orch._load_prompt_async("mediate")
            personality_text = personality.strip() if personality and personality.strip() else ""
            system_prompt = system_prompt.replace("{personality}", personality_text)
            system_prompt = system_prompt.replace("{language}", language_code_to_name(language))
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
                # aclosing: an aborted consumer (stall timeout, client gone)
                # closes the provider stream deterministically.
                async with aclosing(
                    self._orch._call_llm_stream(messages, span_collector=span_collector, **overrides)
                ) as llm_stream:
                    async for token in llm_stream:
                        if token:
                            yield token
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Response mediation stream failed, caller should fall back to _mediate_response", exc_info=True
            )
            raise MediationStreamError("mediation stream failed") from exc
