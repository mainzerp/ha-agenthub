"""Orchestrator-owned helpers for background notifications and HA actions."""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.db.repository import SettingsRepository
from app.models.agent import BackgroundEvent, TaskContext
from app.security.sanitization import wrap_user_input
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


def _error_result(message: str, *, code: str = "internal", recoverable: bool = True) -> dict[str, Any]:
    return {
        "speech": "",
        "error": {
            "code": code,
            "message": message,
            "recoverable": recoverable,
        },
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
        await ha_client.call_service(domain, service, target_entity)
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
        await ha_client.call_service("media_player", "media_stop", media_player)
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
            profile["tts_to_listen_delay"] = float(raw_delay)
    except (TypeError, ValueError):
        logger.debug("Invalid voice_followup_delay value, using default", exc_info=True)

    if area_id:
        satellite = await _resolve_satellite_device(ha_client, area_id, entity_index=entity_index)
        if satellite:
            await _trigger_conversation_continuation(
                ha_client,
                satellite,
                area_id,
                profile,
                entity_index=entity_index,
            )
            return
        logger.debug("No assist_satellite in area %s, falling back to origin device if set", area_id)

    if origin_device_id:
        satellite = await _resolve_satellite_from_origin_device(ha_client, origin_device_id)
        if satellite:
            await _trigger_conversation_continuation(
                ha_client,
                satellite,
                None,
                profile,
                entity_index=entity_index,
            )
            return
        logger.debug(
            "No assist_satellite found for origin_device_id %s, falling back to registry device_id",
            origin_device_id,
        )
        await _trigger_conversation_continuation_on_registry_device(ha_client, origin_device_id, profile)
        return

    logger.debug("Voice follow-up skipped: no satellite and no origin_device_id")


_FALLBACK_MESSAGES = {
    "de": "Timer {name} ist abgelaufen",
    "en": "Timer {name} has finished",
}
_GENERIC_FALLBACK_MESSAGES = {
    "de": "Der Timer ist abgelaufen",
    "en": "The timer has finished",
}
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
    except Exception:
        ha_language = None
    resolved = str(ha_language or "").strip()
    return resolved or "en"


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
    lang_key = "de" if language.startswith("de") else "en"

    media_player = metadata.media_player_entity if metadata else None
    origin_device_id = metadata.origin_device_id if metadata else None
    area = metadata.origin_area if metadata else None
    duration = metadata.duration if metadata else None
    satellite_entity, media_player = await _resolve_notification_audio_target(
        ha_client,
        media_player=media_player,
        origin_device_id=origin_device_id,
        area=area,
        entity_index=entity_index,
        kind_label="Timer",
    )

    has_meaningful_name = _has_meaningful_timer_name(timer_name, entity_id)
    message = await _generate_tts_message(
        timer_name=timer_name,
        duration=duration,
        area=area,
        language=language,
        has_meaningful_name=has_meaningful_name,
    )
    if not message:
        if has_meaningful_name:
            message = _FALLBACK_MESSAGES.get(lang_key, _FALLBACK_MESSAGES["en"]).format(name=timer_name)
        else:
            message = _GENERIC_FALLBACK_MESSAGES.get(lang_key, _GENERIC_FALLBACK_MESSAGES["en"])

    if profile.get("tts_enabled", True):
        if satellite_entity and message:
            await _notify_satellite_announce(ha_client, satellite_entity, message)
            spawn(
                _trigger_conversation_continuation(
                    ha_client,
                    satellite_entity,
                    area,
                    profile,
                    entity_index=entity_index,
                ),
                name="tts-followup",
            )
        elif media_player:
            if profile.get("chime_enabled", True):
                await _play_chime(ha_client, media_player, profile)

            if message:
                await _notify_tts(ha_client, media_player, message, profile)

            spawn(
                _trigger_conversation_continuation(
                    ha_client,
                    media_player,
                    area,
                    profile,
                    entity_index=entity_index,
                ),
                name="tts-followup",
            )

    if profile.get("persistent_enabled", True) and message:
        await _notify_persistent(ha_client, timer_name, message)

    if profile.get("push_enabled", False) and message:
        push_targets = profile.get("push_targets", [])
        await _notify_push(ha_client, push_targets, timer_name, message)


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
    lang_key = "de" if language.startswith("de") else "en"
    media_player = metadata.media_player_entity if metadata else None
    origin_device_id = metadata.origin_device_id if metadata else None
    area = metadata.origin_area if metadata else None
    satellite_entity, media_player = await _resolve_notification_audio_target(
        ha_client,
        media_player=media_player,
        origin_device_id=origin_device_id,
        area=area,
        entity_index=entity_index,
        kind_label="Alarm",
    )

    alarm_messages = {
        "de": "Alarm {name} ist ausgeloest",
        "en": "Alarm {name} has triggered",
    }
    message = alarm_messages.get(lang_key, alarm_messages["en"]).format(name=alarm_name)
    spoken_message = (custom_message or "").strip() or message

    if profile.get("tts_enabled", True):
        if satellite_entity and spoken_message:
            await _notify_satellite_announce(ha_client, satellite_entity, spoken_message)
            spawn(
                _trigger_conversation_continuation(
                    ha_client,
                    satellite_entity,
                    area,
                    profile,
                    entity_index=entity_index,
                ),
                name="alarm-tts-followup",
            )
        elif media_player and spoken_message:
            if profile.get("chime_enabled", True):
                await _play_chime(ha_client, media_player, profile)
            await _notify_tts(ha_client, media_player, spoken_message, profile)
            spawn(
                _trigger_conversation_continuation(
                    ha_client,
                    media_player,
                    area,
                    profile,
                    entity_index=entity_index,
                ),
                name="alarm-tts-followup",
            )

    if profile.get("persistent_enabled", True):
        await _notify_persistent(ha_client, alarm_name, message)

    if profile.get("push_enabled", False):
        push_targets = profile.get("push_targets", [])
        await _notify_push(ha_client, push_targets, alarm_name, message)


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

    lang_instruction = "German" if language.startswith("de") else "English"
    system_prompt = (
        f"You are a smart home voice assistant. "
        f"Generate a single short sentence in {lang_instruction} announcing that a timer is done. "
        f"Mention the timer name naturally. Do NOT suggest any actions, snooze, or follow-up questions. "
        f"Do NOT use markdown or special characters. "
        f"Output ONLY the spoken sentence, nothing else."
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
    except Exception:
        logger.warning("Chime playback failed on %s, continuing with TTS", media_player_entity, exc_info=True)


async def _notify_tts(
    ha_client: Any,
    media_player_entity: str,
    message: str,
    profile: dict,
) -> None:
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
        except Exception:
            logger.error("TTS fallback also failed on %s", media_player_entity, exc_info=True)


async def _notify_satellite_announce(
    ha_client: Any,
    satellite_entity: str,
    message: str,
) -> None:
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
    except Exception:
        logger.warning("Assist satellite announce failed on %s", satellite_entity, exc_info=True)


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
                {
                    "message": message,
                    "title": timer_name,
                    "data": {
                        "actions": [
                            {"action": "SNOOZE_5", "title": "Snooze 5 min"},
                            {"action": "DISMISS", "title": "Dismiss"},
                        ],
                    },
                },
            )
            logger.info("Push notification sent to %s", target)
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
    except Exception:
        logger.warning("Failed to load notification profile, using defaults", exc_info=True)
    return defaults


async def _resolve_satellite_device(
    ha_client: Any,
    area: str | None,
    entity_index: Any = None,
) -> str | None:
    normalized_area = _normalize_area_for_match(area)
    if not normalized_area:
        return None

    if entity_index is not None:
        try:
            entries = await entity_index.list_entries_async(
                domains={"assist_satellite"},
            )
            for entry in entries:
                if _normalize_area_for_match(getattr(entry, "area", None)) == normalized_area:
                    return entry.entity_id
        except Exception:
            logger.warning(
                "EntityIndex satellite lookup failed for area %s",
                area,
                exc_info=True,
            )

    try:
        states = await ha_client.get_states()
        for state in states:
            entity_id = state.get("entity_id", "")
            if not entity_id.startswith("assist_satellite."):
                continue
            state_area = state.get("attributes", {}).get("area_id")
            if _normalize_area_for_match(state_area) == normalized_area:
                return entity_id
    except Exception:
        logger.warning("Failed to resolve satellite for area %s", area, exc_info=True)
    return None


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
    normalized_area = _normalize_area_for_match(area)
    if not normalized_area:
        return None

    if entity_index is not None:
        try:
            entries = await entity_index.list_entries_async(domains={"media_player"})
            for entry in entries:
                if _normalize_area_for_match(getattr(entry, "area", None)) == normalized_area:
                    return entry.entity_id
        except Exception:
            logger.warning("EntityIndex media_player lookup failed for area %s", area, exc_info=True)

    try:
        states = await ha_client.get_states()
        for state in states:
            entity_id = state.get("entity_id", "")
            if not entity_id.startswith("media_player."):
                continue
            state_area = state.get("attributes", {}).get("area_id")
            if _normalize_area_for_match(state_area) == normalized_area:
                return entity_id
    except Exception:
        logger.warning("State scan media_player lookup failed for area %s", area, exc_info=True)
    return None


async def _resolve_timer_playback_target(
    ha_client: Any,
    *,
    origin_device_id: str | None,
    area: str | None,
    entity_index: Any = None,
) -> str | None:
    media_player = await _resolve_media_player_from_origin_device(ha_client, origin_device_id)
    if media_player:
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
    satellite_entity = await _resolve_satellite_from_origin_device(ha_client, origin_device_id)
    if not satellite_entity:
        satellite_entity = await _resolve_satellite_device(ha_client, area, entity_index=entity_index)

    resolved_media_player = media_player
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
    except Exception:
        logger.warning(
            "Failed to trigger conversation continuation on %s -- user must use wake word for follow-up",
            target_entity,
            exc_info=True,
        )
