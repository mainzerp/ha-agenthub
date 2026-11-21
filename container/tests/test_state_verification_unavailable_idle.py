"""State verification for idle/unavailable targets (issue #239).

- A pause/stop on a player that is not playing makes no service call and waits for nothing.
- A pause issued while ``playing`` accepts ``idle`` (Alexa players) and resolves early.
- A light command on an ``unavailable`` entity makes no service call and waits for nothing.
"""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock

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

from app.agents import action_executor as _ae  # noqa: E402
from app.agents.action_executor import (  # noqa: E402
    VERIFY_REACHED,
    VERIFY_UNVERIFIED,
    call_service_with_verification,
    classify_verification_outcome,
)
from app.agents.light_executor import execute_light_action  # noqa: E402
from app.agents.media_executor import execute_media_action  # noqa: E402
from app.agents.music_executor import execute_music_action  # noqa: E402
from app.ha_client.rest import HARestClient  # noqa: E402
from app.ha_client.websocket import HAWebSocketClient  # noqa: E402

PLAYER = "media_player.echo_show_5_schlafzimmer"
PLAYER_NAME = "Echo Show 5 Schlafzimmer"
# Large on purpose: a test that waits the full window fails the elapsed check.
_LONG_WS_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )


def _matcher(entity_id: str, friendly_name: str):
    match = MagicMock()
    match.entity_id = entity_id
    match.friendly_name = friendly_name
    match.score = 0.95
    match.signal_scores = {}
    matcher = MagicMock()
    matcher.match = AsyncMock(return_value=[match])
    return matcher


def _real_client(
    monkeypatch,
    *,
    pre_state: str,
    emitted: list[str],
    poll_state: str | None = None,
    ws_timeout: float = _LONG_WS_TIMEOUT,
    poll_max: float = 0.05,
):
    """Real ``HARestClient.expect_state`` wired to a real WS waiter registry.

    ``call_service`` dispatches one ``state_changed`` event per ``emitted`` state.
    ``get_state`` returns ``pre_state`` before the call and ``poll_state`` after it.
    """

    async def _settings(key, *, default):
        return {
            "state_verify.ws_timeout_sec": ws_timeout,
            "state_verify.poll_interval_sec": 0.01,
            "state_verify.poll_max_sec": poll_max,
        }.get(key, default)

    monkeypatch.setattr(_ae, "_settings_float", _settings)

    ws = HAWebSocketClient()
    ws.is_connected = lambda: True  # type: ignore[method-assign]
    client = HARestClient()
    client.set_state_observer(ws)
    called = {"done": False}

    async def _call_service(domain, service, entity_id, service_data=None, **_kw):
        called["done"] = True
        for state in emitted:
            ws._dispatch_state_waiters(
                {"data": {"entity_id": entity_id, "new_state": {"entity_id": entity_id, "state": state}}}
            )
        return []

    async def _get_state(entity_id):
        state = pre_state if not called["done"] else (poll_state or pre_state)
        return {"entity_id": entity_id, "state": state, "attributes": {}}

    client.call_service = AsyncMock(side_effect=_call_service)  # type: ignore[method-assign]
    client.get_state = AsyncMock(side_effect=_get_state)  # type: ignore[method-assign]
    return client


# ---------------------------------------------------------------------------
# Pause / stop on a player that is not playing
# ---------------------------------------------------------------------------


def _idle_client(state: str, *, call_error: Exception | None = None):
    """Client whose player reports ``state``; any ``expect_state`` use fails the test."""
    client = MagicMock()
    client.get_state = AsyncMock(return_value={"state": state, "attributes": {}})
    client.call_service = AsyncMock(side_effect=call_error, return_value=[])
    client.expect_state = MagicMock(side_effect=AssertionError("must not wait for a state"))
    return client


_EXECUTOR_ACTIONS = [
    pytest.param(execute_music_action, "media_pause", "media_pause", id="music-pause"),
    pytest.param(execute_music_action, "media_stop", "media_stop", id="music-stop"),
    pytest.param(execute_media_action, "pause", "media_pause", id="media-pause"),
    pytest.param(execute_media_action, "stop", "media_stop", id="media-stop"),
]


class TestPauseOnIdlePlayer:
    """The pre-call state may be stale (Alexa polling): the command is sent once, unverified."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["idle", "off", "standby"])
    @pytest.mark.parametrize(("executor", "action_name", "service"), _EXECUTOR_ACTIONS)
    async def test_sends_command_once_without_waiting(self, executor, action_name, service, state):
        client = _idle_client(state)

        started = time.monotonic()
        result = await executor(
            {"action": action_name, "entity": "echo"}, client, MagicMock(), _matcher(PLAYER, PLAYER_NAME)
        )

        assert time.monotonic() - started < 1.0
        client.call_service.assert_awaited_once()
        assert client.call_service.await_args.args[:3] == ("media_player", service, PLAYER)
        client.expect_state.assert_not_called()
        assert result["success"] is True
        assert result["noop"] is True
        assert result["cacheable"] is False
        assert result["entity_id"] == PLAYER
        assert result["new_state"] == state
        assert result["speech"] == f"Nothing is playing on {PLAYER_NAME}."

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("executor", "action_name", "service"), _EXECUTOR_ACTIONS)
    async def test_call_failure_uses_failure_speech(self, executor, action_name, service):
        client = _idle_client("idle", call_error=RuntimeError("boom"))

        result = await executor(
            {"action": action_name, "entity": "echo"}, client, MagicMock(), _matcher(PLAYER, PLAYER_NAME)
        )

        client.call_service.assert_awaited_once()
        client.expect_state.assert_not_called()
        assert result["success"] is False
        assert "Nothing is playing" not in result["speech"]
        assert result["speech"].startswith("Sorry,")
        assert PLAYER_NAME in result["speech"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("executor", "action_name"),
        [(execute_music_action, "media_pause"), (execute_media_action, "pause")],
    )
    async def test_pause_on_paused_is_a_no_call_noop(self, executor, action_name):
        client = _idle_client("paused")

        result = await executor(
            {"action": action_name, "entity": "echo"}, client, MagicMock(), _matcher(PLAYER, PLAYER_NAME)
        )

        client.call_service.assert_not_called()
        client.expect_state.assert_not_called()
        assert result["success"] is True
        assert result["noop"] is True
        assert PLAYER_NAME in result["speech"]
        assert "already paused" in result["speech"]

    @pytest.mark.asyncio
    async def test_media_pause_on_group_still_runs(self):
        group_state = {"state": "idle", "attributes": {"entity_id": ["media_player.a", "media_player.b"]}}
        client = MagicMock()
        client.get_state = AsyncMock(return_value=group_state)
        client.call_service = AsyncMock(return_value=[])
        client.expect_state = MagicMock(return_value=None)  # not a context manager: plain call path

        await execute_media_action(
            {"action": "pause", "entity": "group"}, client, MagicMock(), _matcher("media_player.group", "Group")
        )

        client.call_service.assert_awaited_once()


# ---------------------------------------------------------------------------
# Pause from playing: idle is accepted and resolves early
# ---------------------------------------------------------------------------


class TestPauseFromPlaying:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("executor", "action"),
        [
            (execute_music_action, {"action": "media_pause", "entity": "echo"}),
            (execute_media_action, {"action": "pause", "entity": "echo"}),
        ],
    )
    async def test_observed_idle_is_reached_without_full_wait(self, monkeypatch, executor, action):
        client = _real_client(monkeypatch, pre_state="playing", emitted=["idle"])

        started = time.monotonic()
        result = await executor(action, client, MagicMock(), _matcher(PLAYER, PLAYER_NAME))
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, elapsed
        client.call_service.assert_awaited_once()
        assert result["success"] is True
        assert result["new_state"] == "idle"
        assert result["speech"] == f"Done, {PLAYER_NAME} is now idle."

    @pytest.mark.asyncio
    async def test_observed_paused_still_reached(self, monkeypatch):
        client = _real_client(monkeypatch, pre_state="playing", emitted=["paused"])

        result = await execute_music_action(
            {"action": "media_pause", "entity": "echo"}, client, MagicMock(), _matcher(PLAYER, PLAYER_NAME)
        )

        assert result["new_state"] == "paused"
        assert result["speech"] == f"Done, {PLAYER_NAME} is now paused."

    @pytest.mark.asyncio
    async def test_observed_playing_stays_unverified(self, monkeypatch):
        # Attribute-only state_changed events (still "playing") do not resolve the waiter.
        client = _real_client(
            monkeypatch,
            pre_state="playing",
            emitted=["playing"],
            ws_timeout=0.05,
        )

        result = await execute_music_action(
            {"action": "media_pause", "entity": "echo"}, client, MagicMock(), _matcher(PLAYER, PLAYER_NAME)
        )

        assert result["success"] is True
        assert result["new_state"] == "playing"
        assert "has not confirmed" in result["speech"]

    @pytest.mark.asyncio
    async def test_idle_without_playing_pre_state_is_not_accepted(self, monkeypatch):
        # Pre-call state unknown to the executor: idle is only an intermediate state.
        client = _real_client(monkeypatch, pre_state="on", emitted=["idle"], ws_timeout=0.05)

        result = await execute_music_action(
            {"action": "media_pause", "entity": "echo"}, client, MagicMock(), _matcher(PLAYER, PLAYER_NAME)
        )

        assert "has not confirmed" in result["speech"]


class TestAcceptedStatesPrimitive:
    @pytest.mark.asyncio
    async def test_waiter_registered_for_target_and_accepted_states(self):
        seen: dict = {}

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _expect_state(entity_id, *, expected=None, **_kw):
            seen["expected"] = expected
            yield {"new_state": "idle"}

        client = MagicMock()
        client.call_service = AsyncMock(return_value=[])
        client.expect_state = _expect_state

        result = await call_service_with_verification(
            client,
            "media_player",
            "media_pause",
            PLAYER,
            expected_state="paused",
            previous_state="playing",
            accepted_states=frozenset({"idle"}),
        )

        assert seen["expected"] == frozenset({"paused", "idle"})
        assert result["outcome"] == VERIFY_REACHED
        assert result["verified"] is True

    @pytest.mark.asyncio
    async def test_single_target_keeps_plain_expected(self):
        seen: dict = {}

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _expect_state(entity_id, *, expected=None, **_kw):
            seen["expected"] = expected
            yield {"new_state": "on"}

        client = MagicMock()
        client.call_service = AsyncMock(return_value=[])
        client.expect_state = _expect_state

        await call_service_with_verification(client, "light", "turn_on", "light.x", expected_state="on")

        assert seen["expected"] == "on"

    def test_classify_accepted_state_is_reached_only_when_given(self):
        assert classify_verification_outcome("paused", "idle", previous_state="playing") == VERIFY_UNVERIFIED
        assert (
            classify_verification_outcome(
                "paused", "idle", previous_state="playing", accepted_states=frozenset({"idle"})
            )
            == VERIFY_REACHED
        )
        assert (
            classify_verification_outcome(
                "paused", "playing", previous_state="playing", accepted_states=frozenset({"idle"})
            )
            == VERIFY_UNVERIFIED
        )

    @pytest.mark.asyncio
    async def test_rest_poll_resolves_on_accepted_state(self):
        client = HARestClient()
        client._state_observer = None
        client.get_state = AsyncMock(return_value={"state": "idle"})

        started = time.monotonic()
        async with client.expect_state(
            PLAYER, expected=frozenset({"paused", "idle"}), timeout=5.0, poll_interval=1.0, poll_max=5.0
        ) as result:
            pass

        assert time.monotonic() - started < 0.5
        assert result["new_state"] == "idle"

    @pytest.mark.asyncio
    async def test_ws_waiter_resolves_on_set_member_and_ignores_others(self):
        ws = HAWebSocketClient()
        fut = ws.register_state_waiter(PLAYER, expected=frozenset({"paused", "idle"}))

        def _event(state):
            return {"data": {"entity_id": PLAYER, "new_state": {"state": state}}}

        ws._dispatch_state_waiters(_event("playing"))
        await asyncio.sleep(0)
        assert not fut.done()
        ws._dispatch_state_waiters(_event("idle"))
        assert await asyncio.wait_for(fut, timeout=0.1) == "idle"


# ---------------------------------------------------------------------------
# Unavailable light
# ---------------------------------------------------------------------------


class TestLightUnavailable:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("action_name", ["turn_off", "turn_on", "toggle"])
    async def test_unavailable_light_makes_no_call_and_does_not_wait(self, action_name):
        client = MagicMock()
        client.get_state = AsyncMock(return_value={"state": "unavailable", "attributes": {}})
        client.call_service = AsyncMock()
        client.expect_state = MagicMock(side_effect=AssertionError("must not wait"))

        started = time.monotonic()
        result = await execute_light_action(
            {"action": action_name, "entity": "led ring"},
            client,
            MagicMock(),
            _matcher("light.satellite_led_ring", "Satellite LED Ring"),
        )

        assert time.monotonic() - started < 1.0
        client.call_service.assert_not_called()
        assert result["success"] is False
        assert result["entity_id"] == "light.satellite_led_ring"
        assert result["new_state"] == "unavailable"
        assert result["speech"] == "Satellite LED Ring is unavailable right now."
        assert result["cacheable"] is False

    @pytest.mark.asyncio
    async def test_available_light_still_runs(self):
        client = MagicMock()
        client.get_state = AsyncMock(return_value={"state": "on", "attributes": {}})
        client.call_service = AsyncMock(return_value=[{"entity_id": "light.x", "state": "off"}])
        client.expect_state = MagicMock(return_value=None)

        result = await execute_light_action(
            {"action": "turn_off", "entity": "x"}, client, MagicMock(), _matcher("light.x", "X")
        )

        client.call_service.assert_awaited_once()
        assert result["success"] is True
