"""Tests for P2-2 (FLOW-TIMEOUT-1): per-agent dispatch timeout resolution.

The orchestrator no longer applies a single 5s timeout to every
sub-agent dispatch. Instead each agent_id resolves a timeout via:

    1. Settings key ``agent.dispatch_timeout.<agent_id>`` (operator override).
    2. ``AgentCard.timeout_sec`` declared by the agent module.
    3. ``self._default_timeout`` (orchestrator-wide fallback).

The resolved value is capped at ``a2a.max_dispatch_timeout``
(default 60s) and cached per agent_id for the lifetime of the
orchestrator instance so SettingsRepository is not hit on every
dispatch.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mock litellm before importing any app modules.
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
from app.models.agent import AgentCard  # noqa: E402


def _make_orch_with_registry(
    cards: list[AgentCard],
) -> OrchestratorAgent:
    dispatcher = AsyncMock()
    registry = AsyncMock()
    registry.list_agents = AsyncMock(return_value=cards)
    return OrchestratorAgent(dispatcher=dispatcher, registry=registry)


@pytest.mark.asyncio
async def test_resolve_timeout_uses_agent_card_when_set(monkeypatch):
    """AgentCard.timeout_sec wins when no settings override is present."""
    orch = _make_orch_with_registry(
        [
            AgentCard(
                agent_id="general-agent",
                name="General",
                description="",
                skills=[],
                timeout_sec=30.0,
            ),
        ]
    )

    async def _no_setting(key, default=""):
        return ""

    monkeypatch.setattr(
        "app.agents.orchestrator.SettingsRepository.get_value",
        AsyncMock(side_effect=_no_setting),
    )

    resolved = await orch._resolve_dispatch_timeout("general-agent")
    assert resolved == 30.0


@pytest.mark.asyncio
async def test_resolve_timeout_settings_override_wins(monkeypatch):
    """``agent.dispatch_timeout.<agent_id>`` overrides AgentCard."""
    orch = _make_orch_with_registry(
        [
            AgentCard(
                agent_id="general-agent",
                name="General",
                description="",
                skills=[],
                timeout_sec=30.0,
            ),
        ]
    )

    async def _setting(key, default=""):
        if key == "agent.dispatch_timeout.general-agent":
            return "45"
        return ""

    monkeypatch.setattr(
        "app.agents.orchestrator.SettingsRepository.get_value",
        AsyncMock(side_effect=_setting),
    )

    resolved = await orch._resolve_dispatch_timeout("general-agent")
    assert resolved == 45.0


@pytest.mark.asyncio
async def test_resolve_timeout_falls_back_to_default(monkeypatch):
    """Agents without a card override or setting use the orchestrator default."""
    orch = _make_orch_with_registry(
        [
            AgentCard(
                agent_id="light-agent",
                name="Light",
                description="",
                skills=[],
            ),
        ]
    )
    orch._default_timeout = 7

    async def _no_setting(key, default=""):
        return ""

    monkeypatch.setattr(
        "app.agents.orchestrator.SettingsRepository.get_value",
        AsyncMock(side_effect=_no_setting),
    )

    resolved = await orch._resolve_dispatch_timeout("light-agent")
    assert resolved == 7.0


@pytest.mark.asyncio
async def test_resolve_timeout_caps_at_max(monkeypatch):
    """Misconfigured huge values are clamped to ``_max_dispatch_timeout``."""
    orch = _make_orch_with_registry(
        [
            AgentCard(
                agent_id="general-agent",
                name="General",
                description="",
                skills=[],
                timeout_sec=600.0,
            ),
        ]
    )
    orch._max_dispatch_timeout = 60.0

    async def _no_setting(key, default=""):
        return ""

    monkeypatch.setattr(
        "app.agents.orchestrator.SettingsRepository.get_value",
        AsyncMock(side_effect=_no_setting),
    )

    resolved = await orch._resolve_dispatch_timeout("general-agent")
    assert resolved == 60.0


@pytest.mark.asyncio
async def test_resolve_timeout_caches_per_agent(monkeypatch):
    """Lookup must hit SettingsRepository at most once per agent_id."""
    orch = _make_orch_with_registry(
        [
            AgentCard(
                agent_id="light-agent",
                name="Light",
                description="",
                skills=[],
                timeout_sec=8.0,
            ),
        ]
    )

    setting_calls: list[str] = []

    async def _track(key, default=""):
        setting_calls.append(key)
        return ""

    monkeypatch.setattr(
        "app.agents.orchestrator.SettingsRepository.get_value",
        AsyncMock(side_effect=_track),
    )

    a = await orch._resolve_dispatch_timeout("light-agent")
    b = await orch._resolve_dispatch_timeout("light-agent")
    assert a == b == 8.0
    # Only the first lookup hit the settings store.
    assert setting_calls.count("agent.dispatch_timeout.light-agent") == 1


@pytest.mark.asyncio
async def test_known_long_running_agents_have_card_override():
    """Smoke test: general-agent and custom plugin loader declare a
    timeout_sec > the legacy 5s default so MCP/web-search calls do not
    trip the dispatch timeout."""
    from app.agents.custom_loader import DynamicAgent
    from app.agents.general import GeneralAgent

    general = GeneralAgent()
    assert general.agent_card.timeout_sec is not None
    assert general.agent_card.timeout_sec >= 20.0

    custom = DynamicAgent(name="demo", description="d", system_prompt="p", skills=[])
    assert custom.agent_card.timeout_sec is not None
    assert custom.agent_card.timeout_sec >= 20.0
