"""Multi-channel notification dispatcher for timer and alarm events.

.. deprecated::
    This module is deprecated. Use :mod:`app.agents.background_actions` instead.
"""

from __future__ import annotations

import warnings
from typing import Any

from app.agents import background_actions as _ba
from app.db.repository import SettingsRepository  # noqa: F401

# Re-export private helpers so existing tests continue to work
_FALLBACK_MESSAGES = _ba._FALLBACK_MESSAGES
_GENERIC_FALLBACK_MESSAGES = _ba._GENERIC_FALLBACK_MESSAGES
_TTS_TO_LISTEN_DELAY = _ba._TTS_TO_LISTEN_DELAY
_DEFAULT_CHIME_URL = _ba._DEFAULT_CHIME_URL
_CHIME_TO_TTS_DELAY = _ba._CHIME_TO_TTS_DELAY
_generate_tts_message = _ba._generate_tts_message
_load_notification_profile = _ba._load_notification_profile
_notify_persistent = _ba._notify_persistent
_notify_push = _ba._notify_push
_play_chime = _ba._play_chime
_notify_tts = _ba._notify_tts
_resolve_satellite_device = _ba._resolve_satellite_device
_resolve_ha_device_id = _ba._resolve_ha_device_id
_validate_ha_device_id = _ba._validate_ha_device_id
_resolve_media_player_from_origin_device = _ba._resolve_media_player_from_origin_device
_resolve_media_player_from_area = _ba._resolve_media_player_from_area
_resolve_timer_playback_target = _ba._resolve_timer_playback_target
_trigger_conversation_continuation = _ba._trigger_conversation_continuation
_trigger_conversation_continuation_on_registry_device = _ba._trigger_conversation_continuation_on_registry_device
_run_voice_followup_after_conversation = _ba._run_voice_followup_after_conversation


def _warn_deprecated(func_name: str) -> None:
    warnings.warn(
        f"{func_name} is deprecated; use app.agents.background_actions instead",
        DeprecationWarning,
        stacklevel=3,
    )


def spawn_voice_followup_after_conversation(
    ha_client: Any,
    *,
    area_id: str | None = None,
    origin_device_id: str | None = None,
    entity_index: Any = None,
) -> None:
    """Schedule Assist STT to resume after the spoken response.

    .. deprecated::
        Use :func:`app.agents.background_actions.spawn_voice_followup_after_conversation` instead.
    """
    _warn_deprecated("spawn_voice_followup_after_conversation")
    return _ba.spawn_voice_followup_after_conversation(
        ha_client,
        area_id=area_id,
        origin_device_id=origin_device_id,
        entity_index=entity_index,
    )


async def dispatch_timer_notification(
    ha_client: Any,
    timer_name: str,
    entity_id: str,
    metadata: Any = None,
    entity_index: Any = None,
) -> None:
    """Dispatch notifications across all configured channels.

    .. deprecated::
        Use :func:`app.agents.background_actions.dispatch_timer_notification` instead.
    """
    _warn_deprecated("dispatch_timer_notification")
    return await _ba.dispatch_timer_notification(
        ha_client,
        timer_name,
        entity_id,
        metadata=metadata,
        entity_index=entity_index,
    )


async def dispatch_alarm_notification(
    ha_client: Any,
    alarm_name: str,
    entity_id: str,
    metadata: Any = None,
    entity_index: Any = None,
    custom_message: str | None = None,
) -> None:
    """Dispatch notifications for an alarm (input_datetime) that has fired.

    .. deprecated::
        Use :func:`app.agents.background_actions.dispatch_alarm_notification` instead.
    """
    _warn_deprecated("dispatch_alarm_notification")
    return await _ba.dispatch_alarm_notification(
        ha_client,
        alarm_name,
        entity_id,
        metadata=metadata,
        entity_index=entity_index,
        custom_message=custom_message,
    )
