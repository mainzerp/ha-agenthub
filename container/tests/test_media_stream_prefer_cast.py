"""media-agent: stream target preference for Google Cast (``media.stream_prefer_cast``).

Live facts (2026-11-01): one Android TV box appears as three media players,
each in its own HA device with the same manufacturer / model / name:
``androidtv_remote`` (friendly name "TV Wohnzimmer", device class ``tv``,
can power the box on, rejects ``camera.play_stream`` with "Invalid media
type: application/vnd.apple.mpegurl"), ``cast`` (plays the stream as video)
and ``music_assistant`` (device class ``speaker``, plays only the audio).
A stream requested on the Android TV entity must go to the Cast twin.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import media_executor
from app.agents.media_executor import execute_media_action
from app.analytics.tracer import SpanCollector
from app.entity.visibility import invalidate_visibility_rules_cache
from app.ha_client.rest import HARestClient, parse_media_player_identities
from tests.helpers import attach_expect_state_shim, make_entity_index_entry

_ATV = "media_player.box_r_4k_plus_8"
_CAST = "media_player.box_r_4k_plus"
_CAST_2 = "media_player.box_r_4k_plus_2"
_MASS = "media_player.box_r_4k_plus_5"
_OTHER_CAST = "media_player.kitchen_display"
_CAM = "camera.klingel_eingang"

# MediaPlayerEntityFeature: TURN_ON 128 | TURN_OFF 256 | PLAY_MEDIA 512 | VOLUME_SET 4
_PLAYER_FEATURES = 128 | 256 | 512 | 4
# CameraEntityFeature: ON_OFF 1 | STREAM 2
_CAMERA_FEATURES = 1 | 2

_BOX = {"manufacturer": "SEI Robotics", "model": "Box R 4K Plus", "name": "Box R 4K Plus"}
_INVALID_MEDIA_TYPE = RuntimeError("Invalid media type: application/vnd.apple.mpegurl")
# The real setting reader (the autouse ``prefer_cast`` fixture replaces it).
_read_prefer_cast = media_executor._stream_prefer_cast


# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------


def _rules(rules: list[dict[str, str]]):
    return AsyncMock(return_value=rules)


@pytest.fixture(autouse=True)
def _no_visibility_rules(monkeypatch):
    invalidate_visibility_rules_cache()
    monkeypatch.setattr("app.entity.visibility.EntityVisibilityRepository.get_rules", _rules([]))
    yield
    invalidate_visibility_rules_cache()


@pytest.fixture(autouse=True)
def _fast_waits(monkeypatch):
    from app.agents import action_executor as _ae

    async def _fast(key, *, default):
        return {
            "state_verify.ws_timeout_sec": 0.05,
            "state_verify.poll_interval_sec": 0.01,
            "state_verify.poll_max_sec": 0.05,
        }.get(key, default)

    monkeypatch.setattr(_ae, "_settings_float", _fast)
    monkeypatch.setattr(media_executor, "_POWER_ON_WAIT_SEC", 0.2)
    monkeypatch.setattr(media_executor, "_POWER_ON_POLL_SEC", 0.01)
    monkeypatch.setattr(media_executor, "_POWER_ON_WS_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(media_executor, "_STREAM_VERIFY_WS_TIMEOUT_SEC", 0.01)


@pytest.fixture(autouse=True)
def prefer_cast(monkeypatch):
    setting = AsyncMock(return_value=True)
    monkeypatch.setattr(media_executor, "_stream_prefer_cast", setting)
    return setting


def _identity(platform: str | None, **device) -> dict[str, Any]:
    return {
        "platform": platform,
        "integrations": [platform] if platform else [],
        "manufacturer": device.get("manufacturer"),
        "model": device.get("model"),
        "name": device.get("name"),
    }


def _box_identities(**overrides) -> dict[str, dict[str, Any]]:
    identities = {
        _ATV: _identity("androidtv_remote", **_BOX),
        _CAST: _identity("cast", **_BOX),
        _MASS: _identity("music_assistant", **_BOX),
        _OTHER_CAST: _identity("cast", manufacturer="Google", model="Nest Hub", name="Kitchen Display"),
    }
    identities.update(overrides)
    return identities


def _player(name: str, state: str = "on", **attrs) -> tuple[str, dict[str, Any]]:
    return state, {"friendly_name": name, "supported_features": _PLAYER_FEATURES, **attrs}


class _FakeHA:
    """HA double with per-entity states, recorded calls and an identity lookup."""

    def __init__(
        self,
        states: dict[str, tuple[str, dict[str, Any]]],
        identities: dict[str, Any] | None = None,
        *,
        stream_errors: dict[str, Exception] | None = None,
    ):
        self.states = {
            eid: {"entity_id": eid, "state": st, "attributes": dict(attrs)} for eid, (st, attrs) in states.items()
        }
        self.calls: list[tuple] = []
        self.stream_errors = stream_errors or {}
        # entity_id -> {other entity_id: new state} applied when entity_id is turned on.
        self.on_turn_on: dict[str, dict[str, str]] = {}
        self.get_media_player_identities = AsyncMock(return_value=identities if identities is not None else {})
        attach_expect_state_shim(self)

    async def get_state(self, entity_id: str):
        state = self.states.get(entity_id)
        return {**state, "attributes": dict(state["attributes"])} if state else None

    async def call_service(self, domain, service, entity_id=None, service_data=None, *, return_response=False):
        self.calls.append((domain, service, entity_id, service_data))
        player = (service_data or {}).get("media_player") if service == "play_stream" else entity_id
        if service in ("play_stream", "play_media") and player in self.stream_errors:
            raise self.stream_errors[player]
        if (domain, service) == ("media_player", "turn_on"):
            self.states[entity_id]["state"] = "on"
            for other, new_state in self.on_turn_on.get(entity_id, {}).items():
                self.states[other]["state"] = new_state
        return []


def _home_ha(
    *,
    atv: tuple[str, dict[str, Any]] | None = None,
    cast: tuple[str, dict[str, Any]] | None = None,
    extra: dict[str, tuple[str, dict[str, Any]]] | None = None,
    identities: dict[str, Any] | None = None,
    **kwargs,
) -> _FakeHA:
    states = {
        _ATV: atv or _player("TV Wohnzimmer", device_class="tv"),
        _CAST: cast or _player("Box R 4K Plus", "off"),
        _MASS: _player("Box R 4K Plus", "idle", device_class="speaker"),
        _OTHER_CAST: _player("Kitchen Display", "off"),
        _CAM: ("idle", {"friendly_name": "Klingel Eingang", "supported_features": _CAMERA_FEATURES}),
    }
    states.update(extra or {})
    return _FakeHA(states, _box_identities() if identities is None else identities, **kwargs)


def _index(*, without: tuple[str, ...] = (), cast_area: str = "wohnzimmer", extra_ids: tuple[str, ...] = ()):
    entries = [
        make_entity_index_entry(_ATV, "TV Wohnzimmer", area="wohnzimmer", device_class="tv"),
        make_entity_index_entry(_CAST, "Box R 4K Plus", area=cast_area),
        make_entity_index_entry(_MASS, "Box R 4K Plus", area="wohnzimmer", device_class="speaker"),
        make_entity_index_entry(_OTHER_CAST, "Kitchen Display", area="kuche"),
        make_entity_index_entry(_CAM, "Klingel Eingang", area="eingang"),
    ]
    entries += [make_entity_index_entry(eid, "Box R 4K Plus", area="wohnzimmer") for eid in extra_ids]
    entries = [e for e in entries if e.entity_id not in without]
    by_id = {e.entity_id: e for e in entries}
    index = MagicMock()

    async def _list(domains=None):
        return [e for e in entries if domains is None or e.domain in domains]

    index.list_entries_async = AsyncMock(side_effect=_list)
    index.get_by_id = MagicMock(side_effect=lambda eid: by_id.get(eid))
    index.get_by_id_async = AsyncMock(side_effect=lambda eid: by_id.get(eid))
    return index


async def _play_camera(ha, index=None, *, entity="TV Wohnzimmer", span_collector=None):
    return await execute_media_action(
        {"action": "play_camera", "entity": entity, "parameters": {"camera": "Klingel Eingang"}},
        ha,
        index or _index(),
        None,
        agent_id="media-agent",
        span_collector=span_collector,
    )


async def _play_media(ha, content_id, content_type, *, entity="TV Wohnzimmer"):
    return await execute_media_action(
        {
            "action": "play_media",
            "entity": entity,
            "parameters": {"media_content_id": content_id, "media_content_type": content_type},
        },
        ha,
        _index(),
        None,
        agent_id="media-agent",
    )


def _stream_call(player: str) -> tuple:
    return ("camera", "play_stream", _CAM, {"media_player": player, "format": "hls"})


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


async def test_android_tv_target_streams_camera_via_its_cast_twin():
    ha = _home_ha()
    collector = SpanCollector("trace-prefer-cast")
    result = await _play_camera(ha, span_collector=collector)

    assert result["success"] is True
    assert ha.calls == [_stream_call(_CAST)]
    # The answer names the device the user asked for, never the twin.
    assert result["entity_id"] == _ATV
    assert "TV Wohnzimmer" in result["speech"]
    assert _CAST not in result["speech"] and "Box R 4K Plus" not in result["speech"]
    assert result["executed_command"]["service_data"] == {"media_player": _CAST, "format": "hls"}
    record = {"original_entity_id": _ATV, "substitute_entity_id": _CAST, "reason": "prefer_cast"}
    assert result["metadata"]["stream_target_substituted"] == record
    spans = [s for s in collector.get_spans() if s["span_name"] == "stream_target_substituted"]
    assert len(spans) == 1
    assert spans[0]["metadata"] == record
    assert spans[0]["agent_id"] == "media-agent"


async def test_cast_target_is_used_as_is():
    ha = _home_ha()
    result = await _play_camera(ha, entity="Kitchen Display")

    assert result["success"] is True
    assert ha.calls[-1] == _stream_call(_OTHER_CAST)
    assert "stream_target_substituted" not in result["metadata"]


async def test_original_on_and_cast_idle_streams_without_any_power_on():
    # Cast reports "off" while no app runs; the box is on (Android TV entity).
    ha = _home_ha()
    await _play_camera(ha)
    assert [call for call in ha.calls if call[1] == "turn_on"] == []


async def test_original_off_is_powered_on_then_streamed_via_twin():
    ha = _home_ha(atv=_player("TV Wohnzimmer", "off", device_class="tv"))
    result = await _play_camera(ha)

    assert result["success"] is True
    assert ha.calls == [("media_player", "turn_on", _ATV, None), _stream_call(_CAST)]
    assert "turned on TV Wohnzimmer" in result["speech"]


async def test_two_cast_twins_keep_the_original_target():
    identities = _box_identities(**{_CAST_2: _identity("cast", **_BOX)})
    ha = _home_ha(identities=identities, extra={_CAST_2: _player("Box R 4K Plus", "off")})
    result = await execute_media_action(
        {"action": "play_camera", "entity": "TV Wohnzimmer", "parameters": {"camera": "Klingel Eingang"}},
        ha,
        _index(extra_ids=(_CAST_2,)),
        None,
        agent_id="media-agent",
    )

    assert ha.calls == [_stream_call(_ATV)]
    assert "stream_target_substituted" not in (result.get("metadata") or {})


async def test_unavailable_twin_wakes_original_then_waits_for_twin():
    ha = _home_ha(
        atv=_player("TV Wohnzimmer", "standby", device_class="tv"),
        cast=("unavailable", {"friendly_name": "Box R 4K Plus"}),
    )
    # The Cast entity comes back once the box is awake.
    ha.on_turn_on[_ATV] = {_CAST: "off"}
    result = await _play_camera(ha)

    assert result["success"] is True
    assert ha.calls == [("media_player", "turn_on", _ATV, None), _stream_call(_CAST)]
    assert result["metadata"]["powered_on"] is True
    assert result["metadata"]["stream_target_substituted"]["substitute_entity_id"] == _CAST


async def test_unavailable_twin_that_stays_unavailable_streams_nothing():
    ha = _home_ha(
        atv=_player("TV Wohnzimmer", "off", device_class="tv"),
        cast=("unavailable", {"friendly_name": "Box R 4K Plus"}),
    )
    result = await _play_camera(ha)

    assert result["success"] is False
    assert ha.calls == [("media_player", "turn_on", _ATV, None)]
    assert result["metadata"]["refusal_reason"] == "cast_target_not_ready"
    assert "TV Wohnzimmer" in result["speech"]


async def test_unavailable_twin_with_original_on_keeps_original():
    ha = _home_ha(cast=("unavailable", {"friendly_name": "Box R 4K Plus"}))
    await _play_camera(ha)
    assert ha.calls == [_stream_call(_ATV)]


async def test_twin_excluded_by_visibility_is_never_chosen(monkeypatch):
    invalidate_visibility_rules_cache()
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        _rules(
            [
                {"rule_type": "domain_include", "rule_value": "media_player"},
                {"rule_type": "domain_include", "rule_value": "camera"},
                {"rule_type": "area_exclude", "rule_value": "versteckt"},
            ]
        ),
    )
    ha = _home_ha()
    await _play_camera(ha, _index(cast_area="versteckt"))
    assert ha.calls == [_stream_call(_ATV)]


async def test_twin_missing_from_entity_index_is_never_chosen():
    # Hidden or disabled HA entities are not indexed.
    ha = _home_ha()
    await _play_camera(ha, _index(without=(_CAST,)))
    assert ha.calls == [_stream_call(_ATV)]


async def test_music_assistant_speaker_is_never_the_substitute():
    # Even when the registry wrongly reports it as a Cast entity.
    identities = _box_identities(**{_CAST: _identity("androidtv_remote", **_BOX), _MASS: _identity("cast", **_BOX)})
    ha = _home_ha(identities=identities)
    await _play_camera(ha)
    assert ha.calls == [_stream_call(_ATV)]


async def test_lookup_failure_keeps_todays_behavior():
    ha = _home_ha()
    ha.get_media_player_identities = AsyncMock(side_effect=RuntimeError("template failed"))
    result = await _play_camera(ha)

    assert result["success"] is True
    assert ha.calls == [_stream_call(_ATV)]


async def test_empty_lookup_keeps_todays_behavior():
    ha = _home_ha(identities={})
    await _play_camera(ha)
    assert ha.calls == [_stream_call(_ATV)]


async def test_setting_off_keeps_todays_behavior(prefer_cast):
    prefer_cast.return_value = False
    ha = _home_ha()
    await _play_camera(ha)
    assert ha.calls == [_stream_call(_ATV)]


@pytest.mark.parametrize("missing", ["manufacturer", "model", "name"])
async def test_incomplete_device_identity_never_matches(missing):
    device = {**_BOX, missing: ""}
    identities = _box_identities(**{_ATV: _identity("androidtv_remote", **device), _CAST: _identity("cast", **device)})
    ha = _home_ha(identities=identities)
    await _play_camera(ha)
    assert ha.calls == [_stream_call(_ATV)]


async def test_identity_match_ignores_case_and_whitespace():
    device = {"manufacturer": " sei robotics", "model": "BOX R 4K PLUS", "name": "box r 4k plus "}
    ha = _home_ha(identities=_box_identities(**{_CAST: _identity("cast", **device)}))
    await _play_camera(ha)
    assert ha.calls == [_stream_call(_CAST)]


async def test_different_device_cast_entity_never_matches():
    identities = _box_identities(
        **{_CAST: _identity("cast", manufacturer="SEI Robotics", model="Box R 4K Plus", name="Bedroom Box")}
    )
    ha = _home_ha(identities=identities)
    await _play_camera(ha)
    assert ha.calls == [_stream_call(_ATV)]


# ---------------------------------------------------------------------------
# play_media
# ---------------------------------------------------------------------------


async def test_play_media_with_camera_content_id_uses_the_twin():
    ha = _home_ha()
    result = await _play_media(ha, "Klingel Eingang", "video")

    assert result["success"] is True
    assert ha.calls == [_stream_call(_CAST)]


async def test_play_media_video_url_uses_the_twin():
    ha = _home_ha()
    result = await _play_media(ha, "http://example.com/clip.mp4", "video")

    assert result["success"] is True
    assert ha.calls == [
        (
            "media_player",
            "play_media",
            _CAST,
            {"media_content_id": "http://example.com/clip.mp4", "media_content_type": "video"},
        )
    ]
    assert result["entity_id"] == _ATV
    assert result["cacheable"] is False
    assert result["executed_command"]["entity_id"] == _CAST


async def test_play_media_hls_url_uses_the_twin():
    ha = _home_ha()
    await _play_media(ha, "http://example.com/live/index.m3u8?token=1", "url")
    assert ha.calls[-1][2] == _CAST


@pytest.mark.parametrize(
    ("content_id", "content_type"),
    [
        ("http://example.com/song.mp3", "music"),
        ("http://example.com/song.mp3", "audio/mpeg"),
        ("http://radio.example.com/live.m3u8", "music"),
    ],
)
async def test_play_media_audio_is_never_substituted(content_id, content_type):
    ha = _home_ha()
    result = await _play_media(ha, content_id, content_type)

    assert result["success"] is True
    assert [call[2] for call in ha.calls] == [_ATV]
    assert "metadata" not in result


# ---------------------------------------------------------------------------
# "Invalid media type" hint
# ---------------------------------------------------------------------------


async def test_invalid_media_type_names_the_cast_player_when_not_substituted(prefer_cast):
    prefer_cast.return_value = False
    ha = _home_ha(stream_errors={_ATV: _INVALID_MEDIA_TYPE})
    result = await _play_camera(ha)

    assert result["success"] is False
    assert result["metadata"]["refusal_reason"] == "stream_media_type_rejected"
    assert "Google Cast player Box R 4K Plus" in result["speech"]
    assert "application/vnd.apple.mpegurl" not in result["speech"]
    assert _CAST not in result["speech"]


async def test_invalid_media_type_without_twin_says_a_cast_player_would_work():
    ha = _home_ha(identities={}, stream_errors={_ATV: _INVALID_MEDIA_TYPE})
    result = await _play_camera(ha)

    assert result["metadata"]["refusal_reason"] == "stream_media_type_rejected"
    assert "A Google Cast player of the same device would work" in result["speech"]
    assert "Invalid media type" not in result["speech"]


async def test_invalid_media_type_hint_never_names_an_invisible_twin(prefer_cast):
    prefer_cast.return_value = False
    ha = _home_ha(stream_errors={_ATV: _INVALID_MEDIA_TYPE})
    result = await _play_camera(ha, _index(without=(_CAST,)))

    assert result["metadata"]["refusal_reason"] == "stream_media_type_rejected"
    assert "Box R 4K Plus" not in result["speech"]
    assert "did not find one" in result["speech"]


async def test_invalid_media_type_hint_reads_the_http_response_body():
    class _Response:
        text = '{"message": "Invalid media type: application/vnd.apple.mpegurl"}'

    error = RuntimeError("Server error '500 Internal Server Error'")
    error.response = _Response()  # type: ignore[attr-defined]
    ha = _home_ha(identities={}, stream_errors={_ATV: error})
    result = await _play_camera(ha)

    assert result["metadata"]["refusal_reason"] == "stream_media_type_rejected"


async def test_other_stream_error_keeps_the_generic_answer():
    ha = _home_ha(identities={}, stream_errors={_ATV: RuntimeError("Server error '500'")})
    result = await _play_camera(ha)

    assert result["metadata"]["refusal_reason"] == "stream_failed"
    assert "HLS" in result["speech"]


async def test_failed_stream_on_the_cast_twin_gives_no_cast_hint():
    ha = _home_ha(stream_errors={_CAST: _INVALID_MEDIA_TYPE})
    result = await _play_camera(ha)

    assert result["metadata"]["refusal_reason"] == "stream_failed"
    assert result["metadata"]["stream_target_substituted"]["substitute_entity_id"] == _CAST
    assert "Google Cast" not in result["speech"]


async def test_play_media_video_invalid_media_type_gets_the_hint(prefer_cast):
    prefer_cast.return_value = False
    ha = _home_ha(stream_errors={_ATV: _INVALID_MEDIA_TYPE})
    result = await _play_media(ha, "http://example.com/live/index.m3u8", "application/vnd.apple.mpegurl")

    assert result["metadata"]["refusal_reason"] == "stream_media_type_rejected"
    assert "Box R 4K Plus" in result["speech"]


# ---------------------------------------------------------------------------
# Setting and registry lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("1", True), ("false", False), ("0", False)])
async def test_setting_values(monkeypatch, raw, expected):
    monkeypatch.setattr("app.agents.media_executor.SettingsRepository.get_value", AsyncMock(return_value=raw))
    assert await _read_prefer_cast() is expected


async def test_setting_read_failure_defaults_to_on(monkeypatch):
    monkeypatch.setattr(
        "app.agents.media_executor.SettingsRepository.get_value", AsyncMock(side_effect=RuntimeError("no db"))
    )
    assert await _read_prefer_cast() is True


def test_parse_media_player_identities_with_realistic_template_output():
    # Shape of MEDIA_PLAYER_IDENTITY_TEMPLATE output (``tojson``) on the live home.
    rows = json.loads(
        json.dumps(
            [
                {
                    "id": _ATV,
                    "cast": False,
                    "integrations": ["androidtv_remote"],
                    "manufacturer": "SEI Robotics",
                    "model": "Box R 4K Plus",
                    "name": "Box R 4K Plus",
                },
                {
                    "id": _CAST,
                    "cast": True,
                    "integrations": ["cast"],
                    "manufacturer": "SEI Robotics",
                    "model": "Box R 4K Plus",
                    "name": "Box R 4K Plus",
                },
                {
                    "id": _MASS,
                    "cast": False,
                    "integrations": ["music_assistant"],
                    "manufacturer": "SEI Robotics",
                    "model": "Box R 4K Plus",
                    "name": "Box R 4K Plus",
                },
                {"id": "media_player.multi", "cast": False, "integrations": ["sonos", "upnp"], "model": None},
                {"id": "media_player.no_device", "cast": False},
                {"id": "light.not_a_player", "cast": False},
                "garbage",
            ]
        )
    )
    parsed = parse_media_player_identities(rows)

    assert parsed[_ATV] == {
        "platform": "androidtv_remote",
        "integrations": ["androidtv_remote"],
        "manufacturer": "SEI Robotics",
        "model": "Box R 4K Plus",
        "name": "Box R 4K Plus",
    }
    assert parsed[_CAST]["platform"] == "cast"
    assert parsed[_MASS]["platform"] == "music_assistant"
    assert parsed["media_player.multi"]["platform"] is None
    assert parsed["media_player.multi"]["integrations"] == ["sonos", "upnp"]
    assert parsed["media_player.no_device"] == {
        "platform": None,
        "integrations": [],
        "manufacturer": None,
        "model": None,
        "name": None,
    }
    assert "light.not_a_player" not in parsed


async def test_rest_client_identity_lookup_is_cached_and_fails_soft():
    client = HARestClient()
    client.render_template = AsyncMock(return_value=None)
    assert await client.get_media_player_identities() == {}

    rendered = json.dumps([{"id": _CAST, "cast": True, "integrations": ["cast"], **_BOX}])
    client.render_template = AsyncMock(return_value=rendered)
    first = await client.get_media_player_identities()
    second = await client.get_media_player_identities()

    assert first[_CAST]["platform"] == "cast"
    assert second == first
    client.render_template.assert_awaited_once()
    template = client.render_template.await_args.args[0]
    assert "integration_entities('cast')" in template
    assert "states.media_player" in template
