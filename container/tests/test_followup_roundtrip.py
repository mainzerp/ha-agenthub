"""FOLLOW_UP_QUESTION end-to-end round trip (container side).

Turn 1 produces a clarifying question with ``voice_followup=True`` under
conversation_id X; turn 2 sends a short answer with the same X and must see
the question turn in the classify-stage history (previous-agent hint) and
resolve the dispatched task against the pending question.

Fakes only for the LLM provider and the Home Assistant client, following the
``test_full_flow_demo.py`` conventions; the ConversationManager and the
ClassificationEngine history injection run on production code.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.actionable import LightAgent
from app.agents.base import BaseAgent
from app.agents.classification_engine import ClassificationEngine
from app.agents.conversation_manager import ConversationManager
from app.entity.index import EntityIndex
from app.models.agent import AgentCard, DispatchTask, TaskContext
from tests.helpers import make_entity_index_entry


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )


CID = "roundtrip-conversation-1"

LIGHTS = [
    ("light.wohnzimmer", "Wohnzimmer", "off"),
    ("light.kueche", "Küche", "off"),
]

USER_QUESTION_TEXT = "Mach das Licht an."
ANSWER_TEXT = "das Wohnzimmer"
CONDENSED_ANSWER_TASK = "turn on the Wohnzimmer light"


def _build_entries():
    entries = []
    for entity_id, name, state in LIGHTS:
        entry = make_entity_index_entry(
            entity_id,
            name,
            id_tokens=entity_id.split(".", 1)[1].split("_"),
        )
        entry.state = state
        entries.append(entry)
    return entries


def _make_index(entries):
    def _by_domains(domains=None):
        if not domains:
            return list(entries)
        return [e for e in entries if e.domain in domains]

    index = MagicMock(spec=EntityIndex)
    index.list_entries_async = AsyncMock(side_effect=_by_domains)
    index.list_entries = MagicMock(side_effect=_by_domains)
    index.get_by_id_async = AsyncMock(side_effect=lambda eid: next((e for e in entries if e.entity_id == eid), None))
    return index


def _make_agent(entries, ha_client, llm_responses: list[str], captured: list) -> LightAgent:
    agent = LightAgent()
    agent._entity_index = _make_index(entries)
    agent._entity_matcher = None
    agent._ha_client = ha_client
    agent._load_prompt_async = AsyncMock(return_value="LIGHT AGENT PROMPT")

    responses = list(llm_responses)

    async def _fake_call_llm(messages, **kwargs):
        captured.append(messages)
        return responses.pop(0)

    agent._call_llm = _fake_call_llm
    return agent


def _make_classification_engine(cm: ConversationManager, classify_llm) -> ClassificationEngine:
    registry = MagicMock()
    registry.get_known_agents = AsyncMock(return_value={"light-agent", "general-agent"})
    registry.list_agents = AsyncMock(
        return_value=[
            AgentCard(agent_id="light-agent", name="Light Agent", description="controls lights", skills=["light"]),
            AgentCard(
                agent_id="general-agent", name="General Agent", description="general questions", skills=["general"]
            ),
        ]
    )

    async def _load_prompt(name: str) -> str:
        assert name == "orchestrator"
        return "Route the request. {agent_descriptions} {language_hint} {previous_agent_hint}"

    return ClassificationEngine(
        agent_registry=registry,
        cache_manager=None,
        call_llm=classify_llm,
        load_prompt_async=_load_prompt,
        get_turns=cm.get_turns,
        wrap_user_input=BaseAgent._wrap_user_input,
        append_conversation_turn_messages=BaseAgent._append_conversation_turn_messages,
    )


@pytest.mark.asyncio
async def test_followup_roundtrip_question_then_answer():
    entries = _build_entries()
    ha_client = AsyncMock()
    ha_client.get_state = AsyncMock(return_value={"state": "off", "attributes": {}})
    ha_client.call_service = AsyncMock(
        return_value=[{"entity_id": "light.wohnzimmer", "state": "on", "attributes": {}}]
    )
    ha_client.expect_state = MagicMock(return_value=None)

    with (
        patch("app.agents.conversation_manager.ConversationRepository") as mock_repo,
        patch("app.agents.conversation_manager.get_memory_service", return_value=None),
    ):
        mock_repo.insert = AsyncMock(return_value=1)
        mock_repo.get_by_conversation_id = AsyncMock(return_value=[])
        cm = ConversationManager()

        # --- Turn 1: the agent cannot resolve the entity and asks back. ---
        question_speech = "Welches Licht meinst du?"
        agent1_captured: list = []
        agent1 = _make_agent(entries, ha_client, [question_speech], agent1_captured)
        task1 = DispatchTask(
            description=USER_QUESTION_TEXT,
            context=TaskContext(conversation_turns=[]),
        )
        result1 = await agent1.handle_task(task1)

        assert result1.speech == question_speech
        assert result1.voice_followup is True
        assert result1.action_executed is None
        ha_client.call_service.assert_not_awaited()

        # The question turn is stored like any other turn under conversation_id X.
        await cm.store_turn(CID, USER_QUESTION_TEXT, result1.speech, agent_id="light-agent")

        # --- Turn 2: bare answer with the SAME conversation_id. ---
        classify_captured: list = []

        async def _classify_llm(messages, **kwargs):
            classify_captured.append(messages)
            return f"light-agent: {CONDENSED_ANSWER_TASK}"

        engine = _make_classification_engine(cm, _classify_llm)
        classifications, routing_cached = await engine.classify(
            ANSWER_TEXT,
            conversation_id=CID,
        )

        assert routing_cached is False
        assert classifications[0][0] == "light-agent"
        assert classifications[0][1] == CONDENSED_ANSWER_TASK

        # The classify stage saw the question turn in history and injected the
        # previous-agent hint.
        system_content = classify_captured[0][0]["content"]
        assert "The previous turn was handled by light-agent." in system_content
        history_contents = [m["content"] for m in classify_captured[0]]
        assert any(question_speech in c for c in history_contents)
        assert any(USER_QUESTION_TEXT in c for c in history_contents)

        # The condensed task dispatches to the same agent and executes
        # against the answered entity.
        turns = await cm.get_turns(CID)
        agent2_captured: list = []
        action_response = (
            "Ich schalte das Wohnzimmer Licht ein.\n"
            '```json\n{"action": "turn_on", "entity": "Wohnzimmer", '
            '"entity_id": "light.wohnzimmer"}\n```'
        )
        agent2 = _make_agent(entries, ha_client, [action_response], agent2_captured)
        task2 = DispatchTask(
            description=classifications[0][1],
            context=TaskContext(conversation_turns=turns),
        )
        result2 = await agent2.handle_task(task2)

        assert result2.error is None
        assert result2.action_executed is not None
        assert result2.action_executed.success is True
        assert result2.action_executed.entity_id == "light.wohnzimmer"
        ha_client.call_service.assert_awaited_once()
        call_args = ha_client.call_service.await_args
        assert call_args.args[0] == "light"
        assert call_args.args[1] == "turn_on"
        assert call_args.args[2] == "light.wohnzimmer"


@pytest.mark.asyncio
async def test_followup_answer_with_fresh_conversation_id_loses_history():
    """The break the integration's correlation map repairs: when the answer
    arrives with a fresh conversation_id, the classify stage sees no history
    and no previous-agent hint."""
    with (
        patch("app.agents.conversation_manager.ConversationRepository") as mock_repo,
        patch("app.agents.conversation_manager.get_memory_service", return_value=None),
    ):
        mock_repo.insert = AsyncMock(return_value=1)
        mock_repo.get_by_conversation_id = AsyncMock(return_value=[])
        cm = ConversationManager()
        await cm.store_turn(CID, USER_QUESTION_TEXT, "Welches Licht meinst du?", agent_id="light-agent")

        classify_captured: list = []

        async def _classify_llm(messages, **kwargs):
            classify_captured.append(messages)
            return "general-agent: unknown request"

        engine = _make_classification_engine(cm, _classify_llm)
        await engine.classify(ANSWER_TEXT, conversation_id="some-other-id")

        system_content = classify_captured[0][0]["content"]
        assert "The previous turn was handled by" not in system_content
        history_contents = [m["content"] for m in classify_captured[0]]
        assert not any("Welches Licht meinst du?" in c for c in history_contents)
