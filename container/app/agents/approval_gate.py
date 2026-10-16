"""Executor-side approval gate for sensitive Home Assistant actions.

``call_service_with_verification`` asks :func:`evaluate_approval_gate`
before it calls a service. A sensitive call (see
``app.security.sensitive_actions``) that is not approved, not on the
allowlist, not exempt for the current call source and not exempt because
approval is disabled never runs here:

- On an interactive turn, :class:`app.agents.actionable.ActionableAgent`
  has opened an :class:`ApprovalCollector`; the call is *held* in it and
  the agent turns the held calls into one approval question.
- Otherwise (``ai_task``, ``background``, an OpenAI-compatible request
  without a stable chat id, or any caller without a collector) the call is
  *refused* with :func:`refusal_speech`.

The phrase helpers render English template text; the orchestrator localizes
template speech into the turn language like any other executor speech.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Literal

from app.security import sensitive_actions
from app.security.sensitive_actions import (
    SETTING_ALLOWLIST,
    current_call_source,
    is_call_approved,
    is_exempt_for_source,
    sensitive_reason_for_entity,
)

GateStatus = Literal["pending", "refused"]

# Request sources whose clients can carry an answer turn back to the same
# conversation. ``openai`` additionally needs a stable chat id
# (``TaskContext.conversation_resumable``).
APPROVAL_INTERACTIVE_SOURCES: frozenset[str] = frozenset({"ha", "chat", "api", "openai"})


@dataclass
class HeldCall:
    """A sensitive service call held for approval (service data may carry a code)."""

    domain: str
    service: str
    entity_id: str
    reason: str
    expected_state: str | None = None
    service_data: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ApprovalCollector:
    """Per-turn collector of held calls, opened by the actionable agent."""

    interactive: bool
    held: list[HeldCall] = field(default_factory=list)


@dataclass(frozen=True)
class GateDecision:
    status: GateStatus
    reason: str


_current_collector: ContextVar[ApprovalCollector | None] = ContextVar("approval_collector", default=None)


def open_approval_collector(interactive: bool) -> tuple[Token, ApprovalCollector]:
    collector = ApprovalCollector(interactive=interactive)
    return _current_collector.set(collector), collector


def close_approval_collector(token: Token) -> None:
    _current_collector.reset(token)


@contextmanager
def approval_collector(interactive: bool) -> Iterator[ApprovalCollector]:
    token, collector = open_approval_collector(interactive)
    try:
        yield collector
    finally:
        close_approval_collector(token)


def approval_is_interactive(context: Any, conversation_id: str | None) -> bool:
    """True when the turn can ask for approval and receive the answer turn."""
    if context is None or not conversation_id:
        return False
    if getattr(context, "source", None) not in APPROVAL_INTERACTIVE_SOURCES:
        return False
    return bool(getattr(context, "conversation_resumable", True))


async def evaluate_approval_gate(
    ha_client: Any,
    domain: str,
    service: str,
    entity_id: str,
    *,
    service_data: dict[str, Any] | None = None,
    expected_state: str | None = None,
    device_class: str | None = None,
    device_class_known: bool = False,
) -> GateDecision | None:
    """Hold or refuse an unapproved sensitive call; ``None`` lets it run."""
    reason = await sensitive_reason_for_entity(
        domain,
        service,
        entity_id,
        device_class=device_class,
        device_class_known=device_class_known,
        state_fetcher=getattr(ha_client, "get_state", None),
    )
    if (
        reason is None
        or is_call_approved(domain, service, entity_id)
        or is_exempt_for_source(current_call_source(), domain, service)
    ):
        return None
    settings = await sensitive_actions.load_approval_settings()
    if not settings.enabled or entity_id.strip().lower() in settings.allowlist:
        return None
    collector = _current_collector.get()
    if collector is not None and collector.interactive:
        collector.held.append(
            HeldCall(
                domain=domain,
                service=service,
                entity_id=entity_id,
                reason=reason,
                expected_state=expected_state,
                service_data=dict(service_data or {}),
            )
        )
        return GateDecision("pending", reason)
    return GateDecision("refused", reason)


# ---------------------------------------------------------------------------
# English template phrases (localized downstream like other template speech)
# ---------------------------------------------------------------------------

_ALARM_MODES: dict[str, str] = {
    "alarm_arm_home": "home",
    "alarm_arm_away": "away",
    "alarm_arm_night": "night",
    "alarm_arm_vacation": "vacation",
    "alarm_arm_custom_bypass": "custom bypass",
}


def _verb_and_object(domain: str, service: str, name: str, service_data: dict[str, Any] | None) -> tuple[str, str]:
    """``(verb, rest)`` so ``f"{verb} {rest}"`` reads "unlock Front Door"."""
    data = service_data or {}
    if domain == "lock":
        return ("unlock" if service == "unlock" else "open"), name
    if domain == "alarm_control_panel":
        if service == "alarm_disarm":
            return "disarm", name
        mode = _ALARM_MODES.get(service)
        return "arm", f"{name} in {mode} mode" if mode else name
    if domain == "cover":
        if service == "set_cover_position" and data.get("position") is not None:
            return "set", f"{name} to {data.get('position')}%"
        if service == "toggle":
            return "toggle", name
        return "open", name
    return service.replace("_", " "), name


_GERUNDS: dict[str, str] = {
    "unlock": "Unlocking",
    "open": "Opening",
    "disarm": "Disarming",
    "arm": "Arming",
    "set": "Setting",
    "toggle": "Toggling",
}


def action_phrase(domain: str, service: str, name: str, service_data: dict[str, Any] | None = None) -> str:
    """Imperative phrase: "unlock Front Door", "arm Alarm in away mode"."""
    verb, rest = _verb_and_object(domain, service, name, service_data)
    return f"{verb} {rest}"


def approval_question(phrases: list[str]) -> str:
    """One question for every held call: "Should I unlock X and open Y?"."""
    if not phrases:
        return "Should I go ahead?"
    joined = phrases[0] if len(phrases) == 1 else ", ".join(phrases[:-1]) + " and " + phrases[-1]
    return f"Should I {joined}?"


def decline_speech(phrases: list[str]) -> str:
    if not phrases:
        return "OK, I won't do it."
    joined = phrases[0] if len(phrases) == 1 else ", ".join(phrases[:-1]) + " or " + phrases[-1]
    return f"OK, I won't {joined}."


def refusal_speech(
    domain: str, service: str, name: str, entity_id: str, service_data: dict[str, Any] | None = None
) -> str:
    """Clear refusal for a channel that cannot ask for approval.

    ``entity_id`` is accepted for a uniform call shape but not spoken: raw
    ids read badly and their underscores collide with Markdown stripping.
    """
    del entity_id
    verb, rest = _verb_and_object(domain, service, name, service_data)
    gerund = _GERUNDS.get(verb, verb.capitalize())
    return (
        f"{gerund} {rest} needs a spoken or typed confirmation; it is not allowed from automations "
        f"or channels without a follow-up turn. Add the entity to {SETTING_ALLOWLIST} to allow it."
    )


def gate_speech(verify: dict[str, Any], action_name: str, friendly_name: str) -> str | None:
    """Speech for a gated ``call_service_with_verification`` result, else ``None``.

    A held (pending) call speaks nothing itself: the agent replaces the turn
    speech with one approval question for every held call.
    """
    status = verify.get("approval")
    if status == "pending":
        return ""
    if status == "refused":
        return refusal_speech(
            str(verify.get("approval_domain") or ""),
            str(verify.get("approval_service") or action_name),
            friendly_name,
            str(verify.get("entity_id") or ""),
            verify.get("approval_service_data"),
        )
    return None
