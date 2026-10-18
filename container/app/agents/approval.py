"""Approval of sensitive Home Assistant actions across two turns.

Turn 1 (agent side): ``call_service_with_verification`` holds a sensitive
call (``app.agents.approval_gate``); the actionable agent calls
:func:`build_approval_result`, which stores a :class:`PendingApproval` in
:data:`approval_store` (keyed by ``conversation_id``, 60 s TTL, tombstones
for an hour) and turns the agent result into one approval question with
``voice_followup``. The orchestrator arms the pending question as for any
clarifying question.

Turn 2 (orchestrator prelude, before cache replay and classification):
:func:`handle_pending_approval` classifies the VERBATIM user text with an
LLM (``prompts/approval_confirm.txt``, fail-closed parser shared with the
automation confirmation). ``confirm`` rechecks entity visibility for the
stored agent and executes exactly the stored calls under an approval token;
``decline`` drops them; ``modify`` / ``unrelated`` drop them and let the
turn run through the normal pipeline; ``unclear`` asks once more, then
drops them. Nothing of this result is ever action-cached.

Automation-change proposals (``automation_confirmation``) and approvals are
independent stores; the newer one wins: storing either drops the other for
the same conversation, matching the single pending question.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.agents.action_executor import build_verified_speech, call_service_with_verification
from app.agents.approval_gate import (
    HeldCall,
    action_phrase,
    approval_is_interactive,
    approval_question,
    decline_speech,
)
from app.agents.automation_confirmation import Decision, parse_decision
from app.agents.pending_store import PendingStore
from app.analytics.tracer import _optional_span
from app.models.agent import ActionExecuted, ExecutedCommand, TaskResult, source_is_stateless_task
from app.security.redaction import is_sensitive_key
from app.security.sensitive_actions import approved_calls

logger = logging.getLogger(__name__)

APPROVAL_TTL_SECONDS = 60.0
APPROVAL_TOMBSTONE_TTL_SECONDS = 3600.0
APPROVAL_PROMPT_NAME = "approval_confirm"
_CLASSIFY_TIMEOUT_SEC = 10.0

EXPIRED_SPEECH = "That request expired, nothing was done."
EXPIRED_DECLINE_SPEECH = "OK, nothing was done."
UNCLEAR_FINAL_SPEECH = "I did not get a clear yes, so nothing was done."
NO_HA_SPEECH = "The smart home connection is currently unavailable, so nothing was done."

# Success phrases for actions without a deterministic target state.
_SUCCESS_PHRASES: dict[str, str] = {
    "set_cover_position": "position updated",
    "toggle": "toggled",
    "open": "opened",
}


@dataclass
class PendingSensitiveAction:
    """One held sensitive call. ``service_data`` (may hold an alarm code) stays in memory only."""

    domain: str
    service: str
    entity_id: str
    friendly_name: str
    reason: str
    expected_state: str | None = None
    service_data: dict[str, Any] = field(default_factory=dict, repr=False)
    # Agent that held the call; its visibility rules are rechecked on confirm
    # (a merged multi-agent batch holds calls of several agents).
    agent_id: str = ""

    @property
    def public_service_data(self) -> dict[str, Any]:
        return {k: v for k, v in self.service_data.items() if not is_sensitive_key(k)}

    @property
    def phrase(self) -> str:
        return action_phrase(self.domain, self.service, self.friendly_name, self.public_service_data)


@dataclass
class PendingApproval:
    """The approval batch of one conversation (all held calls of the asking turn)."""

    conversation_id: str
    agent_id: str
    actions: list[PendingSensitiveAction]
    source: str | None = None
    user_id: str | None = None
    language: str = "en"
    trace_id: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    reasked: bool = False

    @property
    def phrases(self) -> list[str]:
        return [action.phrase for action in self.actions]

    @property
    def question(self) -> str:
        return approval_question(self.phrases)


@dataclass(frozen=True)
class ApprovalTombstone:
    """What remains of an expired approval: no service data, no codes."""

    agent_id: str
    question: str
    language: str
    created_at: float
    targets: tuple[tuple[str, str, str], ...]
    source: str | None = None
    user_id: str | None = None


def _tombstone(approval: PendingApproval) -> ApprovalTombstone:
    return ApprovalTombstone(
        agent_id=approval.agent_id,
        question=approval.question,
        language=approval.language,
        created_at=approval.created_at,
        targets=tuple((a.domain, a.service, a.entity_id) for a in approval.actions),
        source=approval.source,
        user_id=approval.user_id,
    )


class ApprovalStore(PendingStore[PendingApproval]):
    """Pending approvals keyed by conversation_id."""

    def __init__(self, ttl_seconds: float = APPROVAL_TTL_SECONDS) -> None:
        super().__init__(
            ttl_seconds,
            tombstone_ttl_seconds=APPROVAL_TOMBSTONE_TTL_SECONDS,
            tombstone=_tombstone,
        )

    def put(self, conversation_id: str, approval: PendingApproval) -> None:
        """Store ``approval``; calls held by other agents in the same turn are merged."""
        status, existing = self.get_entry(conversation_id)
        if (
            status == "active"
            and isinstance(existing, PendingApproval)
            and approval.trace_id is not None
            and existing.trace_id == approval.trace_id
        ):
            known = {(a.domain, a.service, a.entity_id) for a in existing.actions}
            merged = list(existing.actions) + [
                a for a in approval.actions if (a.domain, a.service, a.entity_id) not in known
            ]
            approval = PendingApproval(
                conversation_id=conversation_id,
                agent_id=existing.agent_id,
                actions=merged,
                source=existing.source,
                user_id=existing.user_id,
                language=existing.language,
                trace_id=existing.trace_id,
                created_at=existing.created_at,
            )
        super().put(conversation_id, approval)
        # Newer wins: a pending automation proposal of the same conversation
        # is superseded by this question.
        from app.agents.automation_confirmation import confirmation_store

        confirmation_store.pop(conversation_id)

    def touch(self, conversation_id: str, approval: PendingApproval) -> None:
        """Re-store ``approval`` with a fresh TTL (re-asked question)."""
        PendingStore.put(self, conversation_id, approval)


approval_store = ApprovalStore()


# ---------------------------------------------------------------------------
# Turn 1: build the approval question (agent side)
# ---------------------------------------------------------------------------


async def _friendly_name(entity_index: Any, entity_id: str) -> str:
    getter = getattr(entity_index, "get_by_id_async", None) if entity_index is not None else None
    if getter is not None:
        try:
            entry = await getter(entity_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Friendly-name lookup for approval failed: %s", entity_id, exc_info=True)
            entry = None
        name = getattr(entry, "friendly_name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return entity_id


async def build_approval_result(
    task: Any,
    result: TaskResult,
    held: list[HeldCall],
    *,
    agent_id: str,
    entity_index: Any,
) -> TaskResult:
    """Store the held calls as one pending approval and ask for it.

    Speech of actions that did run (multi-action turns) is kept in front of
    the question; the asking turn is never action-cached.
    """
    context = getattr(task, "context", None)
    conversation_id = getattr(task, "conversation_id", None) or ""
    actions = [
        PendingSensitiveAction(
            domain=call.domain,
            service=call.service,
            entity_id=call.entity_id,
            friendly_name=await _friendly_name(entity_index, call.entity_id),
            reason=call.reason,
            expected_state=call.expected_state,
            service_data=dict(call.service_data),
            agent_id=agent_id,
        )
        for call in held
    ]
    span_collector = getattr(task, "span_collector", None)
    approval = PendingApproval(
        conversation_id=conversation_id,
        agent_id=agent_id,
        actions=actions,
        source=getattr(context, "source", None),
        user_id=getattr(context, "user_id", None),
        language=(getattr(context, "language", None) or "en"),
        trace_id=getattr(span_collector, "trace_id", None),
    )
    approval_store.put(conversation_id, approval)
    question = approval_question([action.phrase for action in actions])
    logger.info(
        "Approval requested conversation=%s agent=%s targets=%s",
        conversation_id,
        agent_id,
        [(a.domain, a.service, a.entity_id) for a in actions],
    )
    multi = bool(result.actions_executed)
    prior = (result.speech or "").strip() if multi else ""
    metadata = {**(result.metadata or {}), "speech_source": "template", "approval": "pending"}
    return TaskResult(
        speech=f"{prior} {question}".strip(),
        voice_followup=True,
        metadata=metadata,
        action_executed=result.action_executed if multi else None,
        actions_executed=result.actions_executed if multi else None,
    )


# ---------------------------------------------------------------------------
# Turn 2: answer handling (orchestrator prelude)
# ---------------------------------------------------------------------------


async def classify_approval_answer(
    orch: Any,
    question: str,
    summary: str,
    user_text: str,
    span_collector: Any = None,
) -> Decision:
    """LLM yes/no/modify/unrelated decision on the verbatim answer; fail closed."""
    from app.agents.base import UNTRUSTED_TEXT_MAX_CHARS, sanitize_untrusted_text

    try:
        system_prompt = await orch._load_prompt_async(APPROVAL_PROMPT_NAME)
        user_content = (
            f"Pending request: {sanitize_untrusted_text(summary, UNTRUSTED_TEXT_MAX_CHARS)}\n"
            f"Question asked: {sanitize_untrusted_text(question, UNTRUSTED_TEXT_MAX_CHARS)}\n"
            f"User answer: {orch._wrap_user_input(user_text or '')}"
        )
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]
        response = await asyncio.wait_for(
            orch._call_llm(messages, span_collector=span_collector), timeout=_CLASSIFY_TIMEOUT_SEC
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Approval answer classification failed", exc_info=True)
        return "unclear"
    return parse_decision(response if isinstance(response, str) else None)


async def _execute_approved(orch: Any, approval: PendingApproval) -> tuple[list[str], list[ActionExecuted]]:
    """Run exactly the approved calls; each target's visibility is rechecked first."""
    from app.agents.executor_state_check import failure_speech
    from app.entity.visibility import entity_is_visible

    ha_client = getattr(orch, "_ha_client", None)
    entity_index = getattr(orch, "_entity_index", None)
    speeches: list[str] = []
    executed: list[ActionExecuted] = []
    if ha_client is None:
        return [NO_HA_SPEECH], executed
    for action in approval.actions:
        name = action.friendly_name
        agent_id = action.agent_id or approval.agent_id
        try:
            visible = await entity_is_visible(
                agent_id, action.entity_id, entity_index, fail_closed_on_metadata_gap=True
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Approval visibility recheck failed for %s", action.entity_id, exc_info=True)
            visible = False
        if not visible:
            logger.warning("Approved call dropped: %s is not visible to %s", action.entity_id, agent_id)
            speeches.append(f"I can no longer control {name}, so nothing was done for it.")
            continue
        with approved_calls([(action.domain, action.service, action.entity_id)]):
            verify = await call_service_with_verification(
                ha_client,
                action.domain,
                action.service,
                action.entity_id,
                service_data=action.service_data or None,
                expected_state=action.expected_state,
            )
        success = bool(verify.get("success"))
        if success:
            speeches.append(
                build_verified_speech(
                    friendly_name=name,
                    action_name=action.service,
                    expected_state=action.expected_state,
                    observed_state=verify.get("observed_state"),
                    verified=bool(verify.get("verified")),
                    action_phrases=_SUCCESS_PHRASES,
                )
            )
        else:
            speeches.append(failure_speech(action.service, name, verify))
        executed.append(
            ActionExecuted(
                action=action.service,
                entity_id=action.entity_id,
                success=success,
                new_state=verify.get("observed_state"),
                cacheable=False,
                service_data=action.public_service_data,
                executed_command=ExecutedCommand(
                    domain=action.domain,
                    service=action.service,
                    entity_id=action.entity_id,
                    service_data=action.public_service_data,
                ),
            )
        )
    return speeches, executed


def _summary(approval: PendingApproval | ApprovalTombstone) -> str:
    if isinstance(approval, PendingApproval):
        return "; ".join(f"{a.phrase} ({a.entity_id})" for a in approval.actions)
    return "; ".join(f"{domain}.{service} on {entity_id}" for domain, service, entity_id in approval.targets)


async def handle_pending_approval(
    orch: Any,
    task: Any,
    conversation_id: str,
    user_text: str,
    span_collector: Any = None,
) -> dict[str, Any] | None:
    """Answer a pending approval, or return ``None`` to run the normal pipeline."""
    context = getattr(task, "context", None)
    source = getattr(context, "source", None)
    if source_is_stateless_task(source) or not approval_is_interactive(context, conversation_id):
        return None
    status, payload = approval_store.get_entry(conversation_id)
    if status == "none" or payload is None:
        if status == "expired":
            approval_store.pop(conversation_id)
        return None

    approval = payload if isinstance(payload, PendingApproval) else None
    tombstone = payload if isinstance(payload, ApprovalTombstone) else None
    record = approval or tombstone
    assert record is not None
    # Only the requester's own channel and user may answer: a turn from
    # another source or user in the same conversation is unrelated, and the
    # held calls are dropped without executing anything.
    if source != record.source or getattr(context, "user_id", None) != record.user_id:
        logger.info("Approval answer from another source/user in %s; held calls dropped", conversation_id)
        async with _optional_span(span_collector, "approval", agent_id="orchestrator") as span:
            span["metadata"]["decision"] = "requester_mismatch"
            span["metadata"]["status"] = status
        approval_store.pop(conversation_id)
        return None
    question = approval.question if approval else tombstone.question  # type: ignore[union-attr]
    age_ms = round((time.monotonic() - record.created_at) * 1000)
    async with _optional_span(span_collector, "approval", agent_id="orchestrator") as span:
        decision = await classify_approval_answer(orch, question, _summary(record), user_text, span_collector)
        span["metadata"]["decision"] = decision
        span["metadata"]["status"] = status
        span["metadata"]["age_ms"] = age_ms
        span["metadata"]["agent_id"] = record.agent_id
        targets = (
            [(a.domain, a.service, a.entity_id) for a in approval.actions] if approval else list(tombstone.targets)  # type: ignore[union-attr]
        )
        span["metadata"]["actions"] = [{"domain": d, "service": s, "entity_id": e} for d, s, e in targets]
        if len(targets) == 1:
            span["metadata"]["domain"], span["metadata"]["service"], span["metadata"]["entity_id"] = targets[0]
    logger.info("Approval answer conversation=%s status=%s decision=%s", conversation_id, status, decision)

    language = record.language or "en"
    if tombstone is not None:
        approval_store.pop(conversation_id)
        if decision == "confirm":
            text = EXPIRED_SPEECH
        elif decision == "decline":
            text = EXPIRED_DECLINE_SPEECH
        else:
            return None
        return await _finish(orch, task, conversation_id, user_text, text, record.agent_id, language, span_collector)

    assert approval is not None
    if decision == "confirm":
        approval_store.pop(conversation_id)
        speeches, executed = await _execute_approved(orch, approval)
        text = " ".join(s for s in speeches if s) or EXPIRED_DECLINE_SPEECH
        return await _finish(
            orch,
            task,
            conversation_id,
            user_text,
            text,
            approval.agent_id,
            language,
            span_collector,
            action_executed=executed[0] if executed else None,
        )
    if decision == "decline":
        approval_store.pop(conversation_id)
        text = decline_speech(approval.phrases)
        return await _finish(orch, task, conversation_id, user_text, text, approval.agent_id, language, span_collector)
    if decision in ("modify", "unrelated"):
        # A new or amended request supersedes the held calls; the pending
        # question stays so classification can still merge an amendment.
        approval_store.pop(conversation_id)
        return None
    if not approval.reasked:
        approval.reasked = True
        approval_store.touch(conversation_id, approval)
        return await _finish(
            orch,
            task,
            conversation_id,
            user_text,
            approval.question,
            approval.agent_id,
            language,
            span_collector,
            followup=True,
        )
    approval_store.pop(conversation_id)
    return await _finish(
        orch, task, conversation_id, user_text, UNCLEAR_FINAL_SPEECH, approval.agent_id, language, span_collector
    )


async def _finish(
    orch: Any,
    task: Any,
    conversation_id: str,
    user_text: str,
    text: str,
    agent_id: str,
    language: str,
    span_collector: Any,
    *,
    action_executed: ActionExecuted | None = None,
    followup: bool = False,
) -> dict[str, Any]:
    """Speak, record the turn and the trace; the answer turn consumes the pending question."""
    from app.agents.cache_orchestrator import record_cache_lookup_skipped
    from app.agents.conversation_manager import extract_resolved_entities
    from app.agents.followup import resolve_voice_followup
    from app.agents.personalization import personalization_user_id
    from app.voice import discard_pending_speaker

    context = getattr(task, "context", None)
    source = getattr(context, "source", None)
    manager = orch._conversation_manager
    manager.pop_pending_question(conversation_id)
    discard_pending_speaker(conversation_id)
    speech = await orch._mediation.localize_message(text, language, span_collector=span_collector)
    if followup:
        manager.set_pending_question(conversation_id, speech, agent_id)
    voice_followup = await resolve_voice_followup(followup, source, span_collector=span_collector)
    # The approval answer is judged before any cache lookup: trace it as skipped.
    await record_cache_lookup_skipped(span_collector, "pending_approval")
    async with _optional_span(span_collector, "return", agent_id="orchestrator") as ret_span:
        ret_span["metadata"]["from_agent"] = agent_id
        ret_span["metadata"]["final_response"] = speech
        ret_span["metadata"]["approval_turn"] = True
        ret_span["metadata"]["voice_followup"] = voice_followup
        ret_span["metadata"]["cache_stored_action"] = False
        ret_span["metadata"]["cache_stored_routing"] = False
        prior_turns = await orch._get_turns(conversation_id)
        resolved_entities = (
            await extract_resolved_entities(action_executed, getattr(orch, "_entity_index", None))
            if action_executed is not None and action_executed.success
            else None
        )
        await orch._store_turn(
            conversation_id,
            user_text,
            speech,
            agent_id=agent_id,
            resolved_entities=resolved_entities,
            user_id=getattr(context, "user_id", None),
            language=getattr(context, "language", None),
            source=source,
            memory_user_id=personalization_user_id(context)[0] if context is not None else None,
        )
    if span_collector is not None:
        await orch._create_trace(
            span_collector,
            conversation_id,
            user_text,
            speech,
            agent_id,
            1.0,
            user_text,
            [(agent_id, user_text, 1.0)],
            prior_turns,
            task_context=context,
            voice_followup=voice_followup,
        )
    return {
        "speech": speech,
        "routed_to": agent_id,
        "action_executed": action_executed,
        "sanitized": True,
        "voice_followup": voice_followup,
    }
