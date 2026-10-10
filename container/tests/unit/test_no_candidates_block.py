"""Empty keyword recall: the no-candidates block is scoped per action.

Each actionable agent declares which of its actions act on a recalled entity
candidate (``entity_actions``) and which run without one
(``entity_free_actions``). On an empty recall:

- every action needs a candidate (undeclared agent): the strict block;
- some actions need one (device agents, automation): a scoped block that
  forbids only those actions and names the entity-free ones as allowed;
- no action needs one (timer, lists, calendar): nothing is injected, so the
  prompt's own JSON contract stays in force.

The executor-side candidate gate is identical in every case.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock litellm before importing any app modules that depend on it
_litellm_mock = MagicMock()


class _AuthenticationError(Exception):
    pass


class _APIError(Exception):
    pass


class _RateLimitError(Exception):
    pass


_litellm_mock.exceptions.AuthenticationError = _AuthenticationError
_litellm_mock.exceptions.APIError = _APIError
_litellm_mock.RateLimitError = _RateLimitError
sys.modules.setdefault("litellm", _litellm_mock)

from tests.helpers import make_dispatch_task  # noqa: E402

from app.agents import action_executor  # noqa: E402
from app.agents.actionable import (  # noqa: E402
    _NO_CANDIDATES_BLOCK,
    ActionableAgent,
    AutomationAgent,
    LightAgent,
)
from app.agents.calendar import CalendarAgent  # noqa: E402
from app.agents.decorator import _AGENT_CLASSES, agent  # noqa: E402
from app.agents.lists import ListsAgent  # noqa: E402
from app.agents.timer import TimerAgent  # noqa: E402

_STRICT_SENTENCE = "Do NOT output a JSON action block."
_NO_MATCH_PREFIX = "No matching devices were found"


def _empty_recall_index():
    index = AsyncMock()
    index.list_entries_async = AsyncMock(return_value=[])
    index.get_by_id_async = AsyncMock(return_value=None)
    return index


def _visible_passthrough():
    return patch(
        "app.agents.actionable.filter_visible_results",
        new_callable=AsyncMock,
        side_effect=lambda _agent_id, entries, _index: entries,
    )


def _context_for(agent_cls):
    """``_no_candidates_context`` for an abstract test class (reads class attributes only)."""
    return ActionableAgent._no_candidates_context(agent_cls)


def _wire(agent_instance):
    agent_instance._entity_index = _empty_recall_index()
    agent_instance._entity_matcher = None
    agent_instance._ha_client = AsyncMock()


async def _run(agent_instance, description, llm_response, execute_result=None):
    """Run one turn with an empty recall; returns (result, system_prompt, mock_exec)."""
    _wire(agent_instance)
    task = make_dispatch_task(description=description)
    with (
        patch.object(agent_instance, "_load_prompt_async", new_callable=AsyncMock, return_value="Agent prompt."),
        patch.object(agent_instance, "_call_llm", new_callable=AsyncMock, return_value=llm_response) as mock_llm,
        patch.object(
            agent_instance,
            "_do_execute",
            new_callable=AsyncMock,
            return_value=execute_result or {"success": True, "entity_id": None, "speech": "Done."},
        ) as mock_exec,
        patch(
            "app.agents.automation_confirmation.handle_pending_automation_answer",
            new=AsyncMock(return_value=None),
        ),
        _visible_passthrough(),
    ):
        result = await agent_instance.handle_task(task)
    return result, mock_llm.call_args.args[0][0]["content"], mock_exec


# ---------------------------------------------------------------------------
# Block selection
# ---------------------------------------------------------------------------


def test_undeclared_agent_gets_strict_block():
    @agent(agent_id="test-nocand-undeclared", name="T", description="d", skills=[])
    class _Undeclared(ActionableAgent):
        pass

    try:
        assert _Undeclared._entity_actions is None
        assert _context_for(_Undeclared) == _NO_CANDIDATES_BLOCK
    finally:
        _AGENT_CLASSES.pop("test-nocand-undeclared", None)


def test_device_agent_gets_scoped_block():
    block = LightAgent()._no_candidates_context()
    assert block is not None
    assert _STRICT_SENTENCE not in block
    forbidden, allowed = block.split("stay allowed", 1)
    for action in ("turn_on", "turn_off", "set_brightness", "query_light_state"):
        assert action in forbidden
    assert "list_lights" in allowed
    assert "list_lights" not in forbidden


@pytest.mark.parametrize("agent_cls", [TimerAgent, ListsAgent, CalendarAgent])
def test_agent_without_entity_actions_gets_no_block(agent_cls):
    assert agent_cls._entity_actions == frozenset()
    assert agent_cls()._no_candidates_context() is None


def test_declared_without_free_actions_falls_back_to_strict_block():
    @agent(
        agent_id="test-nocand-allentity",
        name="T",
        description="d",
        skills=[],
        entity_actions=frozenset({"turn_on"}),
        entity_free_actions=frozenset(),
    )
    class _AllEntity(ActionableAgent):
        pass

    try:
        assert _context_for(_AllEntity) == _NO_CANDIDATES_BLOCK
    finally:
        _AGENT_CLASSES.pop("test-nocand-allentity", None)


# ---------------------------------------------------------------------------
# Device agent: the read/list action survives, device actions ask
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_light_empty_recall_allows_list_lights():
    result, system_msg, mock_exec = await _run(
        LightAgent(),
        "which lights are on",
        '```json\n{"action": "list_lights", "entity": "", "parameters": {}}\n```',
        execute_result={"success": True, "entity_id": None, "speech": "Kitchen is on."},
    )
    assert LightAgent()._no_candidates_context() in system_msg
    mock_exec.assert_awaited_once()
    assert mock_exec.await_args.args[0]["action"] == "list_lights"
    assert result.error is None
    assert result.speech == "Kitchen is on."


@pytest.mark.asyncio
async def test_light_empty_recall_device_action_asks():
    result, system_msg, mock_exec = await _run(LightAgent(), "turn on the kitchen light", "Which light do you mean?")
    assert "turn_on" in system_msg.split("stay allowed", 1)[0]
    mock_exec.assert_not_awaited()
    assert result.speech == "Which light do you mean?"
    assert result.voice_followup is True


@pytest.mark.asyncio
async def test_light_empty_recall_gate_still_empty():
    """The scoped block does not loosen executor validation: the candidate
    gate seen by the executor is still the empty set."""
    seen_gate: list = []

    async def _fake_execute(action, *_args, **_kwargs):
        seen_gate.append(action_executor._request_candidate_ids.get())
        return {"success": True, "entity_id": None, "speech": "Listed."}

    light = LightAgent()
    _wire(light)
    with (
        patch.object(light, "_load_prompt_async", new_callable=AsyncMock, return_value="Agent prompt."),
        patch.object(
            light,
            "_call_llm",
            new_callable=AsyncMock,
            return_value='```json\n{"action": "list_lights", "entity": ""}\n```',
        ),
        patch.object(light, "_do_execute", new=AsyncMock(side_effect=_fake_execute)),
        _visible_passthrough(),
    ):
        await light.handle_task(make_dispatch_task(description="which lights are on"))
    assert seen_gate == [frozenset()]


@pytest.mark.asyncio
async def test_automation_empty_recall_scoped_block_allows_create():
    result, system_msg, mock_exec = await _run(
        AutomationAgent(),
        "create an automation that turns on the porch light at sunset",
        '```json\n{"action": "create_automation", "entity": "", "parameters": {"alias": "Porch"}}\n```',
    )
    forbidden, allowed = system_msg.split("stay allowed", 1)
    assert "enable_automation" in forbidden
    assert "create_automation" in allowed
    mock_exec.assert_awaited_once()
    assert result.error is None


# ---------------------------------------------------------------------------
# Agents without entity actions: no block, empty entity executes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_cls", "description", "llm_action"),
    [
        (
            TimerAgent,
            "set a timer for 5 minutes",
            '{"action": "start_timer", "entity": "", "parameters": {"duration": "00:05:00"}}',
        ),
        (
            ListsAgent,
            "add milk",
            '{"action": "add_item", "entity": "", "parameters": {"item": "milk"}}',
        ),
        (
            CalendarAgent,
            "what is on my calendar tomorrow",
            '{"action": "list_events", "parameters": {"start_date_time": "2026-10-12 00:00:00", '
            '"end_date_time": "2026-10-12 23:59:59"}}',
        ),
    ],
)
async def test_entity_free_agent_empty_recall_no_block_and_executes(agent_cls, description, llm_action):
    result, system_msg, mock_exec = await _run(agent_cls(), description, f"```json\n{llm_action}\n```")
    assert _NO_MATCH_PREFIX not in system_msg
    mock_exec.assert_awaited_once()
    assert result.error is None
    assert result.speech == "Done."


# ---------------------------------------------------------------------------
# Decorator: per-action sets and the coarse shorthand
# ---------------------------------------------------------------------------


def test_decorator_shorthand_maps_to_action_sets():
    @agent(agent_id="test-nocand-true", name="T", description="d", skills=[], entity_candidates_required=True)
    class _AllRequired(ActionableAgent):
        pass

    @agent(agent_id="test-nocand-false", name="T", description="d", skills=[], entity_candidates_required=False)
    class _NoneRequired(ActionableAgent):
        pass

    try:
        assert _AllRequired._entity_actions is None
        assert _context_for(_AllRequired) == _NO_CANDIDATES_BLOCK
        assert _NoneRequired._entity_actions == frozenset()
        assert _context_for(_NoneRequired) is None
        assert _NoneRequired._agent_meta["entity_candidates_required"] is False
    finally:
        _AGENT_CLASSES.pop("test-nocand-true", None)
        _AGENT_CLASSES.pop("test-nocand-false", None)


def test_decorator_without_declaration_keeps_class_default():
    @agent(agent_id="test-nocand-classattr", name="T", description="d", skills=[])
    class _ClassAttrAgent(ActionableAgent):
        _entity_actions = frozenset()

    try:
        assert _ClassAttrAgent._entity_actions == frozenset()
        assert _ClassAttrAgent._agent_meta["entity_actions"] is None
    finally:
        _AGENT_CLASSES.pop("test-nocand-classattr", None)


def test_decorator_rejects_shorthand_with_action_sets():
    with pytest.raises(TypeError):
        agent(
            agent_id="test-nocand-both",
            name="T",
            description="d",
            skills=[],
            entity_candidates_required=True,
            entity_actions=frozenset({"turn_on"}),
        )


def test_decorator_rejects_overlapping_action_sets():
    with pytest.raises(ValueError, match="turn_on"):
        agent(
            agent_id="test-nocand-overlap",
            name="T",
            description="d",
            skills=[],
            entity_actions=frozenset({"turn_on"}),
            entity_free_actions=frozenset({"turn_on", "list_lights"}),
        )
