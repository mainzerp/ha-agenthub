"""Tests for the session-memory read path: prelude overlap task + injection.

Covers: the memory search task is created on the cache-miss path and NOT on
a cache-replay hit (D6); the best_effort / blocking wait modes of
``_resolve_memory_context``; and the GeneralAgent system-prompt injection
(score labels, placement after the static head, delimiter-wrapped snippets).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

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

from app.agents.general import GeneralAgent  # noqa: E402
from app.agents.orchestrator import OrchestratorAgent, _resolve_memory_context, _trace_memory_retrieval  # noqa: E402
from app.cache.cache_manager import ActionReplayOutcome  # noqa: E402
from app.memory.service import MemoryMatch  # noqa: E402
from app.models.agent import AgentCard, DispatchTask, IngressTask, TaskContext  # noqa: E402
from app.security.sanitization import USER_INPUT_END, USER_INPUT_START  # noqa: E402


def _make_match(conversation_id: str = "conv-old", similarity: float = 0.91) -> MemoryMatch:
    return MemoryMatch(
        conversation_id=conversation_id,
        session_id=10,
        similarity=similarity,
        matched_text="what about the timers",
        snippet_turns=[{"user_text": "what about the timers", "response_text": "You set a 5 minute timer."}],
        continuation_turns=[{"user_text": "set a timer", "response_text": "Timer set."}],
        last_turn_at=1754400000,
        user_id="user-1",
    )


def _make_memory_service(*, wait_mode: str = "best_effort", matches=None, wait_timeout_ms: int = 800) -> MagicMock:
    service = MagicMock()
    service.is_enabled = AsyncMock(return_value=True)
    service.search = AsyncMock(return_value=matches if matches is not None else [_make_match()])
    service.wait_config = AsyncMock(return_value=(wait_mode, wait_timeout_ms))
    return service


# ---------------------------------------------------------------------------
# _resolve_memory_context wait modes
# ---------------------------------------------------------------------------


class TestResolveMemoryContext:
    def _make_task(self) -> IngressTask:
        return IngressTask(description="what did we discuss", conversation_id="conv-new", context=TaskContext())

    async def test_best_effort_finished_search_populates_context(self):
        task = self._make_task()
        service = _make_memory_service()
        memory_task = asyncio.create_task(service.search("what did we discuss", None))
        await asyncio.sleep(0)  # let the mocked search finish

        await _resolve_memory_context(task, memory_task, service)

        assert task.context.memory_context is not None
        assert task.context.memory_context[0]["conversation_id"] == "conv-old"
        assert task.context.memory_context[0]["similarity"] == 0.91

    async def test_best_effort_pending_search_proceeds_without_memory(self):
        task = self._make_task()
        service = _make_memory_service()
        gate = asyncio.Event()

        async def _slow_search():
            await gate.wait()
            return [_make_match()]

        memory_task = asyncio.create_task(_slow_search())

        await _resolve_memory_context(task, memory_task, service)

        # Zero added latency: dispatch proceeds with no memory, search keeps running.
        assert task.context.memory_context is None
        assert not memory_task.done()

        # The abandoned task still completes cleanly (done-callback consumes it).
        gate.set()
        assert (await memory_task)[0].conversation_id == "conv-old"
        await asyncio.sleep(0)

    async def test_blocking_fast_search_populates_context(self):
        task = self._make_task()
        service = _make_memory_service(wait_mode="blocking")
        memory_task = asyncio.create_task(service.search("q", None))

        await _resolve_memory_context(task, memory_task, service)

        assert task.context.memory_context is not None
        assert task.context.memory_context[0]["conversation_id"] == "conv-old"

    async def test_blocking_timeout_proceeds_without_memory(self):
        task = self._make_task()
        service = _make_memory_service(wait_mode="blocking", wait_timeout_ms=1)
        gate = asyncio.Event()

        async def _slow_search():
            await gate.wait()
            return [_make_match()]

        memory_task = asyncio.create_task(_slow_search())

        await _resolve_memory_context(task, memory_task, service)

        assert task.context.memory_context is None
        gate.set()
        await memory_task
        await asyncio.sleep(0)

    async def test_search_failure_never_breaks_dispatch(self):
        task = self._make_task()
        service = _make_memory_service()
        service.search = AsyncMock(side_effect=RuntimeError("memory db down"))
        memory_task = asyncio.create_task(service.search("q", None))
        await asyncio.sleep(0)

        await _resolve_memory_context(task, memory_task, service)
        assert task.context.memory_context is None

    async def test_none_task_is_noop(self):
        task = self._make_task()
        await _resolve_memory_context(task, None, _make_memory_service())
        assert task.context.memory_context is None


# ---------------------------------------------------------------------------
# memory_retrieval trace span
# ---------------------------------------------------------------------------


class _FakeSpanCollector:
    """Minimal span collector recording span metadata by span name."""

    def __init__(self):
        self.spans: dict[str, dict] = {}
        self.order: list[tuple[str, str]] = []

    def start_span(self, name, **kwargs):
        span: dict = {"metadata": {}}
        self.spans[name] = span
        self.order.append(("open", name))

        @contextlib.asynccontextmanager
        async def _cm():
            try:
                yield span
            finally:
                self.order.append(("close", name))

        return _cm()


class TestTraceMemoryRetrieval:
    def _make_task(self) -> IngressTask:
        return IngressTask(description="what did we discuss", conversation_id="conv-new", context=TaskContext())

    def _make_span_state(self) -> dict:
        return {"resolved": asyncio.Event(), "metadata": {}}

    async def test_blocking_match_span_metadata(self):
        task = self._make_task()
        service = _make_memory_service(wait_mode="blocking")
        memory_task = asyncio.create_task(service.search("q", None))
        state = self._make_span_state()
        collector = _FakeSpanCollector()
        span_task = asyncio.create_task(_trace_memory_retrieval(memory_task, collector, state))

        await _resolve_memory_context(task, memory_task, service, span_state=state)
        await span_task

        meta = collector.spans["memory_retrieval"]["metadata"]
        assert meta["match_count"] == 1
        assert meta["top_similarity"] == 0.91
        assert meta["matched_conversation_id"] == "conv-old"
        assert meta["wait_mode"] == "blocking"
        assert meta["timed_out"] is False
        assert meta["matches_attached"] is True
        assert ("close", "memory_retrieval") in collector.order

    async def test_best_effort_pending_span_abandoned(self):
        task = self._make_task()
        service = _make_memory_service()  # best_effort
        gate = asyncio.Event()

        async def _slow_search():
            await gate.wait()
            return [_make_match()]

        memory_task = asyncio.create_task(_slow_search())
        state = self._make_span_state()
        collector = _FakeSpanCollector()
        span_task = asyncio.create_task(_trace_memory_retrieval(memory_task, collector, state))

        await _resolve_memory_context(task, memory_task, service, span_state=state)

        # Span still open: resolution is deferred to the abandon
        # done-callback, which fires when the late search finishes.
        assert ("close", "memory_retrieval") not in collector.order
        assert task.context.memory_context is None

        gate.set()
        await span_task

        meta = collector.spans["memory_retrieval"]["metadata"]
        assert meta["abandoned"] is True
        assert meta["matches_attached"] is False
        assert meta["match_count"] == 1
        assert ("close", "memory_retrieval") in collector.order

    async def test_cancelled_search_closes_span(self):
        gate = asyncio.Event()

        async def _slow_search():
            await gate.wait()
            return [_make_match()]

        memory_task = asyncio.create_task(_slow_search())
        state = self._make_span_state()
        collector = _FakeSpanCollector()
        span_task = asyncio.create_task(_trace_memory_retrieval(memory_task, collector, state))
        await asyncio.sleep(0)  # let the span task open its span

        memory_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await memory_task
        with contextlib.suppress(asyncio.CancelledError):
            await span_task

        assert span_task.done()
        meta = collector.spans["memory_retrieval"]["metadata"]
        assert meta["cancelled"] is True
        assert ("close", "memory_retrieval") in collector.order


# ---------------------------------------------------------------------------
# Prelude: task creation only on the cache-miss path
# ---------------------------------------------------------------------------


def _make_prelude_task(text: str) -> IngressTask:
    return IngressTask(description=text, conversation_id="conv-prelude-mem", context=TaskContext(language="en"))


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
    orch._get_turns = AsyncMock(return_value=[])
    orch._resolve_language = AsyncMock(return_value="en")
    orch._get_bool_setting = AsyncMock(side_effect=lambda _key, default: default)
    orch._cache_orchestrator._get_bool_setting_impl = AsyncMock(side_effect=lambda _key, default: default)
    orch._finalize_action_replay_hit = AsyncMock(
        return_value={"speech": "Cached speech", "routed_to": "light-agent", "action_executed": None}
    )
    orch._get_personality_cached = AsyncMock(return_value="")
    return orch


class TestPreludeMemoryTask:
    async def test_memory_task_created_on_cache_miss(self):
        orch = _make_orchestrator()
        orch._cache_orchestrator.try_cache_replay = AsyncMock(return_value=(None, None))
        orch._classification_engine.classify = AsyncMock(
            return_value=([("light-agent", "Turn on kitchen light", 0.95)], False)
        )
        service = _make_memory_service()
        collector = _FakeSpanCollector()
        task = _make_prelude_task("turn on kitchen light")
        task.span_collector = collector

        with patch("app.agents.orchestrator.get_memory_service", return_value=service):
            prelude = await orch._run_pipeline_prelude(task)

        assert prelude.early_exit is None
        # The overlap task was created on the cache-miss path with the raw
        # user text and the request's user_id. (Wait-mode resolution of the
        # task into memory_context is covered by TestResolveMemoryContext.)
        service.search.assert_called_once()
        assert service.search.call_args.args[0] == "turn on kitchen light"
        # Let the search/span tasks settle (the abandoned-search done-callback
        # needs extra loop turns to resolve and close the span).
        for _ in range(10):
            await asyncio.sleep(0)
            if ("close", "memory_retrieval") in collector.order:
                break

        # The memory_retrieval span recorded the concurrent search.
        span = collector.spans.get("memory_retrieval")
        assert span is not None
        assert span["metadata"]["match_count"] == 1
        assert ("close", "memory_retrieval") in collector.order

    async def test_no_memory_task_on_cache_replay_hit(self):
        orch = _make_orchestrator()
        orch._cache_orchestrator.try_cache_replay = AsyncMock(return_value=(_make_action_hit(), None))
        service = _make_memory_service()

        with patch("app.agents.orchestrator.get_memory_service", return_value=service):
            prelude = await orch._run_pipeline_prelude(_make_prelude_task("turn on kitchen light"))

        assert prelude.early_exit is not None
        assert prelude.early_exit["_exit_type"] == "cache_replay"
        service.search.assert_not_called()

    async def test_no_memory_task_when_service_unavailable(self):
        orch = _make_orchestrator()
        orch._cache_orchestrator.try_cache_replay = AsyncMock(return_value=(None, None))
        orch._classification_engine.classify = AsyncMock(
            return_value=([("light-agent", "Turn on kitchen light", 0.95)], False)
        )
        task = _make_prelude_task("turn on kitchen light")

        with patch("app.agents.orchestrator.get_memory_service", return_value=None):
            prelude = await orch._run_pipeline_prelude(task)

        assert prelude.early_exit is None
        assert task.context.memory_context is None

    async def test_no_memory_task_when_disabled(self):
        orch = _make_orchestrator()
        orch._cache_orchestrator.try_cache_replay = AsyncMock(return_value=(None, None))
        orch._classification_engine.classify = AsyncMock(
            return_value=([("light-agent", "Turn on kitchen light", 0.95)], False)
        )
        service = _make_memory_service()
        service.is_enabled = AsyncMock(return_value=False)
        task = _make_prelude_task("turn on kitchen light")

        with patch("app.agents.orchestrator.get_memory_service", return_value=service):
            prelude = await orch._run_pipeline_prelude(task)

        assert prelude.early_exit is None
        service.search.assert_not_called()
        assert task.context.memory_context is None


# ---------------------------------------------------------------------------
# GeneralAgent system-prompt injection
# ---------------------------------------------------------------------------


class TestGeneralAgentMemoryBlock:
    def _make_dispatch_task(self, memory_context) -> DispatchTask:
        return DispatchTask(
            description="what did we discuss yesterday",
            conversation_id="conv-new",
            context=TaskContext(language="en", memory_context=memory_context),
        )

    async def _build_system_prompt(self, memory_context) -> str:
        agent = GeneralAgent()
        with patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="STATIC HEAD."):
            messages, _ = await agent._build_messages(self._make_dispatch_task(memory_context))
        assert messages[0]["role"] == "system"
        return messages[0]["content"]

    async def test_memory_block_after_static_head_with_scores(self):
        system = await self._build_system_prompt(
            [
                {
                    "conversation_id": "conv-old",
                    "similarity": 0.91,
                    "matched_text": "what about the timers",
                    "snippet_turns": [
                        {"user_text": "what about the timers", "response_text": "You set a 5 minute timer."}
                    ],
                    "continuation_turns": [{"user_text": "set a timer", "response_text": "Timer set."}],
                    "last_turn_at": 1754400000,
                    "user_id": "user-1",
                }
            ]
        )

        assert system.startswith("STATIC HEAD.")
        assert "## Possibly related past conversations" in system
        assert "not verified facts" in system
        assert "[score 0.91, 2025-08-05]" in system
        assert "You set a 5 minute timer." in system
        assert "### Previous session content" in system
        # Historical user text is delimiter-wrapped like live user input.
        assert USER_INPUT_START in system
        assert USER_INPUT_END in system
        # Memory enters via the system prompt only, never as a user message.
        assert system.index("STATIC HEAD.") < system.index("## Possibly related past conversations")

    async def test_no_memory_block_without_matches(self):
        system = await self._build_system_prompt(None)
        assert system == system.split("## Possibly related past conversations")[0]
        assert "## Possibly related past conversations" not in system

    async def test_no_memory_block_with_empty_matches(self):
        system = await self._build_system_prompt([])
        assert "## Possibly related past conversations" not in system
