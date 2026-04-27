"""Base agent class with HA client and entity index access."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path

from app.models.agent import AgentCard, AgentError, AgentErrorCode, AgentTask, TaskContext, TaskResult
from app.security.sanitization import USER_INPUT_END, USER_INPUT_START, wrap_user_input

logger = logging.getLogger(__name__)

# Prompts directory (container/app/prompts/)
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Module-level cache for loaded prompt files (Q-4). Prompts only change on
# container restart, so there is no invalidation path.
_prompt_cache: dict[str, str] = {}

_KNOWN_PROMPT_NAMES = (
    "automation",
    "climate",
    "filler",
    "general",
    "light",
    "media",
    "mediate",
    "merge",
    "music",
    "orchestrator",
    "rewrite",
    "scene",
    "security",
    "send",
    "timer",
)


def _prompt_path(name: str) -> Path:
    return _PROMPTS_DIR / f"{name}.txt"


def _load_prompt_path(path: Path) -> str:
    cache_key = str(path)
    cached = _prompt_cache.get(cache_key)
    if cached is not None:
        return cached
    logger.debug("Cold-loading prompt from disk: %s", path.name)
    content = path.read_text(encoding="utf-8").strip()
    if "{personality_base}" in content:
        base_path = _prompt_path("personality_base")
        if base_path.exists():
            base_cache_key = str(base_path)
            base_content = _prompt_cache.get(base_cache_key)
            if base_content is None:
                base_content = base_path.read_text(encoding="utf-8").strip()
                _prompt_cache[base_cache_key] = base_content
            content = content.replace("{personality_base}", base_content)
    _prompt_cache[cache_key] = content
    return content


async def _load_prompt_path_async(path: Path) -> str:
    cache_key = str(path)
    cached = _prompt_cache.get(cache_key)
    if cached is not None:
        return cached
    return await asyncio.to_thread(_load_prompt_path, path)


def preload_prompt_cache(prompt_names: Iterable[str] | None = None) -> None:
    """Warm the shipped prompt cache so request handlers stay in memory."""
    names = tuple(prompt_names) if prompt_names is not None else _KNOWN_PROMPT_NAMES
    for name in names:
        _load_prompt_path(_prompt_path(name))


class BaseAgent(ABC):
    """Abstract base class for all specialized agents.

    Subclasses must implement handle_task(). Optionally override
    handle_task_stream() for token-level streaming support.
    """

    def __init__(
        self,
        ha_client=None,
        entity_index=None,
    ) -> None:
        self._ha_client = ha_client
        self._entity_index = entity_index

    @property
    @abstractmethod
    def agent_card(self) -> AgentCard:
        """Return the AgentCard describing this agent's capabilities."""
        ...

    @abstractmethod
    async def handle_task(self, task: AgentTask) -> dict | TaskResult:
        """Process a task and return the full result.

        Returns:
            TaskResult (preferred) or dict with at least {"speech": str}.
        """
        ...

    async def handle_task_stream(self, task: AgentTask) -> AsyncGenerator[dict, None]:
        """Process a task and yield streaming token dicts.

        Default implementation wraps handle_task() in a single yield.
        Override in subclasses that support true token-level streaming.

        Yields:
            dict with {"token": str, "done": bool} for each chunk.
            The last chunk must have done=True and may include
            conversation_id.
        """
        try:
            result = await self.handle_task(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            agent_id = getattr(
                getattr(self, "agent_card", None),
                "agent_id",
                type(self).__name__,
            )
            logger.exception("handle_task failed inside default stream wrapper for %s", agent_id)
            result = self._error_result(
                AgentErrorCode.INTERNAL,
                "Sorry, something went wrong while handling that request.",
            )
        # Support both TaskResult and raw dict
        result_dict = result.model_dump() if hasattr(result, "model_dump") else result
        chunk = {
            "token": result_dict.get("speech", ""),
            "done": True,
            "conversation_id": task.conversation_id,
        }
        action = result_dict.get("action_executed")
        if action:
            chunk["action_executed"] = action
        if result_dict.get("voice_followup"):
            chunk["voice_followup"] = True
        if result_dict.get("directive"):
            chunk["directive"] = result_dict["directive"]
        if result_dict.get("reason") is not None:
            chunk["reason"] = result_dict["reason"]
        yield chunk

    def _load_prompt(self, name: str) -> str:
        """Load a prompt file from the prompts/ directory.

        Results are cached in ``_prompt_cache`` keyed by the resolved file
        path (Q-4). Prompts only change on container restart.

        Args:
            name: Filename without extension (e.g. "light" loads "light.txt").

        Returns:
            Prompt text content.
        """
        return _load_prompt_path(_prompt_path(name))

    async def _load_prompt_async(self, name: str) -> str:
        """Load a prompt file without blocking the event loop on cache miss."""
        return await _load_prompt_path_async(_prompt_path(name))

    def _error_result(
        self,
        code: AgentErrorCode,
        speech: str,
        *,
        recoverable: bool = True,
    ) -> TaskResult:
        """Build a TaskResult with a structured error."""
        return TaskResult(
            speech=speech,
            error=AgentError(code=code, message=speech, recoverable=recoverable),
        )

    @staticmethod
    def _build_time_location_context(context: TaskContext | None) -> str:
        """Build a short context block for local time and location."""
        if not context or not context.local_time:
            return ""
        parts = [f"Current local time: {context.local_time}"]
        if context.timezone and context.timezone != "UTC":
            parts.append(f"Timezone: {context.timezone}")
        if context.location_name:
            parts.append(f"Home location: {context.location_name}")
        return "\n".join(parts)

    @staticmethod
    def _wrap_user_input(content: str) -> str:
        """Delimit free-form user content before it is sent to an LLM."""
        text = content or ""
        if USER_INPUT_START in text and USER_INPUT_END in text:
            return text
        return wrap_user_input(text)

    @classmethod
    def _append_conversation_turn_messages(
        cls,
        messages: list[dict],
        turns: list[dict],
        *,
        max_content_length: int | None = None,
    ) -> None:
        for turn in turns:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if max_content_length is not None and len(content) > max_content_length:
                content = content[:max_content_length] + "..."
            if role == "user":
                content = cls._wrap_user_input(content)
            messages.append({"role": role, "content": content})

    @classmethod
    def _normalize_llm_messages(cls, messages: list[dict]) -> list[dict]:
        normalized = []
        for message in messages:
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                updated = dict(message)
                updated["content"] = cls._wrap_user_input(updated["content"])
                normalized.append(updated)
            else:
                normalized.append(message)
        return normalized

    async def _call_llm(self, messages: list[dict], **overrides) -> str:
        """Call the LLM using this agent's config.

        Uses the agent_card.agent_id to look up per-agent LLM config
        from the SQLite agent_configs table via llm.complete().
        """
        from app.llm.client import complete

        return await complete(self.agent_card.agent_id, self._normalize_llm_messages(messages), **overrides)
