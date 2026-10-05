"""Rewrite agent for cached response phrasing variation."""

from __future__ import annotations

import logging

from app.agents.base import BaseAgent, language_code_to_name
from app.agents.decorator import agent
from app.agents.sanitize import strip_parenthetical_asides
from app.db.repository import SettingsRepository
from app.models.agent import AgentCard, DispatchTask, TaskResult

logger = logging.getLogger(__name__)


@agent(
    agent_id="rewrite-agent",
    name="Rewrite Agent",
    description="Rephrases cached responses to vary wording while preserving meaning.",
    skills=["rewrite"],
    expected_latency="low",
    timeout_sec=None,
    needs_entity_matcher=False,
)
class RewriteAgent(BaseAgent):
    """Rephrases cached responses for natural variation.

    Internal-use only. NOT registered as a routable agent in the orchestrator.
    Uses Groq for fast LLM calls (target < 100ms latency).
    """

    @property
    def agent_card(self) -> AgentCard:
        """Return the agent card for the rewrite agent."""
        return AgentCard(
            agent_id="rewrite-agent",
            name="Rewrite Agent",
            description="Rephrases cached responses to vary wording while preserving meaning.",
            skills=["rewrite"],
            endpoint="local://rewrite-agent",
            expected_latency="low",
            timeout_sec=None,
        )

    async def rewrite(
        self, cached_text: str, language: str = "en", user_text: str | None = None, reminder_text: str | None = None
    ) -> str | None:
        """Rephrase a cached response and apply personality. Returns the rewritten text.

        Returns None when the LLM call raises, returns nothing, or yields only
        text that sanitization strips away; the caller owns the fallback speech.
        Uses the unmediated (raw) agent response as input so personality
        and rewrite variation are applied in a single LLM call.
        If reminder_text is given the LLM weaves it naturally into the output.
        """
        system_prompt = await self._load_prompt_async("rewrite")
        try:
            personality = await SettingsRepository.get_value("personality.prompt", "")
        except Exception:
            personality = ""
        personality_text = personality.strip() if personality else ""
        system_prompt = system_prompt.replace("{personality}", personality_text)
        system_prompt = system_prompt.replace("{language}", language_code_to_name(language)).strip()
        if user_text:
            user_content = f"User asked:\n{self._wrap_user_input(user_text)}\nAgent responded: {cached_text}"
        else:
            user_content = f"Agent responded: {cached_text}"
        if reminder_text:
            user_content += f"\nReminder to weave in: {reminder_text}"
        user_content += "\n\nRephrase:"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            result = await self._call_llm(messages)
        except Exception:
            logger.warning("Rewrite LLM call failed, no rewrite produced", exc_info=True)
            return None
        if not result:
            logger.warning("Rewrite LLM returned empty, no rewrite produced")
            return None
        rewritten = strip_parenthetical_asides(result)
        if not rewritten or not rewritten.strip():
            logger.warning("Rewrite output was empty after sanitization, no rewrite produced")
            return None
        return rewritten

    async def handle_task(self, task: DispatchTask) -> TaskResult:
        """A2A-compatible interface. Rewrites task.description; falls back to it when the rewrite fails."""
        result = await self.rewrite(task.description)
        return TaskResult(
            speech=result or task.description,
            action_executed=None,
            error=None,
            voice_followup=False,
            directive=None,
            reason=None,
        )
