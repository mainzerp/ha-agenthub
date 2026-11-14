"""Routing scope of stored conversation turns (issue #236).

The current request has priority: a confident speaker change hides the other
speaker's turns, and older history gets no previous-agent routing hint.
"""

from __future__ import annotations

import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.modules.setdefault("litellm", MagicMock())

from app.agents.conversation_manager import ConversationManager  # noqa: E402
from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.agents.personalization import speaker_profile_id  # noqa: E402
from app.agents.turn_scope import (  # noqa: E402
    TURN_FRESH_SECONDS,
    TURN_SPEAKER_KEY,
    TURN_TS_KEY,
    last_user_speaker,
    scope_turns_to_speaker,
    turns_are_fresh,
)
from app.models.agent import AgentCard, SpeakerContext, TaskContext  # noqa: E402


def _exchange(user: str, reply: str, *, speaker: int | None, agent: str = "climate-agent", ts: float | None = None):
    user_turn: dict = {"role": "user", "content": user}
    if speaker is not None:
        user_turn[TURN_SPEAKER_KEY] = speaker
    assistant_turn: dict = {"role": "assistant", "content": reply, "agent_id": agent}
    if ts is not None:
        user_turn[TURN_TS_KEY] = ts
        assistant_turn[TURN_TS_KEY] = ts
    return [user_turn, assistant_turn]


# ---------------------------------------------------------------------------
# scope_turns_to_speaker / turns_are_fresh
# ---------------------------------------------------------------------------


class TestScopeTurnsToSpeaker:
    def test_unknown_current_speaker_keeps_turns(self):
        turns = _exchange("turn on the fan", "No fan.", speaker=7)
        scope = scope_turns_to_speaker(turns, None)
        assert scope.turns == turns and not scope.speaker_changed

    def test_same_speaker_keeps_turns(self):
        turns = _exchange("turn on the fan", "No fan.", speaker=4)
        scope = scope_turns_to_speaker(turns, 4)
        assert scope.turns == turns and not scope.speaker_changed

    def test_unknown_previous_speaker_is_no_change(self):
        turns = _exchange("turn on the fan", "No fan.", speaker=None)
        scope = scope_turns_to_speaker(turns, 4)
        assert scope.turns == turns and not scope.speaker_changed

    def test_change_drops_other_speaker_exchanges(self):
        turns = _exchange("turn on the fan", "No fan.", speaker=7) + _exchange("just the fan", "None.", speaker=7)
        scope = scope_turns_to_speaker(turns, 4)
        assert scope.speaker_changed
        assert scope.turns == []
        assert scope.dropped_turns == 4
        assert scope.previous_speaker_profile_id == 7

    def test_change_keeps_own_and_unknown_exchanges(self):
        own = _exchange("dim the hall", "Dimmed.", speaker=4, agent="light-agent")
        unknown = _exchange("what time is it", "Noon.", speaker=None, agent="general-agent")
        other = _exchange("turn on the fan", "No fan.", speaker=7)
        scope = scope_turns_to_speaker(own + unknown + other, 4)
        assert scope.speaker_changed
        assert scope.turns == own + unknown
        assert scope.dropped_turns == 2

    def test_last_user_speaker_reads_newest_user_turn(self):
        turns = _exchange("a", "b", speaker=7) + _exchange("c", "d", speaker=4)
        assert last_user_speaker(turns) == 4
        assert last_user_speaker([]) is None


class TestTurnsAreFresh:
    def test_recent_turn_is_fresh(self):
        now = 1_000_000.0
        assert turns_are_fresh(_exchange("a", "b", speaker=None, ts=now - 10), now=now)

    def test_old_turn_is_stale(self):
        now = 1_000_000.0
        turns = _exchange("a", "b", speaker=None, ts=now - TURN_FRESH_SECONDS - 1)
        assert not turns_are_fresh(turns, now=now)

    def test_newest_turn_decides(self):
        now = 1_000_000.0
        turns = _exchange("a", "b", speaker=None, ts=now - 600) + _exchange("c", "d", speaker=None, ts=now - 5)
        assert turns_are_fresh(turns, now=now)

    def test_turns_without_timestamp_count_as_fresh(self):
        assert turns_are_fresh(_exchange("a", "b", speaker=None))
        assert turns_are_fresh([])


class TestSpeakerProfileId:
    def test_identified_speaker(self):
        ctx = TaskContext(speaker=SpeakerContext(status="identified", profile_id=4, name="Anna"))
        assert speaker_profile_id(ctx) == 4

    def test_unidentified_or_missing(self):
        assert speaker_profile_id(TaskContext()) is None
        assert speaker_profile_id(TaskContext(speaker=SpeakerContext(status="unknown", profile_id=4))) is None
        assert speaker_profile_id(None) is None


# ---------------------------------------------------------------------------
# ConversationManager stamps
# ---------------------------------------------------------------------------


@pytest.fixture()
def _conversation_repo():
    with (
        patch("app.agents.conversation_manager.ConversationRepository") as repo,
        patch("app.agents.conversation_manager.get_memory_service", return_value=None),
    ):
        repo.insert = AsyncMock(return_value=1)
        yield repo


@pytest.mark.usefixtures("_conversation_repo")
class TestConversationManagerStamps:
    async def test_store_turn_stamps_time_and_speaker(self):
        manager = ConversationManager()
        before = time.time()
        await manager.store_turn("conv-1", "turn on the fan", "No fan.", "climate-agent", speaker_profile_id=7)
        turns = await manager.get_turns("conv-1")
        assert turns[0][TURN_SPEAKER_KEY] == 7
        assert TURN_SPEAKER_KEY not in turns[1]
        assert all(t[TURN_TS_KEY] >= before for t in turns)

    async def test_store_turn_without_speaker(self):
        manager = ConversationManager()
        await manager.store_turn("conv-2", "hello", "Hi.", "general-agent")
        turns = await manager.get_turns("conv-2")
        assert TURN_SPEAKER_KEY not in turns[0]

    async def test_db_fallback_stamps_created_at(self, _conversation_repo):
        _conversation_repo.get_by_conversation_id = AsyncMock(
            return_value=[
                {
                    "user_text": "turn on the fan",
                    "response_text": "No fan.",
                    "agent_id": "climate-agent",
                    "created_at": "2026-11-14 13:42:13",
                }
            ]
        )
        manager = ConversationManager()
        turns = await manager.get_turns("conv-db")
        expected = 1794663733.0  # 2026-11-14 13:42:13 UTC
        assert [t[TURN_TS_KEY] for t in turns] == [expected, expected]
        assert TURN_SPEAKER_KEY not in turns[0]

    async def test_forget_popped_pending_prevents_restore(self):
        manager = ConversationManager()
        manager.set_pending_question("conv-q", "Which fan?", "climate-agent")
        assert manager.pop_pending_question("conv-q") is not None
        manager.forget_popped_pending("conv-q")
        manager.restore_pending_question("conv-q")
        assert not manager.has_pending_question("conv-q")


# ---------------------------------------------------------------------------
# Classification: previous-agent hint only for fresh history
# ---------------------------------------------------------------------------


def _classify_orchestrator(turns: list[dict]) -> OrchestratorAgent:
    orch = OrchestratorAgent(dispatcher=AsyncMock())
    orch._registry = AsyncMock()
    orch._registry.list_agents = AsyncMock(
        return_value=[
            AgentCard(agent_id="light-agent", name="", description="", skills=[]),
            AgentCard(agent_id="climate-agent", name="", description="", skills=[]),
        ]
    )
    orch._build_agent_descriptions = AsyncMock(return_value="light-agent: lights\nclimate-agent: climate")
    orch._get_turns = AsyncMock(return_value=turns)
    orch._call_llm = AsyncMock(return_value="light-agent (90%): turn on the kitchen light")
    return orch


class TestClassifyHistoryFreshness:
    async def test_fresh_history_keeps_previous_agent_hint(self):
        orch = _classify_orchestrator(_exchange("turn on the fan", "No fan.", speaker=None, ts=time.time() - 5))
        await orch._classify("and in the kitchen", conversation_id="conv-fresh")
        system_prompt = orch._call_llm.await_args.args[0][0]["content"]
        assert "previous turn was handled by climate-agent" in system_prompt

    async def test_stale_history_is_background_only(self):
        stale = time.time() - TURN_FRESH_SECONDS - 60
        orch = _classify_orchestrator(_exchange("turn on the fan", "No fan.", speaker=None, ts=stale))
        await orch._classify("turn on the kitchen", conversation_id="conv-stale")
        messages = orch._call_llm.await_args.args[0]
        system_prompt = messages[0]["content"]
        assert "previous turn was handled by" not in system_prompt
        assert "background information only" in system_prompt
        # The history itself is still available to resolve explicit references.
        assert any("turn on the fan" in m["content"] for m in messages[1:])

    async def test_stale_history_with_open_question_keeps_hint(self):
        stale = time.time() - TURN_FRESH_SECONDS - 60
        orch = _classify_orchestrator(_exchange("turn on the fan", "Which fan?", speaker=None, ts=stale))
        await orch._classification_engine.classify(
            "the bathroom one",
            conversation_id="conv-stale-q",
            call_llm=orch._call_llm,
            load_prompt_async=orch._load_prompt_async,
            get_turns=orch._get_turns,
            pending_question="Which fan?",
        )
        system_prompt = orch._call_llm.await_args.args[0][0]["content"]
        assert "previous turn was handled by climate-agent" in system_prompt
