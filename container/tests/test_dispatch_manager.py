"""Tests for app.agents.dispatch_manager."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

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

import app.llm.client  # noqa: E402,F401
from app.agents.dispatch_manager import (  # noqa: E402
    _CANNED_GENERAL_ERROR_SPEECH,
    _CANNED_TIMEOUT_SPEECH,
    DispatchManager,
)


class TestDispatchManagerFallback:
    def _make_dispatch_manager(self, dispatch_side_effect=None):
        dispatcher = AsyncMock()
        if dispatch_side_effect is not None:
            dispatcher.dispatch = AsyncMock(side_effect=dispatch_side_effect)
        agent_registry = AsyncMock()
        agent_registry.resolve_dispatch_timeout = AsyncMock(return_value=5.0)
        dm = DispatchManager(
            dispatcher=dispatcher,
            agent_registry=agent_registry,
        )
        return dm, dispatcher, agent_registry

    @patch("app.agents.dispatch_manager.track_request", new_callable=AsyncMock)
    @patch("app.agents.dispatch_manager.track_agent_timeout", new_callable=AsyncMock)
    async def test_runtime_error_primary_and_fallback_returns_error_speech(
        self, mock_track_timeout, mock_track_request
    ):
        """C2: RuntimeError primary + failing fallback must return general error speech."""
        dm, _dispatcher, _ = self._make_dispatch_manager(
            dispatch_side_effect=[
                RuntimeError("primary agent down"),
                RuntimeError("fallback also fails"),
            ]
        )
        agent_id, speech, _result = await dm.dispatch_single(
            target_agent="light-agent",
            condensed_task="turn on light",
            user_text="turn on light",
            conversation_id="conv-c2",
            turns=[],
            span_collector=[],
        )
        assert speech == _CANNED_GENERAL_ERROR_SPEECH
        assert _result is None
        assert agent_id == "light-agent"

    @patch("app.agents.dispatch_manager.track_request", new_callable=AsyncMock)
    @patch("app.agents.dispatch_manager.track_agent_timeout", new_callable=AsyncMock)
    async def test_timeout_error_primary_and_fallback_returns_timeout_speech(
        self, mock_track_timeout, mock_track_request
    ):
        """C2: TimeoutError primary + failing fallback must still return timeout speech."""
        dm, _dispatcher, _ = self._make_dispatch_manager(
            dispatch_side_effect=[
                TimeoutError("primary timed out"),
                TimeoutError("fallback also timed out"),
            ]
        )
        agent_id, speech, _result = await dm.dispatch_single(
            target_agent="light-agent",
            condensed_task="turn on light",
            user_text="turn on light",
            conversation_id="conv-c2-timeout",
            turns=[],
            span_collector=[],
        )
        assert speech == _CANNED_TIMEOUT_SPEECH
        assert _result is None
        assert agent_id == "light-agent"

    @patch("app.agents.dispatch_manager.track_request", new_callable=AsyncMock)
    @patch("app.agents.dispatch_manager.track_agent_timeout", new_callable=AsyncMock)
    async def test_runtime_error_fallback_agent_primary_returns_error_speech(
        self, mock_track_timeout, mock_track_request
    ):
        """C2: RuntimeError when primary IS fallback agent returns general error speech with error dict."""
        dm, _dispatcher, _ = self._make_dispatch_manager(dispatch_side_effect=RuntimeError("general-agent down"))
        agent_id, speech, result = await dm.dispatch_single(
            target_agent="general-agent",
            condensed_task="help",
            user_text="help",
            conversation_id="conv-c2-fallback-primary",
            turns=[],
            span_collector=[],
        )
        assert speech == _CANNED_GENERAL_ERROR_SPEECH
        assert result is not None
        assert result["speech"] == _CANNED_GENERAL_ERROR_SPEECH
        assert result["error"]["code"] == "general-agent down"
        assert result["error"]["recoverable"] is True
        assert agent_id == "general-agent"

    @patch("app.agents.dispatch_manager.track_request", new_callable=AsyncMock)
    async def test_runtime_error_primary_with_successful_fallback(self, mock_track_request):
        """C2: RuntimeError primary + successful fallback returns fallback response."""
        dm, _dispatcher, _ = self._make_dispatch_manager(
            dispatch_side_effect=[
                RuntimeError("primary agent down"),
                {"speech": "Fallback answered."},
            ]
        )
        agent_id, speech, _result = await dm.dispatch_single(
            target_agent="light-agent",
            condensed_task="turn on light",
            user_text="turn on light",
            conversation_id="conv-c2-fb-ok",
            turns=[],
            span_collector=[],
        )
        assert speech == "Fallback answered."
        assert agent_id == "general-agent"


# ---------------------------------------------------------------------------
# Phase 3 gaps: G15, G18
# ---------------------------------------------------------------------------


class TestDispatchManagerStandalone:
    def _make_dispatch_manager(self, dispatch_side_effect=None):
        dispatcher = AsyncMock()
        if dispatch_side_effect is not None:
            dispatcher.dispatch = AsyncMock(side_effect=dispatch_side_effect)
        agent_registry = AsyncMock()
        agent_registry.resolve_dispatch_timeout = AsyncMock(return_value=5.0)
        dm = DispatchManager(
            dispatcher=dispatcher,
            agent_registry=agent_registry,
        )
        return dm, dispatcher, agent_registry

    # G15: dispatch_fallback
    @patch("app.agents.dispatch_manager.track_request", new_callable=AsyncMock)
    async def test_dispatch_fallback_returns_general_error(self, mock_track_request):
        """G15: dispatch_fallback must return general error speech when fallback fails."""
        dm, dispatcher, _ = self._make_dispatch_manager()
        dispatcher.dispatch = AsyncMock(side_effect=RuntimeError("fallback failed"))

        from app.a2a.protocol import JsonRpcRequest

        request = JsonRpcRequest(method="message/send", params={"agent_id": "light-agent"}, id="req-1")
        result = await dm.dispatch_fallback(request, "light-agent", [], "timeout")
        assert result is None

    @patch("app.agents.dispatch_manager.track_request", new_callable=AsyncMock)
    async def test_dispatch_fallback_successful(self, mock_track_request):
        """G15: dispatch_fallback must return (agent_id, response) when fallback succeeds."""
        dm, dispatcher, _ = self._make_dispatch_manager()
        dispatcher.dispatch = AsyncMock(return_value={"speech": "Fallback ok."})

        from app.a2a.protocol import JsonRpcRequest

        request = JsonRpcRequest(method="message/send", params={"agent_id": "light-agent"}, id="req-1")
        result = await dm.dispatch_fallback(request, "light-agent", [], "timeout")
        assert result == ("general-agent", {"speech": "Fallback ok."})

    @patch("app.agents.dispatch_manager.track_request", new_callable=AsyncMock)
    async def test_resolve_dispatch_timeout_uses_settings_repo(self, mock_track_request):
        """G15: resolve_dispatch_timeout should delegate to agent_registry with settings_repo."""
        dm, _dispatcher, agent_registry = self._make_dispatch_manager()
        settings_repo = AsyncMock()
        settings_repo.get_value = AsyncMock(return_value="7")
        agent_registry.resolve_dispatch_timeout = AsyncMock(return_value=7.0)

        dm._settings_repo = settings_repo
        timeout = await dm.resolve_dispatch_timeout("light-agent")
        assert timeout == 7.0
        agent_registry.resolve_dispatch_timeout.assert_awaited_once_with(
            "light-agent",
            default_timeout=5,
            settings_repo=settings_repo,
        )

    # G18: End-to-end timeout test
    @patch("app.agents.dispatch_manager.track_request", new_callable=AsyncMock)
    @patch("app.agents.dispatch_manager.track_agent_timeout", new_callable=AsyncMock)
    async def test_dispatch_single_timeout_with_successful_fallback(self, mock_track_timeout, mock_track_request):
        """G18: Primary timeout with successful fallback must return fallback speech."""
        dm, _dispatcher, _ = self._make_dispatch_manager(
            dispatch_side_effect=[
                TimeoutError("primary timed out"),
                {"speech": "Fallback answered after timeout."},
            ]
        )
        agent_id, speech, _result = await dm.dispatch_single(
            target_agent="light-agent",
            condensed_task="turn on light",
            user_text="turn on light",
            conversation_id="conv-timeout-fallback",
            turns=[],
            span_collector=[],
        )
        assert speech == "Fallback answered after timeout."
        assert agent_id == "general-agent"
