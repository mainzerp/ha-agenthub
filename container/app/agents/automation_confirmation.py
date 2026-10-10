"""Confirmation state and answer handling for automation config changes.

Automation create/update/delete never write on the first turn. The
automation executor validates the proposed change, stores it here keyed by
``conversation_id`` and asks "Shall I save this?". On a later turn that
reaches the automation agent while a proposal is pending, the agent LLM
classifies the answer (confirm / decline / modify / unrelated) and only a
confirmation applies the change to Home Assistant.

State is in memory with a TTL (no DB): a container restart drops pending
proposals, which is the safe direction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from app.models.agent import ActionExecuted, AgentError, AgentErrorCode, TaskResult

if TYPE_CHECKING:
    from app.models.agent import DispatchTask

logger = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 300.0
# Expired proposals are remembered a little longer so a late "yes" gets an
# honest "that proposal expired" instead of being treated as a new request.
_TOMBSTONE_TTL_SECONDS = 3600.0
_MAX_ENTRIES = 256

ChangeKind = Literal["create", "update", "delete"]
Decision = Literal["confirm", "decline", "modify", "unrelated", "unclear"]
_DECISIONS: frozenset[str] = frozenset({"confirm", "decline", "modify", "unrelated"})
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)

CONFIRM_PROMPT_NAME = "automation_confirm"


@dataclass
class PendingAutomationChange:
    """A validated, not yet applied automation change."""

    kind: ChangeKind
    alias: str
    summary: str
    question: str
    config: dict[str, Any] | None = None
    config_id: str | None = None
    entity_id: str = ""
    # update only: the HA config the change was computed from (optimistic
    # concurrency check) and the validated patch parts (re-validated on confirm).
    base_config: dict[str, Any] | None = None
    patch: dict[str, Any] = field(default_factory=dict)
    agent_id: str = "automation-agent"

    @property
    def action_name(self) -> str:
        return f"{self.kind}_automation"


class AutomationConfirmationStore:
    """In-memory pending-proposal map keyed by conversation_id (TTL-bounded)."""

    def __init__(self, ttl_seconds: float = PENDING_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: OrderedDict[str, tuple[float, PendingAutomationChange | None]] = OrderedDict()

    def put(self, conversation_id: str, change: PendingAutomationChange) -> None:
        self._purge()
        self._entries[conversation_id] = (time.monotonic(), change)
        self._entries.move_to_end(conversation_id)
        while len(self._entries) > _MAX_ENTRIES:
            self._entries.popitem(last=False)

    def get(self, conversation_id: str | None) -> tuple[str, PendingAutomationChange | None]:
        """Return ``(status, change)`` with status ``active``, ``expired`` or ``none``."""
        if not conversation_id:
            return "none", None
        entry = self._entries.get(conversation_id)
        if entry is None:
            return "none", None
        stored_at, change = entry
        age = time.monotonic() - stored_at
        if change is not None and age <= self._ttl:
            return "active", change
        if age <= _TOMBSTONE_TTL_SECONDS:
            return "expired", None
        self._entries.pop(conversation_id, None)
        return "none", None

    def pop(self, conversation_id: str | None) -> PendingAutomationChange | None:
        if not conversation_id:
            return None
        entry = self._entries.pop(conversation_id, None)
        return entry[1] if entry else None

    def clear(self) -> None:
        self._entries.clear()

    def _purge(self) -> None:
        now = time.monotonic()
        for key in list(self._entries):
            stored_at, change = self._entries[key]
            age = now - stored_at
            if age > _TOMBSTONE_TTL_SECONDS:
                self._entries.pop(key, None)
            elif change is not None and age > self._ttl:
                # Keep a tombstone (change dropped) for the late-answer message.
                self._entries[key] = (stored_at, None)


confirmation_store = AutomationConfirmationStore()


def parse_decision(response: str | None) -> Decision:
    """Parse the classifier reply; anything unexpected is ``unclear`` (fail closed)."""
    if not response:
        return "unclear"
    for match in _JSON_OBJECT_RE.finditer(response):
        try:
            payload = json.loads(match.group(0))
        except (ValueError, TypeError):
            continue
        decision = str(payload.get("decision", "")).strip().lower() if isinstance(payload, dict) else ""
        if decision in _DECISIONS:
            return decision  # type: ignore[return-value]
    return "unclear"


async def classify_answer(agent: Any, task: DispatchTask, change: PendingAutomationChange) -> Decision:
    """Let the agent LLM decide whether the turn confirms, declines or modifies the proposal."""
    try:
        system_prompt = await agent._load_prompt_async(CONFIRM_PROMPT_NAME)
        user_content = (
            f"Pending proposal: {change.summary}\n"
            f"Question asked: {change.question}\n"
            f"User answer: {agent._wrap_user_input(task.description or '')}"
        )
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]
        response = await agent._call_llm(messages, span_collector=task.span_collector)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Automation confirmation classification failed", exc_info=True)
        return "unclear"
    decision = parse_decision(response)
    logger.info("Automation confirmation decision=%s kind=%s", decision, change.kind)
    return decision


def _is_followup(task: DispatchTask) -> bool:
    ctx = task.context
    return bool(ctx and (ctx.is_followup or ctx.pending_question))


async def handle_pending_automation_answer(agent: Any, task: DispatchTask) -> TaskResult | None:
    """Handle a turn while an automation proposal is pending for the conversation.

    Returns a final :class:`TaskResult`, or ``None`` when the turn should run
    through the normal agent flow (no proposal, a modification request, or
    an unrelated request -- the stale proposal is dropped in both latter cases).
    """
    conversation_id = task.conversation_id
    status, change = confirmation_store.get(conversation_id)
    if status == "expired":
        confirmation_store.pop(conversation_id)
        if _is_followup(task):
            return TaskResult(
                speech="That automation proposal has expired and nothing was saved. Please tell me the change again.",
            )
        return None
    if status != "active" or change is None:
        return None

    decision = await classify_answer(agent, task, change)
    if decision == "confirm":
        confirmation_store.pop(conversation_id)
        return await _apply(agent, change)
    if decision == "decline":
        confirmation_store.pop(conversation_id)
        if change.kind == "delete":
            return TaskResult(speech=f"OK, the automation '{change.alias}' stays as it is.")
        return TaskResult(speech="OK, I did not save anything.")
    if decision in ("modify", "unrelated"):
        # A new or amended request supersedes the pending proposal; the
        # normal flow builds (and asks to confirm) a fresh one.
        confirmation_store.pop(conversation_id)
        return None
    # Unclear answer: keep the proposal and ask again.
    return TaskResult(speech=f"{change.summary} {change.question}", voice_followup=True)


async def _apply(agent: Any, change: PendingAutomationChange) -> TaskResult:
    from app.agents.automation_executor import apply_pending_automation_change

    try:
        result = await apply_pending_automation_change(
            change,
            agent._ha_client,
            agent._entity_index,
            agent_id=change.agent_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Applying confirmed automation change failed")
        result = {"success": False, "entity_id": change.entity_id, "speech": "Sorry, saving the automation failed."}

    action_executed = ActionExecuted(
        action=change.action_name,
        entity_id=str(result.get("entity_id") or change.entity_id or ""),
        success=bool(result.get("success")),
        new_state=None,
        cacheable=False,
    )
    if not result.get("success"):
        speech = str(result.get("speech") or "Sorry, saving the automation failed.")
        return TaskResult(
            speech=speech,
            action_executed=action_executed,
            error=AgentError(code=AgentErrorCode.ACTION_FAILED, message=speech, recoverable=True),
        )
    return TaskResult(speech=str(result.get("speech") or "Done."), action_executed=action_executed)
