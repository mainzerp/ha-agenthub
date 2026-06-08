"""Unit tests for app.agents.background_actions helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents import background_actions as ba
from app.models.agent import BackgroundEvent

pytestmark = pytest.mark.asyncio


class TestHandleBackgroundEvent:
    async def test_handle_background_event_unsupported_and_sleep_incomplete(self):
        """Unsupported event returns parse_error; sleep_media_stop with empty media_player returns parse_error."""
        ha_client = AsyncMock()

        # Unsupported event type (bypass Literal validation with a mock object)
        event = MagicMock()
        event.event_type = "unknown_event"
        event.payload = {}
        result = await ba.handle_background_event(event, ha_client=ha_client)
        assert result["speech"] == ""
        assert result["error"]["code"] == "parse_error"
        assert "unsupported" in result["error"]["message"].lower()

        # sleep_media_stop with empty media_player
        event = BackgroundEvent(event_type="sleep_media_stop", payload={"media_player": ""})
        result = await ba.handle_background_event(event, ha_client=ha_client)
        assert result["error"]["code"] == "parse_error"
        assert "incomplete" in result["error"]["message"].lower()


class TestSpawnVoiceFollowup:
    async def test_spawn_voice_followup_early_return_and_spawn(self):
        """Early return when ha_client/area_id missing; spawn when valid."""
        with (
            patch.object(ba, "spawn") as mock_spawn,
            patch.object(ba, "_run_voice_followup_after_conversation", return_value=MagicMock()),
        ):
            # No ha_client and no identifiers -> early return
            ba.spawn_voice_followup_after_conversation(None, area_id=None, origin_device_id=None)
            mock_spawn.assert_not_called()

            # No area_id and no origin_device_id -> early return
            ba.spawn_voice_followup_after_conversation(AsyncMock(), area_id=None, origin_device_id=None)
            mock_spawn.assert_not_called()

            # Valid ha_client + area_id -> spawn
            ha_client = AsyncMock()
            ba.spawn_voice_followup_after_conversation(ha_client, area_id="kitchen", origin_device_id=None)
            mock_spawn.assert_called_once()
            assert mock_spawn.call_args.kwargs.get("name") == "conversation-voice-followup"


class TestRunVoiceFollowup:
    async def test_run_voice_followup_area_match_and_origin_fallback(self):
        """Area satellite match returns early; origin device satellite returns early; no satellite falls back to registry device."""
        ha_client = AsyncMock()
        profile = {
            "voice_followup_enabled": True,
            "tts_to_listen_delay": 5.0,
        }

        with (
            patch.object(ba, "_load_notification_profile", new_callable=AsyncMock, return_value=profile),
            patch.object(ba.SettingsRepository, "get_value", new_callable=AsyncMock, return_value=None),
        ):
            # Scenario 1: area has satellite -> early return
            with (
                patch.object(
                    ba, "_resolve_satellite_device", new_callable=AsyncMock, return_value="assist_satellite.kitchen"
                ),
                patch.object(ba, "_trigger_conversation_continuation_on_registry_device") as mock_registry,
            ):
                await ba._run_voice_followup_after_conversation(ha_client, area_id="kitchen", origin_device_id=None)
                mock_registry.assert_not_called()

            # Scenario 2: origin device has satellite -> early return
            with (
                patch.object(ba, "_resolve_satellite_device", new_callable=AsyncMock, return_value=None),
                patch.object(
                    ba,
                    "_resolve_satellite_from_origin_device",
                    new_callable=AsyncMock,
                    return_value="assist_satellite.phone",
                ),
                patch.object(ba, "_trigger_conversation_continuation_on_registry_device") as mock_registry,
            ):
                await ba._run_voice_followup_after_conversation(ha_client, area_id=None, origin_device_id="device_123")
                mock_registry.assert_not_called()

            # Scenario 3: no satellite anywhere -> fallback to registry device
            with (
                patch.object(ba, "_resolve_satellite_device", new_callable=AsyncMock, return_value=None),
                patch.object(ba, "_resolve_satellite_from_origin_device", new_callable=AsyncMock, return_value=None),
                patch.object(ba, "_trigger_conversation_continuation_on_registry_device") as mock_registry,
            ):
                await ba._run_voice_followup_after_conversation(ha_client, area_id=None, origin_device_id="device_123")
                mock_registry.assert_awaited_once_with(ha_client, "device_123", profile)


class TestResolveNotificationLanguage:
    async def test_resolve_notification_language_auto_ha_language(self):
        """'auto' setting resolves HA language; exception falls back to 'en'."""
        ha_client = AsyncMock()

        # metadata language takes precedence
        metadata = ba.NotificationMetadata(
            media_player_entity=None,
            origin_device_id=None,
            origin_area=None,
            duration=None,
            language="de",
        )
        with patch.object(ba.SettingsRepository, "get_value", new_callable=AsyncMock, return_value="auto"):
            result = await ba._resolve_notification_language(ha_client, metadata)
            assert result == "de"

        # auto setting + ha_client returns language
        metadata.language = None
        ha_client.get_user_language = AsyncMock(return_value="fr")
        with patch.object(ba.SettingsRepository, "get_value", new_callable=AsyncMock, return_value="auto"):
            result = await ba._resolve_notification_language(ha_client, metadata)
            assert result == "fr"

        # auto setting + ha_client raises exception -> fallback to 'en'
        ha_client.get_user_language = AsyncMock(side_effect=RuntimeError("HA down"))
        with patch.object(ba.SettingsRepository, "get_value", new_callable=AsyncMock, return_value="auto"):
            result = await ba._resolve_notification_language(ha_client, metadata)
            assert result == "en"


class TestPlayChime:
    async def test_play_chime_success_and_exception(self, caplog):
        """Success path plays chime and sleeps; exception logs warning."""
        ha_client = AsyncMock()
        profile = {"chime_url": "media-source://media_source/local/test.mp3"}

        # Success path
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await ba._play_chime(ha_client, "media_player.living_room", profile)
            ha_client.call_service.assert_awaited_once_with(
                "media_player",
                "play_media",
                "media_player.living_room",
                {
                    "media_content_id": "media-source://media_source/local/test.mp3",
                    "media_content_type": "music",
                },
            )

        # Exception path
        ha_client.call_service = AsyncMock(side_effect=RuntimeError("HA error"))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await ba._play_chime(ha_client, "media_player.living_room", profile)
            # Should not raise; warning is logged


class TestNotifyTTS:
    async def test_notify_tts_primary_fail_legacy_fallback_and_final_fail(self, caplog):
        """Primary tts.speak fail -> legacy fallback; final fail logs error."""
        ha_client = AsyncMock()
        profile = {"tts_engine": "tts.google_translate_say"}

        # Primary success
        await ba._notify_tts(ha_client, "media_player.living_room", "Hello", profile)
        ha_client.call_service.assert_awaited()
        assert ha_client.call_service.await_args.args[0] == "tts"
        assert ha_client.call_service.await_args.args[1] == "speak"

        # Primary fail, legacy fallback success
        ha_client.call_service = AsyncMock(side_effect=[RuntimeError("primary fail"), None])
        await ba._notify_tts(ha_client, "media_player.living_room", "Hello", profile)
        assert ha_client.call_service.await_count == 2
        second_call = ha_client.call_service.await_args_list[1]
        assert second_call.args[0] == "tts"
        assert second_call.args[1] == "google_translate_say"

        # Primary fail, legacy fallback fail
        ha_client.call_service = AsyncMock(side_effect=[RuntimeError("primary fail"), RuntimeError("legacy fail")])
        await ba._notify_tts(ha_client, "media_player.living_room", "Hello", profile)
        assert ha_client.call_service.await_count == 2


class TestNotifyChannelsExceptionPaths:
    async def test_notify_channels_exception_paths(self, caplog):
        """_notify_satellite_announce, _notify_persistent, _notify_push success + exception paths."""
        ha_client = AsyncMock()

        # _notify_satellite_announce success
        await ba._notify_satellite_announce(ha_client, "assist_satellite.kitchen", "Hello")
        ha_client.call_service.assert_awaited_once_with(
            "assist_satellite", "announce", "assist_satellite.kitchen", {"message": "Hello"}
        )

        # _notify_satellite_announce exception
        ha_client.call_service = AsyncMock(side_effect=RuntimeError("HA error"))
        await ba._notify_satellite_announce(ha_client, "assist_satellite.kitchen", "Hello")

        # _notify_persistent success
        ha_client.call_service = AsyncMock()
        await ba._notify_persistent(ha_client, "Timer", "Hello")
        ha_client.call_service.assert_awaited_once_with(
            "persistent_notification", "create", None, {"message": "Hello", "title": "Timer"}
        )

        # _notify_persistent exception
        ha_client.call_service = AsyncMock(side_effect=RuntimeError("HA error"))
        await ba._notify_persistent(ha_client, "Timer", "Hello")

        # _notify_push success
        ha_client.call_service = AsyncMock()
        await ba._notify_push(ha_client, ["mobile_app_phone"], "Timer", "Hello")
        ha_client.call_service.assert_awaited_once_with(
            "notify",
            "mobile_app_phone",
            None,
            {
                "message": "Hello",
                "title": "Timer",
                "data": {
                    "actions": [
                        {"action": "SNOOZE_5", "title": "Snooze 5 min"},
                        {"action": "DISMISS", "title": "Dismiss"},
                    ],
                },
            },
        )

        # _notify_push exception
        ha_client.call_service = AsyncMock(side_effect=RuntimeError("HA error"))
        await ba._notify_push(ha_client, ["mobile_app_phone"], "Timer", "Hello")


class TestTriggerConversationContinuation:
    async def test_trigger_conversation_continuation_satellite_skip_and_exception(self):
        """Satellite target skips; media_player triggers assist_pipeline/run; exception is logged."""
        ha_client = AsyncMock()
        profile = {"voice_followup_enabled": True, "tts_to_listen_delay": 0.01}

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Satellite skip
            with patch.object(
                ba, "_resolve_satellite_device", new_callable=AsyncMock, return_value="assist_satellite.kitchen"
            ):
                await ba._trigger_conversation_continuation(ha_client, "media_player.living_room", "kitchen", profile)
                ha_client.call_service.assert_not_called()

            # assist_pipeline/run success
            ha_client.call_service = AsyncMock()
            with (
                patch.object(ba, "_resolve_satellite_device", new_callable=AsyncMock, return_value=None),
                patch.object(ba, "_resolve_ha_device_id", new_callable=AsyncMock, return_value="device_123"),
            ):
                await ba._trigger_conversation_continuation(ha_client, "media_player.living_room", "kitchen", profile)
                ha_client.call_service.assert_awaited_once()
                call_args = ha_client.call_service.await_args
                assert call_args.args[0] == "assist_pipeline"
                assert call_args.args[1] == "run"
                assert call_args.args[3]["device_id"] == "device_123"

            # Exception path
            ha_client.call_service = AsyncMock(side_effect=RuntimeError("HA error"))
            with (
                patch.object(ba, "_resolve_satellite_device", new_callable=AsyncMock, return_value=None),
                patch.object(ba, "_resolve_ha_device_id", new_callable=AsyncMock, return_value=None),
            ):
                await ba._trigger_conversation_continuation(ha_client, "media_player.living_room", "kitchen", profile)
