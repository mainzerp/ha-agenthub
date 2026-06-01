"""Intent classification engine extracted from OrchestratorAgent.

Handles LLM-based intent classification, classification parsing,
agent description building, and classification repair.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import litellm

from app.agents.agent_registry import CachedAgentRegistry
from app.analytics.tracer import _optional_span
from app.cache.cache_manager import CacheManager
from app.llm.client import LLMError

logger = logging.getLogger(__name__)

_FALLBACK_AGENT = "general-agent"
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

    seen: set[str] = set()
    ordered: list[str] = []
    for seg in text_segments:
        if seg not in seen:
            seen.add(seg)
            ordered.append(seg)

    if ordered:
        cleaned = ordered[0]
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


class ClassificationEngine:
    """LLM-based intent classification and description building."""

    def __init__(
        self,
        agent_registry: CachedAgentRegistry,
        cache_manager: CacheManager | None = None,
        call_llm: Callable[..., Awaitable[str]] | None = None,
        load_prompt_async: Callable[[str], Awaitable[str]] | None = None,
        get_turns: Callable[[str | None], Awaitable[list[dict[str, Any]]]] | None = None,
        wrap_user_input: Callable[[str], str] | None = None,
        append_conversation_turn_messages: Callable[[list[dict], list[dict], Any], None] | None = None,
    ) -> None:
        self._agent_registry = agent_registry
        self._cache_manager = cache_manager
        self._call_llm = call_llm
        self._load_prompt_async = load_prompt_async
        self._get_turns = get_turns
        self._wrap_user_input = wrap_user_input or (lambda x: x)
        self._append_conversation_turn_messages = append_conversation_turn_messages or (lambda msgs, turns, **kw: None)

    async def _get_known_agents(self) -> set[str]:
        return await self._agent_registry.get_known_agents()

    @property
    def agent_registry(self) -> CachedAgentRegistry:
        return self._agent_registry

    @staticmethod
    def cancel_interaction_description_line() -> str:
        return (
            "- cancel-interaction: User dismisses or aborts ONLY the current voice/chat turn "
            "(nevermind, forget it, scratch that, no thanks, stop as in stop talking, "
            "German e.g. abbrechen/egal/schon gut when meaning dismiss—not device control). "
            "NOT for canceling timers, alarms, or media playback—route those to timer-agent, "
            "music-agent, etc."
        )

    async def build_agent_descriptions(self) -> str:
        """Build agent list for classification prompt from registered AgentCards."""
        cancel_line = self.cancel_interaction_description_line()
        if self._agent_registry is not None:
            cards = await self._agent_registry.list_agents()
        else:
            cards = []
        if not cards:
            return "- general-agent: fallback for general questions and unroutable requests\n" + cancel_line

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

    @staticmethod
    def strip_seq_rule(prompt: str) -> str:
        """Remove the sequential dispatch rule block when send-agent is unavailable."""
        start_marker = "Sequential dispatch rule:"
        end_marker = "Format:"
        start_idx = prompt.find(start_marker)
        end_idx = prompt.find(end_marker)
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            return prompt[:start_idx] + prompt[end_idx:]
        return prompt

    async def classify(
        self,
        user_text: str,
        *,
        cache_result=None,
        conversation_id: str | None = None,
        span_collector=None,
        language: str = "en",
        allow_cache_lookup: bool = True,
        call_llm: Callable[..., Awaitable[str]] | None = None,
        load_prompt_async: Callable[[str], Awaitable[str]] | None = None,
        get_turns: Callable[[str | None], Awaitable[list[dict[str, Any]]]] | None = None,
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
        _call_llm = call_llm or self._call_llm
        _load_prompt = load_prompt_async or self._load_prompt_async
        _get_turns_fn = get_turns or self._get_turns
        await self._get_known_agents()

        if cache_result is not None:
            if cache_result.hit_type == "routing_hit" and cache_result.agent_id:
                if cache_result.agent_id == "send-agent" or cache_result.agent_id in _INTERNAL_ONLY_AGENTS:
                    logger.debug(
                        "Ignoring invalid routing cache hit: %s for '%s'", cache_result.agent_id, user_text[:80]
                    )
                else:
                    logger.debug("Routing cache hit: %s for '%s'", cache_result.agent_id, user_text[:80])
                    condensed = user_text
                    return [(cache_result.agent_id, condensed, 1.0)], True
        elif allow_cache_lookup and self._cache_manager:
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
                        logger.debug("Routing cache hit: %s for '%s'", cache_result.agent_id, user_text[:80])
                        condensed = user_text
                        return [(cache_result.agent_id, condensed, 1.0)], True
            except Exception:
                logger.warning("Routing cache check failed, proceeding with LLM", exc_info=True)

        if _load_prompt is None or _call_llm is None:
            return [(_FALLBACK_AGENT, user_text, 0.0)], False

        system_prompt_template = await _load_prompt("orchestrator")
        agent_descriptions = await self.build_agent_descriptions()
        lang = (language or "").strip().lower()
        if lang and lang != "en":
            language_hint = (
                f"User language hint: the user message is in '{lang}'. Write the condensed task in '{lang}'."
            )
        else:
            language_hint = ""
        system_prompt = system_prompt_template.replace("{agent_descriptions}", agent_descriptions).replace(
            "{language_hint}", language_hint
        )
        if "send-agent" not in agent_descriptions:
            system_prompt = self.strip_seq_rule(system_prompt)
        messages = [
            {"role": "system", "content": system_prompt},
        ]

        turns = []
        if _get_turns_fn is not None:
            turns = await _get_turns_fn(conversation_id)
        previous_agent_hint = ""
        if turns:
            for turn in reversed(turns):
                if turn.get("role") == "assistant":
                    agent_id = turn.get("agent_id")
                    if agent_id:
                        previous_agent_hint = (
                            f"The previous turn was handled by {agent_id}. "
                            "Route follow-ups to the same agent unless the user clearly changes subject."
                        )
                    break
        messages[0]["content"] = messages[0]["content"].replace("{previous_agent_hint}", previous_agent_hint)
        if turns:
            self._append_conversation_turn_messages(messages, turns, max_content_length=300)
        messages.append({"role": "user", "content": self._wrap_user_input(user_text)})

        try:
            async with _optional_span(span_collector, "llm_call", agent_id="orchestrator") as llm_span:
                response = await _call_llm(messages, span_collector=span_collector)
                llm_span["metadata"]["model"] = "orchestrator"
                llm_span["metadata"]["routing_cached"] = False
            logger.debug("Classification LLM response for '%s': %s", user_text[:60], repr(response[:300]))
            classifications = await self.parse_classification(response, user_text)
            classifications = await self.sanitize_or_repair_classifications(
                classifications,
                user_text=user_text,
                conversation_id=conversation_id,
                span_collector=span_collector,
                language=language,
            )
            if self._cache_manager and len(classifications) == 1:
                target_agent, condensed, confidence = classifications[0]
                if (
                    target_agent not in (_FALLBACK_AGENT, _CANCEL_INTERACTION_AGENT, "send-agent")
                    and confidence is not None
                ):
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
        except (
            ValueError,
            json.JSONDecodeError,
            LLMError,
            litellm.exceptions.APIError,
            litellm.exceptions.AuthenticationError,
        ) as exc:
            logger.error(
                "Intent classification failed (%s), falling back to %s",
                type(exc).__name__,
                _FALLBACK_AGENT,
                exc_info=True,
            )
            return [(_FALLBACK_AGENT, user_text, 0.0)], False
        except Exception:
            logger.exception("Intent classification failed, falling back to %s", _FALLBACK_AGENT)
            return [(_FALLBACK_AGENT, user_text, 0.0)], False

    async def parse_classification(self, response: str, original_text: str) -> list[tuple[str, str, float | None]]:
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

        if known_agents:
            agent_alt = "|".join(re.escape(a) for a in sorted(known_agents, key=len, reverse=True))
            fragment_re: re.Pattern[str] | None = re.compile(
                rf"({agent_alt})\s*(?:\(\s*\d+\s*%?\s*\))?\s*:\s*",
                re.IGNORECASE,
            )
        else:
            fragment_re = None

        lines = [line.strip() for line in response.split("\n") if line.strip()]
        for line in lines:
            line = line.lstrip()
            line = line.removeprefix("[SEQ]").strip()
            confidence: float | None
            match = re.match(r"^([\w-]+)\s*\((\d+)%?\)\s*:\s*(.+)$", line, re.DOTALL)
            if match:
                agent_id = match.group(1).strip().lower()
                confidence = min(float(match.group(2)) / 100.0, 1.0)
                condensed = match.group(3).strip()
                condensed = _sanitize_condensed(condensed, fragment_re, line)
            else:
                if ":" not in line:
                    logger.warning("Could not parse classification line: %s", line[:100])
                    continue
                agent_id, _, condensed = line.partition(":")
                agent_id = agent_id.strip().lower()
                condensed = condensed.strip()
                condensed = _sanitize_condensed(condensed, fragment_re, line)
                confidence = None

            if agent_id not in known_agents:
                logger.warning("Unknown agent '%s' in classification, skipping line", agent_id)
                continue

            if not condensed:
                condensed = original_text

            results.append((agent_id, condensed, confidence))

        if not results:
            return [(_FALLBACK_AGENT, original_text, 0.0)]

        seen: dict[str, tuple[str, float | None, list[str]]] = {}
        for agent_id, condensed, confidence in results:
            if agent_id in seen:
                existing_condensed, existing_conf, tasks = seen[agent_id]
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

        deduped.sort(key=lambda x: x[2] if x[2] is not None else -1.0, reverse=True)
        return deduped[:3]

    async def repair_send_agent_classifications(
        self,
        user_text: str,
        *,
        conversation_id: str | None,
        span_collector=None,
        language: str = "en",
    ) -> list[tuple[str, str, float | None]]:
        if self._load_prompt_async is None or self._call_llm is None:
            raise _RecoverableClassificationError("I couldn't determine what content to deliver.")

        system_prompt_template = await self._load_prompt_async("orchestrator")
        agent_descriptions = await self.build_agent_descriptions()
        lang = (language or "").strip().lower()
        if lang and lang != "en":
            language_hint = (
                f"User language hint: the user message is in '{lang}'. Write the condensed task in '{lang}'."
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
        turns = []
        if self._get_turns is not None:
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
        return await self.parse_classification(response, user_text)

    async def sanitize_or_repair_classifications(
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
            repaired = await self.repair_send_agent_classifications(
                user_text,
                conversation_id=conversation_id,
                span_collector=span_collector,
                language=language,
            )
            return await self.sanitize_or_repair_classifications(
                repaired,
                user_text=user_text,
                conversation_id=conversation_id,
                span_collector=span_collector,
                language=language,
                allow_repair=False,
                require_send_partner=True,
            )

        raise _RecoverableClassificationError("I couldn't determine what content to deliver.")
