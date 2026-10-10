"""Orchestrator-owned helpers for background notifications and HA actions."""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.base import _load_prompt_path_async, _prompt_path, _render_prompt_template, language_code_to_name
from app.agents.timer_executor._timers import deferred_action_rejection, deferred_owner_agent
from app.db.repository import SettingsRepository
from app.entity.visibility import entity_is_visible
from app.models.agent import BackgroundEvent, TaskContext
from app.security.sanitization import wrap_user_input
from app.util.ha_template import neutralize_ha_template
from app.util.tasks import spawn

logger = logging.getLogger(__name__)


@dataclass
class NotificationMetadata:
    """Context carried into timer notifications."""

    media_player_entity: str | None
    origin_device_id: str | None
    origin_area: str | None
    duration: str | None
    language: str | None = None


def _normalize_area_for_match(area: str | None) -> str | None:
    if area is None:
        return None
    normalized = str(area).strip()
    if not normalized:
        return None
    return normalized.casefold()


async def _deferred_entity_still_visible(agent_id: str, entity_id: str, entity_index: Any) -> bool:
    """Fire-time visibility recheck, fail-closed (mirrors the cache-replay
    recheck): any evaluation error rejects the deferred write."""
    try:
        return await entity_is_visible(agent_id, entity_id, entity_index)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Visibility recheck failed for %s; rejecting deferred action (fail-closed)",
            entity_id,
            exc_info=True,
        )
        return False


async def _announce_deferred_failure(
    ha_client: Any,
    payload: dict[str, Any],
    entity_index: Any,
    *,
    failure: str,
) -> None:
    """Best-effort user notification for a rejected/failed deferred action.

    Announces through the standard notification channels when the event
    carries origin info; otherwise logs at error level.
    """
    origin_device_id = payload.get("origin_device_id")
    origin_area = payload.get("origin_area")
    if not origin_device_id and not origin_area:
        logger.error("Deferred action failed without origin info: %s", failure)
        return
    metadata = NotificationMetadata(
        media_player_entity=payload.get("media_player"),
        origin_device_id=origin_device_id,
        origin_area=origin_area,
        duration=None,
        language=payload.get("language"),
    )
    try:
        await dispatch_text_notification(
            ha_client=ha_client,
            title=_NOTIFICATION_TITLE,
            text=failure,
            metadata=metadata,
            entity_index=entity_index,
            kind_label="Deferred action",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("Failed to announce deferred action failure: %s", failure, exc_info=True)


def _error_result(message: str, *, code: str = "internal", recoverable: bool = True) -> dict[str, Any]:
    """Build a background-event error result.

    Phase-1 contract: chunk ``error`` values are ``str | None`` — the
    message string is attached directly and the code/recoverable detail is
    logged instead of shipped as a dict (which crashed ``StreamToken``
    validation downstream).
    """
    logger.warning("Background action error (code=%s, recoverable=%s): %s", code, recoverable, message)
    return {
        "speech": "",
        "error": message,
    }


async def handle_background_event(
    event: BackgroundEvent,
    *,
    context: TaskContext | None = None,
    ha_client: Any,
    entity_index: Any = None,
    gateway: Any = None,
) -> dict[str, Any]:
    """Execute a structured background event inside orchestrator ownership."""
    payload = dict(event.payload or {})

    if event.event_type == "alarm_notification":
        metadata = NotificationMetadata(
            media_player_entity=payload.get("media_player"),
            origin_device_id=payload.get("origin_device_id") or getattr(context, "device_id", None),
            origin_area=payload.get("origin_area") or getattr(context, "area_id", None),
            duration=None,
            language=payload.get("language"),
        )
        custom_message = None
        if payload.get("briefing") and gateway is not None:
            from app.agents.wake_briefing import compose_wake_briefing

            custom_message = await compose_wake_briefing(
                gateway,
                payload,
                ha_client=ha_client,
                entity_index=entity_index,
            )
        await dispatch_alarm_notification(
            ha_client=ha_client,
            alarm_name=payload.get("alarm_name") or "Alarm",
            entity_id=payload.get("entity_id") or "",
            metadata=metadata,
            entity_index=entity_index,
            custom_message=custom_message,
        )
        return {"speech": ""}

    if event.event_type == "timer_notification" and isinstance(payload.get("missed"), list):
        metadata = NotificationMetadata(
            media_player_entity=payload.get("media_player"),
            origin_device_id=payload.get("origin_device_id"),
            origin_area=payload.get("origin_area"),
            duration=None,
            language=payload.get("language"),
        )
        await dispatch_missed_notification(
            ha_client=ha_client,
            missed=payload["missed"],
            timezone=payload.get("timezone"),
            metadata=metadata,
            entity_index=entity_index,
        )
        return {"speech": ""}

    if event.event_type == "timer_notification":
        metadata = NotificationMetadata(
            media_player_entity=payload.get("media_player"),
            origin_device_id=payload.get("origin_device_id") or getattr(context, "device_id", None),
            origin_area=payload.get("origin_area") or getattr(context, "area_id", None),
            duration=payload.get("duration"),
            language=payload.get("language"),
        )
        await dispatch_timer_notification(
            ha_client=ha_client,
            timer_name=payload.get("timer_name") or "Timer",
            entity_id=payload.get("entity_id") or "",
            metadata=metadata,
            entity_index=entity_index,
        )
        return {"speech": ""}

    if event.event_type == "delayed_action":
        if ha_client is None:
            return _error_result(
                "Background delayed action requires Home Assistant connectivity.", code="ha_unavailable"
            )
        target_entity = payload.get("target_entity") or ""
        target_action = payload.get("target_action") or ""
        if not target_entity or "/" not in target_action:
            return _error_result("Background delayed action payload is incomplete.", code="parse_error")
        domain, service = target_action.split("/", 1)
        # H-2 fire-time recheck (fail-closed): per-domain service allow-list,
        # target in the action's domain, and visibility for the agent that
        # owns the domain. The entity may have been hidden or removed between
        # schedule and fire, and rows persisted before this policy existed
        # are rechecked here too.
        rejection = deferred_action_rejection(domain, service)
        if rejection is None and target_entity.split(".", 1)[0] != domain:
            rejection = f"{target_entity} is not in the '{domain}' domain."
        if rejection:
            failure = f"Delayed action rejected: {rejection}"
            await _announce_deferred_failure(ha_client, payload, entity_index, failure=failure)
            return _error_result(failure, code="forbidden_domain", recoverable=False)
        agent_id = deferred_owner_agent(domain) or "timer-agent"
        if not await _deferred_entity_still_visible(agent_id, target_entity, entity_index):
            failure = f"Delayed action rejected: {target_entity} is not visible to {agent_id}."
            await _announce_deferred_failure(ha_client, payload, entity_index, failure=failure)
            return _error_result(failure, code="entity_not_visible", recoverable=False)
        try:
            await ha_client.call_service(domain, service, target_entity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = f"Delayed action {target_action} on {target_entity} failed."
            logger.error("%s (%s)", failure, exc, exc_info=True)
            await _announce_deferred_failure(ha_client, payload, entity_index, failure=failure)
            return _error_result(failure, code="ha_error")
        return {
            "speech": "",
            "action_executed": {
                "action": service,
                "entity_id": target_entity,
                "success": True,
                "cacheable": False,
            },
        }

    if event.event_type == "sleep_media_stop":
        if ha_client is None:
            return _error_result("Background sleep timer requires Home Assistant connectivity.", code="ha_unavailable")
        media_player = payload.get("media_player") or ""
        if not media_player:
            return _error_result("Background sleep timer payload is incomplete.", code="parse_error")
        # H-2 fire-time recheck (fail-closed): visibility of the media player
        # for the agent that owns media players.
        agent_id = deferred_owner_agent("media_player") or "timer-agent"
        if not media_player.startswith("media_player."):
            failure = f"Sleep timer stop rejected: {media_player} is not a media player."
            await _announce_deferred_failure(ha_client, payload, entity_index, failure=failure)
            return _error_result(failure, code="forbidden_domain", recoverable=False)
        if not await _deferred_entity_still_visible(agent_id, media_player, entity_index):
            failure = f"Sleep timer stop rejected: {media_player} is not visible to {agent_id}."
            await _announce_deferred_failure(ha_client, payload, entity_index, failure=failure)
            return _error_result(failure, code="entity_not_visible", recoverable=False)
        try:
            await ha_client.call_service("media_player", "media_stop", media_player)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = f"Sleep timer media stop on {media_player} failed."
            logger.error("%s (%s)", failure, exc, exc_info=True)
            await _announce_deferred_failure(ha_client, payload, entity_index, failure=failure)
            return _error_result(failure, code="ha_error")
        return {
            "speech": "",
            "action_executed": {
                "action": "media_stop",
                "entity_id": media_player,
                "success": True,
                "cacheable": False,
            },
        }

    if event.event_type == "voice_followup":
        spawn_voice_followup_after_conversation(
            ha_client,
            area_id=payload.get("area_id") or getattr(context, "area_id", None),
            origin_device_id=payload.get("origin_device_id") or getattr(context, "device_id", None),
            entity_index=entity_index,
        )
        return {"speech": ""}

    return _error_result(f"Unsupported background event: {event.event_type}", code="parse_error")


def spawn_voice_followup_after_conversation(
    ha_client: Any,
    *,
    area_id: str | None = None,
    origin_device_id: str | None = None,
    entity_index: Any = None,
) -> None:
    """Schedule Assist STT to resume after the spoken response."""
    if not ha_client or (not area_id and not origin_device_id):
        return

    spawn(
        _run_voice_followup_after_conversation(
            ha_client,
            area_id=area_id,
            origin_device_id=origin_device_id,
            entity_index=entity_index,
        ),
        name="conversation-voice-followup",
    )


async def _trigger_conversation_continuation_on_registry_device(
    ha_client: Any,
    device_registry_id: str,
    profile: dict,
) -> None:
    if not profile.get("voice_followup_enabled", True):
        return
    delay = profile.get("tts_to_listen_delay", _TTS_TO_LISTEN_DELAY)
    await asyncio.sleep(delay)
    try:
        await ha_client.call_service(
            "assist_pipeline",
            "run",
            None,
            {
                "start_stage": "stt",
                "end_stage": "tts",
                "device_id": device_registry_id,
            },
        )
        logger.info(
            "Conversation continuation triggered (registry device_id=%s, e.g. Companion)",
            device_registry_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        body = ""
        if hasattr(exc, "response") and exc.response is not None:
            with contextlib.suppress(Exception):
                body = exc.response.text or ""
        logger.warning(
            "Failed to trigger conversation continuation for device %s (HA response: %s)",
            device_registry_id,
            body,
            exc_info=True,
        )


async def _run_voice_followup_after_conversation(
    ha_client: Any,
    *,
    area_id: str | None = None,
    origin_device_id: str | None = None,
    entity_index: Any = None,
) -> None:
    profile = await _load_notification_profile()
    if not profile.get("voice_followup_enabled", True):
        return
    profile = dict(profile)
    raw_delay = await SettingsRepository.get_value("orchestrator.voice_followup_delay", None)
    try:
        if raw_delay not in (None, ""):
            profile["tts_to_listen_delay"] = float(raw_delay or "0")
    except (TypeError, ValueError):
        logger.debug("Invalid voice_followup_delay value, using default", exc_info=True)

    if area_id:
        satellite = await _resolve_satellite_device(ha_client, area_id, entity_index=entity_index)
        if satellite:
            logger.info(
                "Satellite voice follow-up for area %s is handled by the HA integration (satellite=%s)",
                area_id,
                satellite,
            )
            return
        logger.debug("No assist_satellite in area %s, falling back to origin device if set", area_id)

    if origin_device_id:
        satellite = await _resolve_satellite_from_origin_device(ha_client, origin_device_id)
        if satellite:
            logger.info(
                "Satellite voice follow-up for origin_device_id %s is handled by the HA integration (satellite=%s)",
                origin_device_id,
                satellite,
            )
            return
        logger.debug(
            "No assist_satellite found for origin_device_id %s, falling back to registry device_id",
            origin_device_id,
        )
        await _trigger_conversation_continuation_on_registry_device(ha_client, origin_device_id, profile)
        return

    logger.debug("Voice follow-up skipped: no satellite and no origin_device_id")


# English fallback texts. Other languages are produced from these through the
# rewrite path (``_localize_text``); English is the final fallback. Kept as
# dicts for the deprecated ``notification_dispatcher`` re-exports.
_FALLBACK_MESSAGES = {"en": "Timer {name} has finished"}
_GENERIC_FALLBACK_MESSAGES = {"en": "The timer has finished"}
_ALARM_FALLBACK_MESSAGE = "Alarm {name} has triggered"
_NOTIFICATION_TITLE = "AgentHub"
# Visibility for announcement targets is evaluated for the agent that owns
# timers and alarms.
_ANNOUNCE_VISIBILITY_AGENT = "timer-agent"
_TTS_TO_LISTEN_DELAY = 10.0
_DEFAULT_CHIME_URL = "media-source://media_source/local/notification.mp3"
_CHIME_TO_TTS_DELAY = 1.5


async def _resolve_notification_language(ha_client: Any, metadata: Any = None) -> str:
    """Resolve language for background timer notifications.

    Precedence:
    1) event metadata language
    2) explicit settings language when not 'auto'
    3) HA user language when settings language is 'auto'
    4) 'en' fallback
    """
    metadata_language = (getattr(metadata, "language", None) or "").strip()
    if metadata_language:
        return metadata_language

    setting_value = await SettingsRepository.get_value("language", "auto")
    setting = str(setting_value or "auto").strip()
    if setting and setting.lower() != "auto":
        return setting

    try:
        ha_language = await ha_client.get_user_language() if ha_client else None
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Failed to resolve HA user language, falling back to default", exc_info=True)
        ha_language = None
    resolved = str(ha_language or "").strip()
    return resolved or "en"


def _is_english(language: str | None) -> bool:
    return (language or "en").strip().lower().split("-", 1)[0] in ("", "en")


def _get_rewrite_agent() -> Any | None:
    try:
        from app.main import app

        return getattr(app.state, "rewrite_agent", None)
    except Exception:
        return None


async def _localize_text(text: str, language: str | None) -> str:
    """Return ``text`` (English) in ``language`` via the rewrite path; English is the final fallback."""
    if not text or _is_english(language):
        return text
    rewrite_agent = _get_rewrite_agent()
    rewrite = getattr(rewrite_agent, "rewrite", None)
    if not callable(rewrite):
        return text
    try:
        localized = await rewrite(text, language=str(language))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Localizing a notification to %s failed; using English", language, exc_info=True)
        return text
    return (localized or "").strip() or text


async def _deliver_announcement(
    ha_client: Any,
    *,
    message: str,
    metadata: Any,
    entity_index: Any,
    profile: dict,
    kind_label: str,
) -> None:
    """Speak ``message`` on the best visible target and start follow-up only after success.

    A failed satellite announce falls back to TTS on a media player
    (origin device first, then the origin room).
    """
    if not profile.get("tts_enabled", True) or not message:
        return
    origin_device_id = metadata.origin_device_id if metadata else None
    area = metadata.origin_area if metadata else None
    satellite_entity, media_player = await _resolve_notification_audio_target(
        ha_client,
        media_player=metadata.media_player_entity if metadata else None,
        origin_device_id=origin_device_id,
        area=area,
        entity_index=entity_index,
        kind_label=kind_label,
    )

    spoken_on: str | None = None
    if satellite_entity:
        if await _notify_satellite_announce(ha_client, satellite_entity, message):
            spoken_on = satellite_entity
        elif not media_player:
            media_player = await _resolve_timer_playback_target(
                ha_client, origin_device_id=origin_device_id, area=area, entity_index=entity_index
            )
            if media_player:
                logger.info(
                    "%s announce on %s failed; falling back to TTS on %s", kind_label, satellite_entity, media_player
                )
    if spoken_on is None and media_player:
        if profile.get("chime_enabled", True):
            await _play_chime(ha_client, media_player, profile)
        if await _notify_tts(ha_client, media_player, message, profile):
            spoken_on = media_player
    if spoken_on is None:
        if satellite_entity or media_player:
            logger.warning("%s notification was not spoken: every audio target failed", kind_label)
        return
    spawn(
        _trigger_conversation_continuation(ha_client, spoken_on, area, profile, entity_index=entity_index),
        name="tts-followup",
    )


async def _deliver_text_channels(ha_client: Any, profile: dict, *, title: str, message: str) -> None:
    if not message:
        return
    if profile.get("persistent_enabled", True):
        await _notify_persistent(ha_client, title, message)
    if profile.get("push_enabled", False):
        await _notify_push(ha_client, profile.get("push_targets", []), title, message)


async def dispatch_timer_notification(
    ha_client: Any,
    timer_name: str,
    entity_id: str,
    metadata: Any = None,
    entity_index: Any = None,
) -> None:
    """Dispatch timer notifications across all configured channels."""
    profile = await _load_notification_profile()
    language = await _resolve_notification_language(ha_client, metadata)

    has_meaningful_name = _has_meaningful_timer_name(timer_name, entity_id)
    message = await _generate_tts_message(
        timer_name=timer_name,
        duration=metadata.duration if metadata else None,
        area=metadata.origin_area if metadata else None,
        language=language,
        has_meaningful_name=has_meaningful_name,
    )
    if not message:
        if has_meaningful_name:
            fallback = _render_prompt_template(_FALLBACK_MESSAGES["en"], name=timer_name)
        else:
            fallback = _GENERIC_FALLBACK_MESSAGES["en"]
        message = await _localize_text(fallback, language)

    await _deliver_announcement(
        ha_client, message=message, metadata=metadata, entity_index=entity_index, profile=profile, kind_label="Timer"
    )
    await _deliver_text_channels(ha_client, profile, title=timer_name, message=message)


async def dispatch_alarm_notification(
    ha_client: Any,
    alarm_name: str,
    entity_id: str,
    metadata: Any = None,
    entity_index: Any = None,
    custom_message: str | None = None,
) -> None:
    """Dispatch notifications for an alarm that has fired."""
    profile = await _load_notification_profile()
    language = await _resolve_notification_language(ha_client, metadata)
    message = await _localize_text(_render_prompt_template(_ALARM_FALLBACK_MESSAGE, name=alarm_name), language)
    spoken_message = (custom_message or "").strip() or message

    await _deliver_announcement(
        ha_client,
        message=spoken_message,
        metadata=metadata,
        entity_index=entity_index,
        profile=profile,
        kind_label="Alarm",
    )
    await _deliver_text_channels(ha_client, profile, title=alarm_name, message=message)


async def dispatch_text_notification(
    ha_client: Any,
    *,
    title: str,
    text: str,
    metadata: Any = None,
    entity_index: Any = None,
    kind_label: str = "Notification",
) -> None:
    """Deliver a fixed English ``text`` (localized to the notification language) on all channels."""
    profile = await _load_notification_profile()
    language = await _resolve_notification_language(ha_client, metadata)
    message = await _localize_text(text, language)
    await _deliver_announcement(
        ha_client, message=message, metadata=metadata, entity_index=entity_index, profile=profile, kind_label=kind_label
    )
    await _deliver_text_channels(ha_client, profile, title=title, message=message)


def _format_due(epoch: int, timezone: str | None, now_epoch: int) -> str:
    tz = None
    if timezone:
        try:
            tz = ZoneInfo(str(timezone))
        except Exception:
            tz = None
    due = datetime.fromtimestamp(int(epoch), tz=tz) if tz is not None else datetime.fromtimestamp(int(epoch))
    now = datetime.fromtimestamp(int(now_epoch), tz=tz) if tz is not None else datetime.fromtimestamp(int(now_epoch))
    return due.strftime("%H:%M") if due.date() == now.date() else due.strftime("%Y-%m-%d %H:%M")


def _missed_summary(missed: list[Any], timezone: str | None, now_epoch: int) -> str:
    parts: list[str] = []
    for item in missed:
        if not isinstance(item, dict):
            continue
        kind = "alarm" if item.get("kind") == "alarm" else "timer"
        name = str(item.get("name") or "").strip()
        due = _format_due(int(item.get("due_epoch") or 0), timezone, now_epoch)
        parts.append(f"{kind} '{name}' (due {due})" if name else f"{kind} (due {due})")
    if not parts:
        return ""
    return "While AgentHub was offline, these were missed: " + "; ".join(parts) + "."


async def dispatch_missed_notification(
    ha_client: Any,
    *,
    missed: list[Any],
    timezone: str | None = None,
    metadata: Any = None,
    entity_index: Any = None,
) -> None:
    """Report timers/alarms that came due while the container was down, once and together."""
    text = _missed_summary(missed, timezone, int(time.time()))
    if not text:
        return
    await dispatch_text_notification(
        ha_client,
        title=_NOTIFICATION_TITLE,
        text=text,
        metadata=metadata,
        entity_index=entity_index,
        kind_label="Missed timer",
    )


def _has_meaningful_timer_name(timer_name: str, entity_id: str) -> bool:
    if not timer_name:
        return False
    name_lower = timer_name.strip().lower()
    if name_lower in ("timer", "timer 1", "timer 2", "timer 3"):
        return False
    return name_lower != entity_id.split(".", 1)[-1].replace("_", " ")


async def _generate_tts_message(
    timer_name: str,
    duration: str | None,
    area: str | None,
    language: str,
    has_meaningful_name: bool = True,
) -> str | None:
    if not has_meaningful_name:
        return None

    system_prompt = _render_prompt_template(
        await _load_prompt_path_async(_prompt_path("timer_announcement")),
        language=language_code_to_name(language),
    )
    context_parts = [f"Timer name:\n{wrap_user_input(timer_name)}"]
    if duration:
        context_parts.append(f"Duration:\n{wrap_user_input(duration)}")
    if area:
        context_parts.append(f"Area/Room:\n{wrap_user_input(area)}")
    user_prompt = (
        "A timer has just finished. Context:\n" + "\n".join(context_parts) + "\n\nGenerate a one-sentence announcement."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        from app.llm.client import complete

        result = await complete(
            agent_id="notification-dispatcher",
            messages=messages,
            max_tokens=50,
            temperature=0.7,
        )
        if result and result.strip():
            logger.info("LLM generated TTS message: %s", result.strip())
            return result.strip()
        logger.warning("LLM returned empty TTS message, falling back to static template")
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("LLM TTS message generation failed, falling back to static template", exc_info=True)
        return None


async def _play_chime(
    ha_client: Any,
    media_player_entity: str,
    profile: dict,
) -> None:
    chime_url = profile.get("chime_url", _DEFAULT_CHIME_URL)
    try:
        await ha_client.call_service(
            "media_player",
            "play_media",
            media_player_entity,
            {
                "media_content_id": chime_url,
                "media_content_type": "music",
            },
        )
        logger.info("Chime played on %s", media_player_entity)
        await asyncio.sleep(_CHIME_TO_TTS_DELAY)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Chime playback failed on %s, continuing with TTS", media_player_entity, exc_info=True)


async def _notify_tts(
    ha_client: Any,
    media_player_entity: str,
    message: str,
    profile: dict,
) -> bool:
    """Speak ``message`` on a media player; returns True when a TTS call succeeded."""
    tts_engine = profile.get("tts_engine", "tts.google_translate_say")
    try:
        await ha_client.call_service(
            "tts",
            "speak",
            tts_engine,
            {
                "media_player_entity_id": media_player_entity,
                "message": message,
            },
        )
        logger.info("TTS notification sent to %s: %s", media_player_entity, message)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("TTS notification failed on %s, trying legacy tts.say", media_player_entity, exc_info=True)
        try:
            tts_domain = tts_engine.split(".")[0] if "." in tts_engine else "tts"
            tts_service = tts_engine.split(".")[1] if "." in tts_engine else "google_translate_say"
            await ha_client.call_service(
                tts_domain,
                tts_service,
                media_player_entity,
                {"message": message},
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("TTS fallback also failed on %s", media_player_entity, exc_info=True)
            return False


async def _notify_satellite_announce(
    ha_client: Any,
    satellite_entity: str,
    message: str,
) -> bool:
    """Announce ``message`` on an assist satellite; returns True on success."""
    try:
        await ha_client.call_service(
            "assist_satellite",
            "announce",
            satellite_entity,
            {
                "message": message,
            },
        )
        logger.info("Assist satellite announce sent to %s", satellite_entity)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Assist satellite announce failed on %s", satellite_entity, exc_info=True)
        return False


async def _notify_persistent(
    ha_client: Any,
    timer_name: str,
    message: str,
) -> None:
    try:
        await ha_client.call_service(
            "persistent_notification",
            "create",
            None,
            {"message": message, "title": timer_name},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("persistent_notification failed for %s", timer_name, exc_info=True)


async def _notify_push(
    ha_client: Any,
    push_targets: list[str],
    timer_name: str,
    message: str,
) -> None:
    for target in push_targets:
        try:
            await ha_client.call_service(
                "notify",
                target,
                None,
                # No actionable buttons: nothing handles mobile notification
                # action events, so buttons would do nothing. Legacy notify
                # services render message/title as templates: neutralize the
                # user-provided timer label and generated text.
                {
                    "message": neutralize_ha_template(message),
                    "title": neutralize_ha_template(timer_name),
                },
            )
            logger.info("Push notification sent to %s", target)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Push notification failed for target %s", target, exc_info=True)


async def _load_notification_profile() -> dict:
    defaults = {
        "tts_enabled": True,
        "tts_engine": "tts.google_translate_say",
        "persistent_enabled": True,
        "push_enabled": False,
        "push_targets": [],
        "voice_followup_enabled": True,
        "tts_to_listen_delay": 10.0,
        "chime_enabled": True,
        "chime_url": _DEFAULT_CHIME_URL,
    }
    try:
        raw = await SettingsRepository.get_value("notification.profile")
        if raw:
            profile = _json.loads(raw)
            defaults.update(profile)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Failed to load notification profile, using defaults", exc_info=True)
    return defaults


async def _announce_target_visible(entity_id: str | None, entity_index: Any) -> bool:
    """Fail-closed visibility check for an announcement target (timer agent rules)."""
    if not entity_id:
        return False
    try:
        return await entity_is_visible(_ANNOUNCE_VISIBILITY_AGENT, entity_id, entity_index)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Visibility check failed for announcement target %s; skipping it", entity_id, exc_info=True)
        return False


async def _resolve_area_target(area: str | None, entity_index: Any, domain: str) -> str | None:
    """Pick the first visible ``domain`` entity in ``area`` (sorted by entity_id for determinism).

    The entity index is the only source: HA state attributes carry no area,
    so a state scan cannot answer this.
    """
    normalized_area = _normalize_area_for_match(area)
    if not normalized_area or entity_index is None:
        return None
    try:
        entries = await entity_index.list_entries_async(domains={domain})
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("EntityIndex %s lookup failed for area %s", domain, area, exc_info=True)
        return None
    candidates = sorted(
        str(entry.entity_id)
        for entry in entries
        if str(getattr(entry, "entity_id", "")).startswith(f"{domain}.")
        and _normalize_area_for_match(getattr(entry, "area", None)) == normalized_area
    )
    for entity_id in candidates:
        if await _announce_target_visible(entity_id, entity_index):
            return entity_id
    return None


async def _resolve_satellite_device(
    ha_client: Any,
    area: str | None,
    entity_index: Any = None,
) -> str | None:
    return await _resolve_area_target(area, entity_index, "assist_satellite")


_HA_DEVICE_ID_RE = re.compile(r"^[a-zA-Z0-9_]+$")


def _validate_ha_device_id(device_id: str | None) -> str | None:
    """Validate a Home Assistant device_id to prevent Jinja2 injection.

    Returns the device_id if safe, otherwise None.
    """
    if not device_id:
        return None
    if _HA_DEVICE_ID_RE.match(device_id):
        return device_id
    logger.warning("Rejected unsafe origin_device_id: %s", device_id)
    return None


async def _resolve_media_player_from_origin_device(
    ha_client: Any,
    origin_device_id: str | None,
) -> str | None:
    origin_device_id = _validate_ha_device_id(origin_device_id)
    if not origin_device_id:
        return None
    template = "{{ expand(device_entities('" + origin_device_id + "')) | map(attribute='entity_id') | join(',') }}"
    rendered: str | None = None
    try:
        if hasattr(ha_client, "render_template"):
            rendered = await ha_client.render_template(template)
        else:
            client = getattr(ha_client, "_client", None)
            if client is None:
                return None
            resp = await client.post("/api/template", json={"template": template})
            resp.raise_for_status()
            rendered = (resp.text or "").strip()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "Failed to resolve media_player from origin device %s",
            origin_device_id,
            exc_info=True,
        )
        return None

    if not rendered:
        return None

    candidates = [item.strip() for item in rendered.split(",") if item and item.strip()]
    for candidate in candidates:
        if candidate.startswith("media_player."):
            return candidate
    return None


async def _resolve_satellite_from_origin_device(
    ha_client: Any,
    origin_device_id: str | None,
) -> str | None:
    origin_device_id = _validate_ha_device_id(origin_device_id)
    if not origin_device_id:
        return None
    template = "{{ expand(device_entities('" + origin_device_id + "')) | map(attribute='entity_id') | join(',') }}"
    rendered: str | None = None
    try:
        if hasattr(ha_client, "render_template"):
            rendered = await ha_client.render_template(template)
        else:
            client = getattr(ha_client, "_client", None)
            if client is None:
                return None
            resp = await client.post("/api/template", json={"template": template})
            resp.raise_for_status()
            rendered = (resp.text or "").strip()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(
            "Failed to resolve assist_satellite from origin device %s",
            origin_device_id,
            exc_info=True,
        )
        return None

    if not rendered:
        return None

    candidates = [item.strip() for item in rendered.split(",") if item and item.strip()]
    for candidate in candidates:
        if candidate.startswith("assist_satellite."):
            return candidate
    return None


async def _resolve_media_player_from_area(
    ha_client: Any,
    area: str | None,
    entity_index: Any = None,
) -> str | None:
    return await _resolve_area_target(area, entity_index, "media_player")


async def _resolve_timer_playback_target(
    ha_client: Any,
    *,
    origin_device_id: str | None,
    area: str | None,
    entity_index: Any = None,
) -> str | None:
    """Media player of the origin device first, then a visible one in the origin room."""
    media_player = await _resolve_media_player_from_origin_device(ha_client, origin_device_id)
    if media_player and await _announce_target_visible(media_player, entity_index):
        return media_player
    return await _resolve_media_player_from_area(ha_client, area, entity_index=entity_index)


async def _resolve_notification_audio_target(
    ha_client: Any,
    *,
    media_player: str | None,
    origin_device_id: str | None,
    area: str | None,
    entity_index: Any = None,
    kind_label: str,
) -> tuple[str | None, str | None]:
    # Origin device first (the device the request came from), then the
    # origin room. Every target must be visible to the timer agent.
    satellite_entity = await _resolve_satellite_from_origin_device(ha_client, origin_device_id)
    if satellite_entity and not await _announce_target_visible(satellite_entity, entity_index):
        satellite_entity = None
    if not satellite_entity:
        satellite_entity = await _resolve_satellite_device(ha_client, area, entity_index=entity_index)

    resolved_media_player = media_player
    if resolved_media_player and not await _announce_target_visible(resolved_media_player, entity_index):
        resolved_media_player = None
    if not resolved_media_player and not satellite_entity:
        resolved_media_player = await _resolve_timer_playback_target(
            ha_client,
            origin_device_id=origin_device_id,
            area=area,
            entity_index=entity_index,
        )
        if resolved_media_player:
            logger.info(
                "%s notification playback target resolved from origin metadata (device_id=%s, area=%s): %s",
                kind_label,
                origin_device_id,
                area,
                resolved_media_player,
            )
        else:
            logger.warning(
                "%s notification has no resolvable playback target (device_id=%s, area=%s)",
                kind_label,
                origin_device_id,
                area,
            )

    return satellite_entity, resolved_media_player


async def _resolve_ha_device_id(
    ha_client: Any,
    entity_id: str,
) -> str | None:
    if not entity_id:
        return None
    template = "{{ device_id('" + entity_id + "') }}"
    rendered: str | None = None
    try:
        if hasattr(ha_client, "render_template"):
            rendered = await ha_client.render_template(template)
        else:
            client = getattr(ha_client, "_client", None)
            if client is None:
                return None
            resp = await client.post("/api/template", json={"template": template})
            resp.raise_for_status()
            rendered = (resp.text or "").strip()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Failed to resolve device_id for %s", entity_id, exc_info=True)
        return None

    if not rendered or rendered.lower() == "none":
        return None
    return rendered


async def _trigger_conversation_continuation(
    ha_client: Any,
    media_player_entity: str,
    area: str | None,
    profile: dict,
    entity_index: Any = None,
) -> None:
    if not profile.get("voice_followup_enabled", True):
        return

    delay = profile.get("tts_to_listen_delay", _TTS_TO_LISTEN_DELAY)
    await asyncio.sleep(delay)

    target_entity = (await _resolve_satellite_device(ha_client, area, entity_index=entity_index)) or media_player_entity

    if target_entity.startswith("assist_satellite."):
        logger.info(
            "Satellite conversation continuation is handled by the HA integration (target=%s, area=%s)",
            target_entity,
            area,
        )
        return

    try:
        pipeline_data: dict[str, Any] = {
            "start_stage": "stt",
            "end_stage": "tts",
        }
        device_id = await _resolve_ha_device_id(ha_client, target_entity)
        if device_id:
            pipeline_data["device_id"] = device_id

        await ha_client.call_service(
            "assist_pipeline",
            "run",
            None,
            pipeline_data,
        )
        logger.info(
            "Conversation continuation triggered on %s (area=%s, device_id=%s)",
            target_entity,
            area,
            device_id or "<unresolved>",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Failed to trigger conversation continuation on %s -- user must use wake word for follow-up",
            target_entity,
            exc_info=True,
        )
