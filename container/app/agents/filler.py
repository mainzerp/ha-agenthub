"""Filler agent -- generates short interim TTS phrases while slow agents process."""

from __future__ import annotations

import asyncio
import logging

from app.agents.base import (
    _LANGUAGE_NAMES,  # noqa: F401 -- re-exported for test compat
    BaseAgent,
    _render_prompt_template,
    language_code_to_name,
)
from app.agents.decorator import agent
from app.db.repository import SettingsRepository
from app.models.agent import AgentCard, AgentTask, TaskResult

logger = logging.getLogger(__name__)

# P3-11: hard upper bound on filler-LLM latency. Filler is only useful
# while the real agent is still working; if Groq is itself slow, give
# up rather than block the streaming path.
_FILLER_LLM_TIMEOUT_SEC = 3.0


@agent(
    agent_id="filler-agent",
    name="Filler Agent",
    description="Generates short interim TTS filler phrases while slow agents process requests.",
    skills=["filler_generation"],
    expected_latency="low",
    needs_entity_matcher=False,
)
class FillerAgent(BaseAgent):
    """Generates short interim TTS filler phrases while slow agents process requests."""

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="filler-agent",
            name="Filler Agent",
            description="Generates short interim TTS filler phrases while slow agents process requests.",
            skills=["filler_generation"],
            endpoint="local://filler-agent",
            expected_latency="low",
        )

    async def handle_task(self, task: AgentTask) -> TaskResult:
        """Generate a filler phrase.

        Expects task.description in format "generate_filler:<target_agent>".
        Language is read from task.context.language.
        """
        try:
            # Parse target_agent from description
            target_agent = ""
            if task.description and task.description.startswith("generate_filler:"):
                target_agent = task.description.split(":", 1)[1]

            language = "en"
            if task.context:
                language = task.context.language or "en"

            # Load personality prompt fresh each call
            personality = await SettingsRepository.get_value("personality.prompt", "")

            language_name = language_code_to_name(language)
            system_prompt = _render_prompt_template(
                await self._load_prompt_async("filler"),
                personality=personality or "",
                language=language_name,
            )
            user_content = f"{self._wrap_user_input(task.user_text[:200])}\n\nAgent: {target_agent}"

            result = await asyncio.wait_for(
                self._call_llm(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ]
                ),
                timeout=_FILLER_LLM_TIMEOUT_SEC,
            )
            if not result or not result.strip():
                logger.warning(
                    "Filler generation produced empty response (agent=%s model=%s max_tokens=%s)",
                    self.agent_card.agent_id,
                    self._config.model if self._config else "unknown",
                    self._config.max_tokens if self._config else "unknown",
                )
                return TaskResult(speech="")
            return TaskResult(speech=result.strip())
        except TimeoutError:
            logger.warning("Filler generation timed out (>%.0fs)", _FILLER_LLM_TIMEOUT_SEC)
            return TaskResult(speech="")
        except Exception:
            logger.warning("Filler generation failed", exc_info=True)
            return TaskResult(speech="")
