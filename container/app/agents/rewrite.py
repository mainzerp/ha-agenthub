"""Rewrite agent for cached response phrasing variation."""

from __future__ import annotations

import logging

from app.agents.base import BaseAgent
from app.db.repository import SettingsRepository
from app.models.agent import AgentCard, AgentTask, TaskResult

logger = logging.getLogger(__name__)


class RewriteAgent(BaseAgent):
    """Rephrases cached responses for natural variation.

    Internal-use only. NOT registered as a routable agent in the orchestrator.
    Uses Groq for fast LLM calls (target < 100ms latency).
    """

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="rewrite-agent",
            name="Rewrite Agent",
            description="Rephrases cached responses to vary wording while preserving meaning.",
            skills=["rewrite"],
            endpoint="local://rewrite-agent",
        )

    async def rewrite(self, cached_text: str, language: str = "en", user_text: str | None = None) -> str:
        """Rephrase a cached response and apply personality. Returns the rewritten text.

        Falls back to returning cached_text verbatim on any failure.
        Uses the unmediated (raw) agent response as input so personality
        and rewrite variation are applied in a single LLM call.
        """
        system_prompt = await self._load_prompt_async("rewrite")
        try:
            personality = await SettingsRepository.get_value("personality.prompt", "")
        except Exception:
            personality = ""
        personality_text = personality.strip() if personality else ""
        system_prompt = system_prompt.replace("{personality}", personality_text)
        system_prompt = system_prompt.replace("{language}", language or "en").strip()
        if user_text:
            user_content = (
                f"User asked:\n{self._wrap_user_input(user_text)}\n"
                f"Agent responded: {cached_text}\n\n"
                f"Rephrase in {language}:"
            )
        else:
            user_content = self._wrap_user_input(cached_text)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            result = await self._call_llm(messages)
            if not result:
                logger.warning("Rewrite LLM returned empty, using cached text")
                return cached_text
            return result
        except Exception:
            logger.warning("Rewrite failed, returning cached text verbatim", exc_info=True)
            return cached_text

    async def handle_task(self, task: AgentTask) -> TaskResult:
        """A2A-compatible interface. Rewrites task.description."""
        result = await self.rewrite(task.description)
        return TaskResult(speech=result)
