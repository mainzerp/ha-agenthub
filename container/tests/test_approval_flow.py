"""Sensitive-action approval end to end through the real orchestrator pipeline.

Uses the real-scenario harness (real orchestrator, dispatcher, agents, entity
index; recording HA client; deterministic LLM stub keyed by agent id). The
approval answer is classified by the orchestrator LLM (agent id
``orchestrator``), so each answer turn feeds one decision reply there.
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_litellm_mock = MagicMock()
_litellm_mock.exceptions.AuthenticationError = type("AuthenticationError", (Exception,), {})
_litellm_mock.exceptions.APIError = type("APIError", (Exception,), {})
_litellm_mock.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _litellm_mock)

from app.agents.approval import PendingApproval, approval_store  # noqa: E402
from app.agents.automation_confirmation import PendingAutomationChange, confirmation_store  # noqa: E402
from app.analytics.tracer import SpanCollector  # noqa: E402
from app.models.agent import IngressTask, TaskContext  # noqa: E402
from app.security.sensitive_actions import is_call_approved  # noqa: E402
from tests.scenarios.runner import _temp_db, build_pipeline  # noqa: E402
from tests.scenarios.types import (  # noqa: E402
    EntityOverride,
    Expected,
    LlmReplies,
    Preconditions,
    Scenario,
    ScenarioContext,
)

CONV = "approval-conv"
CONFIRM = '{"decision": "confirm"}'
DECLINE = '{"decision": "decline"}'
UNRELATED = '{"decision": "unrelated"}'
UNCLEAR = "I am not sure what the user wants."


def _block(action: str, entity: str, parameters: dict | None = None) -> str:
    import json

    return "```json\n" + json.dumps({"action": action, "entity": entity, "parameters": parameters or {}}) + "\n```"


class _Harness:
    def __init__(self, handles, source: str, language: str) -> None:
        self.handles = handles
        self.source = source
        self.language = language

    @property
    def calls(self) -> list[tuple[str, str, str | None]]:
        return [(c.domain, c.service, c.entity_id) for c in self.handles.ha_client.calls]

    def feed(self, agent_id: str, reply: str) -> None:
        self.handles.llm.feed(agent_id, reply)

    async def turn(
        self,
        text: str,
        *,
        span_collector=None,
        conversation_resumable: bool = True,
        source: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        task = IngressTask(
            description=text,
            conversation_id=CONV,
            context=TaskContext(
                source=source or self.source,
                language=self.language,
                area_id="hallway",
                user_id=user_id,
                conversation_resumable=conversation_resumable,
            ),
        )
        task.span_collector = span_collector
        return await self.handles.orchestrator.handle_task(task)


@asynccontextmanager
async def _pipeline(tmp_path, *, source: str = "ha", settings: dict[str, str] | None = None, language: str = "en"):
    from app.agents.actionable import CoverAgent
    from app.db.repository import SettingsRepository

    scenario = Scenario(
        id="approval",
        agent="security-agent",
        description="approval flow",
        snapshot="home_default",
        language=language,
        request_text="",
        context=ScenarioContext(source=source),
        preconditions=Preconditions(
            entity_overrides=[
                EntityOverride(entity_id="lock.back_door", state="locked"),
                EntityOverride(entity_id="alarm_control_panel.home_alarm", state="armed_away"),
            ]
        ),
        llm=LlmReplies(),
        expected=Expected(),
    )
    db_path = tmp_path / "approval.db"
    await SettingsRepository._cache_invalidate()
    async with _temp_db(db_path):
        if settings:
            from app.db.schema import get_db_write

            async with get_db_write() as db:
                for key, value in settings.items():
                    await db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
                await db.commit()
        handles = await build_pipeline(scenario, db_path)
        await handles.registry.register(
            CoverAgent(
                ha_client=handles.ha_client, entity_index=handles.entity_index, entity_matcher=handles.entity_matcher
            )
        )
        handles.llm._scenario_id = "approval"

        async def _complete(agent_id, messages, **kwargs):
            return await handles.llm.complete(agent_id, messages, **kwargs)

        with patch("app.llm.client.complete", new=_complete):
            yield _Harness(handles, source, language)
    await SettingsRepository._cache_invalidate()


async def _ask_unlock(h: _Harness) -> dict:
    h.feed("orchestrator", "security-agent (95%): Unlock the back door")
    h.feed("security-agent", _block("unlock", "back door lock"))
    return await h.turn("unlock the back door")


# ---------------------------------------------------------------------------
# Interactive flow
# ---------------------------------------------------------------------------


async def test_ask_then_yes_executes_once_under_token(tmp_path):
    async with _pipeline(tmp_path) as h:
        first = await _ask_unlock(h)
        assert h.calls == []
        assert first["speech"] == "Should I unlock Back Door Lock?"
        assert first["voice_followup"] is True
        assert first.get("action_executed") is None
        assert approval_store.get_entry(CONV)[0] == "active"
        assert h.handles.orchestrator._conversation_manager.has_pending_question(CONV)

        tokens_seen: list[bool] = []
        original = h.handles.ha_client.call_service

        async def _spy(domain, service, entity_id=None, service_data=None, **kwargs):
            tokens_seen.append(is_call_approved(domain, service, entity_id))
            return await original(domain, service, entity_id, service_data, **kwargs)

        h.handles.ha_client.call_service = _spy
        h.feed("orchestrator", CONFIRM)
        second = await h.turn("yes")
        assert h.calls == [("lock", "unlock", "lock.back_door")]
        assert tokens_seen == [True]
        assert "unlocked" in second["speech"]
        assert second["routed_to"] == "security-agent"
        assert second["action_executed"].cacheable is False
        assert approval_store.get_entry(CONV)[0] == "none"
        assert not h.handles.orchestrator._conversation_manager.has_pending_question(CONV)
        assert not is_call_approved("lock", "unlock", "lock.back_door")


async def test_ask_then_no_declines(tmp_path):
    async with _pipeline(tmp_path) as h:
        await _ask_unlock(h)
        h.feed("orchestrator", DECLINE)
        result = await h.turn("no, leave it")
        assert h.calls == []
        assert result["speech"] == "OK, I won't unlock Back Door Lock."
        assert approval_store.get_entry(CONV)[0] == "none"


async def test_unrelated_answer_drops_approval_and_runs_normal_pipeline(tmp_path):
    async with _pipeline(tmp_path) as h:
        await _ask_unlock(h)
        h.feed("orchestrator", UNRELATED)
        h.feed("orchestrator", "light-agent (95%): Turn on the kitchen ceiling light")
        h.feed("light-agent", _block("turn_on", "Kitchen Ceiling"))
        result = await h.turn("turn on the kitchen ceiling light")
        assert ("lock", "unlock", "lock.back_door") not in h.calls
        assert any(domain == "light" and service == "turn_on" for domain, service, _ in h.calls)
        assert result["routed_to"] == "light-agent"
        assert approval_store.get_entry(CONV)[0] == "none"


async def test_unclear_answer_asks_once_more_then_gives_up(tmp_path):
    async with _pipeline(tmp_path) as h:
        await _ask_unlock(h)
        h.feed("orchestrator", UNCLEAR)
        again = await h.turn("hmm")
        assert again["speech"] == "Should I unlock Back Door Lock?"
        assert again["voice_followup"] is True
        assert approval_store.get_entry(CONV)[0] == "active"
        h.feed("orchestrator", UNCLEAR)
        final = await h.turn("well")
        assert final["speech"] == "I did not get a clear yes, so nothing was done."
        assert final["voice_followup"] is False
        assert h.calls == []
        assert approval_store.get_entry(CONV)[0] == "none"


async def test_unclear_then_yes_still_executes(tmp_path):
    async with _pipeline(tmp_path) as h:
        await _ask_unlock(h)
        h.feed("orchestrator", UNCLEAR)
        await h.turn("hmm")
        h.feed("orchestrator", CONFIRM)
        await h.turn("yes, do it")
        assert h.calls == [("lock", "unlock", "lock.back_door")]


async def test_expired_approval_reports_and_does_nothing(tmp_path):
    async with _pipeline(tmp_path) as h:
        await _ask_unlock(h)
        _stored_at, payload = approval_store._entries[CONV]
        approval_store._entries[CONV] = (time.monotonic() - 120, payload)
        h.feed("orchestrator", CONFIRM)
        result = await h.turn("yes")
        assert result["speech"] == "That request expired, nothing was done."
        assert h.calls == []
        assert approval_store.get_entry(CONV)[0] == "none"


def test_approval_ttl_is_sixty_seconds():
    assert approval_store.ttl_seconds == 60.0


async def test_visibility_is_rechecked_on_confirm(tmp_path):
    async with _pipeline(tmp_path) as h:
        await _ask_unlock(h)
        h.feed("orchestrator", CONFIRM)
        with patch("app.entity.visibility.entity_is_visible", new=AsyncMock(return_value=False)) as visible:
            result = await h.turn("yes")
        visible.assert_awaited()
        assert visible.await_args.args[:2] == ("security-agent", "lock.back_door")
        assert h.calls == []
        assert "no longer control" in result["speech"]


async def test_multi_action_runs_safe_blocks_and_batches_sensitive_ones(tmp_path):
    async with _pipeline(tmp_path) as h:
        h.feed("orchestrator", "security-agent (95%): Lock the front door, unlock the back door, disarm the alarm")
        h.feed(
            "security-agent",
            "\n".join(
                [
                    _block("lock", "front door lock"),
                    _block("unlock", "back door lock"),
                    _block("alarm_disarm", "home alarm"),
                ]
            ),
        )
        first = await h.turn("lock the front door, unlock the back door and disarm the alarm")
        assert h.calls == [("lock", "lock", "lock.front_door")]
        assert first["speech"].endswith("Should I unlock Back Door Lock and disarm Home Alarm?")
        assert first["speech"].count("?") == 1
        status, pending = approval_store.get_entry(CONV)
        assert status == "active"
        assert isinstance(pending, PendingApproval)
        assert [(a.service, a.entity_id) for a in pending.actions] == [
            ("unlock", "lock.back_door"),
            ("alarm_disarm", "alarm_control_panel.home_alarm"),
        ]

        h.feed("orchestrator", CONFIRM)
        await h.turn("yes")
        assert h.calls[1:] == [
            ("lock", "unlock", "lock.back_door"),
            ("alarm_control_panel", "alarm_disarm", "alarm_control_panel.home_alarm"),
        ]


async def test_garage_cover_open_needs_approval_blinds_do_not(tmp_path):
    async with _pipeline(tmp_path) as h:
        h.feed("orchestrator", "cover-agent (95%): Open the living room blinds")
        h.feed("cover-agent", _block("close_cover", "living room blinds"))
        await h.turn("close the living room blinds")
        assert h.calls == [("cover", "close_cover", "cover.living_room_blinds")]

        h.feed("orchestrator", "cover-agent (95%): Open the garage door")
        h.feed("cover-agent", _block("open_cover", "garage door"))
        result = await h.turn("open the garage door")
        assert result["speech"] == "Should I open Garage Door?"
        assert len(h.calls) == 1
        h.feed("orchestrator", CONFIRM)
        await h.turn("yes")
        assert h.calls[-1] == ("cover", "open_cover", "cover.garage_door")


# ---------------------------------------------------------------------------
# Settings and non-interactive channels
# ---------------------------------------------------------------------------


async def test_disabled_approval_executes_immediately(tmp_path):
    async with _pipeline(tmp_path, settings={"approval.enabled": "false"}) as h:
        result = await _ask_unlock(h)
        assert h.calls == [("lock", "unlock", "lock.back_door")]
        assert "unlocked" in result["speech"]
        assert approval_store.get_entry(CONV)[0] == "none"


async def test_allowlisted_entity_executes_immediately(tmp_path):
    async with _pipeline(tmp_path, settings={"approval.allowlist_entities": '["lock.back_door"]'}) as h:
        await _ask_unlock(h)
        assert h.calls == [("lock", "unlock", "lock.back_door")]


async def test_ai_task_is_refused_with_plain_result_text(tmp_path):
    async with _pipeline(tmp_path, source="ai_task") as h:
        result = await _ask_unlock(h)
        assert h.calls == []
        assert "needs a spoken or typed confirmation" in result["speech"]
        assert "approval.allowlist_entities" in result["speech"]
        assert not result.get("error")
        action = result.get("action_executed")
        success = action.success if hasattr(action, "success") else (action or {}).get("success")
        assert not success
        assert approval_store.get_entry(CONV)[0] == "none"
        assert result.get("voice_followup") is not True


async def _ask_arm(h: _Harness) -> dict:
    h.feed("orchestrator", "security-agent (95%): Arm the alarm in home mode")
    h.feed("security-agent", _block("alarm_arm_home", "home alarm"))
    return await h.turn("arm the alarm in home mode")


async def test_interactive_alarm_arming_still_asks(tmp_path):
    # Only delayed actions may arm without approval; a conversation turn asks.
    async with _pipeline(tmp_path) as h:
        result = await _ask_arm(h)
        assert h.calls == []
        assert result["speech"].startswith("Should I arm")
        assert "home mode" in result["speech"]
        assert approval_store.get_entry(CONV)[0] == "active"


async def test_ai_task_alarm_arming_is_still_refused(tmp_path):
    async with _pipeline(tmp_path, source="ai_task") as h:
        result = await _ask_arm(h)
        assert h.calls == []
        assert "needs a spoken or typed confirmation" in result["speech"]
        assert approval_store.get_entry(CONV)[0] == "none"


async def test_openai_without_stable_chat_id_is_refused(tmp_path):
    async with _pipeline(tmp_path, source="openai") as h:
        h.feed("orchestrator", "security-agent (95%): Unlock the back door")
        h.feed("security-agent", _block("unlock", "back door lock"))
        result = await h.turn("unlock the back door", conversation_resumable=False)
        assert h.calls == []
        assert "needs a spoken or typed confirmation" in result["speech"]
        assert approval_store.get_entry(CONV)[0] == "none"


async def test_chat_source_asks_without_reopening_the_microphone(tmp_path):
    async with _pipeline(tmp_path, source="chat") as h:
        result = await _ask_unlock(h)
        assert result["speech"] == "Should I unlock Back Door Lock?"
        assert approval_store.get_entry(CONV)[0] == "active"


# ---------------------------------------------------------------------------
# Secrets and coexistence with the automation confirmation
# ---------------------------------------------------------------------------


async def test_alarm_code_never_reaches_logs_spans_or_repr(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    collector = SpanCollector("trace-approval-1", source="ha")
    async with _pipeline(tmp_path) as h:
        h.feed("orchestrator", "security-agent (95%): Disarm the alarm with code 4711")
        h.feed("security-agent", _block("alarm_disarm", "home alarm", {"code": "4711"}))
        await h.turn("disarm the alarm with code 4711", span_collector=collector)
        _status, pending = approval_store.get_entry(CONV)
        assert "4711" not in repr(pending)
        assert pending.actions[0].service_data == {"code": "4711"}

        answer_collector = SpanCollector("trace-approval-2", source="ha")
        h.feed("orchestrator", CONFIRM)
        await h.turn("yes", span_collector=answer_collector)
        assert h.handles.ha_client.calls[-1].service_data.get("code") == "4711"

        approval_spans = [s for s in answer_collector.get_spans() if s.get("span_name") == "approval"]
        assert approval_spans and approval_spans[0]["metadata"]["decision"] == "confirm"
        assert approval_spans[0]["metadata"]["service"] == "alarm_disarm"
        assert isinstance(approval_spans[0]["metadata"]["age_ms"], int)
        # The user's own utterance is traced as user input; the approval
        # machinery (executor, approval and HA call spans) never echoes the code.
        for span in collector.get_spans() + answer_collector.get_spans():
            if span.get("span_name") in ("approval", "ha_action", "return", "ha_call"):
                assert "4711" not in repr(span.get("metadata")), span.get("span_name")
    approval_loggers = (
        "app.agents.approval",
        "app.agents.approval_gate",
        "app.security.sensitive_actions",
        "app.agents.action_executor",
        "app.agents.security_executor",
        "app.agents.actionable",
    )
    logged = [r.getMessage() for r in caplog.records if r.name.startswith(approval_loggers)]
    assert logged
    assert not [line for line in logged if "4711" in line]


async def test_newer_question_wins_over_pending_automation_proposal(tmp_path):
    async with _pipeline(tmp_path) as h:
        confirmation_store.put(
            CONV,
            PendingAutomationChange(kind="delete", alias="A", summary="Delete 'A'.", question="Shall I delete it?"),
        )
        await _ask_unlock(h)
        assert confirmation_store.get(CONV)[0] == "none"
        assert approval_store.get_entry(CONV)[0] == "active"
        confirmation_store.put(
            CONV,
            PendingAutomationChange(kind="delete", alias="A", summary="Delete 'A'.", question="Shall I delete it?"),
        )
        assert approval_store.get_entry(CONV)[0] == "none"


@pytest.mark.usefixtures("approval_disabled")
async def test_sensitive_results_are_never_cacheable():
    from app.agents.cover_executor import execute_cover_action
    from app.agents.security_executor import execute_security_action

    async def _resolved(*_args, **kwargs):
        entity_id = kwargs.get("direct_entity_id") or "lock.front_door"
        return {"not_found_result": None, "entity_id": entity_id, "friendly_name": "X", "resolution": {}}

    def _ha(state: dict) -> AsyncMock:
        ha = AsyncMock()
        ha.get_state = AsyncMock(return_value=state)
        ha.call_service = AsyncMock(return_value=[])
        ha.expect_state = None
        return ha

    with patch("app.agents.security_executor.resolve_and_validate_entity", new=_resolved):
        unlock = await execute_security_action(
            {"action": "unlock", "entity": "front door", "entity_id": "lock.front_door"},
            _ha({"state": "locked", "attributes": {}}),
            MagicMock(),
            MagicMock(),
        )
        lock = await execute_security_action(
            {"action": "lock", "entity": "front door", "entity_id": "lock.front_door"},
            _ha({"state": "unlocked", "attributes": {}}),
            MagicMock(),
            MagicMock(),
        )
    assert unlock["success"] and unlock["cacheable"] is False
    assert lock["success"] and lock.get("cacheable", True) is True

    with patch("app.agents.cover_executor.resolve_and_validate_entity", new=_resolved):
        garage = await execute_cover_action(
            {"action": "open_cover", "entity": "garage", "entity_id": "cover.garage_door"},
            _ha({"state": "closed", "attributes": {"device_class": "garage"}}),
            MagicMock(),
            MagicMock(),
        )
        blinds = await execute_cover_action(
            {"action": "open_cover", "entity": "blinds", "entity_id": "cover.blinds"},
            _ha({"state": "closed", "attributes": {"device_class": "blind"}}),
            MagicMock(),
            MagicMock(),
        )
    assert garage["success"] and garage["cacheable"] is False
    assert blinds["success"] and blinds.get("cacheable", True) is True


# ---------------------------------------------------------------------------
# Requester binding and merged multi-agent batches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer_source", "answer_user"),
    [("chat", None), ("ha", "someone-else")],
)
async def test_yes_from_another_source_or_user_executes_nothing(tmp_path, answer_source, answer_user):
    async with _pipeline(tmp_path) as h:
        await _ask_unlock(h)
        _status, pending = approval_store.get_entry(CONV)
        assert (pending.source, pending.user_id) == ("ha", None)
        # Mismatch: handled as unrelated without an approval LLM call; the
        # turn runs through the normal pipeline instead.
        h.feed("orchestrator", "general-agent (95%): Answer yes")
        h.feed("general-agent", "Yes to what?")
        await h.turn("yes", source=answer_source, user_id=answer_user)
        assert h.calls == []
        assert approval_store.get_entry(CONV)[0] == "none"


async def test_yes_from_the_same_user_executes(tmp_path):
    async with _pipeline(tmp_path) as h:
        h.feed("orchestrator", "security-agent (95%): Unlock the back door")
        h.feed("security-agent", _block("unlock", "back door lock"))
        await h.turn("unlock the back door", user_id="user-1")
        h.feed("orchestrator", CONFIRM)
        await h.turn("yes", user_id="user-1")
        assert h.calls == [("lock", "unlock", "lock.back_door")]


async def test_merged_batch_rechecks_visibility_per_holding_agent():
    from types import SimpleNamespace

    from app.agents import approval as ap

    store = ap.ApprovalStore()
    lock = ap.PendingSensitiveAction("lock", "unlock", "lock.front", "Front", "lock_open", agent_id="security-agent")
    garage = ap.PendingSensitiveAction(
        "cover", "open_cover", "cover.garage", "Garage", "access_cover_open", agent_id="cover-agent"
    )
    store.put("c1", ap.PendingApproval("c1", "security-agent", [lock], trace_id="t"))
    store.put("c1", ap.PendingApproval("c1", "cover-agent", [garage], trace_id="t"))
    _status, merged = store.get_entry("c1")
    assert [(a.agent_id, a.entity_id) for a in merged.actions] == [
        ("security-agent", "lock.front"),
        ("cover-agent", "cover.garage"),
    ]

    seen: list[tuple[str, str]] = []

    async def _visible(agent_id, entity_id, _index, **_kwargs):
        seen.append((agent_id, entity_id))
        return agent_id == "security-agent"

    orch = SimpleNamespace(_ha_client=object(), _entity_index=None)
    verify = AsyncMock(return_value={"success": True, "verified": True, "observed_state": "unlocked"})
    with (
        patch("app.entity.visibility.entity_is_visible", _visible),
        patch.object(ap, "call_service_with_verification", verify),
    ):
        speeches, executed = await ap._execute_approved(orch, merged)
    assert seen == [("security-agent", "lock.front"), ("cover-agent", "cover.garage")]
    assert [e.entity_id for e in executed] == ["lock.front"]
    assert verify.await_count == 1
    assert "no longer control Garage" in " ".join(speeches)
