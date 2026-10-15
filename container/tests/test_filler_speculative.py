"""Tests for the speculative filler start (filler generation overlaps classification)."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from datetime import datetime
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
from app.agents.filler import FillerAgent  # noqa: E402
from app.agents.filler_coordinator import FillerCoordinator  # noqa: E402
from app.agents.orchestrator import (  # noqa: E402
    OrchestratorAgent,
    PipelinePreludeResult,
    _cancel_filler_future,
    _FillerSlot,
)
from app.analytics.tracer import SpanCollector  # noqa: E402
from app.models.agent import AgentCard, DispatchTask, IngressTask, TaskContext  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(text: str = "search the web for lasagna", conversation_id: str = "conv-spec") -> IngressTask:
    task = IngressTask(
        description=text,
        conversation_id=conversation_id,
        context=TaskContext(language="en"),
    )
    task.span_collector = SpanCollector(f"trace-{conversation_id}")
    return task


def _settings(**overrides: str) -> AsyncMock:
    values = {
        "filler.enabled": "true",
        "filler.threshold_ms": "50",
        "filler.speculative_start": "true",
        "language": "en",
    }
    values.update({k.replace("__", "."): v for k, v in overrides.items()})
    return AsyncMock(side_effect=lambda k, d=None: values.get(k, d))


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
            AgentCard(agent_id="light-agent", name="Light Agent", description="", skills=["light"]),
            AgentCard(
                agent_id="general-agent",
                name="General Agent",
                description="",
                skills=["general"],
                expected_latency="high",
            ),
        ]
    )
    orch = OrchestratorAgent(dispatcher=dispatcher, registry=registry, cache_manager=cache_manager)
    orch._create_trace = AsyncMock()
    orch._store_turn = AsyncMock()
    return orch, dispatcher


def _classifier(
    classifications: list[tuple[str, str, float | None]],
    *,
    wait_for: asyncio.Event | None = None,
    delay: float = 0.0,
    log: list[str] | None = None,
):
    """Replacement for ``PipelineDirector.run_classification``."""

    async def _run(*_args, **_kwargs):
        if wait_for is not None:
            await asyncio.wait_for(wait_for.wait(), timeout=2)
        if delay:
            await asyncio.sleep(delay)
        if log is not None:
            log.append("classified")
        target_agent, condensed_task, confidence = classifications[0]
        return classifications, False, target_agent, condensed_task, confidence

    return _run


def _hanging_filler(started: asyncio.Event, cancelled: asyncio.Event):
    async def _filler(user_text, agent_id, language):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    return _filler


def _spans(task: IngressTask, name: str) -> list[dict]:
    return [s for s in task.span_collector._spans if s.get("span_name") == name]


def _ts(span: dict, key: str = "start_time") -> datetime:
    return datetime.fromisoformat(span[key])


_SINGLE_HIGH = [("general-agent", "search lasagna", 0.95)]
_SINGLE_LOW = [("light-agent", "turn on light", 0.95)]


# ---------------------------------------------------------------------------
# Streaming: speculative start and handover
# ---------------------------------------------------------------------------


@patch("app.agents.orchestrator.SettingsRepository")
@patch("app.agents.orchestrator.track_request", new_callable=AsyncMock)
class TestSpeculativeFillerStreaming:
    async def test_speculative_filler_starts_before_classification_completes(self, mock_track, mock_settings):
        mock_settings.get_value = _settings()
        orch, dispatcher = _make_orchestrator()
        started, cancelled = asyncio.Event(), asyncio.Event()
        orch._invoke_filler_agent = AsyncMock(side_effect=_hanging_filler(started, cancelled))
        orch._should_send_filler = AsyncMock(return_value=False)
        # Classification only completes once the filler LLM call is running.
        orch._pipeline_director.run_classification = AsyncMock(side_effect=_classifier(_SINGLE_LOW, wait_for=started))

        async def _stream(_req):
            yield {"token": "Done", "done": True}

        dispatcher.dispatch_stream = _stream
        chunks = [c async for c in orch.handle_task_stream(_make_task())]

        assert started.is_set()
        # Target agent unknown before routing: empty target.
        assert orch._invoke_filler_agent.await_args.args[1] == ""
        assert chunks[-1]["done"] is True

    async def test_speculative_filler_reused_for_high_latency_target(self, mock_track, mock_settings):
        """Routed to a high-latency agent: the speculative task is reused (one
        filler call), and a filler finished before the race is pushed at once."""
        mock_settings.get_value = _settings()
        orch, dispatcher = _make_orchestrator()
        filler_done = asyncio.Event()

        async def _quick_filler(user_text, agent_id, language):
            await asyncio.sleep(0.01)
            filler_done.set()
            return "One moment please."

        orch._invoke_filler_agent = AsyncMock(side_effect=_quick_filler)
        orch._should_send_filler = AsyncMock(return_value=True)
        # Classification completes only after filler generation finished, so
        # the filler is already done when the threshold race starts.
        orch._pipeline_director.run_classification = AsyncMock(
            side_effect=_classifier(_SINGLE_HIGH, wait_for=filler_done)
        )

        filler_seen = asyncio.Event()

        async def _slow_stream(_req):
            await asyncio.wait_for(filler_seen.wait(), timeout=5)
            yield {"token": "Here is the recipe", "done": False}
            yield {"token": "", "done": True}

        dispatcher.dispatch_stream = _slow_stream
        task = _make_task()
        chunks = []
        async for c in orch.handle_task_stream(task):
            chunks.append(c)
            if c.get("filler_push"):
                filler_seen.set()

        assert [c["filler_push"] for c in chunks if c.get("filler_push")] == ["One moment please."]
        assert orch._invoke_filler_agent.await_count == 1
        assert orch._invoke_filler_agent.await_args.args[1] == ""

        (fg,) = _spans(task, "filler_generate")
        (fs,) = _spans(task, "filler_send")
        (dispatch,) = _spans(task, "dispatch")
        meta = fg["metadata"]
        assert meta["speculative"] is True
        assert meta["was_sent"] is True
        assert meta["threshold_ms"] == 50
        assert meta["target_agent"] == "general-agent"
        assert meta["filler_text"] == "One moment please."
        # Real timing: generation started before dispatch (during
        # classification) and ended before the push; its duration is the
        # generation time, not the time until the push (which also covers
        # the 50 ms threshold counted from request start).
        assert _ts(fg) < _ts(dispatch)
        assert _ts(fg, "end_time") <= _ts(fs)
        assert fg["duration_ms"] > 0
        assert fg["duration_ms"] < (_ts(fs) - _ts(fg)).total_seconds() * 1000
        assert fs["duration_ms"] == 0

    async def test_speculative_filler_cancelled_for_low_latency_target(self, mock_track, mock_settings):
        mock_settings.get_value = _settings()
        orch, dispatcher = _make_orchestrator()
        started, cancelled = asyncio.Event(), asyncio.Event()
        orch._invoke_filler_agent = AsyncMock(side_effect=_hanging_filler(started, cancelled))
        orch._should_send_filler = AsyncMock(return_value=False)
        orch._pipeline_director.run_classification = AsyncMock(side_effect=_classifier(_SINGLE_LOW, wait_for=started))

        async def _stream(_req):
            yield {"token": "Done", "done": True}

        dispatcher.dispatch_stream = _stream
        task = _make_task("turn on light")
        chunks = [c async for c in orch.handle_task_stream(task)]

        assert cancelled.is_set()
        assert not [c for c in chunks if c.get("filler_push")]
        assert orch._invoke_filler_agent.await_count == 1
        (fg,) = _spans(task, "filler_generate")
        assert fg["metadata"]["was_sent"] is False
        assert fg["metadata"]["cancelled"] is True
        assert fg["metadata"]["cancel_reason"] == "target_not_eligible"
        assert fg["metadata"]["speculative"] is True
        assert not _spans(task, "filler_send")

    @pytest.mark.parametrize("target", ["noise", "cancel-interaction"])
    async def test_speculative_filler_cancelled_on_early_exit(self, mock_track, mock_settings, target):
        mock_settings.get_value = _settings()
        orch, dispatcher = _make_orchestrator()
        started, cancelled = asyncio.Event(), asyncio.Event()
        orch._invoke_filler_agent = AsyncMock(side_effect=_hanging_filler(started, cancelled))
        orch._should_send_filler = AsyncMock(return_value=True)
        orch._finalize_noise_turn = AsyncMock()
        orch._pipeline_director.run_classification = AsyncMock(
            side_effect=_classifier([(target, "x", 1.0)], wait_for=started)
        )
        task = _make_task("never mind")
        with patch("app.agents.orchestrator.generate_cancel_speech", new=AsyncMock(return_value="Okay.")):
            chunks = [c async for c in orch.handle_task_stream(task)]

        assert chunks[-1]["done"] is True
        assert cancelled.is_set()
        assert not [c for c in chunks if c.get("filler_push")]
        dispatcher.dispatch_stream.assert_not_called()
        (fg,) = _spans(task, "filler_generate")
        assert fg["metadata"]["cancel_reason"] == ("noise" if target == "noise" else "cancel_interaction")

    async def test_speculative_filler_cancelled_on_multi_agent(self, mock_track, mock_settings):
        mock_settings.get_value = _settings()
        orch, _ = _make_orchestrator()
        started, cancelled = asyncio.Event(), asyncio.Event()
        orch._invoke_filler_agent = AsyncMock(side_effect=_hanging_filler(started, cancelled))
        orch._should_send_filler = AsyncMock(return_value=True)
        orch._dispatch_and_finalize = AsyncMock(return_value={"speech": "Done."})
        orch._pipeline_director.run_classification = AsyncMock(
            side_effect=_classifier(_SINGLE_LOW + _SINGLE_HIGH, wait_for=started)
        )
        task = _make_task("turn on light and search lasagna")
        chunks = [c async for c in orch.handle_task_stream(task)]

        assert chunks[-1]["mediated_speech"] == "Done."
        assert cancelled.is_set()
        assert not [c for c in chunks if c.get("filler_push")]
        (fg,) = _spans(task, "filler_generate")
        assert fg["metadata"]["cancel_reason"] == "multi_agent"

    async def test_speculative_filler_reused_in_sequential_send(self, mock_track, mock_settings):
        mock_settings.get_value = _settings()
        orch, _ = _make_orchestrator()
        orch._invoke_filler_agent = AsyncMock(return_value="Hold on.")
        orch._should_send_filler = AsyncMock(return_value=True)
        filler_seen = asyncio.Event()

        async def _slow_handle(task, prelude, **_kw):
            await asyncio.wait_for(filler_seen.wait(), timeout=5)
            return {"speech": "Recipe sent."}

        orch._dispatch_and_finalize = AsyncMock(side_effect=_slow_handle)
        orch._pipeline_director.run_classification = AsyncMock(
            side_effect=_classifier([*_SINGLE_HIGH, ("send-agent", "send to Laura", 0.95)])
        )
        task = _make_task("find a recipe and send it to Laura")
        chunks = []
        async for c in orch.handle_task_stream(task):
            chunks.append(c)
            if c.get("filler_push"):
                filler_seen.set()

        assert [c["filler_push"] for c in chunks if c.get("filler_push")] == ["Hold on."]
        assert orch._invoke_filler_agent.await_count == 1
        assert orch._invoke_filler_agent.await_args.args[1] == ""
        (fg,) = _spans(task, "filler_generate")
        assert fg["metadata"]["speculative"] is True
        assert fg["metadata"]["sequential_send"] is True
        assert fg["metadata"]["was_sent"] is True

    async def test_setting_off_starts_filler_at_dispatch(self, mock_track, mock_settings):
        mock_settings.get_value = _settings(filler__speculative_start="false")
        orch, dispatcher = _make_orchestrator()
        log: list[str] = []

        async def _filler(user_text, agent_id, language):
            log.append("filler")
            return "One moment please."

        orch._invoke_filler_agent = AsyncMock(side_effect=_filler)
        orch._should_send_filler = AsyncMock(return_value=True)
        orch._pipeline_director.run_classification = AsyncMock(side_effect=_classifier(_SINGLE_HIGH, log=log))
        filler_seen = asyncio.Event()

        async def _slow_stream(_req):
            await asyncio.wait_for(filler_seen.wait(), timeout=5)
            yield {"token": "Here", "done": True}

        dispatcher.dispatch_stream = _slow_stream
        task = _make_task()
        chunks = []
        async for c in orch.handle_task_stream(task):
            chunks.append(c)
            if c.get("filler_push"):
                filler_seen.set()

        assert [c["filler_push"] for c in chunks if c.get("filler_push")] == ["One moment please."]
        # Started after routing, with the routed target agent.
        assert log == ["classified", "filler"]
        assert orch._invoke_filler_agent.await_count == 1
        assert orch._invoke_filler_agent.await_args.args[1] == "general-agent"
        (fg,) = _spans(task, "filler_generate")
        assert fg["metadata"]["speculative"] is False

    async def test_filler_disabled_starts_nothing(self, mock_track, mock_settings):
        mock_settings.get_value = _settings(filler__enabled="false")
        orch, dispatcher = _make_orchestrator()
        orch._invoke_filler_agent = AsyncMock(return_value="One moment please.")
        orch._pipeline_director.run_classification = AsyncMock(side_effect=_classifier(_SINGLE_HIGH))

        async def _stream(_req):
            yield {"token": "Here", "done": True}

        dispatcher.dispatch_stream = _stream
        task = _make_task()
        [c async for c in orch.handle_task_stream(task)]

        orch._invoke_filler_agent.assert_not_called()
        assert not _spans(task, "filler_generate")

    async def test_client_cancellation_cancels_speculative_filler(self, mock_track, mock_settings):
        mock_settings.get_value = _settings()
        orch, _ = _make_orchestrator()
        started, cancelled = asyncio.Event(), asyncio.Event()
        orch._invoke_filler_agent = AsyncMock(side_effect=_hanging_filler(started, cancelled))
        never = asyncio.Event()

        async def _hanging_classify(*_args, **_kwargs):
            await never.wait()

        orch._pipeline_director.run_classification = AsyncMock(side_effect=_hanging_classify)

        async def _consume():
            return [c async for c in orch.handle_task_stream(_make_task())]

        consumer = asyncio.create_task(_consume())
        await asyncio.wait_for(started.wait(), timeout=2)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert cancelled.is_set()

    async def test_classification_exception_cancels_speculative_filler(self, mock_track, mock_settings):
        mock_settings.get_value = _settings()
        orch, _ = _make_orchestrator()
        started, cancelled = asyncio.Event(), asyncio.Event()
        orch._invoke_filler_agent = AsyncMock(side_effect=_hanging_filler(started, cancelled))

        async def _failing_classify(*_args, **_kwargs):
            await asyncio.wait_for(started.wait(), timeout=2)
            raise RuntimeError("classifier down")

        orch._pipeline_director.run_classification = AsyncMock(side_effect=_failing_classify)

        with pytest.raises(RuntimeError, match="classifier down"):
            [c async for c in orch.handle_task_stream(_make_task())]
        assert cancelled.is_set()

    async def test_text_only_source_never_starts_speculative_filler(self, mock_track, mock_settings):
        mock_settings.get_value = _settings()
        orch, dispatcher = _make_orchestrator()
        orch._invoke_filler_agent = AsyncMock(return_value="One moment please.")
        orch._pipeline_director.run_classification = AsyncMock(side_effect=_classifier(_SINGLE_HIGH))

        async def _stream(_req):
            yield {"token": "Here", "done": True}

        dispatcher.dispatch_stream = _stream
        task = _make_task()
        task.context.source = "openai"
        [c async for c in orch.handle_task_stream(task)]

        orch._invoke_filler_agent.assert_not_called()

    async def test_sequential_send_low_latency_target_discards_filler(self, mock_track, mock_settings):
        mock_settings.get_value = _settings()
        orch, _ = _make_orchestrator()
        started, cancelled = asyncio.Event(), asyncio.Event()
        orch._invoke_filler_agent = AsyncMock(side_effect=_hanging_filler(started, cancelled))
        orch._should_send_filler = AsyncMock(return_value=False)
        orch._dispatch_and_finalize = AsyncMock(return_value={"speech": "Light on and sent."})
        orch._pipeline_director.run_classification = AsyncMock(
            side_effect=_classifier([*_SINGLE_LOW, ("send-agent", "send to Laura", 0.95)], wait_for=started)
        )
        task = _make_task("turn on the light and tell Laura")
        chunks = [c async for c in orch.handle_task_stream(task)]

        assert chunks[-1]["mediated_speech"] == "Light on and sent."
        assert not [c for c in chunks if c.get("filler_push")]
        assert cancelled.is_set()
        (fg,) = _spans(task, "filler_generate")
        meta = fg["metadata"]
        assert meta["cancel_reason"] == "target_not_eligible"
        assert meta["cancelled"] is True
        assert meta["was_sent"] is False
        assert meta["sequential_send"] is True
        assert meta["target_agent"] == "light-agent"

    @pytest.mark.parametrize("outcome", ["empty", "error"])
    async def test_failed_speculative_filler_does_not_affect_agent(self, mock_track, mock_settings, outcome):
        mock_settings.get_value = _settings()
        orch, dispatcher = _make_orchestrator()
        filler_done = asyncio.Event()

        async def _bad_filler(user_text, agent_id, language):
            filler_done.set()
            if outcome == "error":
                raise RuntimeError("filler backend down")
            return None

        orch._invoke_filler_agent = AsyncMock(side_effect=_bad_filler)
        orch._should_send_filler = AsyncMock(return_value=True)
        orch._pipeline_director.run_classification = AsyncMock(
            side_effect=_classifier(_SINGLE_HIGH, wait_for=filler_done)
        )

        async def _slow_stream(_req):
            # Past the 50 ms threshold, so the (failed) filler is consulted.
            await asyncio.sleep(0.1)
            yield {"token": "Here is the recipe", "done": True}

        dispatcher.dispatch_stream = _slow_stream
        task = _make_task()
        chunks = [c async for c in orch.handle_task_stream(task)]

        assert not [c for c in chunks if c.get("filler_push")]
        spoken = "".join(c.get("token", "") + (c.get("mediated_speech") or "") for c in chunks)
        assert "Here is the recipe" in spoken
        assert chunks[-1]["done"] is True
        assert "error" not in chunks[-1]
        assert not _spans(task, "filler_generate")

    async def test_finished_filler_agent_first_records_span(self, mock_track, mock_settings):
        """The filler finished, but the agent's first chunk beat the threshold."""
        mock_settings.get_value = _settings(filler__threshold_ms="5000")
        orch, dispatcher = _make_orchestrator()
        filler_done = asyncio.Event()

        async def _filler(user_text, agent_id, language):
            filler_done.set()
            return "One moment please."

        orch._invoke_filler_agent = AsyncMock(side_effect=_filler)
        orch._should_send_filler = AsyncMock(return_value=True)
        orch._pipeline_director.run_classification = AsyncMock(
            side_effect=_classifier(_SINGLE_HIGH, wait_for=filler_done)
        )

        async def _stream(_req):
            yield {"token": "Here is the recipe", "done": True}

        dispatcher.dispatch_stream = _stream
        task = _make_task()
        chunks = [c async for c in orch.handle_task_stream(task)]

        assert not [c for c in chunks if c.get("filler_push")]
        (fg,) = _spans(task, "filler_generate")
        (dispatch,) = _spans(task, "dispatch")
        meta = fg["metadata"]
        assert meta["was_sent"] is False
        assert meta["cancelled"] is False
        assert meta["cancel_reason"] == "agent_first"
        assert meta["filler_text"] == "One moment please."
        assert meta["speculative"] is True
        assert _ts(fg) < _ts(dispatch)
        assert not _spans(task, "filler_send")

    async def test_sequential_finished_filler_agent_first_records_span(self, mock_track, mock_settings):
        mock_settings.get_value = _settings(filler__threshold_ms="5000")
        orch, _ = _make_orchestrator()
        filler_done = asyncio.Event()

        async def _filler(user_text, agent_id, language):
            filler_done.set()
            return "Hold on."

        orch._invoke_filler_agent = AsyncMock(side_effect=_filler)
        orch._should_send_filler = AsyncMock(return_value=True)
        orch._dispatch_and_finalize = AsyncMock(return_value={"speech": "Recipe sent."})
        orch._pipeline_director.run_classification = AsyncMock(
            side_effect=_classifier([*_SINGLE_HIGH, ("send-agent", "send to Laura", 0.95)], wait_for=filler_done)
        )
        task = _make_task("find a recipe and send it to Laura")
        chunks = [c async for c in orch.handle_task_stream(task)]

        assert chunks[-1]["mediated_speech"] == "Recipe sent."
        assert not [c for c in chunks if c.get("filler_push")]
        (fg,) = _spans(task, "filler_generate")
        meta = fg["metadata"]
        assert meta["was_sent"] is False
        assert meta["cancelled"] is False
        assert meta["cancel_reason"] == "agent_first"
        assert meta["sequential_send"] is True
        assert meta["filler_text"] == "Hold on."

    async def test_finished_filler_discarded_reports_not_cancelled(self, mock_track, mock_settings):
        mock_settings.get_value = _settings()
        orch, dispatcher = _make_orchestrator()
        filler_done = asyncio.Event()

        async def _filler(user_text, agent_id, language):
            filler_done.set()
            return "One moment please."

        orch._invoke_filler_agent = AsyncMock(side_effect=_filler)
        orch._should_send_filler = AsyncMock(return_value=False)
        orch._pipeline_director.run_classification = AsyncMock(
            side_effect=_classifier(_SINGLE_LOW, wait_for=filler_done)
        )

        async def _stream(_req):
            yield {"token": "Done", "done": True}

        dispatcher.dispatch_stream = _stream
        task = _make_task("turn on light")
        [c async for c in orch.handle_task_stream(task)]

        (fg,) = _spans(task, "filler_generate")
        assert fg["metadata"]["cancelled"] is False
        assert fg["metadata"]["cancel_reason"] == "target_not_eligible"
        assert fg["metadata"]["filler_text"] == "One moment please."

    @pytest.mark.parametrize(("target", "expect_push"), [("general-agent", True), ("light-agent", False)])
    async def test_real_gate_by_expected_latency(self, mock_track, mock_settings, target, expect_push):
        """No mock on ``_should_send_filler``: the agent card's expected_latency decides."""
        mock_settings.get_value = _settings()
        orch, dispatcher = _make_orchestrator()
        orch._invoke_filler_agent = AsyncMock(return_value="One moment please.")
        orch._pipeline_director.run_classification = AsyncMock(side_effect=_classifier([(target, "do it", 0.95)]))
        filler_seen = asyncio.Event()

        async def _stream(_req):
            if expect_push:
                await asyncio.wait_for(filler_seen.wait(), timeout=5)
            yield {"token": "Done", "done": True}

        dispatcher.dispatch_stream = _stream
        task = _make_task()
        chunks = []
        async for c in orch.handle_task_stream(task):
            chunks.append(c)
            if c.get("filler_push"):
                filler_seen.set()

        pushes = [c["filler_push"] for c in chunks if c.get("filler_push")]
        (fg,) = _spans(task, "filler_generate")
        if expect_push:
            assert pushes == ["One moment please."]
            assert fg["metadata"]["was_sent"] is True
        else:
            assert pushes == []
            assert fg["metadata"]["was_sent"] is False
            assert fg["metadata"]["cancel_reason"] == "target_not_eligible"
        assert orch._invoke_filler_agent.await_count == 1


# ---------------------------------------------------------------------------
# Non-streaming path never starts a speculative filler
# ---------------------------------------------------------------------------


@patch("app.agents.orchestrator.SettingsRepository")
class TestSpeculativeFillerNonStreaming:
    async def test_prelude_without_slot_starts_no_filler(self, mock_settings):
        mock_settings.get_value = _settings()
        orch, _ = _make_orchestrator()
        orch._invoke_filler_agent = AsyncMock(return_value="One moment please.")
        orch._should_start_speculative_filler = AsyncMock(return_value=True)
        orch._pipeline_director.run_classification = AsyncMock(side_effect=_classifier(_SINGLE_HIGH, delay=0.02))

        prelude = await orch._run_pipeline_prelude(_make_task())

        assert prelude.target_agent == "general-agent"
        orch._should_start_speculative_filler.assert_not_awaited()
        orch._invoke_filler_agent.assert_not_called()

    async def test_handle_task_passes_no_filler_slot(self, mock_settings):
        mock_settings.get_value = _settings()
        orch, _ = _make_orchestrator()
        seen_kwargs: list[dict] = []
        orch._run_pipeline_prelude = _prelude_spy(seen_kwargs)

        result = await orch.handle_task(_make_task())

        assert result["speech"] == "Done."
        assert len(seen_kwargs) == 1
        assert "filler_slot" not in seen_kwargs[0]

    async def test_stream_passes_filler_slot(self, mock_settings):
        """Counterpart of the non-streaming check: the spy does see the kwarg."""
        mock_settings.get_value = _settings()
        orch, _ = _make_orchestrator()
        seen_kwargs: list[dict] = []
        orch._run_pipeline_prelude = _prelude_spy(seen_kwargs)

        chunks = [c async for c in orch.handle_task_stream(_make_task())]

        assert chunks[-1]["mediated_speech"] == "Done."
        assert len(seen_kwargs) == 1
        assert isinstance(seen_kwargs[0]["filler_slot"], _FillerSlot)


def _prelude_spy(seen_kwargs: list[dict]):
    """Prelude replacement that records its kwargs and exits as a cache replay."""

    async def _spy(task, **kwargs):
        seen_kwargs.append(kwargs)
        return PipelinePreludeResult(
            conversation_id=task.conversation_id,
            detected_language="en",
            lang_turns=[],
            span_collector=task.span_collector,
            classifications=[],
            routing_cached=False,
            target_agent="",
            condensed_task="",
            confidence=None,
            used_origin_context=False,
            early_exit={"_exit_type": "cache_replay", "speech": "Done.", "routed_to": "light-agent"},
        )

    return _spy


# ---------------------------------------------------------------------------
# _cancel_filler_future: caller cancellation is not swallowed
# ---------------------------------------------------------------------------


class TestCancelFillerFuture:
    async def test_outer_cancellation_during_await_is_reraised(self):
        filler_cancelled = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def _slow_unwinding_filler():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                filler_cancelled.set()
                # Slow cleanup keeps the caller inside its await.
                await asyncio.shield(release_cleanup.wait())
                raise

        filler = asyncio.create_task(_slow_unwinding_filler())
        await asyncio.sleep(0)
        continued_after_cancel = False

        async def _caller():
            nonlocal continued_after_cancel
            await _cancel_filler_future(filler)
            continued_after_cancel = True

        caller = asyncio.create_task(_caller())
        await asyncio.wait_for(filler_cancelled.wait(), timeout=2)
        caller.cancel()
        await asyncio.sleep(0)
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert continued_after_cancel is False
        with contextlib.suppress(asyncio.CancelledError):
            await filler
        assert filler.cancelled()

    async def test_filler_own_cancellation_is_suppressed(self):
        async def _hanging():
            await asyncio.Event().wait()

        filler = asyncio.create_task(_hanging())
        await asyncio.sleep(0)
        await _cancel_filler_future(filler)
        assert filler.cancelled()
        # Idempotent on an already-cancelled task.
        await _cancel_filler_future(filler)


# ---------------------------------------------------------------------------
# Coordinator gate
# ---------------------------------------------------------------------------


class TestShouldStartSpeculativeFiller:
    @pytest.mark.parametrize(
        ("enabled", "speculative", "expected"),
        [
            ("true", "true", True),
            ("true", None, True),
            ("true", "false", False),
            ("false", "true", False),
            (None, "true", False),
        ],
    )
    async def test_gate(self, enabled, speculative, expected):
        values = {"filler.enabled": enabled, "filler.speculative_start": speculative}
        settings_repo = MagicMock()
        settings_repo.get_value = AsyncMock(
            side_effect=lambda k, d=None: values.get(k) if values.get(k) is not None else d
        )
        coord = FillerCoordinator(settings_repo, MagicMock(), MagicMock(), MagicMock())
        assert await coord.should_start_speculative_filler() is expected

    async def test_gate_read_failure_is_false(self):
        settings_repo = MagicMock()
        settings_repo.get_value = AsyncMock(side_effect=RuntimeError("db locked"))
        coord = FillerCoordinator(settings_repo, MagicMock(), MagicMock(), MagicMock())
        assert await coord.should_start_speculative_filler() is False


# ---------------------------------------------------------------------------
# FillerAgent: empty target agent
# ---------------------------------------------------------------------------


class TestFillerAgentTargetLine:
    @patch("app.agents.filler.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock, return_value="Let me check that for you.")
    async def test_empty_target_omits_agent_line(self, mock_complete, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="")
        task = DispatchTask(description="generate_filler:\nwhat is the weather", context=TaskContext(language="en"))
        result = await FillerAgent().handle_task(task)

        assert result.speech == "Let me check that for you."
        user_content = mock_complete.call_args[0][1][1]["content"]
        assert "what is the weather" in user_content
        assert "Agent:" not in user_content

    @patch("app.agents.filler.SettingsRepository")
    @patch("app.llm.client.complete", new_callable=AsyncMock, return_value="One moment.")
    async def test_known_target_keeps_agent_line(self, mock_complete, mock_settings):
        mock_settings.get_value = AsyncMock(return_value="")
        task = DispatchTask(
            description="generate_filler:general-agent\nwhat is the weather", context=TaskContext(language="en")
        )
        await FillerAgent().handle_task(task)

        user_content = mock_complete.call_args[0][1][1]["content"]
        assert user_content.endswith("\n\nAgent: general-agent")
