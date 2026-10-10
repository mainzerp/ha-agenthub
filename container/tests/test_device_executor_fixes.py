"""Regression tests for the device-executor fixes of issue #132 (theme T5).

Covers the no-op check (parameters, groups, cover tilt), climate per-domain
service mapping, relative changes, ``executed_command`` / cacheability,
supported-features checks, honest failure speech, shared entity resolution
for media/music, and the satellite-target visibility filter.
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass, field
from typing import Any
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

from app.agents import satellite_targeting  # noqa: E402
from app.agents.action_executor import (  # noqa: E402
    reset_request_candidate_ids,
    set_request_candidate_ids,
)
from app.agents.climate_executor import execute_climate_action  # noqa: E402
from app.agents.cover_executor import execute_cover_action  # noqa: E402
from app.agents.executor_state_check import is_redundant_action  # noqa: E402
from app.agents.light_executor import execute_light_action  # noqa: E402
from app.agents.media_executor import execute_media_action  # noqa: E402
from app.agents.music_executor import execute_music_action  # noqa: E402
from app.agents.scene_executor import execute_scene_action  # noqa: E402
from app.agents.security_executor import execute_security_action  # noqa: E402
from app.agents.vacuum_executor import execute_vacuum_action  # noqa: E402
from app.entity.visibility import invalidate_visibility_rules_cache  # noqa: E402
from tests.helpers import attach_expect_state_shim  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    invalidate_visibility_rules_cache()
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[]),
    )
    yield
    invalidate_visibility_rules_cache()


@pytest.fixture(autouse=True)
def _fast_state_verify(monkeypatch):
    from app.agents import action_executor as _ae

    async def _fast(key, *, default):
        return {
            "state_verify.ws_timeout_sec": 0.05,
            "state_verify.poll_interval_sec": 0.01,
            "state_verify.poll_max_sec": 0.05,
        }.get(key, default)

    monkeypatch.setattr(_ae, "_settings_float", _fast)


@dataclass
class _FakeMatch:
    entity_id: str
    friendly_name: str
    score: float = 0.95
    signal_scores: dict[str, float] = field(default_factory=dict)


def _matcher(entity_id: str, friendly_name: str = "Target") -> MagicMock:
    matcher = MagicMock()
    matcher.match = AsyncMock(return_value=[_FakeMatch(entity_id, friendly_name)])
    return matcher


class _FakeHA:
    """HA client double: fixed pre-action state, records service calls.

    ``call_service`` returns ``changed`` (HA's changed-states list) so the
    shared verification helper sees an authoritative post-action state.
    """

    def __init__(
        self,
        entity_id: str,
        state: str | None,
        attributes: dict[str, Any] | None = None,
        *,
        post_state: str | None = None,
        call_error: Exception | None = None,
        response: Any = None,
    ) -> None:
        self._state = {"entity_id": entity_id, "state": state, "attributes": attributes or {}}
        self._entity_id = entity_id
        self._post_state = post_state
        self._call_error = call_error
        self._response = response
        self.calls: list[tuple] = []
        attach_expect_state_shim(self)

    async def get_state(self, entity_id: str):
        return dict(self._state)

    async def call_service(self, domain, service, entity_id=None, service_data=None, *, return_response=False):
        self.calls.append((domain, service, entity_id, service_data, return_response))
        if self._call_error is not None:
            raise self._call_error
        if self._response is not None:
            return self._response
        if self._post_state is not None:
            return [{"entity_id": self._entity_id, "state": self._post_state}]
        return []


# ---------------------------------------------------------------------------
# Item 1 / 4: no-op check
# ---------------------------------------------------------------------------


class TestNoopCheck:
    def test_parameters_are_never_redundant(self):
        state = {"state": "on", "attributes": {}}
        assert is_redundant_action("turn_on", state, {}) is True
        assert is_redundant_action("turn_on", state, {"brightness_pct": 50}) is False

    def test_groups_are_never_redundant(self):
        group = {"state": "on", "attributes": {"entity_id": ["light.a", "light.b"]}}
        assert is_redundant_action("turn_on", group, {}) is False

    def test_tilt_is_not_checked_against_position_state(self):
        state = {"state": "open", "attributes": {}}
        assert is_redundant_action("open_cover_tilt", state, {}) is False
        assert is_redundant_action("close_cover_tilt", {"state": "closed"}, {}) is False

    @pytest.mark.asyncio
    async def test_brightness_on_lit_light_calls_service(self):
        ha = _FakeHA("light.bedroom", "on", post_state="on")
        result = await execute_light_action(
            {"action": "turn_on", "entity": "bedroom light", "parameters": {"brightness_pct": 50}},
            ha,
            MagicMock(),
            _matcher("light.bedroom", "Bedroom Light"),
        )
        assert result.get("noop") is not True
        assert ha.calls == [("light", "turn_on", "light.bedroom", {"brightness_pct": 50}, False)]

    @pytest.mark.asyncio
    async def test_color_on_lit_light_calls_service(self):
        ha = _FakeHA("light.living", "on", post_state="on")
        result = await execute_light_action(
            {"action": "turn_on", "entity": "living room light", "parameters": {"color_name": "red"}},
            ha,
            MagicMock(),
            _matcher("light.living"),
        )
        assert result.get("noop") is not True
        assert ha.calls[0][3] == {"color_name": "red"}

    @pytest.mark.asyncio
    async def test_light_group_partially_on_still_turns_on(self):
        ha = _FakeHA("light.all_kitchen", "on", {"entity_id": ["light.a", "light.b"]}, post_state="on")
        result = await execute_light_action(
            {"action": "turn_on", "entity": "kitchen lights"},
            ha,
            MagicMock(),
            _matcher("light.all_kitchen"),
        )
        assert result.get("noop") is not True
        assert len(ha.calls) == 1

    @pytest.mark.asyncio
    async def test_plain_turn_on_on_lit_single_light_is_noop(self):
        ha = _FakeHA("light.bedroom", "on")
        result = await execute_light_action(
            {"action": "turn_on", "entity": "bedroom light"},
            ha,
            MagicMock(),
            _matcher("light.bedroom", "Bedroom Light"),
        )
        assert result["noop"] is True
        assert ha.calls == []

    @pytest.mark.asyncio
    async def test_cover_tilt_on_open_cover_runs(self):
        ha = _FakeHA("cover.office", "open", post_state="open")
        result = await execute_cover_action(
            {"action": "close_cover_tilt", "entity": "office blind"},
            ha,
            MagicMock(),
            _matcher("cover.office", "Office Blind"),
        )
        assert result.get("noop") is not True
        assert ha.calls[0][:2] == ("cover", "close_cover_tilt")
        # Tilt has no deterministic main state: never claim "is now closed".
        assert "is now" not in result["speech"]

    @pytest.mark.asyncio
    async def test_cover_group_runs(self):
        ha = _FakeHA("cover.all", "open", {"entity_id": ["cover.a", "cover.b"]}, post_state="open")
        result = await execute_cover_action(
            {"action": "open_cover", "entity": "all blinds"},
            ha,
            MagicMock(),
            _matcher("cover.all"),
        )
        assert result.get("noop") is not True
        assert len(ha.calls) == 1

    @pytest.mark.asyncio
    async def test_security_code_skips_noop(self):
        ha = _FakeHA("lock.front", "unlocked", post_state="unlocked")
        result = await execute_security_action(
            {"action": "unlock", "entity": "front door", "parameters": {"code": "1234"}},
            ha,
            MagicMock(),
            _matcher("lock.front", "Front Door"),
        )
        assert result.get("noop") is not True
        assert ha.calls[0][3] == {"code": "1234"}


# ---------------------------------------------------------------------------
# Item 3: climate per-domain services, item 11: relative temperature
# ---------------------------------------------------------------------------


class TestClimateDomainMapping:
    @pytest.mark.asyncio
    async def test_set_fan_mode_on_fan_entity_uses_fan_service(self):
        ha = _FakeHA("fan.bedroom", "on", post_state="on")
        result = await execute_climate_action(
            {"action": "set_fan_mode", "entity": "fan", "parameters": {"fan_mode": "high"}},
            ha,
            MagicMock(),
            _matcher("fan.bedroom", "Bedroom Fan"),
        )
        assert result["success"] is True
        assert ha.calls[0][:4] == ("fan", "set_percentage", "fan.bedroom", {"percentage": 100})
        assert result["executed_command"]["domain"] == "fan"

    @pytest.mark.asyncio
    async def test_set_fan_mode_on_fan_prefers_matching_preset(self):
        ha = _FakeHA("fan.office", "on", {"preset_modes": ["Breeze", "Sleep"]}, post_state="on")
        await execute_climate_action(
            {"action": "set_fan_mode", "entity": "office fan", "parameters": {"fan_mode": "breeze"}},
            ha,
            MagicMock(),
            _matcher("fan.office"),
        )
        assert ha.calls[0][:4] == ("fan", "set_preset_mode", "fan.office", {"preset_mode": "Breeze"})

    @pytest.mark.asyncio
    async def test_set_fan_mode_on_thermostat_keeps_climate_service(self):
        ha = _FakeHA("climate.living", "cool", post_state="cool")
        await execute_climate_action(
            {"action": "set_fan_mode", "entity": "living room", "parameters": {"fan_mode": "high"}},
            ha,
            MagicMock(),
            _matcher("climate.living"),
        )
        assert ha.calls[0][:4] == ("climate", "set_fan_mode", "climate.living", {"fan_mode": "high"})

    @pytest.mark.asyncio
    async def test_set_humidity_on_humidifier_uses_humidifier_service(self):
        ha = _FakeHA("humidifier.bedroom", "on", post_state="on")
        await execute_climate_action(
            {"action": "set_humidity", "entity": "bedroom humidifier", "parameters": {"humidity": 45}},
            ha,
            MagicMock(),
            _matcher("humidifier.bedroom"),
        )
        assert ha.calls[0][:4] == ("humidifier", "set_humidity", "humidifier.bedroom", {"humidity": 45})

    @pytest.mark.asyncio
    async def test_fan_turn_on_expects_on(self):
        ha = _FakeHA("fan.bedroom", "off", post_state="on")
        result = await execute_climate_action(
            {"action": "turn_on", "entity": "bedroom fan"},
            ha,
            MagicMock(),
            _matcher("fan.bedroom", "Bedroom Fan"),
        )
        assert ha.calls[0][:2] == ("fan", "turn_on")
        assert "is now on" in result["speech"]

    @pytest.mark.asyncio
    async def test_set_temperature_does_not_resolve_to_fan(self):
        matcher = _matcher("climate.living")
        ha = _FakeHA("climate.living", "heat", {"temperature": 20}, post_state="heat")
        await execute_climate_action(
            {"action": "set_temperature", "entity": "living room", "parameters": {"temperature": 21}},
            ha,
            MagicMock(),
            matcher,
        )
        preferred = matcher.match.await_args.kwargs.get("preferred_domains")
        assert preferred is not None and set(preferred) == {"climate"}

    @pytest.mark.asyncio
    async def test_relative_temperature_uses_current_target(self):
        ha = _FakeHA("climate.living", "heat", {"temperature": 20.5, "target_temp_step": 0.5}, post_state="heat")
        result = await execute_climate_action(
            {"action": "set_temperature", "entity": "living room", "parameters": {"temperature_delta": 2}},
            ha,
            MagicMock(),
            _matcher("climate.living"),
        )
        assert ha.calls[0][:4] == ("climate", "set_temperature", "climate.living", {"temperature": 22.5})
        assert result["cacheable"] is False

    @pytest.mark.asyncio
    async def test_relative_temperature_without_current_target_is_honest(self):
        ha = _FakeHA("climate.living", "heat", {})
        result = await execute_climate_action(
            {"action": "set_temperature", "entity": "living room", "parameters": {"temperature_delta": -1}},
            ha,
            MagicMock(),
            _matcher("climate.living", "Living Room"),
        )
        assert result["success"] is False
        assert "current target temperature" in result["speech"]
        assert ha.calls == []


# ---------------------------------------------------------------------------
# Item 7: executed_command and cacheability
# ---------------------------------------------------------------------------


class TestExecutedCommand:
    @pytest.mark.asyncio
    async def test_light_returns_executed_command(self):
        ha = _FakeHA("light.kitchen", "off", post_state="on")
        result = await execute_light_action(
            {"action": "set_brightness", "entity": "kitchen", "parameters": {"brightness": 128}},
            ha,
            MagicMock(),
            _matcher("light.kitchen"),
        )
        assert result["executed_command"] == {
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.kitchen",
            "service_data": {"brightness": 128},
        }
        assert result.get("cacheable", True) is True

    @pytest.mark.asyncio
    async def test_light_toggle_is_not_cacheable(self):
        ha = _FakeHA("light.kitchen", "off", post_state="on")
        result = await execute_light_action(
            {"action": "toggle", "entity": "kitchen"},
            ha,
            MagicMock(),
            _matcher("light.kitchen"),
        )
        assert result["cacheable"] is False
        assert result["executed_command"]["service"] == "toggle"

    @pytest.mark.asyncio
    async def test_media_set_volume_returns_real_service(self):
        ha = _FakeHA("media_player.tv", "on", post_state="on")
        result = await execute_media_action(
            {"action": "set_volume", "entity": "tv", "parameters": {"volume_level": 0.3}},
            ha,
            MagicMock(),
            _matcher("media_player.tv"),
        )
        assert result["executed_command"] == {
            "domain": "media_player",
            "service": "volume_set",
            "entity_id": "media_player.tv",
            "service_data": {"volume_level": 0.3},
        }

    @pytest.mark.asyncio
    async def test_scene_returns_turn_on_command(self):
        ha = _FakeHA("scene.movie", "2026-10-10T20:00:00", post_state="2026-10-10T20:00:05")
        result = await execute_scene_action(
            {"action": "activate_scene", "entity": "movie"},
            ha,
            MagicMock(),
            _matcher("scene.movie", "Movie"),
        )
        assert result["executed_command"]["service"] == "turn_on"
        assert result["executed_command"]["domain"] == "scene"
        assert "activated" in result["speech"]

    @pytest.mark.asyncio
    async def test_security_camera_returns_camera_turn_on(self):
        ha = _FakeHA("camera.garage", "idle", post_state="streaming")
        result = await execute_security_action(
            {"action": "camera_turn_on", "entity": "garage camera"},
            ha,
            MagicMock(),
            _matcher("camera.garage"),
        )
        assert result["executed_command"]["domain"] == "camera"
        assert result["executed_command"]["service"] == "turn_on"

    @pytest.mark.asyncio
    async def test_security_code_never_cached_or_echoed(self):
        ha = _FakeHA("alarm_control_panel.house", "armed_away", post_state="disarmed")
        result = await execute_security_action(
            {"action": "alarm_disarm", "entity": "house alarm", "parameters": {"code": "4321"}},
            ha,
            MagicMock(),
            _matcher("alarm_control_panel.house"),
        )
        assert result["cacheable"] is False
        assert "code" not in result["executed_command"]["service_data"]
        assert "code" not in result["service_data"]

    @pytest.mark.asyncio
    async def test_vacuum_returns_executed_command(self):
        ha = _FakeHA("vacuum.robot", "docked", post_state="cleaning")
        result = await execute_vacuum_action(
            {"action": "start", "entity": "robot"},
            ha,
            MagicMock(),
            _matcher("vacuum.robot"),
        )
        assert result["executed_command"] == {
            "domain": "vacuum",
            "service": "start",
            "entity_id": "vacuum.robot",
            "service_data": {},
        }


# ---------------------------------------------------------------------------
# Item 8: media/music shared resolution, music area + search response
# ---------------------------------------------------------------------------


class TestMediaMusicResolution:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("executor", [execute_media_action, execute_music_action])
    async def test_entity_id_outside_candidate_set_is_rejected(self, executor):
        ha = _FakeHA("media_player.tv", "on", post_state="on")
        action_name = "pause" if executor is execute_media_action else "media_pause"
        token = set_request_candidate_ids({"media_player.kitchen"})
        try:
            result = await executor(
                {"action": action_name, "entity": "tv", "entity_id": "media_player.tv"},
                ha,
                MagicMock(),
                _matcher("media_player.tv"),
            )
        finally:
            reset_request_candidate_ids(token)
        assert result["success"] is False
        assert ha.calls == []

    def test_music_accepts_preferred_area_id(self):
        params = inspect.signature(execute_music_action).parameters
        assert "preferred_area_id" in params

    @pytest.mark.asyncio
    async def test_music_forwards_preferred_area_to_resolver(self, monkeypatch):
        captured: dict[str, Any] = {}

        async def _fake_resolve(query, index, matcher, **kwargs):
            captured.update(kwargs)
            return {
                "entity_id": "media_player.kitchen",
                "friendly_name": "Kitchen",
                "speech": None,
                "metadata": {},
            }

        monkeypatch.setattr("app.agents.action_executor.resolve_entity_deterministic_first", _fake_resolve)
        ha = _FakeHA("media_player.kitchen", "paused", post_state="playing")
        await execute_music_action(
            {"action": "media_play", "entity": "speaker"},
            ha,
            MagicMock(),
            MagicMock(),
            preferred_area_id="kitchen",
        )
        assert captured.get("preferred_area_id") == "kitchen"

    @pytest.mark.asyncio
    async def test_music_search_requests_service_response(self):
        ha = _FakeHA(
            "media_player.kitchen",
            "idle",
            response={"tracks": [{"name": "So What", "artists": [{"name": "Miles Davis"}]}]},
        )
        result = await execute_music_action(
            {"action": "search", "entity": "kitchen", "parameters": {"name": "so what"}},
            ha,
            MagicMock(),
            _matcher("media_player.kitchen"),
        )
        assert ha.calls[0][4] is True
        assert "So What by Miles Davis" in result["speech"]
        assert result["cacheable"] is False


# ---------------------------------------------------------------------------
# Item 9: speech and supported features
# ---------------------------------------------------------------------------


class TestSpeechAndFeatures:
    @pytest.mark.asyncio
    async def test_unmute_is_spoken_as_unmuted(self):
        ha = _FakeHA("media_player.tv", "on", post_state="on")
        result = await execute_media_action(
            {"action": "mute", "entity": "tv", "parameters": {"is_volume_muted": False}},
            ha,
            MagicMock(),
            _matcher("media_player.tv", "TV"),
        )
        assert "unmuted" in result["speech"]
        assert ha.calls[0][3] == {"is_volume_muted": False}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "executor,action",
        [
            (execute_light_action, {"action": "turn_on", "entity": "x"}),
            (execute_climate_action, {"action": "turn_off", "entity": "x"}),
            (execute_cover_action, {"action": "open_cover", "entity": "x"}),
            (execute_media_action, {"action": "turn_off", "entity": "x"}),
            (execute_music_action, {"action": "media_play", "entity": "x"}),
            (execute_scene_action, {"action": "activate_scene", "entity": "x"}),
            (execute_security_action, {"action": "lock", "entity": "x"}),
            (execute_vacuum_action, {"action": "start", "entity": "x"}),
        ],
    )
    async def test_failure_speech_never_leaks_internals(self, executor, action):
        entity_id = {
            execute_light_action: "light.x",
            execute_climate_action: "climate.x",
            execute_cover_action: "cover.x",
            execute_media_action: "media_player.x",
            execute_music_action: "media_player.x",
            execute_scene_action: "scene.x",
            execute_security_action: "lock.x",
            execute_vacuum_action: "vacuum.x",
        }[executor]
        error = RuntimeError("Server error '500' for url 'http://homeassistant.local:8123/api/services/x'")
        ha = _FakeHA(entity_id, "unknown", call_error=error)
        result = await executor(action, ha, MagicMock(), _matcher(entity_id, "Thing"))
        assert result["success"] is False
        assert "http" not in result["speech"]
        assert "500" not in result["speech"]
        assert "Thing" in result["speech"]

    @pytest.mark.asyncio
    async def test_cover_without_tilt_support_is_honest(self):
        ha = _FakeHA("cover.garage", "closed", {"supported_features": 15})
        result = await execute_cover_action(
            {"action": "set_cover_tilt_position", "entity": "garage", "parameters": {"tilt_position": 40}},
            ha,
            MagicMock(),
            _matcher("cover.garage", "Garage Door"),
        )
        assert result["success"] is False
        assert "does not support" in result["speech"]
        assert ha.calls == []

    @pytest.mark.asyncio
    async def test_cover_without_position_support_is_honest(self):
        ha = _FakeHA("cover.gate", "closed", {"supported_features": 3})
        result = await execute_cover_action(
            {"action": "set_cover_position", "entity": "gate", "parameters": {"position": 50}},
            ha,
            MagicMock(),
            _matcher("cover.gate", "Gate"),
        )
        assert result["success"] is False
        assert "setting a position" in result["speech"]
        assert ha.calls == []

    @pytest.mark.asyncio
    async def test_media_without_volume_set_is_honest(self):
        ha = _FakeHA("media_player.tv", "on", {"supported_features": 1})
        result = await execute_media_action(
            {"action": "set_volume", "entity": "tv", "parameters": {"volume_level": 0.5}},
            ha,
            MagicMock(),
            _matcher("media_player.tv", "TV"),
        )
        assert result["success"] is False
        assert "does not support" in result["speech"]
        assert ha.calls == []

    @pytest.mark.asyncio
    async def test_onoff_light_rejects_brightness(self):
        ha = _FakeHA("light.porch", "off", {"supported_color_modes": ["onoff"]})
        result = await execute_light_action(
            {"action": "turn_on", "entity": "porch", "parameters": {"brightness_pct": 40}},
            ha,
            MagicMock(),
            _matcher("light.porch", "Porch Light"),
        )
        assert result["success"] is False
        assert "does not support dimming" in result["speech"]
        assert ha.calls == []

    @pytest.mark.asyncio
    async def test_brightness_only_light_rejects_color(self):
        ha = _FakeHA("light.hall", "on", {"supported_color_modes": ["brightness"]})
        result = await execute_light_action(
            {"action": "turn_on", "entity": "hall", "parameters": {"color_name": "red"}},
            ha,
            MagicMock(),
            _matcher("light.hall", "Hall Light"),
        )
        assert result["success"] is False
        assert "does not support colors" in result["speech"]

    @pytest.mark.asyncio
    async def test_switch_rejects_brightness(self):
        ha = _FakeHA("switch.pump", "off")
        result = await execute_light_action(
            {"action": "turn_on", "entity": "pump", "parameters": {"brightness": 100}},
            ha,
            MagicMock(),
            _matcher("switch.pump", "Pump"),
        )
        assert result["success"] is False
        assert "switch" in result["speech"]
        assert ha.calls == []

    @pytest.mark.asyncio
    async def test_scene_without_confirmation_does_not_claim_activation(self):
        ha = _FakeHA("scene.movie", "2026-10-10T20:00:00")
        ha.expect_state = None  # no WS evidence, empty REST response
        result = await execute_scene_action(
            {"action": "activate_scene", "entity": "movie"},
            ha,
            MagicMock(),
            _matcher("scene.movie", "Movie"),
        )
        assert result["success"] is True
        assert "has been activated" not in result["speech"]
        assert result["cacheable"] is False


# ---------------------------------------------------------------------------
# Item 11: relative changes and parameter handling
# ---------------------------------------------------------------------------


class TestRelativeAndParameters:
    @pytest.mark.asyncio
    async def test_brightness_step_passes_through(self):
        ha = _FakeHA("light.living", "on", post_state="on")
        await execute_light_action(
            {"action": "turn_on", "entity": "living", "parameters": {"brightness_step_pct": 20}},
            ha,
            MagicMock(),
            _matcher("light.living"),
        )
        assert ha.calls[0][3] == {"brightness_step_pct": 20}

    @pytest.mark.asyncio
    async def test_unknown_light_parameter_is_rejected_not_dropped(self):
        ha = _FakeHA("light.living", "off")
        result = await execute_light_action(
            {"action": "turn_on", "entity": "living", "parameters": {"warmth": "cozy"}},
            ha,
            MagicMock(),
            _matcher("light.living", "Living Light"),
        )
        assert result["success"] is False
        assert "warmth" in result["speech"]
        assert ha.calls == []

    @pytest.mark.asyncio
    async def test_kelvin_alias_is_mapped(self):
        ha = _FakeHA("light.living", "on", post_state="on")
        await execute_light_action(
            {"action": "turn_on", "entity": "living", "parameters": {"kelvin": 2700}},
            ha,
            MagicMock(),
            _matcher("light.living"),
        )
        assert ha.calls[0][3] == {"color_temp_kelvin": 2700}

    @pytest.mark.asyncio
    async def test_media_volume_up_uses_native_service(self):
        ha = _FakeHA("media_player.tv", "on", {"supported_features": 1024}, post_state="on")
        result = await execute_media_action(
            {"action": "volume_up", "entity": "tv"},
            ha,
            MagicMock(),
            _matcher("media_player.tv"),
        )
        assert ha.calls[0][:2] == ("media_player", "volume_up")
        assert result["executed_command"]["service"] == "volume_up"

    @pytest.mark.asyncio
    async def test_media_volume_delta_is_relative_and_not_cacheable(self):
        ha = _FakeHA("media_player.tv", "on", {"volume_level": 0.4}, post_state="on")
        result = await execute_media_action(
            {"action": "set_volume", "entity": "tv", "parameters": {"volume_delta": 0.1}},
            ha,
            MagicMock(),
            _matcher("media_player.tv"),
        )
        assert ha.calls[0][3] == {"volume_level": 0.5}
        assert result["cacheable"] is False

    @pytest.mark.asyncio
    async def test_media_volume_percentage_is_normalized(self):
        ha = _FakeHA("media_player.tv", "on", post_state="on")
        await execute_media_action(
            {"action": "set_volume", "entity": "tv", "parameters": {"volume_level": 30}},
            ha,
            MagicMock(),
            _matcher("media_player.tv"),
        )
        assert ha.calls[0][3] == {"volume_level": 0.3}


# ---------------------------------------------------------------------------
# Item 10: satellite targeting visibility
# ---------------------------------------------------------------------------


@dataclass
class _SatEntry:
    entity_id: str
    friendly_name: str
    domain: str = "assist_satellite"
    area: str | None = None
    area_name: str | None = None
    device_class: str | None = None
    aliases: list[str] = field(default_factory=list)


class TestSatelliteTargeting:
    def test_unused_phrase_table_extractor_is_removed(self):
        assert not hasattr(satellite_targeting, "extract_explicit_satellite_target")
        assert not hasattr(satellite_targeting, "_EXPLICIT_TARGET_PATTERNS")

    @pytest.mark.asyncio
    async def test_invisible_satellite_is_not_resolved(self, monkeypatch):
        invalidate_visibility_rules_cache()
        monkeypatch.setattr(
            "app.entity.visibility.EntityVisibilityRepository.get_rules",
            AsyncMock(return_value=[{"rule_type": "domain_exclude", "rule_value": "assist_satellite"}]),
        )
        index = MagicMock()
        index.list_entries_async = AsyncMock(return_value=[_SatEntry("assist_satellite.kitchen", "Kitchen")])
        ha = MagicMock()
        ha.render_template = AsyncMock(return_value="device-1")

        target, error = await satellite_targeting.resolve_satellite_target_name(
            "Kitchen", entity_index=index, ha_client=ha, agent_id="timer-agent"
        )
        assert target is None
        assert error is not None and error.code == "not_found"

    @pytest.mark.asyncio
    async def test_visible_satellite_is_resolved(self):
        index = MagicMock()
        index.list_entries_async = AsyncMock(return_value=[_SatEntry("assist_satellite.kitchen", "Kitchen")])
        ha = MagicMock()
        ha.render_template = AsyncMock(return_value="device-1")

        target, error = await satellite_targeting.resolve_satellite_target_name(
            "Kitchen", entity_index=index, ha_client=ha, agent_id="timer-agent"
        )
        assert error is None
        assert target is not None and target.device_id == "device-1"
