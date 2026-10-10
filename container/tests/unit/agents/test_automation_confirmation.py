"""Automation create/update/delete: validation, proposal, confirmation (issue #132, T8)."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.helpers import make_dispatch_task, make_entity_index_entry

from app.agents.actionable import AutomationAgent
from app.agents.automation_confirmation import (
    AutomationConfirmationStore,
    PendingAutomationChange,
    confirmation_store,
    parse_decision,
)
from app.agents.automation_executor import apply_pending_automation_change, execute_automation_action
from app.models.agent import TaskContext

pytestmark = pytest.mark.asyncio

CONV = "conv-1"
_KNOWN = {
    "light.living_room",
    "light.kitchen",
    "person.me",
    "automation.morning_routine",
    "automation.vacation_mode",
    "script.goodnight",
}


@pytest.fixture(autouse=True)
def _clean_state():
    confirmation_store.clear()
    with patch("app.entity.visibility.EntityVisibilityRepository.get_rules", new=AsyncMock(return_value=[])):
        yield
    confirmation_store.clear()


def _index(known: set[str] = _KNOWN):
    index = MagicMock()
    index.get_by_id = MagicMock(
        side_effect=lambda eid: make_entity_index_entry(eid, eid, area=None) if eid in known else None
    )
    return index


def _create_action(config: dict) -> dict:
    return {"action": "create_automation", "entity": "Evening Lights", "parameters": {"config": config}}


_GOOD_CONFIG = {
    "alias": "Evening Lights",
    "triggers": [{"trigger": "sun", "event": "sunset"}],
    "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.living_room"}}],
}


def _resolving_matcher(entity_id: str, name: str):
    matcher = AsyncMock()
    matcher.match = AsyncMock(return_value=[MagicMock(entity_id=entity_id, friendly_name=name)])
    return matcher


class TestCreateProposal:
    async def test_first_turn_writes_nothing_and_asks(self):
        ha = AsyncMock()
        result = await execute_automation_action(
            _create_action(dict(_GOOD_CONFIG)),
            ha,
            _index(),
            None,
            agent_id="automation-agent",
            conversation_id=CONV,
        )
        assert result["success"] is True
        assert result["speech"].endswith("Shall I save this?")
        assert "light.turn_on" in result["speech"] and "light.living_room" in result["speech"]
        assert result["voice_followup"] is True
        assert result["cacheable"] is False
        ha.save_automation_config.assert_not_awaited()
        status, change = confirmation_store.get(CONV)
        assert status == "active" and change.kind == "create"

    async def test_without_conversation_id_is_refused(self):
        ha = AsyncMock()
        result = await execute_automation_action(
            _create_action(dict(_GOOD_CONFIG)), ha, _index(), None, agent_id="automation-agent"
        )
        assert result["success"] is False
        ha.save_automation_config.assert_not_awaited()

    async def test_unknown_entity_rejected_by_name(self):
        config = dict(_GOOD_CONFIG, actions=[{"action": "light.turn_on", "target": {"entity_id": "light.garage"}}])
        result = await execute_automation_action(
            _create_action(config), AsyncMock(), _index(), None, agent_id="automation-agent", conversation_id=CONV
        )
        assert result["success"] is False
        assert "light.garage" in result["speech"]
        assert confirmation_store.get(CONV)[0] == "none"

    async def test_invisible_entity_rejected(self):
        async def _rules(agent_id: str):
            return [{"rule_type": "domain_include", "rule_value": "automation"}]

        with patch("app.entity.visibility.EntityVisibilityRepository.get_rules", new=AsyncMock(side_effect=_rules)):
            result = await execute_automation_action(
                _create_action(dict(_GOOD_CONFIG)),
                AsyncMock(),
                _index(),
                None,
                agent_id="automation-agent",
                conversation_id=CONV,
            )
        assert result["success"] is False
        assert "light.living_room" in result["speech"]

    async def test_forbidden_service_domain_rejected(self):
        config = dict(_GOOD_CONFIG, actions=[{"service": "homeassistant.restart"}])
        result = await execute_automation_action(
            _create_action(config), AsyncMock(), _index(), None, agent_id="automation-agent", conversation_id=CONV
        )
        assert result["success"] is False
        assert "homeassistant.restart" in result["speech"]

    async def test_unlock_rejected_but_lock_allowed(self):
        unlock = dict(_GOOD_CONFIG, actions=[{"action": "lock.unlock", "target": {"entity_id": "light.kitchen"}}])
        result = await execute_automation_action(
            _create_action(unlock), AsyncMock(), _index(), None, agent_id="automation-agent", conversation_id=CONV
        )
        assert result["success"] is False
        assert "lock.unlock" in result["speech"]

    async def test_device_and_area_targets_rejected(self):
        config = dict(_GOOD_CONFIG, actions=[{"action": "light.turn_on", "target": {"area_id": "kitchen"}}])
        result = await execute_automation_action(
            _create_action(config), AsyncMock(), _index(), None, agent_id="automation-agent", conversation_id=CONV
        )
        assert result["success"] is False
        assert "area_id" in result["speech"]

    async def test_templated_entity_rejected(self):
        config = dict(
            _GOOD_CONFIG,
            actions=[{"action": "light.turn_on", "target": {"entity_id": "{{ states.light | first }}"}}],
        )
        result = await execute_automation_action(
            _create_action(config), AsyncMock(), _index(), None, agent_id="automation-agent", conversation_id=CONV
        )
        assert result["success"] is False
        assert "templated" in result["speech"]

    async def test_named_script_service_is_validated_as_entity(self):
        config = dict(_GOOD_CONFIG, actions=[{"action": "script.unknown_script"}])
        result = await execute_automation_action(
            _create_action(config), AsyncMock(), _index(), None, agent_id="automation-agent", conversation_id=CONV
        )
        assert result["success"] is False
        assert "script.unknown_script" in result["speech"]

    async def test_missing_trigger_rejected(self):
        config = {"alias": "X", "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]}
        result = await execute_automation_action(
            _create_action(config), AsyncMock(), _index(), None, agent_id="automation-agent", conversation_id=CONV
        )
        assert result["success"] is False


class TestApplyCreate:
    async def test_apply_creates_with_unique_id(self):
        ha = AsyncMock()
        ha.get_automation_config = AsyncMock(side_effect=[{"existing": True}, None])
        change = PendingAutomationChange(
            kind="create", alias="Evening Lights", summary="s", question="q", config=dict(_GOOD_CONFIG)
        )
        result = await apply_pending_automation_change(change, ha, _index(), agent_id="automation-agent")
        assert result["success"] is True
        assert result["entity_id"] == "ah_evening_lights_2"
        ha.save_automation_config.assert_awaited_once_with("ah_evening_lights_2", _GOOD_CONFIG)
        assert result["cacheable"] is False

    async def test_apply_create_ha_error(self):
        ha = AsyncMock()
        ha.get_automation_config = AsyncMock(return_value=None)
        ha.save_automation_config = AsyncMock(side_effect=Exception("Connection refused"))
        change = PendingAutomationChange(
            kind="create", alias="Evening Lights", summary="s", question="q", config=dict(_GOOD_CONFIG)
        )
        result = await apply_pending_automation_change(change, ha, _index(), agent_id="automation-agent")
        assert result["success"] is False
        assert "Failed" in result["speech"]


_EXISTING = {
    "id": "morning_routine_001",
    "alias": "Morning Routine",
    "trigger": [{"platform": "time", "at": "07:00:00"}],
    "action": [{"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}],
}


def _update_ha(existing: dict | None = None):
    ha = AsyncMock()
    ha.get_state = AsyncMock(return_value={"state": "on", "attributes": {"id": "morning_routine_001"}})
    ha.get_automation_config = AsyncMock(
        return_value=existing if existing is not None else json.loads(json.dumps(_EXISTING))
    )
    return ha


class TestUpdateProposal:
    async def test_add_condition_merges_into_existing_config(self):
        ha = _update_ha()
        result = await execute_automation_action(
            {
                "action": "update_automation",
                "entity": "morning routine",
                "parameters": {
                    "add": {"conditions": [{"condition": "state", "entity_id": "person.me", "state": "home"}]}
                },
            },
            ha,
            _index(),
            _resolving_matcher("automation.morning_routine", "Morning Routine"),
            agent_id="automation-agent",
            conversation_id=CONV,
        )
        assert result["success"] is True, result["speech"]
        ha.save_automation_config.assert_not_awaited()
        change = confirmation_store.get(CONV)[1]
        assert change.kind == "update"
        merged = change.config
        assert merged["id"] == "morning_routine_001"
        assert merged["trigger"] == _EXISTING["trigger"]
        assert merged["action"] == _EXISTING["action"]
        assert merged["conditions"] == [{"condition": "state", "entity_id": "person.me", "state": "home"}]

    async def test_legacy_full_config_only_replaces_given_keys(self):
        ha = _update_ha()
        await execute_automation_action(
            {
                "action": "update_automation",
                "entity": "morning routine",
                "parameters": {"config": {"trigger": [{"platform": "time", "at": "07:30:00"}]}},
            },
            ha,
            _index(),
            _resolving_matcher("automation.morning_routine", "Morning Routine"),
            agent_id="automation-agent",
            conversation_id=CONV,
        )
        merged = confirmation_store.get(CONV)[1].config
        assert merged["trigger"] == [{"platform": "time", "at": "07:30:00"}]
        assert "triggers" not in merged
        assert merged["action"] == _EXISTING["action"]
        assert merged["alias"] == "Morning Routine"

    async def test_update_with_unknown_entity_rejected(self):
        ha = _update_ha()
        result = await execute_automation_action(
            {
                "action": "update_automation",
                "entity": "morning routine",
                "parameters": {"add": {"actions": [{"action": "switch.turn_on", "entity_id": "switch.unknown"}]}},
            },
            ha,
            _index(),
            _resolving_matcher("automation.morning_routine", "Morning Routine"),
            agent_id="automation-agent",
            conversation_id=CONV,
        )
        assert result["success"] is False
        assert "switch.unknown" in result["speech"]
        assert confirmation_store.get(CONV)[0] == "none"

    async def test_update_missing_config_id(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(return_value={"state": "on", "attributes": {"friendly_name": "Morning Routine"}})
        result = await execute_automation_action(
            {"action": "update_automation", "entity": "morning routine", "parameters": {"set": {"alias": "X"}}},
            ha,
            _index(),
            _resolving_matcher("automation.morning_routine", "Morning Routine"),
            agent_id="automation-agent",
            conversation_id=CONV,
        )
        assert result["success"] is False
        assert "editable configuration" in result["speech"]

    async def test_apply_aborts_when_config_changed_meanwhile(self):
        ha = _update_ha()
        await execute_automation_action(
            {"action": "update_automation", "entity": "morning routine", "parameters": {"set": {"alias": "Wake"}}},
            ha,
            _index(),
            _resolving_matcher("automation.morning_routine", "Morning Routine"),
            agent_id="automation-agent",
            conversation_id=CONV,
        )
        change = confirmation_store.pop(CONV)
        ha.get_automation_config = AsyncMock(return_value={**_EXISTING, "alias": "Edited in HA"})
        result = await apply_pending_automation_change(change, ha, _index(), agent_id="automation-agent")
        assert result["success"] is False
        assert "changed in the meantime" in result["speech"]
        ha.save_automation_config.assert_not_awaited()

    async def test_apply_saves_merged_config(self):
        ha = _update_ha()
        await execute_automation_action(
            {"action": "update_automation", "entity": "morning routine", "parameters": {"set": {"alias": "Wake"}}},
            ha,
            _index(),
            _resolving_matcher("automation.morning_routine", "Morning Routine"),
            agent_id="automation-agent",
            conversation_id=CONV,
        )
        change = confirmation_store.pop(CONV)
        result = await apply_pending_automation_change(change, ha, _index(), agent_id="automation-agent")
        assert result["success"] is True
        saved_id, saved = ha.save_automation_config.await_args.args
        assert saved_id == "morning_routine_001"
        assert saved == {**_EXISTING, "alias": "Wake"}


class TestDelete:
    async def test_delete_proposes_then_applies(self):
        ha = AsyncMock()
        ha.get_state = AsyncMock(return_value={"state": "on", "attributes": {"id": "abc123"}})
        result = await execute_automation_action(
            {"action": "delete_automation", "entity": "vacation mode", "parameters": {}},
            ha,
            _index(),
            _resolving_matcher("automation.vacation_mode", "Vacation Mode"),
            agent_id="automation-agent",
            conversation_id=CONV,
        )
        assert result["success"] is True
        assert result["speech"].endswith("Shall I delete it?")
        ha.delete_automation_config.assert_not_awaited()
        change = confirmation_store.pop(CONV)
        applied = await apply_pending_automation_change(change, ha, _index(), agent_id="automation-agent")
        assert applied["success"] is True
        assert "deleted" in applied["speech"]
        ha.delete_automation_config.assert_awaited_once_with("abc123")

    async def test_delete_not_found(self):
        matcher = AsyncMock()
        matcher.match = AsyncMock(return_value=[])
        result = await execute_automation_action(
            {"action": "delete_automation", "entity": "nonexistent", "parameters": {}},
            AsyncMock(),
            None,
            matcher,
            agent_id="automation-agent",
            conversation_id=CONV,
        )
        assert result["success"] is False
        assert "Could not find" in result["speech"]


class TestParseDecision:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('{"decision": "confirm"}', "confirm"),
            ('Sure: {"decision": "DECLINE"}', "decline"),
            ('```json\n{"decision": "modify"}\n```', "modify"),
            ('{"decision": "maybe"}', "unclear"),
            ("yes", "unclear"),
            (None, "unclear"),
        ],
    )
    async def test_parse(self, raw, expected):
        assert parse_decision(raw) == expected


class TestStore:
    async def test_expiry_leaves_tombstone(self):
        store = AutomationConfirmationStore(ttl_seconds=0.01)
        store.put(CONV, PendingAutomationChange(kind="delete", alias="a", summary="s", question="q"))
        assert store.get(CONV)[0] == "active"
        time.sleep(0.02)
        assert store.get(CONV) == ("expired", None)


def _answer_task(text: str, *, followup: bool = True):
    ctx = TaskContext(pending_question="... Shall I save this?" if followup else None, is_followup=followup)
    return make_dispatch_task(text, conversation_id=CONV, context=ctx)


class TestAgentConfirmationFlow:
    async def test_two_turn_create_confirm(self):
        ha = AsyncMock()
        ha.get_automation_config = AsyncMock(return_value=None)
        agent = AutomationAgent(ha_client=ha, entity_index=_index(), entity_matcher=None)
        proposal_reply = "```json\n" + json.dumps(_create_action(dict(_GOOD_CONFIG))) + "\n```"

        with patch("app.llm.client.complete", new_callable=AsyncMock, return_value=proposal_reply):
            first = await agent.handle_task(make_dispatch_task("create evening lights", conversation_id=CONV))
        assert first.voice_followup is True
        assert first.speech.endswith("Shall I save this?")
        assert first.action_executed is not None and first.action_executed.cacheable is False
        ha.save_automation_config.assert_not_awaited()

        with patch("app.llm.client.complete", new_callable=AsyncMock, return_value='{"decision": "confirm"}'):
            second = await agent.handle_task(_answer_task("yes"))
        assert "created" in second.speech
        assert second.action_executed.action == "create_automation"
        assert second.action_executed.cacheable is False
        ha.save_automation_config.assert_awaited_once()
        assert confirmation_store.get(CONV)[0] == "none"

    async def test_decline_writes_nothing(self):
        ha = AsyncMock()
        agent = AutomationAgent(ha_client=ha, entity_index=_index(), entity_matcher=None)
        confirmation_store.put(
            CONV,
            PendingAutomationChange(kind="create", alias="A", summary="s", question="q", config=dict(_GOOD_CONFIG)),
        )
        with patch("app.llm.client.complete", new_callable=AsyncMock, return_value='{"decision": "decline"}'):
            result = await agent.handle_task(_answer_task("no"))
        assert "did not save" in result.speech
        ha.save_automation_config.assert_not_awaited()
        assert confirmation_store.get(CONV)[0] == "none"

    async def test_unclear_keeps_proposal_and_asks_again(self):
        ha = AsyncMock()
        agent = AutomationAgent(ha_client=ha, entity_index=_index(), entity_matcher=None)
        confirmation_store.put(
            CONV,
            PendingAutomationChange(kind="create", alias="A", summary="Summary.", question="Shall I save this?"),
        )
        with patch("app.llm.client.complete", new_callable=AsyncMock, return_value="hmm"):
            result = await agent.handle_task(_answer_task("hmm"))
        assert result.voice_followup is True
        assert result.speech == "Summary. Shall I save this?"
        assert confirmation_store.get(CONV)[0] == "active"
        ha.save_automation_config.assert_not_awaited()

    async def test_modify_drops_proposal_and_runs_normal_flow(self):
        ha = AsyncMock()
        agent = AutomationAgent(ha_client=ha, entity_index=_index(), entity_matcher=None)
        confirmation_store.put(
            CONV,
            PendingAutomationChange(kind="create", alias="A", summary="s", question="q", config=dict(_GOOD_CONFIG)),
        )
        replies = ['{"decision": "modify"}', "Which time do you want instead?"]
        with patch("app.llm.client.complete", new_callable=AsyncMock, side_effect=replies):
            result = await agent.handle_task(_answer_task("make it 30 minutes later"))
        assert "Which time" in result.speech
        ha.save_automation_config.assert_not_awaited()
        assert confirmation_store.get(CONV)[0] == "none"

    async def test_expired_followup_is_reported(self):
        agent = AutomationAgent(ha_client=AsyncMock(), entity_index=_index(), entity_matcher=None)
        confirmation_store.put(CONV, PendingAutomationChange(kind="delete", alias="A", summary="s", question="q"))
        # Age the entry past the 5-minute TTL but within the tombstone window.
        _stored_at, change = confirmation_store._entries[CONV]
        confirmation_store._entries[CONV] = (time.monotonic() - 400, change)
        with patch("app.llm.client.complete", new_callable=AsyncMock) as mock_llm:
            result = await agent.handle_task(_answer_task("yes"))
        assert "expired" in result.speech
        mock_llm.assert_not_awaited()

    async def test_enable_still_executes_without_confirmation(self):
        agent = AutomationAgent(ha_client=AsyncMock(), entity_index=_index(), entity_matcher=None)
        with (
            patch(
                "app.agents.automation_executor.execute_automation_action",
                new_callable=AsyncMock,
                return_value={"success": True, "entity_id": "automation.morning_routine", "speech": "Enabled."},
            ) as mock_exec,
            patch(
                "app.llm.client.complete",
                new_callable=AsyncMock,
                return_value='```json\n{"action": "enable_automation", "entity": "morning routine"}\n```',
            ),
        ):
            result = await agent.handle_task(make_dispatch_task("enable morning routine", conversation_id=CONV))
        assert result.action_executed.success is True
        assert mock_exec.await_args.kwargs["conversation_id"] == CONV
