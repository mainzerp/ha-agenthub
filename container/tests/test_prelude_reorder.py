"""Tests for the P3 prelude reorder (cache replay before language detection).

The action/routing cache replay now runs BEFORE language auto-detection and
the conversation-turn prefetch, keyed by the explicit language (settings
override or request language). On a miss the prefetched turns are threaded
into ``classify`` and reused for dispatch, so the turn list is fetched
exactly once per non-cached turn.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.cache.cache_manager import ActionReplayOutcome  # noqa: E402
from app.models.agent import AgentCard, IngressTask, TaskContext  # noqa: E402


def _make_task(text: str, language: str = "en") -> IngressTask:
    return IngressTask(
        description=text,
        conversation_id="conv-prelude-reorder",
        context=TaskContext(language=language),
    )


def _make_action_hit() -> ActionReplayOutcome:
    return ActionReplayOutcome(
        kind="full_hit",
        entry_id="action-1",
        agent_id="light-agent",
        response_text="Cached speech",
        replay_result={"success": True},
        similarity=1.0,
    )


def _make_orchestrator() -> OrchestratorAgent:
    dispatcher = AsyncMock()
    registry = AsyncMock()
    registry.list_agents = AsyncMock(
        return_value=[
            AgentCard(agent_id="light-agent", name="Light Agent", description="", skills=["light"]),
        ]
    )
    orch = OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=MagicMock())
    orch._is_background_turn = MagicMock(return_value=False)
    orch._get_turns = AsyncMock(return_value=[{"role": "user", "content": "earlier"}])
    orch._resolve_language = AsyncMock(return_value="en")
    orch._get_bool_setting = AsyncMock(side_effect=lambda _key, default: default)
    orch._cache_orchestrator._get_bool_setting_impl = AsyncMock(side_effect=lambda _key, default: default)
    orch._finalize_action_replay_hit = AsyncMock(
        return_value={"speech": "Cached speech", "routed_to": "light-agent", "action_executed": None}
    )
    orch._finalize_single_agent_response = AsyncMock(return_value=("Live dispatch speech", False))
    orch._get_personality_cached = AsyncMock(return_value="")
    return orch


@pytest.mark.asyncio
async def test_action_hit_skips_langdetect_and_turn_prefetch():
    """Cache-hit turns must not pay language detection or the turn fetch."""
    orch = _make_orchestrator()
    orch._cache_orchestrator.try_cache_replay = AsyncMock(return_value=(_make_action_hit(), None))

    prelude = await orch._run_pipeline_prelude(_make_task("turn on kitchen light"))

    assert prelude.early_exit is not None
    assert prelude.early_exit["_exit_type"] == "cache_replay"
    orch._get_turns.assert_not_awaited()
    orch._resolve_language.assert_not_awaited()
    # The replay lookup used the explicit (request) language.
    assert orch._cache_orchestrator.try_cache_replay.call_args.kwargs["language"] == "en"


@pytest.mark.asyncio
async def test_action_hit_uses_manual_language_override():
    """A pinned ``language`` setting is the explicit lookup language."""
    orch = _make_orchestrator()
    orch._cache_orchestrator.try_cache_replay = AsyncMock(return_value=(_make_action_hit(), None))

    with patch(
        "app.agents.orchestrator.SettingsRepository.get_value",
        new_callable=AsyncMock,
        return_value="de",
    ):
        prelude = await orch._run_pipeline_prelude(_make_task("turn on kitchen light"))

    assert prelude.early_exit is not None
    assert orch._cache_orchestrator.try_cache_replay.call_args.kwargs["language"] == "de"


@pytest.mark.asyncio
async def test_settings_read_failure_degrades_to_request_language():
    """A failing settings read must not kill the replay lookup."""
    orch = _make_orchestrator()
    orch._cache_orchestrator.try_cache_replay = AsyncMock(return_value=(_make_action_hit(), None))

    with patch(
        "app.agents.orchestrator.SettingsRepository.get_value",
        new_callable=AsyncMock,
        side_effect=RuntimeError("db down"),
    ):
        prelude = await orch._run_pipeline_prelude(_make_task("turn on kitchen light"))

    assert prelude.early_exit is not None
    assert orch._cache_orchestrator.try_cache_replay.call_args.kwargs["language"] == "en"


@pytest.mark.asyncio
async def test_miss_fetches_turns_once_and_threads_into_classify():
    """On a miss the prefetched turns flow into classify (no duplicate fetch)."""
    orch = _make_orchestrator()
    orch._cache_orchestrator.try_cache_replay = AsyncMock(return_value=(None, None))
    turns = [{"role": "user", "content": "earlier"}]
    orch._get_turns = AsyncMock(return_value=turns)
    orch._classification_engine.classify = AsyncMock(
        return_value=([("light-agent", "Turn on kitchen light", 0.95, [])], False)
    )

    prelude = await orch._run_pipeline_prelude(_make_task("turn on kitchen light"))

    assert prelude.early_exit is None
    assert orch._get_turns.await_count == 1
    assert orch._classification_engine.classify.call_args.kwargs["prefetched_turns"] is turns
    assert prelude.lang_turns is turns


@pytest.mark.asyncio
async def test_dispatch_reuses_prelude_turns_without_second_fetch():
    """The dispatch context reuses the prelude snapshot (full impl path)."""
    orch = _make_orchestrator()
    orch._cache_orchestrator.try_cache_replay = AsyncMock(return_value=(None, None))
    turns = [{"role": "user", "content": "earlier"}]
    orch._get_turns = AsyncMock(return_value=turns)
    orch._classification_engine.classify = AsyncMock(
        return_value=([("light-agent", "Turn on kitchen light", 0.95, [])], False)
    )
    orch._dispatch_manager.dispatch_single = AsyncMock(
        return_value=("light-agent", "Live dispatch speech", {"speech": "Live dispatch speech"})
    )

    result = await orch._handle_task_impl(_make_task("turn on kitchen light"))

    assert result["speech"] == "Live dispatch speech"
    # Exactly one turn fetch for the whole turn (prelude), reused by
    # classify AND dispatch.
    assert orch._get_turns.await_count == 1
    dispatch_turns = orch._dispatch_manager.dispatch_single.call_args.args[4]
    assert dispatch_turns == turns
