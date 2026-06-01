"""Conversation state manager extracted from OrchestratorAgent.

Manages the in-memory conversation buffer, DB persistence,
pruning, and turn retrieval.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

from app.db.repository import ConversationRepository, SettingsRepository

logger = logging.getLogger(__name__)

# Conversation memory limits
_MAX_CONVERSATIONS = 1000
_CONVERSATION_TTL_SECONDS = 1800  # 30 minutes

# Conversation context setting defaults
_DEFAULT_CONVERSATION_CONTEXT_TURNS = 3
_MIN_CONVERSATION_CONTEXT_TURNS = 1
_MAX_CONVERSATION_CONTEXT_TURNS = 20


class ConversationManager:
    """Manages per-conversation turn storage and retrieval."""

    def __init__(self) -> None:
        self._conversations: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()

    async def _get_conversation_context_turn_limit(self) -> int:
        fallback = _DEFAULT_CONVERSATION_CONTEXT_TURNS
        try:
            raw_value = await SettingsRepository.get_value(
                "general.conversation_context_turns",
                str(fallback),
            )
            parsed = int(str(raw_value).strip())
        except Exception:
            logger.debug(
                "Failed to read general.conversation_context_turns; using default %d",
                fallback,
                exc_info=True,
            )
            return fallback
        return max(
            _MIN_CONVERSATION_CONTEXT_TURNS,
            min(_MAX_CONVERSATION_CONTEXT_TURNS, parsed),
        )

    async def get_turns(self, conversation_id: str | None) -> list[dict[str, Any]]:
        """Get recent conversation turns for context.

        FLOW-MED-7: on in-memory miss, fall back to the DB so
        multi-worker deployments and post-restart replays still see
        conversation context. The result is cached back into
        ``_conversations`` so subsequent calls stay in-memory.
        """
        if not conversation_id:
            return []
        turn_limit = await self._get_conversation_context_turn_limit()
        max_messages = turn_limit * 2
        entry = self._conversations.get(conversation_id)
        if entry is not None:
            ts, turns = entry
            if time.monotonic() - ts <= _CONVERSATION_TTL_SECONDS:
                trimmed_turns = list(turns[-max_messages:]) if len(turns) > max_messages else list(turns)
                if len(trimmed_turns) != len(turns):
                    self._conversations[conversation_id] = (ts, trimmed_turns)
                    self._evict_stale_conversations()
                return trimmed_turns
            self._conversations.pop(conversation_id, None)

        try:
            rows = await ConversationRepository.get_by_conversation_id(
                conversation_id,
            )
        except Exception:
            logger.debug(
                "DB fallback for conversation turns failed for %s",
                conversation_id,
                exc_info=True,
            )
            return []

        if not rows:
            return []

        conversation_turns: list[dict[str, Any]] = []
        for row in rows[-turn_limit:]:
            user_text = row.get("user_text") or ""
            if user_text:
                conversation_turns.append({"role": "user", "content": user_text})
            resp_text = row.get("response_text") or ""
            if resp_text:
                assistant_turn: dict[str, Any] = {"role": "assistant", "content": resp_text}
                agent_id = row.get("agent_id")
                if agent_id:
                    assistant_turn["agent_id"] = agent_id
                conversation_turns.append(assistant_turn)

        if conversation_turns:
            self._conversations[conversation_id] = (time.monotonic(), conversation_turns)
            self._evict_stale_conversations()
        return conversation_turns

    async def store_turn(
        self, conversation_id: str | None, user_text: str, assistant_text: str, agent_id: str | None = None
    ) -> None:
        """Store a conversation turn, keeping the configured number of exchanges."""
        if not conversation_id:
            return
        turn_limit = await self._get_conversation_context_turn_limit()
        self._evict_stale_conversations()
        now = time.monotonic()
        if conversation_id in self._conversations:
            self._conversations.move_to_end(conversation_id)
            _, turns = self._conversations[conversation_id]
        else:
            turns = []
        turns.append({"role": "user", "content": user_text})
        assistant_turn = {"role": "assistant", "content": assistant_text}
        if agent_id:
            assistant_turn["agent_id"] = agent_id
        turns.append(assistant_turn)
        max_messages = turn_limit * 2
        if len(turns) > max_messages:
            turns = turns[-max_messages:]
        self._conversations[conversation_id] = (now, turns)

        try:
            await ConversationRepository.insert(
                conversation_id=conversation_id,
                user_text=user_text,
                agent_id=agent_id,
                response_text=assistant_text,
            )
        except Exception:
            logger.warning("Failed to persist conversation turn to DB", exc_info=True)

    def _evict_stale_conversations(self) -> None:
        """Remove conversations older than TTL and enforce max count."""
        now = time.monotonic()
        while self._conversations:
            oldest_key = next(iter(self._conversations))
            ts, _ = self._conversations[oldest_key]
            if now - ts > _CONVERSATION_TTL_SECONDS:
                self._conversations.pop(oldest_key)
            else:
                break
        while len(self._conversations) > _MAX_CONVERSATIONS:
            self._conversations.popitem(last=False)
