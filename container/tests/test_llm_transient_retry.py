"""LLM client: bounded retry of transient provider errors (rate limit, overload, 5xx).

Other test modules may replace ``litellm`` in ``sys.modules`` with a
MagicMock, so the client tests install their own stand-in exception
hierarchy (mirroring litellm's class names and ``status_code`` attributes)
on ``app.llm.client.litellm``. A separate class checks the real litellm
classes and skips when litellm is mocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_CONFIG = {
    "agent_id": "light-agent",
    "model": "cerebras/test-model",
    "timeout": 5,
    "max_tokens": 256,
    "temperature": 0.2,
}


class _ProviderTimeoutError(Exception):
    """Stand-in for litellm.exceptions.Timeout."""


class _APIError(Exception):
    """Stand-in for litellm.exceptions.APIError."""

    default_status: int | None = None

    def __init__(self, message: str = "", status_code: int | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status_code = status_code if status_code is not None else self.default_status
        self.headers = headers


class _RateLimitError(_APIError):
    default_status = 429


class _ServiceUnavailableError(_APIError):
    default_status = 503


class _AuthenticationError(_APIError):
    default_status = 401


class _BadRequestError(_APIError):
    default_status = 400


class _ContextWindowExceededError(_BadRequestError):
    pass


class _NotFoundError(_APIError):
    default_status = 404


class _APIConnectionError(_APIError):
    """Stand-in for litellm's catch-all APIConnectionError (status hardcoded to 500)."""

    default_status = 500


litellm_exc = SimpleNamespace(
    APIError=_APIError,
    RateLimitError=_RateLimitError,
    ServiceUnavailableError=_ServiceUnavailableError,
    AuthenticationError=_AuthenticationError,
    BadRequestError=_BadRequestError,
    ContextWindowExceededError=_ContextWindowExceededError,
    NotFoundError=_NotFoundError,
    APIConnectionError=_APIConnectionError,
    Timeout=_ProviderTimeoutError,
)


def _rate_limit(headers: dict[str, str] | None = None) -> Exception:
    return _RateLimitError("provider overloaded", headers=headers)


def _auth_error() -> Exception:
    return _AuthenticationError("bad key")


def _text_response(text: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = text
    response.choices[0].message.tool_calls = None
    response.choices[0].finish_reason = "stop"
    response.usage = None
    return response


class _Delta:
    def __init__(self, content: str | None):
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content: str | None, finish_reason: str | None = None):
        self.delta = _Delta(content)
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, content: str | None, finish_reason: str | None = None):
        self.choices = [_Choice(content, finish_reason)]
        self.usage = None


class _FakeStream:
    """Async-iterable provider stream that yields ``items`` and optionally raises at the end."""

    def __init__(self, items: list, error: Exception | None = None):
        self._items = list(items)
        self._error = error
        self.usage = None
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


class _SpanCollector:
    """Minimal span collector that records every span's metadata."""

    def __init__(self):
        self.spans: list[dict] = []

    @contextlib.asynccontextmanager
    async def start_span(self, name, **kwargs):
        span = {"name": name, "metadata": {}}
        self.spans.append(span)
        yield span


@pytest.fixture
def fake_litellm():
    fake = SimpleNamespace(acompletion=AsyncMock(), exceptions=litellm_exc)
    with (
        patch("app.llm.client.litellm", fake),
        patch("app.llm.client.LiteLLMTimeout", _ProviderTimeoutError),
    ):
        yield fake


@pytest.fixture
def llm_env(fake_litellm):
    with (
        patch("app.llm.client.resolve_provider_params", new_callable=AsyncMock, return_value={}),
        patch("app.llm.client.AgentConfigRepository") as repo,
        patch("app.llm.client.track_token_usage", new_callable=AsyncMock),
        patch("app.llm.client._LLM_TRANSIENT_RETRY_BASE_DELAY_SEC", 0.0),
        patch("app.llm.client._LLM_TRANSIENT_RETRY_JITTER_SEC", 0.0),
    ):
        repo.get = AsyncMock(return_value=dict(_CONFIG))
        yield fake_litellm.acompletion


class TestTransientClassification:
    def test_rate_limit_is_transient(self, fake_litellm):
        from app.llm.client import _is_transient_provider_error

        assert _is_transient_provider_error(_rate_limit())

    def test_service_unavailable_is_transient(self, fake_litellm):
        from app.llm.client import _is_transient_provider_error

        assert _is_transient_provider_error(_ServiceUnavailableError("overloaded"))

    def test_generic_api_error_with_503_status_is_transient(self, fake_litellm):
        from app.llm.client import _is_transient_provider_error

        assert _is_transient_provider_error(_APIError("busy", status_code=503))
        assert not _is_transient_provider_error(_APIError("bad", status_code=422))

    def test_unwrapped_httpx_protocol_error_is_transient(self, fake_litellm):
        from app.llm.client import _is_transient_provider_error

        assert _is_transient_provider_error(httpx.RemoteProtocolError("peer closed connection"))
        assert _is_transient_provider_error(httpx.ConnectError("connection refused"))

    def test_permanent_errors_are_not_transient(self, fake_litellm):
        from app.llm.client import _is_transient_provider_error

        assert not _is_transient_provider_error(_auth_error())
        assert not _is_transient_provider_error(_ContextWindowExceededError("too long"))
        assert not _is_transient_provider_error(_BadRequestError("bad"))
        assert not _is_transient_provider_error(_NotFoundError("no model"))

    def test_permanent_class_wins_over_transient_status(self, fake_litellm):
        from app.llm.client import _is_transient_provider_error

        assert not _is_transient_provider_error(_AuthenticationError("odd", status_code=503))

    def test_timeouts_are_not_transient(self, fake_litellm):
        from app.llm.client import _is_transient_provider_error

        assert not _is_transient_provider_error(_ProviderTimeoutError("timed out"))
        assert not _is_transient_provider_error(httpx.ReadTimeout("slow"))

    def test_message_text_alone_does_not_make_an_error_transient(self, fake_litellm):
        from app.llm.client import _is_transient_provider_error

        assert not _is_transient_provider_error(RuntimeError("We're experiencing high traffic right now!"))

    def test_short_retry_after_is_honored(self, fake_litellm):
        from app.llm.client import _transient_retry_delay

        with (
            patch("app.llm.client._LLM_TRANSIENT_RETRY_BASE_DELAY_SEC", 0.3),
            patch("app.llm.client._LLM_TRANSIENT_RETRY_JITTER_SEC", 0.0),
        ):
            assert _transient_retry_delay(_rate_limit({"retry-after": "1"}), 0, None) == pytest.approx(1.0)
            assert _transient_retry_delay(_rate_limit(), 0, None) == pytest.approx(0.3)
            assert _transient_retry_delay(_rate_limit(), 1, None) is None

    def test_retry_after_above_cap_means_no_retry(self, fake_litellm):
        from app.llm.client import _transient_retry_delay

        assert _transient_retry_delay(_rate_limit({"retry-after": "60"}), 0, None) is None

    def test_slow_failure_without_deadline_is_not_retried(self, fake_litellm):
        from app.llm.client import _LLM_TRANSIENT_FAST_FAIL_SEC, _transient_retry_delay

        assert _transient_retry_delay(_rate_limit(), 0, None, attempt_elapsed=_LLM_TRANSIENT_FAST_FAIL_SEC + 3) is None
        assert _transient_retry_delay(_rate_limit(), 0, None, attempt_elapsed=0.1) is not None

    def test_slow_failure_with_deadline_budget_is_retried(self, fake_litellm):
        from app.llm.client import _transient_retry_delay

        deadline = time.monotonic() + 30
        assert _transient_retry_delay(_rate_limit(), 0, deadline, attempt_elapsed=4.5) is not None

    def test_connection_catch_all_without_network_cause_is_not_transient(self, fake_litellm):
        from app.llm.client import _is_transient_provider_error

        try:
            try:
                raise KeyError("unmapped provider bug")
            except KeyError:
                raise _APIConnectionError("wrapped") from None
        except _APIConnectionError as exc:
            wrapped = exc
        assert not _is_transient_provider_error(_APIConnectionError("no cause"))
        assert not _is_transient_provider_error(wrapped)

    def test_connection_catch_all_with_network_cause_is_transient(self, fake_litellm):
        from app.llm.client import _is_transient_provider_error

        def _wrap(cause: BaseException) -> BaseException:
            try:
                try:
                    raise cause
                except Exception:
                    raise _APIConnectionError("wrapped")  # noqa: B904 - litellm relies on implicit __context__ chaining
            except _APIConnectionError as exc:
                return exc

        assert _is_transient_provider_error(_wrap(httpx.RemoteProtocolError("peer closed")))
        assert _is_transient_provider_error(_wrap(httpx.ConnectError("refused")))
        assert not _is_transient_provider_error(_wrap(httpx.ReadTimeout("slow")))


class TestRealLitellmClassification:
    """Classification against the real litellm exception classes."""

    @pytest.fixture
    def real_exc(self):
        import app.llm.client as client

        exceptions = getattr(client.litellm, "exceptions", None)
        if not isinstance(getattr(exceptions, "RateLimitError", None), type):
            pytest.skip("litellm is mocked by another test module in this session")
        return exceptions

    def test_real_transient_classes(self, real_exc):
        from app.llm.client import _is_transient_provider_error

        kwargs = {"message": "x", "llm_provider": "cerebras", "model": "m"}
        assert _is_transient_provider_error(real_exc.RateLimitError(**kwargs))
        assert _is_transient_provider_error(real_exc.ServiceUnavailableError(**kwargs))
        assert _is_transient_provider_error(real_exc.InternalServerError(**kwargs))
        assert _is_transient_provider_error(real_exc.APIError(status_code=503, **kwargs))

    def test_real_connection_catch_all_needs_network_cause(self, real_exc):
        from app.llm.client import _is_transient_provider_error

        kwargs = {"message": "x", "llm_provider": "cerebras", "model": "m"}
        assert not _is_transient_provider_error(real_exc.APIConnectionError(**kwargs))
        with_cause = real_exc.APIConnectionError(**kwargs)
        with_cause.__cause__ = httpx.ReadError("connection reset")
        assert _is_transient_provider_error(with_cause)

    def test_real_permanent_classes_and_timeout(self, real_exc):
        from app.llm.client import _is_transient_provider_error

        kwargs = {"message": "x", "llm_provider": "cerebras", "model": "m"}
        assert not _is_transient_provider_error(real_exc.AuthenticationError(**kwargs))
        assert not _is_transient_provider_error(real_exc.BadRequestError(**kwargs))
        assert not _is_transient_provider_error(real_exc.ContextWindowExceededError(**kwargs))
        assert not _is_transient_provider_error(real_exc.ContentPolicyViolationError(**kwargs))
        assert not _is_transient_provider_error(real_exc.NotFoundError(**kwargs))
        assert not _is_transient_provider_error(real_exc.Timeout(**kwargs))


class TestCompleteTransientRetry:
    async def test_rate_limit_then_success_retries_once(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_rate_limit(), _text_response("ok")]
        collector = _SpanCollector()

        from app.llm.client import complete

        result = await complete("light-agent", [{"role": "user", "content": "q"}], span_collector=collector)

        assert result == "ok"
        assert acompletion.await_count == 2
        provider_spans = [s for s in collector.spans if s["name"] == "llm_provider_call"]
        assert provider_spans[0]["metadata"]["retry_count"] == 1
        assert provider_spans[0]["metadata"]["retried_error"] == "_RateLimitError(429)"

    async def test_auth_error_is_not_retried(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_auth_error(), _text_response("ok")]

        from app.llm.client import complete

        with pytest.raises(litellm_exc.AuthenticationError):
            await complete("light-agent", [{"role": "user", "content": "q"}])
        assert acompletion.await_count == 1

    async def test_transient_error_without_deadline_budget_is_not_retried(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_rate_limit(), _text_response("ok")]

        from app.llm.client import complete

        with pytest.raises(litellm_exc.RateLimitError):
            await complete(
                "light-agent",
                [{"role": "user", "content": "q"}],
                deadline=time.monotonic() + 0.5,
            )
        assert acompletion.await_count == 1

    async def test_retry_budget_is_bounded(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_rate_limit(), _rate_limit(), _text_response("ok")]

        from app.llm.client import complete

        with pytest.raises(litellm_exc.RateLimitError):
            await complete("light-agent", [{"role": "user", "content": "q"}])
        assert acompletion.await_count == 2

    async def test_domain_agent_without_timeout_retry_still_retries_rate_limit(self, llm_env):
        """retry_on_timeout=False (domain agents) must not disable the transient-error retry."""
        acompletion = llm_env
        acompletion.side_effect = [_rate_limit(), _text_response("turned on")]

        from app.llm.client import complete

        result = await complete("light-agent", [{"role": "user", "content": "q"}], retry_on_timeout=False)

        assert result == "turned on"
        assert acompletion.await_count == 2

    async def test_domain_agent_timeout_is_still_not_retried(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_ProviderTimeoutError("slow"), _text_response("ok")]

        from app.llm.client import complete

        with pytest.raises(_ProviderTimeoutError):
            await complete("light-agent", [{"role": "user", "content": "q"}], retry_on_timeout=False)
        assert acompletion.await_count == 1

    async def test_tool_loop_round_retries_transient_error(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_rate_limit(), _text_response("answer")]

        from app.llm.client import complete_with_tools

        result = await complete_with_tools(
            "light-agent",
            [{"role": "user", "content": "q"}],
            tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
            tool_executor=AsyncMock(return_value="r"),
        )
        assert result == "answer"
        assert acompletion.await_count == 2


class TestStreamTransientRetry:
    async def test_error_before_first_chunk_is_retried(self, llm_env):
        acompletion = llm_env
        failed = _FakeStream([], error=_rate_limit())
        acompletion.side_effect = [failed, _FakeStream([_Chunk("Hel"), _Chunk("lo", "stop")])]
        collector = _SpanCollector()

        from app.llm.client import complete_stream

        tokens = [
            t
            async for t in complete_stream("light-agent", [{"role": "user", "content": "q"}], span_collector=collector)
        ]

        assert tokens == ["Hel", "lo"]
        assert acompletion.await_count == 2
        assert failed.closed
        assert collector.spans[0]["metadata"]["retry_count"] == 1

    async def test_error_raised_by_stream_call_is_retried(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_rate_limit(), _FakeStream([_Chunk("ok", "stop")])]

        from app.llm.client import complete_stream

        tokens = [t async for t in complete_stream("light-agent", [{"role": "user", "content": "q"}])]

        assert tokens == ["ok"]
        assert acompletion.await_count == 2

    async def test_error_after_first_chunk_is_not_retried(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [
            _FakeStream([_Chunk("Hel")], error=_rate_limit()),
            _FakeStream([_Chunk("never", "stop")]),
        ]

        from app.llm.client import complete_stream

        tokens: list[str] = []
        with pytest.raises(litellm_exc.RateLimitError):
            async for token in complete_stream("light-agent", [{"role": "user", "content": "q"}]):
                tokens.append(token)

        assert tokens == ["Hel"]
        assert acompletion.await_count == 1

    async def test_auth_error_in_stream_is_not_retried(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_auth_error(), _FakeStream([_Chunk("ok", "stop")])]

        from app.llm.client import complete_stream

        with pytest.raises(litellm_exc.AuthenticationError):
            async for _token in complete_stream("light-agent", [{"role": "user", "content": "q"}]):
                pass
        assert acompletion.await_count == 1

    async def test_tool_stream_round_retries_before_first_chunk(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [
            _FakeStream([], error=_rate_limit()),
            _FakeStream([_Chunk("final", "stop")]),
        ]

        from app.llm.client import complete_with_tools_stream

        tokens = [
            t
            async for t in complete_with_tools_stream(
                "light-agent",
                [{"role": "user", "content": "q"}],
                tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
                tool_executor=AsyncMock(return_value="r"),
            )
        ]
        assert tokens == ["final"]
        assert acompletion.await_count == 2

    async def test_tool_stream_round_not_retried_after_first_chunk(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [
            _FakeStream([_Chunk("partial")], error=_rate_limit()),
            _FakeStream([_Chunk("never", "stop")]),
        ]

        from app.llm.client import complete_with_tools_stream

        tokens: list[str] = []
        with pytest.raises(litellm_exc.RateLimitError):
            async for token in complete_with_tools_stream(
                "light-agent",
                [{"role": "user", "content": "q"}],
                tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
                tool_executor=AsyncMock(return_value="r"),
            ):
                tokens.append(token)
        assert tokens == ["partial"]
        assert acompletion.await_count == 1


def _usage() -> SimpleNamespace:
    return SimpleNamespace(prompt_tokens=3, completion_tokens=5)


class TestRetryBounds:
    async def test_slow_failure_without_deadline_is_not_retried(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_rate_limit(), _text_response("ok")]

        from app.llm.client import complete

        with patch("app.llm.client._LLM_TRANSIENT_FAST_FAIL_SEC", -1.0), pytest.raises(_RateLimitError):
            await complete("light-agent", [{"role": "user", "content": "q"}], retry_on_timeout=False)
        assert acompletion.await_count == 1

    async def test_stream_slow_failure_without_deadline_is_not_retried(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_FakeStream([], error=_rate_limit()), _FakeStream([_Chunk("ok", "stop")])]

        from app.llm.client import complete_stream

        with patch("app.llm.client._LLM_TRANSIENT_FAST_FAIL_SEC", -1.0), pytest.raises(_RateLimitError):
            async for _token in complete_stream("light-agent", [{"role": "user", "content": "q"}]):
                pass
        assert acompletion.await_count == 1

    async def test_retry_after_above_cap_is_not_retried(self, llm_env):
        acompletion = llm_env
        acompletion.side_effect = [_rate_limit({"retry-after": "30"}), _text_response("ok")]

        from app.llm.client import complete

        with pytest.raises(_RateLimitError):
            await complete("light-agent", [{"role": "user", "content": "q"}])
        assert acompletion.await_count == 1


class TestRetryMetrics:
    """Latency/TTFT metrics describe the successful attempt, not the failed one plus backoff."""

    async def test_nonstream_latency_excludes_failed_attempt(self, llm_env):
        acompletion = llm_env
        response = _text_response("ok")
        response.usage = _usage()
        calls = {"n": 0}

        async def _provider(**_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(0.3)
                raise _rate_limit()
            return response

        acompletion.side_effect = _provider
        collector = _SpanCollector()

        from app.llm.client import complete

        with patch("app.llm.client.track_token_usage", new_callable=AsyncMock) as track:
            result = await complete("light-agent", [{"role": "user", "content": "q"}], span_collector=collector)

        assert result == "ok"
        assert calls["n"] == 2
        assert track.await_args.kwargs["latency_ms"] < 250
        assert collector.spans[0]["metadata"]["latency_ms"] < 250
        assert collector.spans[0]["metadata"]["retry_count"] == 1

    async def test_stream_latency_excludes_failed_attempt(self, llm_env):
        acompletion = llm_env
        good = _FakeStream([_Chunk("Hel"), _Chunk("lo", "stop")])
        good.usage = _usage()
        calls = {"n": 0}

        async def _provider(**_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(0.3)
                raise _rate_limit()
            return good

        acompletion.side_effect = _provider
        collector = _SpanCollector()

        from app.llm.client import complete_stream

        with patch("app.llm.client.track_token_usage", new_callable=AsyncMock) as track:
            tokens = [
                t
                async for t in complete_stream(
                    "light-agent", [{"role": "user", "content": "q"}], span_collector=collector
                )
            ]

        assert tokens == ["Hel", "lo"]
        assert track.await_args.kwargs["latency_ms"] < 250
        assert track.await_args.kwargs["ttft_ms"] < 250
        assert collector.spans[0]["metadata"]["latency_ms"] < 250


class TestActionableDeadline:
    """Domain agents bound the LLM call (and its transient retry) by the dispatch budget."""

    @staticmethod
    def _agent():
        from app.agents.actionable import LightAgent

        agent = LightAgent()
        agent._ha_client = AsyncMock()
        agent._entity_index = None
        agent._entity_matcher = None
        return agent

    async def test_dispatch_budget_becomes_llm_deadline(self):
        from tests.helpers import make_dispatch_task

        agent = self._agent()
        task = make_dispatch_task(description="turn on the kitchen light")
        before = time.monotonic()
        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(agent, "_resolve_dispatch_budget_sec", new_callable=AsyncMock, return_value=10.0),
            patch.object(agent, "_call_llm", new_callable=AsyncMock, side_effect=RuntimeError("LLM down")) as llm,
            patch.object(agent, "_do_execute", new_callable=AsyncMock),
        ):
            await agent.handle_task(task)

        kwargs = llm.await_args.kwargs
        assert kwargs["retry_on_timeout"] is False
        assert before < kwargs["deadline"] <= time.monotonic() + 10.0

    async def test_no_dispatch_budget_keeps_call_unbounded(self):
        from tests.helpers import make_dispatch_task

        agent = self._agent()
        task = make_dispatch_task(description="turn on the kitchen light")
        with (
            patch.object(agent, "_load_prompt_async", new_callable=AsyncMock, return_value="You are a light agent."),
            patch.object(agent, "_resolve_dispatch_budget_sec", new_callable=AsyncMock, return_value=None),
            patch.object(agent, "_call_llm", new_callable=AsyncMock, side_effect=RuntimeError("LLM down")) as llm,
            patch.object(agent, "_do_execute", new_callable=AsyncMock),
        ):
            await agent.handle_task(task)

        kwargs = llm.await_args.kwargs
        assert kwargs["retry_on_timeout"] is False
        assert "deadline" not in kwargs
