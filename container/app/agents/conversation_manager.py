"""Conversation state manager extracted from OrchestratorAgent.

Manages the in-memory conversation buffer, DB persistence,
pruning, and turn retrieval.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from typing import Any

from app.db.repository import ConversationRepository, SettingsRepository
from app.memory import get_memory_service
from app.util.tasks import spawn

logger = logging.getLogger(__name__)

# Conversation memory limits
_MAX_CONVERSATIONS = 1000
_CONVERSATION_TTL_SECONDS = 1800  # 30 minutes

# Conversation context setting defaults
_DEFAULT_CONVERSATION_CONTEXT_TURNS = 3
_MIN_CONVERSATION_CONTEXT_TURNS = 1
_MAX_CONVERSATION_CONTEXT_TURNS = 20

# ENTITY_RES_REDESIGN Phase 6: recency hints for anaphora resolution.
# Kept small -- the prompt block renders at most the newest few.
_MAX_LAST_ENTITIES_KEPT = 5
_DEFAULT_LAST_ENTITIES_LIMIT = 3

# Pending-question map: TTL for the single-shot follow-up signal set when
# the last assistant turn was a clarifying question (voice_followup).
_PENDING_QUESTION_TTL_SECONDS = 300


async def extract_resolved_entities(
    action_executed: Any,
    entity_index: Any | None = None,
) -> list[dict[str, Any]] | None:
    """Build the ``resolved_entities`` payload for ``store_turn`` from an
    ``action_executed`` result (dict or pydantic model).

    Success path only: failed or missing actions yield ``None``. The
    friendly name is looked up in the entity index when available and
    falls back to the entity_id. Failure-contained: an index error drops
    only the name, never the record.
    """
    if not action_executed:
        return None
    if hasattr(action_executed, "model_dump"):
        action_executed = action_executed.model_dump()
    if not isinstance(action_executed, dict):
        return None
    if not action_executed.get("success", True):
        return None
    entity_id = str(action_executed.get("entity_id") or "").strip()
    if not entity_id:
        return None
    friendly_name = ""
    if entity_index is not None:
        try:
            if hasattr(entity_index, "get_by_id_async"):
                entry = await entity_index.get_by_id_async(entity_id)
            else:
                entry = entity_index.get_by_id(entity_id)
            if entry is not None:
                friendly_name = getattr(entry, "friendly_name", None) or ""
        except Exception:
            logger.debug("Friendly-name lookup failed for %s", entity_id, exc_info=True)
    return [{"entity_id": entity_id, "friendly_name": friendly_name or entity_id}]


class ConversationManager:
    """Manages per-conversation turn storage and retrieval."""

    def __init__(self) -> None:
        self._conversations: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        # ENTITY_RES_REDESIGN Phase 6: per-conversation recency hints
        # (most recent first), kept in lockstep with the turn buffer TTL.
        self._last_entities: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        # Pending clarifying questions keyed by conversation_id. Set at
        # finalization when voice_followup is effective; consumed
        # single-shot by the orchestrator prelude on the answering turn.
        self._pending_questions: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        # Payloads consumed by the prelude this turn, kept so a noise
        # classification can re-arm the question it should never have eaten.
        self._last_popped_pending: OrderedDict[str, dict[str, Any]] = OrderedDict()

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
        self,
        conversation_id: str | None,
        user_text: str,
        assistant_text: str,
        agent_id: str | None = None,
        resolved_entities: list[dict[str, Any]] | None = None,
        user_id: str | None = None,
        language: str | None = None,
        source: str | None = None,
    ) -> None:
        """Store a conversation turn, keeping the configured number of exchanges.

        ``resolved_entities`` (ENTITY_RES_REDESIGN Phase 6): entities that
        were resolved and acted on this turn (success path only), as
        ``{"entity_id": ..., "friendly_name": ...}`` dicts. They are
        stamped with the turn index, kept in memory as anaphora recency
        hints, and persisted as JSON in the ``conversations.action_executed``
        TEXT column so they survive restarts (no migration -- the column
        already exists and was previously unused).
        """
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

        stamped_entities: list[dict[str, Any]] | None = None
        if resolved_entities:
            turn_index = sum(1 for t in turns if t.get("role") == "user")
            stamped_entities = [
                {
                    "entity_id": str(e.get("entity_id") or ""),
                    "friendly_name": str(e.get("friendly_name") or e.get("entity_id") or ""),
                    "turn_index": turn_index,
                }
                for e in resolved_entities
                if e.get("entity_id")
            ]
            if stamped_entities:
                self._record_last_entities(conversation_id, stamped_entities, now)

        conversation_row_id = 0
        try:
            conversation_row_id = await ConversationRepository.insert(
                conversation_id=conversation_id,
                user_text=user_text,
                agent_id=agent_id,
                response_text=assistant_text,
                action_executed=json.dumps(stamped_entities) if stamped_entities else None,
                user_id=user_id,
            )
        except Exception:
            logger.warning("Failed to persist conversation turn to DB", exc_info=True)

        # Session memory: fire-and-forget per-turn embedding. Single funnel,
        # so every store_turn call site is covered. Fully guarded -- a memory
        # failure must never surface into the request path.
        if conversation_row_id:
            try:
                memory = get_memory_service()
                if memory is not None and await memory.is_enabled():
                    spawn(
                        memory.index_turn(
                            conversation_id=conversation_id,
                            conversation_row_id=conversation_row_id,
                            user_id=user_id,
                            user_text=user_text,
                            response_text=assistant_text,
                            language=language,
                            source=source,
                        ),
                        name="memory-index-turn",
                    )
            except Exception:
                logger.warning("Session-memory hook failed for %s", conversation_id, exc_info=True)

    def _record_last_entities(
        self,
        conversation_id: str,
        stamped_entities: list[dict[str, Any]],
        now: float,
    ) -> None:
        """Prepend freshly resolved entities to the recency hint list."""
        entry = self._last_entities.get(conversation_id)
        _, existing = entry if entry is not None else (now, [])
        seen = {e["entity_id"] for e in stamped_entities}
        merged = list(stamped_entities) + [e for e in existing if e.get("entity_id") not in seen]
        self._last_entities[conversation_id] = (now, merged[:_MAX_LAST_ENTITIES_KEPT])
        self._last_entities.move_to_end(conversation_id)

    async def get_last_entities(
        self,
        conversation_id: str | None,
        limit: int = _DEFAULT_LAST_ENTITIES_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return the most recently resolved entities for a conversation.

        ENTITY_RES_REDESIGN Phase 6: slim anaphora hints (entity_id,
        friendly_name, turn_index), most recent first. Falls back to the
        DB (JSON in ``conversations.action_executed``) on an in-memory
        miss so post-restart turns still see the hints. Failure-contained:
        any error yields an empty list.
        """
        if not conversation_id:
            return []
        entry = self._last_entities.get(conversation_id)
        if entry is not None:
            ts, entities = entry
            if time.monotonic() - ts <= _CONVERSATION_TTL_SECONDS:
                self._last_entities.move_to_end(conversation_id)
                return [dict(e) for e in entities[:limit]]
            self._last_entities.pop(conversation_id, None)

        try:
            rows = await ConversationRepository.get_by_conversation_id(conversation_id)
        except Exception:
            logger.debug(
                "DB fallback for last entities failed for %s",
                conversation_id,
                exc_info=True,
            )
            return []

        entities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in reversed(rows):
            raw = row.get("action_executed")
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, list):
                continue
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                entity_id = str(item.get("entity_id") or "")
                if not entity_id or entity_id in seen:
                    continue
                seen.add(entity_id)
                entities.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": str(item.get("friendly_name") or entity_id),
                        "turn_index": int(item.get("turn_index") or 0),
                    }
                )
                if len(entities) >= _MAX_LAST_ENTITIES_KEPT:
                    break
            if len(entities) >= _MAX_LAST_ENTITIES_KEPT:
                break

        if entities:
            self._last_entities[conversation_id] = (time.monotonic(), entities)
            self._evict_stale_conversations()
        return [dict(e) for e in entities[:limit]]

    def set_pending_question(self, conversation_id: str, question: str, agent_id: str) -> None:
        """Record that the last assistant turn asked a clarifying question.

        Stored in memory only (no DB): the state is meaningless after a
        container restart because HA's re-listen window is long gone.
        """
        if not conversation_id:
            return
        self._pending_questions[conversation_id] = (
            time.monotonic(),
            {"question": question, "agent_id": agent_id},
        )
        self._pending_questions.move_to_end(conversation_id)
        self._last_popped_pending.pop(conversation_id, None)
        self._evict_stale_conversations()

    def has_pending_question(self, conversation_id: str | None) -> bool:
        """Non-consuming, TTL-checked peek at the pending-question map."""
        if not conversation_id:
            return False
        entry = self._pending_questions.get(conversation_id)
        if entry is None:
            return False
        ts, _ = entry
        if time.monotonic() - ts > _PENDING_QUESTION_TTL_SECONDS:
            self._pending_questions.pop(conversation_id, None)
            return False
        return True

    def pop_pending_question(self, conversation_id: str | None) -> dict[str, Any] | None:
        """Single-shot, TTL-checked consume of the pending question.

        Aborts, topic changes, and answers each consume the entry exactly
        once; the next question turn re-sets it at finalization. Expired
        entries are dropped and return None.
        """
        if not conversation_id:
            return None
        # Any pop attempt invalidates an older stashed payload -- only the
        # payload consumed by THIS turn may ever be restored.
        self._last_popped_pending.pop(conversation_id, None)
        entry = self._pending_questions.pop(conversation_id, None)
        if entry is None:
            return None
        ts, payload = entry
        if time.monotonic() - ts > _PENDING_QUESTION_TTL_SECONDS:
            return None
        self._last_popped_pending[conversation_id] = payload
        self._last_popped_pending.move_to_end(conversation_id)
        while len(self._last_popped_pending) > _MAX_CONVERSATIONS:
            self._last_popped_pending.popitem(last=False)
        return payload

    def restore_pending_question(self, conversation_id: str | None) -> None:
        """Re-arm the pending question popped earlier in the same turn.

        Used by the noise route: an unrelated background fragment must not
        consume an open clarifying question. No-op when nothing was popped.
        """
        if not conversation_id:
            return
        payload = self._last_popped_pending.pop(conversation_id, None)
        if payload:
            self.set_pending_question(conversation_id, payload["question"], payload["agent_id"])

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
        # Recency hints share the turn buffer's TTL and size bound.
        while self._last_entities:
            oldest_key = next(iter(self._last_entities))
            ts, _ = self._last_entities[oldest_key]
            if now - ts > _CONVERSATION_TTL_SECONDS:
                self._last_entities.pop(oldest_key)
            else:
                break
        while len(self._last_entities) > _MAX_CONVERSATIONS:
            self._last_entities.popitem(last=False)
        # Pending questions share the lazy-eviction pattern with their own TTL.
        while self._pending_questions:
            oldest_key = next(iter(self._pending_questions))
            ts, _ = self._pending_questions[oldest_key]
            if now - ts > _PENDING_QUESTION_TTL_SECONDS:
                self._pending_questions.pop(oldest_key)
            else:
                break
        while len(self._pending_questions) > _MAX_CONVERSATIONS:
            self._pending_questions.popitem(last=False)
