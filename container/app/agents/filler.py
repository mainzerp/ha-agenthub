"""Filler agent -- generates short interim TTS phrases while slow agents process."""

from __future__ import annotations

import asyncio
import logging

from app.agents.base import BaseAgent
from app.db.repository import SettingsRepository
from app.models.agent import AgentCard, AgentTask, TaskResult

logger = logging.getLogger(__name__)

# P3-11: hard upper bound on filler-LLM latency. Filler is only useful
# while the real agent is still working; if Groq is itself slow, give
# up rather than block the streaming path.
_FILLER_LLM_TIMEOUT_SEC = 3.0

# Common ISO-639-1 codes to full language names
_LANGUAGE_NAMES: dict[str, str] = {
    "de": "German (Deutsch)",
    "en": "English",
    "fr": "French (Francais)",
    "es": "Spanish (Espanol)",
    "it": "Italian (Italiano)",
    "nl": "Dutch (Nederlands)",
    "pt": "Portuguese (Portugues)",
    "pl": "Polish (Polski)",
    "ru": "Russian",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "sv": "Swedish (Svenska)",
    "da": "Danish (Dansk)",
    "no": "Norwegian (Norsk)",
    "fi": "Finnish (Suomi)",
    "cs": "Czech (Cestina)",
    "tr": "Turkish (Turkce)",
    "uk": "Ukrainian",
    "ar": "Arabic",
}


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
            try:
                personality = await SettingsRepository.get_value("personality.prompt", "")
            except (ValueError, TypeError):
                personality = ""

            language_name = _LANGUAGE_NAMES.get(language, language)
            system_prompt = (await self._load_prompt_async("filler")).format(
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
            return TaskResult(speech=result.strip() if result else "")
        except TimeoutError:
            logger.warning("Filler generation timed out (>%.0fs)", _FILLER_LLM_TIMEOUT_SEC)
            return TaskResult(speech="")
        except Exception:
            logger.warning("Filler generation failed", exc_info=True)
            return TaskResult(speech="")
