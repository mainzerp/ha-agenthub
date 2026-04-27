"""Rewrite agent for cached response phrasing variation."""

from __future__ import annotations

import logging

from app.agents.base import BaseAgent
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

    async def rewrite(self, cached_text: str) -> str:
        """Rephrase a cached response. Returns the rewritten text.

        Falls back to returning cached_text verbatim on any failure.
        The input is already personality-mediated, so no personality injection needed.
        """
        system_prompt = await self._load_prompt_async("rewrite")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._wrap_user_input(cached_text)},
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
