"""media-agent: camera live picture on a screen (``play_camera``) and checked ``play_media``.

Live trace 2026-10-24 (4c09c7bb8a454c38 / 4b51ce5b47c44d55): "Kamerabild
Klingel Eingang auf den Fernseher legen" ended in
``media_player.play_media`` with ``media_content_id="Klingel Eingang"`` and
a bare "play media failed". The camera must be resolved like every other
entity and streamed with ``camera.play_stream`` (target: the camera; data:
``media_player`` + ``format: hls``), and an incompatible target must be
refused with the reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from app.agents import media_executor
from app.agents.action_executor import (
    parse_actions,
    reset_request_candidate_ids,
    set_request_candidate_ids,
)
from app.agents.actionable import MediaAgent
from app.agents.media_executor import execute_media_action
from app.db.schema import _create_indexes, _create_tables, _seed_defaults
from app.db.schema._migrations import _migrate_to_55, _run_migrations
from app.entity.visibility import invalidate_visibility_rules_cache
from tests.helpers import (
    attach_expect_state_shim,
    make_dispatch_task,
    make_entity_index_entry,
    shutdown_aiosqlite,
)

_TV = "media_player.wohnzimmer_tv"
_SPEAKER = "media_player.kuche_lautsprecher"
_CAM = "camera.klingel_eingang"
_CAM_SWAPPED = "camera.eingang_klingel"

# MediaPlayerEntityFeature: TURN_ON 128 | TURN_OFF 256 | PLAY_MEDIA 512 | VOLUME_SET 4
_TV_FEATURES = 128 | 256 | 512 | 4
_SPEAKER_FEATURES = 128 | 256 | 512 | 4
# CameraEntityFeature: ON_OFF 1 | STREAM 2
_CAMERA_FEATURES = 1 | 2


# ---------------------------------------------------------------------------
# Fixtures and doubles
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


def _index(entries):
    by_id = {e.entity_id: e for e in entries}
    index = MagicMock()

    async def _list(domains=None):
        return [e for e in entries if domains is None or e.domain in domains]

    index.list_entries_async = AsyncMock(side_effect=_list)
    index.get_by_id = MagicMock(side_effect=lambda eid: by_id.get(eid))
    index.get_by_id_async = AsyncMock(side_effect=lambda eid: by_id.get(eid))
    return index


def _home_index(*, with_swapped_camera: bool = True):
    entries = [
        make_entity_index_entry(_TV, "Fernseher Wohnzimmer", area="wohnzimmer"),
        make_entity_index_entry(_SPEAKER, "Küche Lautsprecher", area="kuche"),
        make_entity_index_entry(_CAM, "Klingel Eingang", area="eingang"),
    ]
    if with_swapped_camera:
        entries.append(make_entity_index_entry(_CAM_SWAPPED, "Eingang Klingel", area="eingang"))
    return _index(entries)


class _FakeHA:
    """Multi-entity HA double: per-entity states, recorded calls, optional transitions."""

    def __init__(self, states: dict[str, tuple[str, dict[str, Any]]], *, call_error: Exception | None = None):
        self.states = {
            eid: {"entity_id": eid, "state": st, "attributes": dict(attrs)} for eid, (st, attrs) in states.items()
        }
        self.calls: list[tuple] = []
        self.turn_on_result: str | None = "on"
        self.call_error = call_error
        attach_expect_state_shim(self)

    async def get_state(self, entity_id: str):
        state = self.states.get(entity_id)
        return dict(state) if state else None

    async def call_service(self, domain, service, entity_id=None, service_data=None, *, return_response=False):
        self.calls.append((domain, service, entity_id, service_data))
        if self.call_error is not None and service == "play_stream":
            raise self.call_error
        if (domain, service) == ("media_player", "turn_on") and self.turn_on_result is not None:
            self.states[entity_id]["state"] = self.turn_on_result
        return []


def _tv(state: str = "on", **attrs) -> tuple[str, dict[str, Any]]:
    return state, {"friendly_name": "Fernseher Wohnzimmer", "supported_features": _TV_FEATURES, **attrs}


def _home_ha(tv=None, camera_features: int = _CAMERA_FEATURES, **kwargs) -> _FakeHA:
    return _FakeHA(
        {
            _TV: tv or _tv(),
            _SPEAKER: (
                "idle",
                {
                    "friendly_name": "Küche Lautsprecher",
                    "supported_features": _SPEAKER_FEATURES,
                    "device_class": "speaker",
                },
            ),
            _CAM: ("idle", {"friendly_name": "Klingel Eingang", "supported_features": camera_features}),
            _CAM_SWAPPED: ("idle", {"friendly_name": "Eingang Klingel", "supported_features": _CAMERA_FEATURES}),
        },
        **kwargs,
    )


async def _play_camera(ha, index, *, entity="Fernseher Wohnzimmer", camera="Klingel Eingang", **params):
    parameters = {"camera": camera, **params} if camera is not None else dict(params)
    return await execute_media_action(
        {"action": "play_camera", "entity": entity, "parameters": parameters},
        ha,
        index,
        None,
        agent_id="media-agent",
    )


# ---------------------------------------------------------------------------
# play_camera
# ---------------------------------------------------------------------------


async def test_play_camera_streams_to_compatible_target():
    ha = _home_ha()
    result = await _play_camera(ha, _home_index())

    assert result["success"] is True
    assert ha.calls == [("camera", "play_stream", _CAM, {"media_player": _TV, "format": "hls"})]
    assert result["entity_id"] == _TV
    assert result["executed_command"] == {
        "domain": "camera",
        "service": "play_stream",
        "entity_id": _CAM,
        "service_data": {"media_player": _TV, "format": "hls"},
    }
    assert result["cacheable"] is False
    assert "Klingel Eingang" in result["speech"]
    assert "Fernseher Wohnzimmer" in result["speech"]


async def test_exact_camera_name_wins_over_swapped_name():
    # "Klingel Eingang" and "Eingang Klingel" both exist; the exact name is selected.
    ha = _home_ha()
    await _play_camera(ha, _home_index(), camera="Eingang Klingel")
    assert ha.calls[0][2] == _CAM_SWAPPED


async def test_audio_only_speaker_is_refused_with_reason():
    ha = _home_ha()
    result = await _play_camera(ha, _home_index(), entity="Küche Lautsprecher")

    assert result["success"] is False
    assert ha.calls == []
    assert "audio-only speaker" in result["speech"]
    assert "Klingel Eingang" in result["speech"]
    assert result["metadata"]["refusal_reason"] == "target_audio_only"
    assert result["entity_id"] == _SPEAKER


async def test_target_without_play_media_feature_is_refused():
    ha = _home_ha(tv=_tv(supported_features=128 | 256 | 4))
    result = await _play_camera(ha, _home_index())

    assert result["success"] is False
    assert ha.calls == []
    assert "does not support playing media" in result["speech"]


async def test_unavailable_target_is_refused():
    ha = _home_ha(tv=_tv("unavailable"))
    result = await _play_camera(ha, _home_index())

    assert result["success"] is False
    assert ha.calls == []
    assert "unavailable" in result["speech"]


async def test_camera_without_stream_support_is_refused():
    ha = _home_ha(camera_features=1)
    result = await _play_camera(ha, _home_index())

    assert result["success"] is False
    assert ha.calls == []
    assert "does not provide a video stream" in result["speech"]


async def test_target_off_is_turned_on_then_streamed():
    ha = _home_ha(tv=_tv("off"))
    result = await _play_camera(ha, _home_index())

    assert result["success"] is True
    assert ha.calls == [
        ("media_player", "turn_on", _TV, None),
        ("camera", "play_stream", _CAM, {"media_player": _TV, "format": "hls"}),
    ]
    assert "turned on" in result["speech"]
    assert result["metadata"]["powered_on"] is True


async def test_target_that_does_not_turn_on_in_time_is_not_streamed():
    ha = _home_ha(tv=_tv("standby"))
    ha.turn_on_result = None  # the TV stays in standby
    result = await _play_camera(ha, _home_index())

    assert result["success"] is False
    assert ha.calls == [("media_player", "turn_on", _TV, None)]
    assert "did not report being on" in result["speech"]


async def test_stream_error_names_the_likely_reason_without_internals():
    error = RuntimeError("Server error '500' for url 'http://homeassistant.local:8123/api/services/camera/play_stream'")
    ha = _home_ha(call_error=error)
    result = await _play_camera(ha, _home_index())

    assert result["success"] is False
    assert "HLS" in result["speech"]
    assert "stream integration" in result["speech"]
    assert "http" not in result["speech"]
    assert "500" not in result["speech"]


async def test_unknown_camera_asks_and_lists_cameras():
    ha = _home_ha()
    result = await _play_camera(ha, _home_index(), camera="Garten")

    assert result["success"] is False
    assert ha.calls == []
    assert result["voice_followup"] is True
    assert "Garten" in result["speech"]
    assert "Eingang Klingel" in result["speech"] and "Klingel Eingang" in result["speech"]
    assert result["speech"].rstrip().endswith("?")


async def test_ambiguous_camera_asks_listing_both():
    ha = _home_ha()
    result = await _play_camera(ha, _home_index(), camera="Klingel")

    assert result["success"] is False
    assert ha.calls == []
    assert result["voice_followup"] is True
    assert result["metadata"]["refusal_reason"] == "camera_ambiguous"
    assert "Eingang Klingel or Klingel Eingang" in result["speech"]


async def test_missing_camera_parameter_asks_which_camera():
    ha = _home_ha()
    result = await _play_camera(ha, _home_index(), camera=None)

    assert result["voice_followup"] is True
    assert ha.calls == []
    assert "Which camera" in result["speech"]


async def test_camera_is_resolved_from_its_name_while_the_target_gate_is_active():
    # Cameras are not recall candidates: the candidate gate (players only)
    # guards the target, the camera goes through deterministic resolution.
    # An entity_id written as the camera name only matches the exact-id stage.
    ha = _home_ha()
    token = set_request_candidate_ids({_TV})
    try:
        result = await _play_camera(ha, _home_index(), camera=_CAM)
    finally:
        reset_request_candidate_ids(token)

    assert result["success"] is True
    assert ha.calls[0][:3] == ("camera", "play_stream", _CAM)


async def test_target_outside_candidate_gate_is_rejected():
    ha = _home_ha()
    token = set_request_candidate_ids({_SPEAKER})
    try:
        result = await execute_media_action(
            {"action": "play_camera", "entity": "TV", "entity_id": _TV, "parameters": {"camera": "Klingel Eingang"}},
            ha,
            _home_index(),
            None,
            agent_id="media-agent",
        )
    finally:
        reset_request_candidate_ids(token)

    assert result["success"] is False
    assert result["entity_id"] is None
    assert ha.calls == []


async def test_camera_hidden_by_visibility_rules_is_not_resolved(monkeypatch):
    invalidate_visibility_rules_cache()
    monkeypatch.setattr(
        "app.entity.visibility.EntityVisibilityRepository.get_rules",
        AsyncMock(return_value=[{"rule_type": "domain_include", "rule_value": "media_player"}]),
    )
    ha = _home_ha()
    result = await _play_camera(ha, _home_index())

    assert result["success"] is False
    assert ha.calls == []
    assert "could not find a camera" in result["speech"]


async def test_camera_is_never_a_media_player_target():
    ha = _home_ha()
    result = await execute_media_action(
        {"action": "turn_off", "entity": "Klingel Eingang", "entity_id": _CAM},
        ha,
        _home_index(),
        None,
        agent_id="media-agent",
    )
    assert result["success"] is False
    assert ha.calls == []


# ---------------------------------------------------------------------------
# play_media
# ---------------------------------------------------------------------------


async def _play_media(ha, index, content_id, content_type="video", entity="Fernseher Wohnzimmer"):
    params = {"media_content_id": content_id}
    if content_type is not None:
        params["media_content_type"] = content_type
    return await execute_media_action(
        {"action": "play_media", "entity": entity, "parameters": params},
        ha,
        index,
        None,
        agent_id="media-agent",
    )


async def test_play_media_with_camera_name_streams_the_camera():
    # The exact failure of the live trace: a camera name as media_content_id.
    ha = _home_ha()
    result = await _play_media(ha, _home_index(), "Klingel Eingang")

    assert result["success"] is True
    assert ha.calls == [("camera", "play_stream", _CAM, {"media_player": _TV, "format": "hls"})]


async def test_play_media_url_on_compatible_target():
    ha = _home_ha()
    result = await _play_media(ha, _home_index(), "http://example.com/clip.mp4")

    assert result["success"] is True
    assert ha.calls == [
        (
            "media_player",
            "play_media",
            _TV,
            {"media_content_id": "http://example.com/clip.mp4", "media_content_type": "video"},
        )
    ]


async def test_play_media_video_on_speaker_is_refused_but_music_runs():
    ha = _home_ha()
    refused = await _play_media(ha, _home_index(), "http://example.com/clip.mp4", entity="Küche Lautsprecher")
    assert refused["success"] is False
    assert "audio-only speaker" in refused["speech"]
    assert ha.calls == []

    played = await _play_media(
        ha, _home_index(), "http://example.com/song.mp3", content_type="music", entity="Küche Lautsprecher"
    )
    assert played["success"] is True
    assert ha.calls[0][1] == "play_media"


async def test_play_media_without_feature_is_refused():
    ha = _home_ha(tv=_tv(supported_features=128 | 256))
    result = await _play_media(ha, _home_index(), "media-source://media_source/local/clip.mp4")

    assert result["success"] is False
    assert ha.calls == []
    assert "does not support playing media" in result["speech"]


async def test_play_media_without_content_asks():
    ha = _home_ha()
    result = await _play_media(ha, _home_index(), "", content_type=None)

    assert result["voice_followup"] is True
    assert ha.calls == []


async def test_play_media_failure_says_why():
    ha = _home_ha()

    async def _boom(domain, service, entity_id=None, service_data=None, *, return_response=False):
        ha.calls.append((domain, service, entity_id, service_data))
        raise RuntimeError("Server error '500' for url 'http://ha/api/services/media_player/play_media'")

    ha.call_service = _boom
    result = await _play_media(ha, _home_index(), "http://example.com/clip.mp4")

    assert result["success"] is False
    assert "may not support this content or format" in result["speech"]
    assert "http" not in result["speech"].replace("Home Assistant", "")


# ---------------------------------------------------------------------------
# Agent and parser integration
# ---------------------------------------------------------------------------


def test_play_camera_block_parses_with_parameters():
    response = (
        "```json\n"
        '{"action": "play_camera", "entity": "Fernseher Wohnzimmer", "entity_id": "media_player.wohnzimmer_tv", '
        '"parameters": {"camera": "Klingel Eingang"}}\n'
        "```"
    )
    actions = parse_actions(response)
    assert len(actions) == 1
    assert actions[0]["action"] == "play_camera"
    assert actions[0]["entity_id"] == "media_player.wohnzimmer_tv"
    assert actions[0]["parameters"] == {"camera": "Klingel Eingang"}


def test_play_camera_without_target_is_not_parsed():
    assert parse_actions('```json\n{"action": "play_camera", "entity": "", "parameters": {"camera": "x"}}\n```') == []


def test_media_agent_declares_camera_domain_and_action():
    assert "play_camera" in MediaAgent._entity_actions
    # Cameras are stream sources only: never recall candidates or action targets.
    assert MediaAgent._agent_meta["allowed_domains"] == frozenset({"media_player"})


async def test_agent_keeps_camera_question_and_requests_followup():
    ha = _home_ha()
    agent = MediaAgent(ha_client=ha, entity_index=_home_index(), entity_matcher=None)
    agent._generate_not_found_speech = AsyncMock(return_value="generic question?")
    action = {"action": "play_camera", "entity": "Fernseher Wohnzimmer", "parameters": {"camera": "Klingel"}}

    result = await agent._execute_parsed_action(action, make_dispatch_task("Zeig Klingel"), "media-agent")

    agent._generate_not_found_speech.assert_not_awaited()
    assert "Klingel Eingang" in result["speech"]
    assert agent._result_requests_voice_followup(result) is True


# ---------------------------------------------------------------------------
# Visibility seed and migration 55
# ---------------------------------------------------------------------------


async def _fresh_install(path: Path) -> None:
    db = await aiosqlite.connect(str(path))
    try:
        await _create_tables(db)
        await _create_indexes(db)
        await _seed_defaults(db)
        await _run_migrations(db)
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)


async def _media_rules(path: Path) -> set[tuple[str, str]]:
    db = await aiosqlite.connect(str(path))
    try:
        cursor = await db.execute(
            "SELECT rule_type, rule_value FROM entity_visibility_rules WHERE agent_id = 'media-agent'"
        )
        return {(row[0], row[1]) for row in await cursor.fetchall()}
    finally:
        await shutdown_aiosqlite(db)


async def _at_version_54(path: Path, rules: set[tuple[str, str]]) -> None:
    await _fresh_install(path)
    db = await aiosqlite.connect(str(path))
    try:
        await db.execute("DELETE FROM entity_visibility_rules WHERE agent_id = 'media-agent'")
        await db.executemany(
            "INSERT INTO entity_visibility_rules (agent_id, rule_type, rule_value) VALUES ('media-agent', ?, ?)",
            sorted(rules),
        )
        await db.execute("DELETE FROM schema_version WHERE version > 54")
        await db.commit()
        await _run_migrations(db)
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)


async def test_fresh_install_lets_media_agent_see_cameras(tmp_path: Path):
    path = tmp_path / "fresh.db"
    await _fresh_install(path)
    assert await _media_rules(path) == {("domain_include", "media_player"), ("domain_include", "camera")}


async def test_migration_55_adds_camera_to_default_rules(tmp_path: Path):
    path = tmp_path / "old.db"
    await _at_version_54(path, {("domain_include", "media_player")})
    assert await _media_rules(path) == {("domain_include", "media_player"), ("domain_include", "camera")}


async def test_migration_55_respects_admin_choices(tmp_path: Path):
    excluded = tmp_path / "excluded.db"
    rules = {("domain_include", "media_player"), ("domain_exclude", "camera")}
    await _at_version_54(excluded, rules)
    assert await _media_rules(excluded) == rules

    unrestricted = tmp_path / "unrestricted.db"
    await _at_version_54(unrestricted, {("area_exclude", "garage")})
    assert await _media_rules(unrestricted) == {("area_exclude", "garage")}


async def test_migration_55_is_idempotent(tmp_path: Path):
    path = tmp_path / "twice.db"
    await _fresh_install(path)
    db = await aiosqlite.connect(str(path))
    try:
        await _migrate_to_55(db)
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)
    assert len(await _media_rules(path)) == 2
