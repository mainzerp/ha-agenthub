"""Tests for the unified orchestrator pipeline introduced by P1-1 and the
terminal-frame streaming mediation contract.

P1-1 keeps the existing public ``handle_task`` / ``handle_task_stream`` API
but routes both methods through ``_run_pipeline`` which selects between the
non-streaming and streaming impls. The legacy direct-call path can be
restored at runtime via ``ORCHESTRATOR_LEGACY_PIPELINE=1`` for emergency
rollback.

The current canonical flow relays sub-agent tokens immediately when
mediation is inactive (no personality configured and no calendar
reminder pending). Tokens stay buffered until the terminal frame when
mediation applies (personality set OR reminder pending).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock litellm before importing any app modules that depend on it.
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

import app.llm.client  # noqa: E402,F401 -- force module load for patch targets
from app.agents.orchestrator import OrchestratorAgent  # noqa: E402
from app.agents.task_pipeline import CacheReplayResult  # noqa: E402
from app.cache.cache_manager import ActionReplayOutcome, RoutingSkipOutcome  # noqa: E402
from app.models.agent import AgentCard, DispatchTask, IngressTask, TaskContext  # noqa: E402


def _make_task(text: str, *, conversation_id: str = "conv-pipe") -> IngressTask:
    return IngressTask(
        description=text,
        conversation_id=conversation_id,
        context=TaskContext(language="en"),
    )


def _make_orchestrator() -> tuple[OrchestratorAgent, AsyncMock]:
    dispatcher = AsyncMock()
    registry = AsyncMock()
    cache_manager = MagicMock()
    cache_manager.apply_rewrite = AsyncMock()
    cache_manager.try_replay_action = AsyncMock(return_value=None)
    cache_manager.try_routing_skip = AsyncMock(return_value=None)
    cache_manager.store_action_async = AsyncMock()

    async def _store_routing_async(*args, **kwargs):
        return cache_manager.store_routing(*args, **kwargs)

    cache_manager.store_routing_async = _store_routing_async

    registry.list_agents = AsyncMock(
        return_value=[
            AgentCard(
                agent_id="light-agent",
                name="Light Agent",
                description="",
                skills=["light"],
            ),
        ]
    )
    orch = OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=cache_manager)
    return orch, dispatcher


# ---------------------------------------------------------------------------
# P1-1: Pipeline parity between handle_task and _run_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("app.agents.orchestrator.SettingsRepository")
@patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
@patch("app.llm.client.complete", new_callable=AsyncMock)
async def test_handle_task_equals_run_pipeline_payload(mock_complete, mock_track, mock_settings):
    """handle_task() must return exactly the payload yielded by
    _run_pipeline(streaming=False) for the same task. This proves the
    wrapper does not lose or mutate the dict."""
    orch, _ = _make_orchestrator()
    mock_complete.side_effect = [
        "light-agent (95%): Turn on light",
        "light-agent (95%): Turn on light",
    ]
    mock_settings.get_value = AsyncMock(return_value="")

    captured = {"speech": "Light is on."}
    orch._dispatch_manager.dispatch_single = AsyncMock(return_value=("light-agent", "Light is on.", captured))

    task_a = _make_task("turn on light", conversation_id="conv-a")
    task_b = _make_task("turn on light", conversation_id="conv-b")

    direct = await orch.handle_task(task_a)

    pipeline_chunks = []
    async for chunk in orch._run_pipeline(task_b, streaming=False):
        pipeline_chunks.append(chunk)
    assert len(pipeline_chunks) == 1
    assert pipeline_chunks[0]["done"] is True
    payload = pipeline_chunks[0]["payload"]

    # Conversation ids differ by construction; everything else must match.
    direct.pop("conversation_id", None)
    payload.pop("conversation_id", None)
    assert direct == payload


@pytest.mark.asyncio
@patch("app.agents.orchestrator.SettingsRepository")
@patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
@patch("app.llm.client.complete", new_callable=AsyncMock)
async def test_streaming_pipeline_terminates_with_done(mock_complete, mock_track, mock_settings):
    """_run_pipeline(streaming=True) must yield a terminal done chunk
    that mirrors handle_task_stream's contract."""
    orch, dispatcher = _make_orchestrator()
    mock_complete.return_value = "light-agent (95%): Turn on light"
    mock_settings.get_value = AsyncMock(return_value="")

    async def mock_stream(_request):
        yield {"token": "Light is on.", "done": True}

    dispatcher.dispatch_stream = mock_stream
    task = _make_task("turn on light", conversation_id="conv-stream")

    chunks = [c async for c in orch._run_pipeline(task, streaming=True)]
    assert chunks, "streaming pipeline must yield at least the done chunk"
    assert chunks[-1]["done"] is True
    # P0: with mediation inactive (no personality, no reminder), the agent
    # token relays immediately and the terminal chunk omits mediated_speech.
    relayed = [c for c in chunks if not c["done"] and c.get("token")]
    assert [c["token"] for c in relayed] == ["Light is on."]
    assert chunks[-1].get("mediated_speech") is None


# ---------------------------------------------------------------------------
# Feature flag rollback path (ORCHESTRATOR_LEGACY_PIPELINE=1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_pipeline_flag_routes_directly_to_impls(monkeypatch):
    """With the rollback flag set, handle_task must bypass _run_pipeline
    and call _handle_task_impl directly; same for the streaming entry."""
    orch, _ = _make_orchestrator()
    monkeypatch.setenv("ORCHESTRATOR_LEGACY_PIPELINE", "1")

    impl_called = {"sync": 0, "stream": 0}

    async def _fake_impl(task, *, _pre_classified=None, _classify_reason=None, _allow_classify_cache_lookup=None):
        impl_called["sync"] += 1
        return {"speech": "ok", "conversation_id": task.conversation_id, "routed_to": "x"}

    async def _fake_stream_impl(task):
        impl_called["stream"] += 1
        yield {"token": "", "done": True, "conversation_id": task.conversation_id}

    orch._handle_task_impl = _fake_impl
    orch._handle_task_stream_impl = _fake_stream_impl

    # _run_pipeline must NOT be invoked in legacy mode -- spy that fails on call.
    async def _trap(*_args, **_kwargs):
        pytest.fail("_run_pipeline must be bypassed when legacy flag is set")
        yield {}  # pragma: no cover

    orch._run_pipeline = _trap

    task = _make_task("hello", conversation_id="conv-legacy")
    result = await orch.handle_task(task)
    assert result["speech"] == "ok"
    assert impl_called["sync"] == 1

    chunks = [c async for c in orch.handle_task_stream(task)]
    assert chunks and chunks[-1]["done"] is True
    assert impl_called["stream"] == 1


# ---------------------------------------------------------------------------
# Streaming mediation buffers tokens until the terminal frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("app.agents.orchestrator.SettingsRepository")
@patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
@patch("app.llm.client.complete", new_callable=AsyncMock)
async def test_streaming_mediation_buffers_tokens_until_terminal_frame(mock_complete, mock_track, mock_settings):
    """When mediation applies (personality configured, reminder pending),
    non-filler sub-agent tokens stay buffered until the terminal frame,
    which carries the mediated speech.
    """
    orch, dispatcher = _make_orchestrator()
    mock_complete.side_effect = [
        "light-agent (95%): Turn on light",  # classify
        "Hey! The light is now on. Don't forget your meeting.",  # mediation
    ]
    mock_settings.get_value = AsyncMock(
        side_effect=lambda k, d=None: {
            "personality.prompt": "You are a friendly assistant.",
            "orchestrator.mediation_streaming_enabled": "false",
            "rewrite.model": "groq/llama-3.1-8b-instant",
            "rewrite.temperature": "0.3",
        }.get(k, d)
    )
    orch._calendar_injector = MagicMock()
    orch._calendar_injector.inject_reminders = AsyncMock(return_value="Meeting at 3pm.")

    async def mock_stream(_request):
        yield {"token": "Light ", "done": False}
        yield {"token": "is ", "done": False}
        yield {"token": "on.", "done": True}

    dispatcher.dispatch_stream = mock_stream
    task = _make_task("turn on light", conversation_id="conv-stream-med")

    chunks = [c async for c in orch.handle_task_stream(task)]

    raw_tokens = [c for c in chunks if not c["done"] and not c.get("is_filler") and c.get("token")]
    assert raw_tokens == []

    final = [c for c in chunks if c["done"]]
    assert len(final) == 1
    assert final[0].get("mediated_speech")


@pytest.mark.asyncio
@patch("app.agents.orchestrator.SettingsRepository")
@patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
@patch("app.llm.client.complete", new_callable=AsyncMock)
async def test_stream_with_filler_cancels_reader_on_timeout(mock_complete, mock_track, mock_settings):
    """CONT-6.3: When filler threshold is exceeded, the reader task must be cancelled in finally."""

    mock_settings.get_value = AsyncMock(return_value="")
    mock_complete.return_value = "light-agent (95%): Turn on light"
    orch, dispatcher = _make_orchestrator()

    async def _slow_stream(_request):
        await asyncio.sleep(0.06)
        yield {"token": "late", "done": True}

    dispatcher.dispatch_stream = _slow_stream
    task = _make_task("turn on light", conversation_id="conv-slow")

    # Force filler to be used with a very short threshold
    orch._should_send_filler = AsyncMock(return_value=True)
    orch._get_filler_threshold_ms = AsyncMock(return_value=50)
    orch._invoke_filler_agent = AsyncMock(return_value="One moment please.")

    chunks = []
    async for chunk in orch.handle_task_stream(task):
        chunks.append(chunk)

    # Should have received filler and then terminal chunk without hanging
    assert any(c.get("filler_push") for c in chunks)


@pytest.mark.asyncio
async def test_conversation_cache_max_size():
    """CONT-8.3: The conversation cache must enforce a max size of 1000 entries."""
    import time

    orch, _dispatcher = _make_orchestrator()

    now = time.monotonic()
    for i in range(1002):
        orch._conversation_manager._conversations[f"conv-{i}"] = (now, [{"role": "user", "content": "hi"}])

    orch._evict_stale_conversations()

    assert len(orch._conversation_manager._conversations) == 1000
    # Oldest entries should have been evicted
    assert "conv-0" not in orch._conversation_manager._conversations
    assert "conv-1" not in orch._conversation_manager._conversations


# ---------------------------------------------------------------------------
# Phase 3 gaps: G2, G11, L6, L7
# ---------------------------------------------------------------------------


class TestEventBusPublishing:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_event_bus_pre_classify_and_post_classify(self, mock_complete, mock_track, mock_settings):
        """G2: Event bus must publish pre_classify and post_classify events."""
        mock_settings.get_value = AsyncMock(return_value="")
        mock_complete.return_value = "light-agent (95%): Turn on light"
        orch, _dispatcher = _make_orchestrator()

        event_bus = AsyncMock()
        orch._event_bus = event_bus
        orch._get_turns = AsyncMock(return_value=[])
        orch._dispatch_manager.dispatch_single = AsyncMock(
            return_value=("light-agent", "Light is on.", {"speech": "Light is on."})
        )

        task = _make_task("turn on light")
        await orch.handle_task(task)

        published_events = [call.args[0] for call in event_bus.publish.await_args_list]
        assert "pipeline.pre_classify" in published_events
        assert "pipeline.post_classify" in published_events

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_event_bus_pre_dispatch_and_post_dispatch(self, mock_complete, mock_track, mock_settings):
        """G2: Event bus must publish pre_dispatch and post_dispatch events."""
        mock_settings.get_value = AsyncMock(return_value="")
        mock_complete.return_value = "light-agent (95%): Turn on light"
        orch, _dispatcher = _make_orchestrator()

        event_bus = AsyncMock()
        orch._event_bus = event_bus
        orch._get_turns = AsyncMock(return_value=[])
        orch._dispatch_manager.dispatch_single = AsyncMock(
            return_value=("light-agent", "Light is on.", {"speech": "Light is on."})
        )

        task = _make_task("turn on light")
        await orch.handle_task(task)

        published_events = [call.args[0] for call in event_bus.publish.await_args_list]
        assert "pipeline.pre_dispatch" in published_events
        assert "pipeline.post_dispatch" in published_events


class TestRunPipelineDefensiveFallback:
    @pytest.mark.asyncio
    async def test_run_pipeline_defensive_fallback_on_malformed_chunks(self):
        """G11: _run_pipeline must handle malformed chunks gracefully in non-streaming mode."""
        orch, _dispatcher = _make_orchestrator()

        # Simulate _run_pipeline receiving chunks without "payload"
        async def _broken_run_pipeline(task, streaming=False, **kwargs):
            yield {"done": False, "token": "partial"}
            yield {"done": True}  # missing payload

        orch._run_pipeline = _broken_run_pipeline
        orch._legacy_pipeline_enabled = lambda: False

        task = _make_task("turn on light")
        # The fallback should call _handle_task_impl directly
        orch._handle_task_impl = AsyncMock(return_value={"speech": "Fallback!", "routed_to": "light-agent"})

        result = await orch.handle_task(task)
        assert result["speech"] == "Fallback!"

    @pytest.mark.asyncio
    async def test_run_pipeline_no_terminal_chunk_fallback(self):
        """L7: Test defensive fallback path explicitly when no terminal chunk arrives."""
        orch, _dispatcher = _make_orchestrator()

        async def _empty_run_pipeline(task, streaming=False, **kwargs):
            return
            yield  # make it an async generator

        orch._run_pipeline = _empty_run_pipeline
        orch._legacy_pipeline_enabled = lambda: False

        task = _make_task("turn on light")
        orch._handle_task_impl = AsyncMock(return_value={"speech": "Fallback!", "routed_to": "light-agent"})

        result = await orch.handle_task(task)
        assert result["speech"] == "Fallback!"


# ---------------------------------------------------------------------------
# ENTITY_RESOLUTION_REWORK: prelude without orchestrator pre-resolution
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


def _make_orchestrator_with_matcher(pool):
    """Orchestrator with a mocked entity matcher.

    The orchestrator no longer runs an ingress matcher pass
    (ENTITY_RESOLUTION_REWORK); the double exists to PROVE the matcher is
    never invoked from the prelude.
    """
    orch, dispatcher = _make_orchestrator()
    matcher = AsyncMock()
    matcher.match = AsyncMock(return_value=list(pool))
    matcher.filter_visible_results = AsyncMock(side_effect=lambda _agent_id, results: list(results))
    orch._entity_matcher = matcher
    return orch, dispatcher, matcher


def _pool_item(entity_id: str, friendly_name: str, score: float):
    return SimpleNamespace(entity_id=entity_id, friendly_name=friendly_name, score=score)


def _patch_prelude_classification(orch, classifications, routing_cached=False):
    """Shortcut: stub the classification phase of the prelude."""
    target_agent, condensed_task, confidence = classifications[0]
    orch._pipeline_director.run_classification = AsyncMock(
        return_value=(classifications, routing_cached, target_agent, condensed_task, confidence)
    )
    orch._pipeline_resolve_conversation_and_language = AsyncMock(return_value=("conv-prelude", "en", []))


class TestPreludeWithoutIngressResolution:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    async def test_matcher_not_started_on_action_replay_hit(self, mock_settings):
        """A replay hit short-circuits the prelude and never touches the matcher."""
        mock_settings.get_value = AsyncMock(return_value="")
        pool = [_pool_item("light.couch", "Couch", 0.9)]
        orch, _dispatcher, matcher = _make_orchestrator_with_matcher(pool)

        replay = ActionReplayOutcome(
            kind="full_hit",
            entry_id="entry-1",
            agent_id="light-agent",
            response_text="Light is on.",
        )
        orch._pipeline_director.run_cache_replay = AsyncMock(return_value=CacheReplayResult(action_replay=replay))
        orch._finalize_action_replay_hit = AsyncMock(
            return_value={"speech": "Light is on.", "routed_to": "light-agent"}
        )

        task = _make_task("turn on couch light", conversation_id="conv-prelude-replay")
        prelude = await orch._run_pipeline_prelude(task)

        assert prelude.early_exit is not None
        assert prelude.early_exit["_exit_type"] == "cache_replay"
        matcher.match.assert_not_called()
        matcher.filter_visible_results.assert_not_called()
        assert prelude.routing_entry_id is None

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    async def test_cache_miss_never_runs_ingress_matcher(self, mock_settings):
        """The prelude routes only: no matcher pass, no ingress spans, no
        entity pool leaves the orchestrator (agents recall entities
        themselves)."""
        mock_settings.get_value = AsyncMock(return_value="")
        pool = [_pool_item("light.couch", "Couch", 0.9)]
        orch, _dispatcher, matcher = _make_orchestrator_with_matcher(pool)
        orch._pipeline_director.run_cache_replay = AsyncMock(return_value=CacheReplayResult())
        _patch_prelude_classification(orch, [("light-agent", "Turn on couch light", 0.95)])

        collector = _FakeSpanCollector()
        task = IngressTask(
            description="turn on couch light",
            conversation_id="conv-prelude-miss",
            context=TaskContext(language="en"),
            span_collector=collector,
        )
        prelude = await orch._run_pipeline_prelude(task)

        assert prelude.early_exit is None
        assert prelude.target_agent == "light-agent"
        matcher.match.assert_not_called()
        matcher.filter_visible_results.assert_not_called()
        assert "ingress_resolution" not in collector.spans
        assert "ingress_candidates" not in collector.spans
        assert prelude.routing_entry_id is None

    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    async def test_routing_cache_hit_threads_entry_id(self, mock_settings):
        """A served routing-cache hit threads its entry id onto the prelude
        result (R-B: a failed cached-agent turn invalidates the entry at
        finalization)."""
        mock_settings.get_value = AsyncMock(return_value="")
        orch, _dispatcher, matcher = _make_orchestrator_with_matcher([])
        routing_skip = RoutingSkipOutcome(
            kind="routing_hit",
            entry_id="route-1",
            agent_id="light-agent",
            condensed_task="turn on light",
            similarity=1.0,
            entity_ids=["light.couch"],
        )
        orch._pipeline_director.run_cache_replay = AsyncMock(return_value=CacheReplayResult(routing_skip=routing_skip))
        _patch_prelude_classification(
            orch,
            [("light-agent", "turn on light", 1.0)],
            routing_cached=True,
        )

        task = _make_task("turn on light", conversation_id="conv-prelude-routing")
        prelude = await orch._run_pipeline_prelude(task)

        assert prelude.early_exit is None
        assert prelude.routing_cached is True
        assert prelude.routing_entry_id == "route-1"
        matcher.match.assert_not_called()


class TestDispatchTaskWithoutCandidates:
    @pytest.mark.asyncio
    @patch("app.agents.orchestrator.SettingsRepository")
    @patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
    @patch("app.llm.client.complete", new_callable=AsyncMock)
    async def test_streaming_dispatch_task_carries_no_candidates(self, mock_complete, mock_track, mock_settings):
        """The streaming DispatchTask construction site no longer carries
        entity candidates (agent-side keyword recall replaced them)."""
        mock_settings.get_value = AsyncMock(return_value="")
        mock_complete.return_value = "light-agent (95%): Turn on couch light"
        orch, dispatcher = _make_orchestrator()

        captured: dict = {}

        async def mock_stream(request):
            captured["task"] = request.params["task"]
            yield {"token": "Light is on.", "done": True}

        dispatcher.dispatch_stream = mock_stream
        task = _make_task("turn on couch light", conversation_id="conv-stream-nocands")

        chunks = [c async for c in orch.handle_task_stream(task)]
        assert chunks[-1]["done"] is True
        dispatched = captured["task"]
        assert isinstance(dispatched, DispatchTask)
        assert not hasattr(dispatched, "candidates")

    @pytest.mark.asyncio
    async def test_sequential_send_dispatches_without_candidates(self):
        """Sequential send: neither leg receives a candidates kwarg."""
        orch, _dispatcher = _make_orchestrator()
        orch._dispatch_single = AsyncMock(
            side_effect=[
                ("lists-agent", "Here is the content.", {"speech": "Here is the content."}),
                ("send-agent", "Sent.", {"speech": "Sent."}),
            ]
        )
        classifications = [
            ("lists-agent", "list the tasks", 0.9),
            ("send-agent", "send it", 0.9),
        ]
        routed_to, speech, _result = await orch._handle_sequential_send(
            classifications,
            "send my tasks",
            "conv-seq",
            [],
            None,
            None,
        )

        assert routed_to == "lists-agent, send-agent"
        assert speech == "Sent."
        content_call, send_call = orch._dispatch_single.await_args_list
        assert "candidates" not in content_call.kwargs
        assert "candidates" not in send_call.kwargs
