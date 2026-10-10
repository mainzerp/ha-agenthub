"""Empty keyword recall: strict no-candidates block vs. neutral note.

Agents whose actions act on a recalled entity candidate keep the strict
"no matching devices -- do NOT output a JSON action block" instruction.
Agents whose actions run without a candidate (``entity_candidates_required``
False) get a neutral note instead, so the injected context never forbids the
JSON action block their own prompt contract requires. The executor-side
candidate gate is identical for both.
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
    _NO_CANDIDATES_NOTE,
    ActionableAgent,
    AutomationAgent,
    ClimateAgent,
    CoverAgent,
    LightAgent,
    MediaAgent,
    MusicAgent,
    SceneAgent,
    SecurityAgent,
    VacuumAgent,
)
from app.agents.calendar import CalendarAgent  # noqa: E402
from app.agents.decorator import agent  # noqa: E402
from app.agents.lists import ListsAgent  # noqa: E402
from app.agents.timer import TimerAgent  # noqa: E402

_STRICT_SENTENCE = "Do NOT output a JSON action block."


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


def _wire(agent_instance):
    agent_instance._entity_index = _empty_recall_index()
    agent_instance._entity_matcher = None
    agent_instance._ha_client = AsyncMock()


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "agent_cls",
    [LightAgent, ClimateAgent, CoverAgent, VacuumAgent, SceneAgent, SecurityAgent, MediaAgent, MusicAgent],
)
def test_device_agents_require_entity_candidates(agent_cls):
    assert agent_cls._entity_candidates_required is True


@pytest.mark.parametrize("agent_cls", [TimerAgent, ListsAgent, CalendarAgent, AutomationAgent])
def test_candidate_free_agents_declare_flag(agent_cls):
    assert agent_cls._entity_candidates_required is False
    assert agent_cls._agent_meta["entity_candidates_required"] is False


def test_decorator_without_flag_keeps_class_default():
    """Omitting the decorator argument keeps the class attribute (default True)."""

    @agent(agent_id="test-nocand-default", name="T", description="d", skills=[])
    class _DefaultAgent(ActionableAgent):
        pass

    @agent(agent_id="test-nocand-classattr", name="T", description="d", skills=[])
    class _ClassAttrAgent(ActionableAgent):
        _entity_candidates_required = False

    from app.agents.decorator import _AGENT_CLASSES

    try:
        assert _DefaultAgent._entity_candidates_required is True
        assert _DefaultAgent._agent_meta["entity_candidates_required"] is None
        assert _ClassAttrAgent._entity_candidates_required is False
    finally:
        _AGENT_CLASSES.pop("test-nocand-default", None)
        _AGENT_CLASSES.pop("test-nocand-classattr", None)


def test_note_does_not_forbid_json_action():
    assert _STRICT_SENTENCE not in _NO_CANDIDATES_NOTE
    assert _STRICT_SENTENCE in _NO_CANDIDATES_BLOCK


# ---------------------------------------------------------------------------
# Timer: empty recall, prompt without the block, timer action executes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timer_empty_recall_prompt_has_note_and_action_executes():
    timer = TimerAgent()
    _wire(timer)
    task = make_dispatch_task(description="set a timer for 5 minutes")
    llm_response = '```json\n{"action": "start_timer", "entity": "timer", "parameters": {"duration": "00:05:00"}}\n```'

    seen_gate: list = []

    async def _fake_execute(action, *_args, **_kwargs):
        # The closed-contract gate is still published (empty set), so any
        # LLM-supplied entity_id would be rejected by the executor.
        seen_gate.append(action_executor._request_candidate_ids.get())
        return {"success": True, "entity_id": None, "speech": "Started timer for 5 minutes."}

    with (
        patch.object(timer, "_load_prompt_async", new_callable=AsyncMock, return_value="You control timers."),
        patch.object(timer, "_call_llm", new_callable=AsyncMock, return_value=llm_response) as mock_llm,
        patch("app.agents.timer.execute_timer_action", new=AsyncMock(side_effect=_fake_execute)) as mock_exec,
        _visible_passthrough(),
    ):
        result = await timer.handle_task(task)

    system_msg = mock_llm.call_args.args[0][0]["content"]
    assert _STRICT_SENTENCE not in system_msg
    assert _NO_CANDIDATES_NOTE in system_msg

    mock_exec.assert_awaited_once()
    executed_action = mock_exec.await_args.args[0]
    assert executed_action["action"] == "start_timer"
    assert executed_action["parameters"] == {"duration": "00:05:00"}
    assert seen_gate == [frozenset()]
    assert result.error is None
    assert result.speech == "Started timer for 5 minutes."
    assert result.action_executed is not None
    assert result.action_executed.action == "start_timer"


@pytest.mark.asyncio
async def test_timer_empty_recall_llm_entity_id_still_rejected_by_gate():
    """The neutral note does not loosen executor validation: with an empty
    recall the candidate gate is an empty set, so a direct entity_id is
    rejected fail-closed by ``resolve_and_validate_entity``."""
    token = action_executor.set_request_candidate_ids(set())
    try:
        resolved = await action_executor.resolve_and_validate_entity(
            "kitchen",
            entity_index=None,
            entity_matcher=None,
            agent_id="timer-agent",
            allowed_domains=frozenset({"media_player"}),
            validate_domain_fn=lambda _eid: True,
            direct_entity_id="media_player.kitchen",
        )
    finally:
        action_executor.reset_request_candidate_ids(token)
    assert resolved["entity_id"] is None


# ---------------------------------------------------------------------------
# Device agent: empty recall keeps the strict block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_light_empty_recall_keeps_strict_block():
    light = LightAgent()
    _wire(light)
    task = make_dispatch_task(description="turn on the kitchen light")

    with (
        patch.object(light, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
        patch.object(light, "_call_llm", new_callable=AsyncMock, return_value="Which light do you mean?") as mock_llm,
        patch.object(light, "_do_execute", new_callable=AsyncMock) as mock_exec,
        _visible_passthrough(),
    ):
        result = await light.handle_task(task)

    system_msg = mock_llm.call_args.args[0][0]["content"]
    assert _NO_CANDIDATES_BLOCK in system_msg
    assert _NO_CANDIDATES_NOTE not in system_msg
    mock_exec.assert_not_awaited()
    assert result.speech == "Which light do you mean?"
    assert result.voice_followup is True


# ---------------------------------------------------------------------------
# Other candidate-free agents: note instead of block, action executes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_cls", "description", "llm_action"),
    [
        (
            ListsAgent,
            "add milk to the shopping list",
            '{"action": "add_item", "entity": "shopping list", "parameters": {"item": "milk"}}',
        ),
        (
            CalendarAgent,
            "what is on my calendar tomorrow",
            '{"action": "list_events", "entity": "calendar", "parameters": {"start": "tomorrow"}}',
        ),
        (
            AutomationAgent,
            "create an automation that turns on the porch light at sunset",
            '{"action": "create_automation", "entity": "", "parameters": {"alias": "Porch light at sunset"}}',
        ),
    ],
)
async def test_candidate_free_agent_empty_recall_gets_note(agent_cls, description, llm_action):
    instance = agent_cls()
    _wire(instance)
    task = make_dispatch_task(description=description)

    with (
        patch.object(instance, "_load_prompt_async", new_callable=AsyncMock, return_value="Agent prompt."),
        patch.object(
            instance, "_call_llm", new_callable=AsyncMock, return_value=f"```json\n{llm_action}\n```"
        ) as mock_llm,
        patch.object(
            instance,
            "_do_execute",
            new_callable=AsyncMock,
            return_value={"success": True, "entity_id": None, "speech": "Done."},
        ) as mock_exec,
        patch(
            "app.agents.automation_confirmation.handle_pending_automation_answer",
            new=AsyncMock(return_value=None),
        ),
        _visible_passthrough(),
    ):
        result = await instance.handle_task(task)

    system_msg = mock_llm.call_args.args[0][0]["content"]
    assert _STRICT_SENTENCE not in system_msg
    assert _NO_CANDIDATES_NOTE in system_msg
    mock_exec.assert_awaited_once()
    assert result.error is None
    assert result.speech == "Done."
