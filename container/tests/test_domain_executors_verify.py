"""Shared verification tests for domain executors (0.18.5 FLOW-VERIFY-SHARED).

Covers:
- ``call_service_with_verification`` primitive in ``action_executor``.
- Each domain executor (climate, media, music, security, scene,
  automation, timer) using it instead of the legacy
  ``asyncio.sleep(0.3) + get_state`` dance.

The scenario we are pinning down is the async-bus actor case: HA's REST
``call_service`` returns ``[]`` immediately but the state change fires via
WebSocket slightly later. Without the shared helper we'd speak the stale
state; with it we must report the verified target state.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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


from app.agents.action_executor import (  # noqa: E402
    build_verified_speech,
    call_service_with_verification,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeMatch:
    """Minimal stand-in for ``EntityMatcher.MatchResult``."""

    entity_id: str
    friendly_name: str
    score: float = 0.95
    signal_scores: dict[str, float] = field(default_factory=dict)


def _make_matcher(entity_id: str, friendly_name: str):
    """An entity_matcher whose ``.match`` returns a single fixed match."""
    matcher = MagicMock()
    matcher.match = AsyncMock(return_value=[_FakeMatch(entity_id, friendly_name)])
    return matcher


def _attach_ws_observer_shim(client, *, observed_state: str | None):
    """Install ``expect_state`` that yields an observer pre-populated with
    ``observed_state``.

    Simulates the async-bus case: WS fires *during* the ``with`` block, so
    when the caller exits the context ``observer["new_state"]`` is already
    set. ``get_state`` is intentionally broken so the test fails loudly
    if anyone falls back to it.
    """

    @asynccontextmanager
    async def _expect_state(
        entity_id,
        *,
        expected=None,
        timeout=0.05,
        poll_interval=0.01,
        poll_max=0.05,
    ):
        observer = {"new_state": observed_state}
        yield observer

    client.expect_state = _expect_state
    client.set_state_observer = MagicMock()
    client.get_state = AsyncMock(
        side_effect=AssertionError(
            "domain executors must not call get_state directly anymore - use call_service_with_verification",
        ),
    )
    return client


def _make_ha_client(*, call_result=None, observed_state: str | None = None):
    client = MagicMock()
    client.call_service = AsyncMock(return_value=call_result)
    _attach_ws_observer_shim(client, observed_state=observed_state)
    return client


# ---------------------------------------------------------------------------
# call_service_with_verification primitive
# ---------------------------------------------------------------------------


class TestCallServiceWithVerification:
    """Direct unit tests for the shared helper."""

    @pytest.mark.asyncio
    async def test_non_empty_rest_response_is_authoritative(self):
        client = _make_ha_client(
            call_result=[{"entity_id": "light.x", "state": "on"}],
            observed_state="off",  # stale, must be ignored
        )
        result = await call_service_with_verification(
            client,
            "light",
            "turn_on",
            "light.x",
            expected_state="on",
        )
        assert result["success"] is True
        assert result["observed_state"] == "on"
        assert result["verified"] is True

    @pytest.mark.asyncio
    async def test_empty_rest_falls_back_to_ws_observer(self):
        client = _make_ha_client(call_result=[], observed_state="on")
        result = await call_service_with_verification(
            client,
            "light",
            "turn_on",
            "light.x",
            expected_state="on",
        )
        assert result["success"] is True
        assert result["observed_state"] == "on"
        assert result["verified"] is True

    @pytest.mark.asyncio
    async def test_empty_rest_no_observer_returns_unverified(self):
        client = _make_ha_client(call_result=[], observed_state=None)
        result = await call_service_with_verification(
            client,
            "light",
            "turn_on",
            "light.x",
            expected_state="on",
        )
        assert result["success"] is True
        assert result["observed_state"] is None
        assert result["verified"] is False

    @pytest.mark.asyncio
    async def test_call_service_exception_surfaces_failure(self):
        client = _make_ha_client(observed_state="on")
        client.call_service = AsyncMock(side_effect=RuntimeError("boom"))
        result = await call_service_with_verification(
            client,
            "light",
            "turn_on",
            "light.x",
            expected_state="on",
        )
        assert result["success"] is False
        assert result["verified"] is False
        assert isinstance(result["error"], RuntimeError)

    @pytest.mark.asyncio
    async def test_no_expected_accepts_any_observed_change(self):
        client = _make_ha_client(call_result=[], observed_state="playing")
        result = await call_service_with_verification(
            client,
            "media_player",
            "media_play",
            "media_player.x",
            expected_state=None,
        )
        assert result["verified"] is True
        assert result["observed_state"] == "playing"


# ---------------------------------------------------------------------------
# build_verified_speech
# ---------------------------------------------------------------------------


class TestBuildVerifiedSpeech:
    def test_verified_uses_expected_state(self):
        speech = build_verified_speech(
            friendly_name="Front Door",
            action_name="lock",
            expected_state="locked",
            observed_state="locked",
            verified=True,
            action_phrases={"lock": "locked"},
        )
        assert speech == "Done, Front Door is now locked."

    def test_unverified_with_expected_falls_back_to_intent(self):
        speech = build_verified_speech(
            friendly_name="Front Door",
            action_name="lock",
            expected_state="locked",
            observed_state=None,
            verified=False,
            action_phrases={"lock": "locked"},
        )
        # Intent-first phrasing takes precedence over the expected-state
        # fallback when an action phrase is registered.
        assert speech == "Done, Front Door locked."

    def test_stale_observation_does_not_override_expected(self):
        speech = build_verified_speech(
            friendly_name="Keller",
            action_name="turn_off",
            expected_state="off",
            observed_state="on",
            verified=False,
            action_phrases={"turn_off": "turned off"},
        )
        assert "is now on" not in speech
        assert speech == "Done, Keller turned off."

    def test_falls_back_to_humanized_action_name(self):
        speech = build_verified_speech(
            friendly_name="Thermostat",
            action_name="set_fan_mode",
            expected_state=None,
            observed_state=None,
            verified=False,
            action_phrases=None,
        )
        assert speech == "Done, Thermostat set fan mode."


# ---------------------------------------------------------------------------
# Per-executor WS-observer success tests
# ---------------------------------------------------------------------------

# The helper below drives each domain executor through the "REST empty +
# WS observer confirms" path. It proves:
#   * the executor uses the shared verification helper (``get_state`` is
#     wired to raise if called),
#   * the verified state is reported in ``new_state`` and speech,
#   * the service call was awaited with the expected domain/service.


async def _assert_verified_action(
    executor,
    *,
    action: dict,
    entity_id: str,
    friendly_name: str,
    observed_state: str | None,
    expected_domain: str,
    expected_service: str,
    expected_new_state: str | None,
    speech_assertions: list[str],
    negative_speech_assertions: list[str] | None = None,
    call_result=None,
):
    ha_client = _make_ha_client(
        call_result=[] if call_result is None else call_result,
        observed_state=observed_state,
    )
    matcher = _make_matcher(entity_id, friendly_name)
    entity_index = MagicMock()

    result = await executor(action, ha_client, entity_index, matcher)

    assert result["success"] is True, result
    assert result["entity_id"] == entity_id
    assert result["new_state"] == expected_new_state
    for needle in speech_assertions:
        assert needle in result["speech"], result["speech"]
    for needle in negative_speech_assertions or []:
        assert needle not in result["speech"], result["speech"]
    ha_client.call_service.assert_awaited_once()
    call_args = ha_client.call_service.await_args
    assert call_args.args[0] == expected_domain
    assert call_args.args[1] == expected_service


# ---- cover -----------------------------------------------------------------


class TestCoverExecutorVerification:
    @pytest.mark.asyncio
    async def test_cover_open_cover_empty_rest_ws_confirms(self):
        from app.agents.cover_executor import execute_cover_action

        await _assert_verified_action(
            execute_cover_action,
            action={"action": "open_cover", "entity": "living room blind"},
            entity_id="cover.living_room_blind",
            friendly_name="Living Room Blind",
            observed_state="open",
            expected_domain="cover",
            expected_service="open_cover",
            expected_new_state="open",
            speech_assertions=["Living Room Blind", "open"],
        )

    @pytest.mark.asyncio
    async def test_cover_close_cover_empty_rest_ws_confirms(self):
        from app.agents.cover_executor import execute_cover_action

        await _assert_verified_action(
            execute_cover_action,
            action={"action": "close_cover", "entity": "bedroom curtain"},
            entity_id="cover.bedroom_curtain",
            friendly_name="Bedroom Curtain",
            observed_state="closed",
            expected_domain="cover",
            expected_service="close_cover",
            expected_new_state="closed",
            speech_assertions=["Bedroom Curtain", "closed"],
        )

    @pytest.mark.asyncio
    async def test_cover_set_position_empty_rest_ws_confirms(self):
        from app.agents.cover_executor import execute_cover_action

        await _assert_verified_action(
            execute_cover_action,
            action={"action": "set_cover_position", "entity": "office blind", "parameters": {"position": 50}},
            entity_id="cover.office_blind",
            friendly_name="Office Blind",
            observed_state="open",
            expected_domain="cover",
            expected_service="set_cover_position",
            expected_new_state="open",
            speech_assertions=["Office Blind"],
        )


# ---- climate ---------------------------------------------------------------


class TestClimateExecutorVerification:
    @pytest.mark.asyncio
    async def test_turn_off_empty_rest_ws_confirms(self):
        from app.agents.climate_executor import execute_climate_action

        await _assert_verified_action(
            execute_climate_action,
            action={"action": "turn_off", "entity": "living room"},
            entity_id="climate.living_room",
            friendly_name="Living Room",
            observed_state="off",
            expected_domain="climate",
            expected_service="turn_off",
            expected_new_state="off",
            speech_assertions=["Living Room", "off"],
        )

    @pytest.mark.asyncio
    async def test_set_hvac_mode_uses_dynamic_expected(self):
        from app.agents.climate_executor import execute_climate_action

        await _assert_verified_action(
            execute_climate_action,
            action={
                "action": "set_hvac_mode",
                "entity": "living room",
                "parameters": {"hvac_mode": "heat"},
            },
            entity_id="climate.living_room",
            friendly_name="Living Room",
            observed_state="heat",
            expected_domain="climate",
            expected_service="set_hvac_mode",
            expected_new_state="heat",
            speech_assertions=["Living Room", "heat"],
        )

    @pytest.mark.asyncio
    async def test_set_hvac_mode_stale_observation_falls_back_to_intent(self):
        """Observer saw the *old* mode -- don't contradict intent."""
        from app.agents.climate_executor import execute_climate_action

        ha_client = _make_ha_client(
            call_result=[],
            observed_state="cool",  # stale: we asked for heat
        )
        matcher = _make_matcher("climate.living_room", "Living Room")
        result = await execute_climate_action(
            {
                "action": "set_hvac_mode",
                "entity": "living room",
                "parameters": {"hvac_mode": "heat"},
            },
            ha_client,
            MagicMock(),
            matcher,
        )
        assert result["success"] is True
        assert "is now cool" not in result["speech"]

    @pytest.mark.asyncio
    async def test_fan_turn_off_empty_rest_ws_confirms(self):
        from app.agents.climate_executor import execute_climate_action

        await _assert_verified_action(
            execute_climate_action,
            action={"action": "turn_off", "entity": "living room fan"},
            entity_id="fan.living_room",
            friendly_name="Living Room Fan",
            observed_state="off",
            expected_domain="fan",
            expected_service="turn_off",
            expected_new_state="off",
            speech_assertions=["Living Room Fan", "off"],
        )

    @pytest.mark.asyncio
    async def test_fan_set_percentage_empty_rest_ws_confirms(self):
        from app.agents.climate_executor import execute_climate_action

        await _assert_verified_action(
            execute_climate_action,
            action={"action": "set_fan_percentage", "entity": "office fan", "parameters": {"percentage": 75}},
            entity_id="fan.office",
            friendly_name="Office Fan",
            observed_state="on",
            expected_domain="fan",
            expected_service="set_percentage",
            expected_new_state="on",
            speech_assertions=["Office Fan", "fan speed updated"],
        )

    @pytest.mark.asyncio
    async def test_humidifier_turn_off_empty_rest_ws_confirms(self):
        from app.agents.climate_executor import execute_climate_action

        await _assert_verified_action(
            execute_climate_action,
            action={"action": "turn_off", "entity": "bedroom humidifier"},
            entity_id="humidifier.bedroom",
            friendly_name="Bedroom Humidifier",
            observed_state="off",
            expected_domain="humidifier",
            expected_service="turn_off",
            expected_new_state="off",
            speech_assertions=["Bedroom Humidifier", "off"],
        )

    @pytest.mark.asyncio
    async def test_humidifier_set_humidity_empty_rest_ws_confirms(self):
        from app.agents.climate_executor import execute_climate_action

        await _assert_verified_action(
            execute_climate_action,
            action={
                "action": "set_humidifier_humidity",
                "entity": "bedroom humidifier",
                "parameters": {"humidity": 45},
            },
            entity_id="humidifier.bedroom",
            friendly_name="Bedroom Humidifier",
            observed_state="on",
            expected_domain="humidifier",
            expected_service="set_humidity",
            expected_new_state="on",
            speech_assertions=["Bedroom Humidifier", "humidity target updated"],
        )


# ---- media -----------------------------------------------------------------


class TestMediaExecutorVerification:
    @pytest.mark.asyncio
    async def test_play_empty_rest_ws_confirms_playing(self):
        from app.agents.media_executor import execute_media_action

        await _assert_verified_action(
            execute_media_action,
            action={"action": "play", "entity": "living room tv"},
            entity_id="media_player.living_room_tv",
            friendly_name="Living Room TV",
            observed_state="playing",
            expected_domain="media_player",
            expected_service="media_play",
            expected_new_state="playing",
            speech_assertions=["Living Room TV", "playing"],
        )

    @pytest.mark.asyncio
    async def test_turn_off_stale_observed_on_does_not_speak_on(self):
        from app.agents.media_executor import execute_media_action

        ha_client = _make_ha_client(call_result=[], observed_state="playing")
        matcher = _make_matcher("media_player.tv", "TV")
        result = await execute_media_action(
            {"action": "turn_off", "entity": "tv"},
            ha_client,
            MagicMock(),
            matcher,
        )
        assert result["success"] is True
        assert "is now playing" not in result["speech"]
        assert "TV" in result["speech"]


# ---- music -----------------------------------------------------------------


class TestMusicExecutorVerification:
    @pytest.mark.asyncio
    async def test_media_pause_empty_rest_ws_confirms_paused(self):
        from app.agents.music_executor import execute_music_action

        await _assert_verified_action(
            execute_music_action,
            action={"action": "media_pause", "entity": "kitchen speaker"},
            entity_id="media_player.kitchen_speaker",
            friendly_name="Kitchen Speaker",
            observed_state="paused",
            expected_domain="media_player",
            expected_service="media_pause",
            expected_new_state="paused",
            speech_assertions=["Kitchen Speaker", "paused"],
        )


# ---- security --------------------------------------------------------------


class TestSecurityExecutorVerification:
    @pytest.mark.asyncio
    async def test_lock_empty_rest_ws_confirms_locked(self):
        from app.agents.security_executor import execute_security_action

        await _assert_verified_action(
            execute_security_action,
            action={"action": "lock", "entity": "front door"},
            entity_id="lock.front_door",
            friendly_name="Front Door",
            observed_state="locked",
            expected_domain="lock",
            expected_service="lock",
            expected_new_state="locked",
            speech_assertions=["Front Door", "locked"],
        )

    @pytest.mark.asyncio
    async def test_alarm_arm_home_stale_disarmed_does_not_report_disarmed(self):
        """Critical safety test: never claim the alarm is disarmed when we
        issued an arm command."""
        from app.agents.security_executor import execute_security_action

        ha_client = _make_ha_client(call_result=[], observed_state="disarmed")
        matcher = _make_matcher("alarm_control_panel.home", "Alarm")
        result = await execute_security_action(
            {"action": "alarm_arm_home", "entity": "alarm"},
            ha_client,
            MagicMock(),
            matcher,
        )
        assert result["success"] is True
        assert "disarmed" not in result["speech"]
        assert "armed" in result["speech"]


# ---- scene -----------------------------------------------------------------


class TestSceneExecutorVerification:
    @pytest.mark.asyncio
    async def test_activate_scene_fires_and_reports_activation(self):
        from app.agents.scene_executor import execute_scene_action

        await _assert_verified_action(
            execute_scene_action,
            action={"action": "activate_scene", "entity": "movie night"},
            entity_id="scene.movie_night",
            friendly_name="Movie Night",
            observed_state="2026-04-19T12:00:00Z",  # scene timestamp
            expected_domain="scene",
            expected_service="turn_on",
            expected_new_state="2026-04-19T12:00:00Z",
            speech_assertions=["Movie Night", "activated"],
        )


# ---- vacuum ----------------------------------------------------------------


class TestVacuumExecutorVerification:
    @pytest.mark.asyncio
    async def test_vacuum_start_empty_rest_ws_confirms(self):
        from app.agents.vacuum_executor import execute_vacuum_action

        await _assert_verified_action(
            execute_vacuum_action,
            action={"action": "start", "entity": "robot vacuum"},
            entity_id="vacuum.robot",
            friendly_name="Robot Vacuum",
            observed_state="cleaning",
            expected_domain="vacuum",
            expected_service="start",
            expected_new_state="cleaning",
            speech_assertions=["Robot Vacuum", "cleaning"],
        )

    @pytest.mark.asyncio
    async def test_vacuum_return_to_base_empty_rest_ws_confirms(self):
        from app.agents.vacuum_executor import execute_vacuum_action

        await _assert_verified_action(
            execute_vacuum_action,
            action={"action": "return_to_base", "entity": "robot vacuum"},
            entity_id="vacuum.robot",
            friendly_name="Robot Vacuum",
            observed_state="returning",
            expected_domain="vacuum",
            expected_service="return_to_base",
            expected_new_state="returning",
            speech_assertions=["Robot Vacuum", "returning"],
        )

    @pytest.mark.asyncio
    async def test_vacuum_set_fan_speed_empty_rest_ws_confirms(self):
        from app.agents.vacuum_executor import execute_vacuum_action

        await _assert_verified_action(
            execute_vacuum_action,
            action={"action": "set_fan_speed", "entity": "robot vacuum", "parameters": {"fan_speed": "turbo"}},
            entity_id="vacuum.robot",
            friendly_name="Robot Vacuum",
            observed_state=None,
            expected_domain="vacuum",
            expected_service="set_fan_speed",
            expected_new_state=None,
            speech_assertions=["Robot Vacuum", "fan speed updated"],
        )


# ---- automation ------------------------------------------------------------


class TestAutomationExecutorVerification:
    @pytest.mark.asyncio
    async def test_enable_automation_ws_confirms_on(self):
        from app.agents.automation_executor import execute_automation_action

        await _assert_verified_action(
            execute_automation_action,
            action={"action": "enable_automation", "entity": "morning lights"},
            entity_id="automation.morning_lights",
            friendly_name="Morning Lights",
            observed_state="on",
            expected_domain="automation",
            expected_service="turn_on",
            expected_new_state="on",
            speech_assertions=["Morning Lights"],
        )

    @pytest.mark.asyncio
    async def test_trigger_automation_no_expected_state_uses_intent_phrase(self):
        """Triggering does NOT change the entity state; speech must be
        intent-first ("triggered"), not "is now on"."""
        from app.agents.automation_executor import execute_automation_action

        ha_client = _make_ha_client(call_result=[], observed_state="on")
        matcher = _make_matcher("automation.morning_lights", "Morning Lights")
        result = await execute_automation_action(
            {"action": "trigger_automation", "entity": "morning lights"},
            ha_client,
            MagicMock(),
            matcher,
        )
        assert result["success"] is True
        assert "triggered" in result["speech"]


# ---- timer -----------------------------------------------------------------


class TestTimerExecutorVerification:
    """Scheduler-routed timer actions.

    In 0.26.0 the HA timer.* helper pool was removed; ``start_timer`` /
    ``cancel_timer`` go through the AgentHub-managed
    ``TimerScheduler`` and never call HA timer.* services. These tests
    patch ``_get_scheduler`` to verify routing without spinning up the
    full scheduler.
    """

    @pytest.mark.asyncio
    async def test_start_timer_routes_to_scheduler(self):
        from unittest.mock import AsyncMock, patch

        from app.agents.timer_executor import execute_timer_action

        scheduler = MagicMock()
        scheduler.schedule = AsyncMock(return_value="timer-id-abc")
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {
                    "action": "start_timer",
                    "entity": "pasta",
                    "parameters": {"duration": "00:05:00"},
                },
                _make_ha_client(call_result=[], observed_state=None),
                MagicMock(),
                _make_matcher("timer.pasta", "Pasta"),
                area_id="kitchen",
            )
        assert result["success"] is True
        assert result["new_state"] == "active"
        scheduler.schedule.assert_awaited_once()
        kwargs = scheduler.schedule.await_args.kwargs
        assert kwargs["kind"] == "plain"
        assert kwargs["duration_seconds"] == 300
        assert kwargs["origin_area"] == "kitchen"

    @pytest.mark.asyncio
    async def test_start_timer_without_duration_fails(self):
        from app.agents.timer_executor import execute_timer_action

        result = await execute_timer_action(
            {"action": "start_timer", "entity": "pasta", "parameters": {}},
            _make_ha_client(call_result=[], observed_state=None),
            MagicMock(),
            _make_matcher("timer.pasta", "Pasta"),
        )
        assert result["success"] is False
        assert "duration" in result["speech"].lower()

    @pytest.mark.asyncio
    async def test_cancel_timer_routes_to_scheduler(self):
        from unittest.mock import AsyncMock, patch

        from app.agents.timer_executor import execute_timer_action

        scheduler = MagicMock()
        scheduler.cancel = AsyncMock(return_value=1)
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "cancel_timer", "entity": "pasta"},
                _make_ha_client(call_result=[], observed_state=None),
                MagicMock(),
                _make_matcher("timer.pasta", "Pasta"),
                area_id="kitchen",
            )
        assert result["success"] is True
        assert result["new_state"] == "idle"
        scheduler.cancel.assert_awaited_once_with(logical_name="pasta", area="kitchen")

    @pytest.mark.asyncio
    async def test_cancel_timer_when_none_match_fails(self):
        from unittest.mock import AsyncMock, patch

        from app.agents.timer_executor import execute_timer_action

        scheduler = MagicMock()
        scheduler.cancel = AsyncMock(return_value=0)
        scheduler.list = AsyncMock(return_value=[])
        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "cancel_timer", "entity": "pasta"},
                _make_ha_client(call_result=[], observed_state=None),
                MagicMock(),
                _make_matcher("timer.pasta", "Pasta"),
            )
        assert result["success"] is False
        assert "no timer named" in result["speech"].lower()

    @pytest.mark.asyncio
    async def test_extend_timer_extends_existing_timer(self):
        from unittest.mock import AsyncMock, patch

        from app.agents.timer_executor import execute_timer_action

        existing_row = {
            "id": "t-001",
            "logical_name": "pasta",
            "kind": "plain",
            "fires_at": int(__import__("time").time()) + 120,
            "origin_device_id": None,
            "origin_area": "kitchen",
            "payload_json": '{"language": "en"}',
        }
        scheduler = MagicMock()
        scheduler.list = AsyncMock(return_value=[existing_row])
        scheduler.cancel = AsyncMock(return_value=1)
        scheduler.schedule = AsyncMock(return_value="t-002")

        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "extend_timer", "entity": "pasta", "parameters": {"duration": "00:01:00"}},
                _make_ha_client(call_result=[], observed_state=None),
                MagicMock(),
                _make_matcher("timer.pasta", "Pasta"),
                area_id="kitchen",
            )
        assert result["success"] is True
        assert result["new_state"] == "active"
        scheduler.cancel.assert_awaited_once_with(id_="t-001")
        scheduler.schedule.assert_awaited_once()
        kwargs = scheduler.schedule.await_args.kwargs
        assert kwargs["logical_name"] == "pasta"
        assert kwargs["duration_seconds"] >= 120 + 60 - 5

    @pytest.mark.asyncio
    async def test_extend_timer_with_omitted_entity_uses_single_running_timer(self):
        from unittest.mock import AsyncMock, patch

        from app.agents.timer_executor import execute_timer_action

        existing_row = {
            "id": "t-003",
            "logical_name": "egg timer",
            "kind": "plain",
            "fires_at": int(__import__("time").time()) + 60,
            "origin_device_id": None,
            "origin_area": None,
            "payload_json": "{}",
        }
        scheduler = MagicMock()
        scheduler.list = AsyncMock(return_value=[existing_row])
        scheduler.cancel = AsyncMock(return_value=1)
        scheduler.schedule = AsyncMock(return_value="t-004")

        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "extend_timer", "entity": "", "parameters": {"duration": "00:02:00"}},
                _make_ha_client(call_result=[], observed_state=None),
                MagicMock(),
                _make_matcher("", ""),
            )
        assert result["success"] is True
        scheduler.schedule.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_extend_timer_no_running_timers_fails(self):
        from unittest.mock import AsyncMock, patch

        from app.agents.timer_executor import execute_timer_action

        scheduler = MagicMock()
        scheduler.list = AsyncMock(return_value=[])

        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "extend_timer", "entity": "", "parameters": {"duration": "00:01:00"}},
                _make_ha_client(call_result=[], observed_state=None),
                MagicMock(),
                _make_matcher("", ""),
            )
        assert result["success"] is False
        assert "no active timer" in result["speech"].lower()

    @pytest.mark.asyncio
    async def test_extend_timer_without_duration_fails(self):
        from app.agents.timer_executor import execute_timer_action

        result = await execute_timer_action(
            {"action": "extend_timer", "entity": "pasta", "parameters": {}},
            _make_ha_client(call_result=[], observed_state=None),
            MagicMock(),
            _make_matcher("timer.pasta", "Pasta"),
        )
        assert result["success"] is False
        assert "duration" in result["speech"].lower()

    @pytest.mark.asyncio
    async def test_cancel_timer_spoken_variant_fallback(self):
        """Einminutentimer (spoken) cancels a timer stored as 1-Minuten-Timer."""
        from unittest.mock import AsyncMock, patch

        from app.agents.timer_executor import execute_timer_action

        stored_row = {
            "id": "t-010",
            "logical_name": "1-Minuten-Timer",
            "kind": "plain",
            "fires_at": int(__import__("time").time()) + 55,
            "origin_device_id": None,
            "origin_area": None,
            "payload_json": "{}",
        }
        scheduler = MagicMock()
        scheduler.cancel = AsyncMock(side_effect=[0, 1])
        scheduler.list = AsyncMock(return_value=[stored_row])

        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "cancel_timer", "entity": "Einminutentimer"},
                _make_ha_client(call_result=[], observed_state=None),
                MagicMock(),
                _make_matcher("", ""),
            )
        assert result["success"] is True
        second_call_kwargs = scheduler.cancel.await_args_list[1].kwargs
        assert second_call_kwargs.get("id_") == "t-010"

    @pytest.mark.asyncio
    async def test_cancel_timer_exact_match_takes_precedence(self):
        """When exact-match succeeds, list() is never called (no fallback executed)."""
        from unittest.mock import AsyncMock, patch

        from app.agents.timer_executor import execute_timer_action

        scheduler = MagicMock()
        scheduler.cancel = AsyncMock(return_value=1)
        scheduler.list = AsyncMock()

        with patch("app.agents.timer_executor._get_scheduler", return_value=scheduler):
            result = await execute_timer_action(
                {"action": "cancel_timer", "entity": "pasta"},
                _make_ha_client(call_result=[], observed_state=None),
                MagicMock(),
                _make_matcher("timer.pasta", "Pasta"),
            )
        assert result["success"] is True
        scheduler.list.assert_not_awaited()
