"""Announcement regressions: fallback on failed announce, visible targeting, localization, missed timers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents import background_actions as ba
from app.models.agent import BackgroundEvent

pytestmark = pytest.mark.asyncio

_PROFILE = {"tts_enabled": True, "chime_enabled": False, "persistent_enabled": False, "push_enabled": False}


def _metadata(**overrides) -> SimpleNamespace:
    values = {
        "media_player_entity": None,
        "origin_device_id": "device-1",
        "origin_area": "kitchen",
        "duration": None,
        "language": "en",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def visible():
    with patch.object(ba, "entity_is_visible", new=AsyncMock(return_value=True)) as mock:
        yield mock


@pytest.fixture
def spawned():
    calls: list[tuple] = []

    def _spawn(coro, name=None):
        calls.append(coro.cr_frame.f_locals.get("media_player_entity"))
        coro.close()

    with patch.object(ba, "spawn", side_effect=_spawn):
        yield calls


class TestAnnounceFailureFallback:
    async def test_failed_satellite_announce_falls_back_to_media_player_tts(self, visible, spawned):
        with (
            patch.object(ba, "_load_notification_profile", new=AsyncMock(return_value=dict(_PROFILE))),
            patch.object(ba, "_resolve_satellite_from_origin_device", new=AsyncMock(return_value="assist_satellite.k")),
            patch.object(ba, "_resolve_timer_playback_target", new=AsyncMock(return_value="media_player.k")),
            patch.object(ba, "_notify_satellite_announce", new=AsyncMock(return_value=False)),
            patch.object(ba, "_notify_tts", new=AsyncMock(return_value=True)) as notify_tts,
        ):
            await ba.dispatch_alarm_notification(MagicMock(), "Wake", "agenthub_alarm:1", metadata=_metadata())
        notify_tts.assert_awaited_once()
        assert notify_tts.await_args.args[1] == "media_player.k"
        assert spawned == ["media_player.k"]

    async def test_no_follow_up_when_every_audio_target_fails(self, visible, spawned):
        with (
            patch.object(ba, "_load_notification_profile", new=AsyncMock(return_value=dict(_PROFILE))),
            patch.object(ba, "_resolve_satellite_from_origin_device", new=AsyncMock(return_value="assist_satellite.k")),
            patch.object(ba, "_resolve_timer_playback_target", new=AsyncMock(return_value="media_player.k")),
            patch.object(ba, "_notify_satellite_announce", new=AsyncMock(return_value=False)),
            patch.object(ba, "_notify_tts", new=AsyncMock(return_value=False)),
        ):
            await ba.dispatch_alarm_notification(MagicMock(), "Wake", "agenthub_alarm:1", metadata=_metadata())
        assert spawned == []

    async def test_follow_up_only_after_successful_satellite_announce(self, visible, spawned):
        with (
            patch.object(ba, "_load_notification_profile", new=AsyncMock(return_value=dict(_PROFILE))),
            patch.object(ba, "_resolve_satellite_from_origin_device", new=AsyncMock(return_value="assist_satellite.k")),
            patch.object(ba, "_resolve_timer_playback_target", new=AsyncMock()) as playback,
            patch.object(ba, "_notify_satellite_announce", new=AsyncMock(return_value=True)),
            patch.object(ba, "_notify_tts", new=AsyncMock()) as notify_tts,
        ):
            await ba.dispatch_alarm_notification(MagicMock(), "Wake", "agenthub_alarm:1", metadata=_metadata())
        playback.assert_not_awaited()
        notify_tts.assert_not_awaited()
        assert spawned == ["assist_satellite.k"]


class TestVisibleTargeting:
    async def test_area_scan_skips_invisible_and_is_deterministic(self):
        entries = [
            SimpleNamespace(entity_id="media_player.z_speaker", area="kitchen"),
            SimpleNamespace(entity_id="media_player.a_hidden", area="kitchen"),
            SimpleNamespace(entity_id="media_player.office", area="office"),
        ]
        index = MagicMock()
        index.list_entries_async = AsyncMock(return_value=entries)

        async def _visible(agent_id, entity_id, _index):
            assert agent_id == "timer-agent"
            return entity_id != "media_player.a_hidden"

        with patch.object(ba, "entity_is_visible", new=AsyncMock(side_effect=_visible)):
            got = await ba._resolve_media_player_from_area(MagicMock(), "Kitchen", entity_index=index)
        assert got == "media_player.z_speaker"

    async def test_no_state_scan_fallback_without_entity_index(self):
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(
            return_value=[{"entity_id": "assist_satellite.k", "attributes": {"area_id": "kitchen"}}]
        )
        assert await ba._resolve_satellite_device(ha_client, "kitchen", entity_index=None) is None
        ha_client.get_states.assert_not_awaited()

    async def test_invisible_origin_satellite_is_not_used(self):
        index = MagicMock()
        index.list_entries_async = AsyncMock(return_value=[])
        with (
            patch.object(ba, "entity_is_visible", new=AsyncMock(return_value=False)),
            patch.object(ba, "_resolve_satellite_from_origin_device", new=AsyncMock(return_value="assist_satellite.k")),
            patch.object(ba, "_resolve_media_player_from_origin_device", new=AsyncMock(return_value=None)),
        ):
            satellite, media_player = await ba._resolve_notification_audio_target(
                MagicMock(),
                media_player="media_player.explicit",
                origin_device_id="device-1",
                area="kitchen",
                entity_index=index,
                kind_label="Timer",
            )
        assert satellite is None
        assert media_player is None

    async def test_visibility_errors_fail_closed(self):
        with patch.object(ba, "entity_is_visible", new=AsyncMock(side_effect=RuntimeError("db down"))):
            assert await ba._announce_target_visible("media_player.k", MagicMock()) is False


class TestLocalization:
    async def test_tts_prompt_uses_configured_language_name_and_async_loader(self):
        with (
            patch.object(ba, "_load_prompt_path_async", new=AsyncMock(return_value="Speak {language}.")) as loader,
            patch("app.llm.client.complete", new=AsyncMock(return_value="Le minuteur est fini.")) as complete,
        ):
            result = await ba._generate_tts_message("pasta", None, None, "fr-FR", has_meaningful_name=True)
        assert result == "Le minuteur est fini."
        loader.assert_awaited_once()
        assert complete.await_args.kwargs["messages"][0]["content"] == "Speak French (Francais)."

    async def test_alarm_fallback_is_localized_through_rewrite(self):
        rewrite_agent = SimpleNamespace(rewrite=AsyncMock(return_value="Le reveil Wake sonne"))
        profile = dict(_PROFILE, tts_enabled=False, persistent_enabled=True)
        with (
            patch.object(ba, "_load_notification_profile", new=AsyncMock(return_value=profile)),
            patch.object(ba, "_get_rewrite_agent", return_value=rewrite_agent),
            patch.object(ba, "_notify_persistent", new=AsyncMock()) as persistent,
        ):
            await ba.dispatch_alarm_notification(MagicMock(), "Wake", "x", metadata=_metadata(language="fr"))
        assert rewrite_agent.rewrite.await_args.args[0] == "Alarm Wake has triggered"
        assert rewrite_agent.rewrite.await_args.kwargs["language"] == "fr"
        assert persistent.await_args.args[2] == "Le reveil Wake sonne"

    async def test_english_needs_no_rewrite_and_failure_falls_back_to_english(self):
        rewrite_agent = SimpleNamespace(rewrite=AsyncMock(side_effect=RuntimeError("llm down")))
        with patch.object(ba, "_get_rewrite_agent", return_value=rewrite_agent):
            assert await ba._localize_text("Timer done", "en-US") == "Timer done"
            rewrite_agent.rewrite.assert_not_awaited()
            assert await ba._localize_text("Timer done", "it") == "Timer done"


class TestMissedNotification:
    async def test_missed_event_produces_one_consolidated_notification(self):
        event = BackgroundEvent(
            event_type="timer_notification",
            payload={
                "missed": [
                    {"name": "pasta", "kind": "timer", "due_epoch": 1_790_000_000},
                    {"name": "Wake", "kind": "alarm", "due_epoch": 1_790_000_600},
                ],
                "origin_area": "kitchen",
                "language": "en",
                "timezone": "UTC",
            },
        )
        with patch.object(ba, "dispatch_text_notification", new=AsyncMock()) as notify:
            result = await ba.handle_background_event(event, ha_client=MagicMock())
        assert result == {"speech": ""}
        notify.assert_awaited_once()
        text = notify.await_args.kwargs["text"]
        assert "timer 'pasta'" in text and "alarm 'Wake'" in text
        assert notify.await_args.kwargs["metadata"].origin_area == "kitchen"
