"""Parity between the per-action entity declarations, the executors and the parser.

Every actionable agent declares ``entity_actions`` (act on one recalled entity
candidate) and ``entity_free_actions`` (run without one) from its executor's
``ENTITY_ACTIONS`` / ``ENTITY_FREE_ACTIONS`` tables. These tests pin that:

- the agent declaration is exactly the executor's table;
- the executor dispatches every declared action and rejects any other name;
- the parser accepts an empty ``entity`` for exactly the entity-free actions;
- every action a prompt's few-shot examples emit is declared.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_litellm_mock = MagicMock()
sys.modules.setdefault("litellm", _litellm_mock)

from app.agents import action_executor  # noqa: E402
from app.agents.actionable import (  # noqa: E402
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
from app.agents.lists import ListsAgent  # noqa: E402
from app.agents.timer import TimerAgent  # noqa: E402

_PROMPTS = Path(__file__).resolve().parents[2] / "app" / "prompts"

# agent class -> (executor module, executor function, prompt name, read handler to stub or None)
_AGENTS = {
    LightAgent: ("app.agents.light_executor", "execute_light_action", "light", "_handle_light_read_action"),
    ClimateAgent: ("app.agents.climate_executor", "execute_climate_action", "climate", "_handle_climate_read_action"),
    CoverAgent: ("app.agents.cover_executor", "execute_cover_action", "cover", "_handle_cover_read_action"),
    VacuumAgent: ("app.agents.vacuum_executor", "execute_vacuum_action", "vacuum", "_handle_vacuum_read_action"),
    SceneAgent: ("app.agents.scene_executor", "execute_scene_action", "scene", "_handle_scene_read_action"),
    SecurityAgent: (
        "app.agents.security_executor",
        "execute_security_action",
        "security",
        "_handle_security_read_action",
    ),
    MediaAgent: ("app.agents.media_executor", "execute_media_action", "media", "_handle_media_read_action"),
    MusicAgent: ("app.agents.music_executor", "execute_music_action", "music", "_handle_music_read_action"),
    AutomationAgent: (
        "app.agents.automation_executor",
        "execute_automation_action",
        "automation",
        "_handle_automation_read_action",
    ),
    TimerAgent: ("app.agents.timer_executor", "execute_timer_action", "timer", None),
    ListsAgent: ("app.agents.lists_executor", "execute_lists_action", "lists", None),
    CalendarAgent: ("app.agents.calendar_executor", "execute_calendar_action", "calendar", None),
}

_EXPECTED_ENTITY_FREE = {
    LightAgent: {"list_lights"},
    ClimateAgent: {"list_climate", "query_weather", "query_weather_forecast"},
    CoverAgent: {"list_covers"},
    VacuumAgent: {"list_vacuums"},
    SceneAgent: {"list_scenes"},
    SecurityAgent: {"list_security"},
    MediaAgent: {"list_media_players"},
    MusicAgent: {"list_music_players"},
    AutomationAgent: {"create_automation", "list_automations"},
}

_FEW_SHOT_ACTION_RE = (re.compile(r'"action":\s*"([a-z_]+)"'), re.compile(r"action:\s*([a-z_]+)"))


def _all_actions(agent_cls) -> frozenset[str]:
    return agent_cls._entity_actions | agent_cls._entity_free_actions


@pytest.mark.parametrize("agent_cls", list(_AGENTS), ids=lambda c: c.__name__)
def test_agent_declaration_matches_executor_table(agent_cls):
    module = importlib.import_module(_AGENTS[agent_cls][0])
    assert agent_cls._entity_actions == module.ENTITY_ACTIONS
    assert agent_cls._entity_free_actions == module.ENTITY_FREE_ACTIONS
    assert not module.ENTITY_ACTIONS & module.ENTITY_FREE_ACTIONS
    assert agent_cls._agent_meta["entity_actions"] == module.ENTITY_ACTIONS
    assert agent_cls._agent_meta["entity_free_actions"] == module.ENTITY_FREE_ACTIONS


@pytest.mark.parametrize("agent_cls", list(_EXPECTED_ENTITY_FREE), ids=lambda c: c.__name__)
def test_device_agent_entity_free_actions(agent_cls):
    assert agent_cls._entity_free_actions == frozenset(_EXPECTED_ENTITY_FREE[agent_cls])
    assert agent_cls._entity_actions, "device agents act on entities"


@pytest.mark.parametrize("agent_cls", [TimerAgent, ListsAgent, CalendarAgent], ids=lambda c: c.__name__)
def test_internal_target_agents_have_no_entity_actions(agent_cls):
    assert agent_cls._entity_actions == frozenset()
    assert agent_cls._entity_free_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_cls", list(_AGENTS), ids=lambda c: c.__name__)
async def test_executor_dispatches_exactly_the_declared_actions(agent_cls):
    module_name, fn_name, _prompt, read_handler = _AGENTS[agent_cls]
    module = importlib.import_module(module_name)
    execute = getattr(module, fn_name)
    not_found = {"entity_id": None, "friendly_name": None, "not_found_result": {"success": False, "speech": "nf"}}
    stubs = []
    if read_handler:
        stubs.append(patch.object(module, read_handler, new=AsyncMock(return_value={"speech": "read"})))
    if hasattr(module, "resolve_and_validate_entity"):
        stubs.append(patch.object(module, "resolve_and_validate_entity", new=AsyncMock(return_value=not_found)))
    if hasattr(module, "_handle_automation_config_action"):
        stubs.append(
            patch.object(module, "_handle_automation_config_action", new=AsyncMock(return_value={"speech": "cfg"}))
        )
    stubs.append(patch("app.agents.timer_executor._helpers._get_scheduler", return_value=None))
    for stub in stubs:
        stub.start()
    try:
        for action_name in sorted(_all_actions(agent_cls)):
            result = await execute({"action": action_name, "entity": ""}, AsyncMock(), None, None, agent_id="probe")
            assert not str(result.get("speech", "")).startswith("Unknown"), (action_name, result)
        result = await execute({"action": "not_a_declared_action", "entity": "x"}, AsyncMock(), None, None)
        assert str(result.get("speech", "")).startswith("Unknown"), result
    finally:
        for stub in reversed(stubs):
            stub.stop()


@pytest.mark.parametrize("agent_cls", list(_AGENTS), ids=lambda c: c.__name__)
def test_parser_accepts_empty_entity_exactly_for_entity_free_actions(agent_cls):
    parser_free = action_executor._ACTIONS_WITHOUT_ENTITY
    assert agent_cls._entity_free_actions <= parser_free
    assert not agent_cls._entity_actions & parser_free
    for action_name in agent_cls._entity_free_actions:
        assert len(action_executor.parse_actions(f'{{"action": "{action_name}", "entity": ""}}')) == 1
    for action_name in agent_cls._entity_actions:
        assert action_executor.parse_actions(f'{{"action": "{action_name}", "entity": ""}}') == []


def test_parser_set_has_no_undeclared_actions():
    declared_free = frozenset().union(*(agent_cls._entity_free_actions for agent_cls in _AGENTS))
    assert declared_free == action_executor._ACTIONS_WITHOUT_ENTITY


def test_no_action_is_entity_free_in_one_agent_and_entity_in_another():
    """The parser set is global, so an action name must mean the same everywhere."""
    all_entity = frozenset().union(*(agent_cls._entity_actions for agent_cls in _AGENTS))
    all_free = frozenset().union(*(agent_cls._entity_free_actions for agent_cls in _AGENTS))
    assert not all_entity & all_free


@pytest.mark.parametrize("agent_cls", list(_AGENTS), ids=lambda c: c.__name__)
def test_prompt_few_shot_actions_are_declared(agent_cls):
    prompt = (_PROMPTS / f"{_AGENTS[agent_cls][2]}.txt").read_text(encoding="utf-8")
    emitted = set()
    for regex in _FEW_SHOT_ACTION_RE:
        emitted.update(regex.findall(prompt))
    assert emitted, "prompt has no few-shot actions"
    assert emitted <= _all_actions(agent_cls), sorted(emitted - _all_actions(agent_cls))
