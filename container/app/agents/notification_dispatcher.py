"""Multi-channel notification dispatcher for timer and alarm events."""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from typing import Any

from app.db.repository import SettingsRepository
from app.security.sanitization import wrap_user_input
from app.util.tasks import spawn

logger = logging.getLogger(__name__)


def spawn_voice_followup_after_conversation(
    ha_client: Any,
    *,
    area_id: str | None = None,
    origin_device_id: str | None = None,
    entity_index: Any = None,
) -> None:
    """Schedule Assist STT to resume after the spoken response.

    - With ``area_id``: prefer ``assist_satellite`` in that area (fixed
      speakers).
    - With ``origin_device_id`` only (typical **Companion app**): run the
      pipeline on that **device registry** id — phones often have no
      ``area_id`` on the Assist request.

    No-op without ``ha_client`` or without at least one of
    ``area_id`` / ``origin_device_id``.
    """
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
    """Start ``assist_pipeline.run`` using a known HA device registry UUID."""
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
    except Exception:
        logger.warning(
            "Failed to trigger conversation continuation for device %s",
            device_registry_id,
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
        await _trigger_conversation_continuation_on_registry_device(ha_client, origin_device_id, profile)
        return

    logger.debug("Voice follow-up skipped: no satellite and no origin_device_id")


# Fallback message templates (used when LLM call fails)
_FALLBACK_MESSAGES = {
    "de": "Timer {name} ist abgelaufen",
    "en": "Timer {name} has finished",
}

_GENERIC_FALLBACK_MESSAGES = {
    "de": "Der Timer ist abgelaufen",
    "en": "The timer has finished",
}

# Delay after TTS before starting to listen (seconds).
_TTS_TO_LISTEN_DELAY = 10.0

# Default chime URL
_DEFAULT_CHIME_URL = "media-source://media_source/local/notification.mp3"

# Delay between chime and TTS (seconds)
_CHIME_TO_TTS_DELAY = 1.5


async def dispatch_timer_notification(
    ha_client: Any,
    timer_name: str,
    entity_id: str,
    metadata: Any = None,
    entity_index: Any = None,
) -> None:
    """Dispatch notifications across all configured channels.

    ``entity_index`` (optional) is the live EntityIndex; when supplied
    it is used to resolve the assist_satellite entity_id for the
    origin area via FLOW-HIGH-6 (EntityIndex.area is authoritative,
    unlike HA state attributes which do not carry area_id).
    """
    profile = await _load_notification_profile()
    language = await SettingsRepository.get_value("language") or "en"
    lang_key = "de" if language.startswith("de") else "en"

    media_player = metadata.media_player_entity if metadata else None
    origin_device_id = getattr(metadata, "origin_device_id", None) if metadata else None
    area = metadata.origin_area if metadata else None
    duration = metadata.duration if metadata else None

    if not media_player:
        media_player = await _resolve_timer_playback_target(
            ha_client,
            origin_device_id=origin_device_id,
            area=area,
            entity_index=entity_index,
        )
        if media_player:
            logger.info(
                "Timer notification playback target resolved from origin metadata (device_id=%s, area=%s): %s",
                origin_device_id,
                area,
                media_player,
            )
        else:
            logger.warning(
                "Timer notification has no resolvable playback target (device_id=%s, area=%s)",
                origin_device_id,
                area,
            )

    # Determine if timer has a meaningful name
    has_meaningful_name = _has_meaningful_timer_name(timer_name, entity_id)

    # Generate TTS message via LLM (returns None for unnamed timers)
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

    # Channel 1: TTS on origin device
    if profile.get("tts_enabled", True) and media_player:
        # Play chime first
        if profile.get("chime_enabled", True):
            await _play_chime(ha_client, media_player, profile)

        if message:
            # Named timer: chime + TTS + listen
            await _notify_tts(ha_client, media_player, message, profile)

        # Trigger conversation continuation to listen for follow-up
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

    # Channel 2: Persistent notification (always, if we have a message)
    if profile.get("persistent_enabled", True) and message:
        await _notify_persistent(ha_client, timer_name, message)

    # Channel 3: Mobile push
    if profile.get("push_enabled", False) and message:
        push_targets = profile.get("push_targets", [])
        await _notify_push(ha_client, push_targets, timer_name, message)


async def dispatch_alarm_notification(
    ha_client: Any,
    alarm_name: str,
    entity_id: str,
) -> None:
    """Dispatch notifications for an alarm (input_datetime) that has fired."""
    profile = await _load_notification_profile()
    language = await SettingsRepository.get_value("language") or "en"
    lang_key = "de" if language.startswith("de") else "en"

    alarm_messages = {
        "de": "Alarm {name} ist ausgeloest",
        "en": "Alarm {name} has triggered",
    }
    message = alarm_messages.get(lang_key, alarm_messages["en"]).format(name=alarm_name)

    if profile.get("persistent_enabled", True):
        await _notify_persistent(ha_client, alarm_name, message)

    if profile.get("push_enabled", False):
        push_targets = profile.get("push_targets", [])
        await _notify_push(ha_client, push_targets, alarm_name, message)


def _has_meaningful_timer_name(timer_name: str, entity_id: str) -> bool:
    """Check if the timer has a meaningful user-given name."""
    if not timer_name:
        return False
    name_lower = timer_name.strip().lower()
    if name_lower in ("timer", "timer 1", "timer 2", "timer 3"):
        return False
    # entity_id-style names like "timer.timer_1" are not meaningful
    return name_lower != entity_id.split(".", 1)[-1].replace("_", " ")


async def _generate_tts_message(
    timer_name: str,
    duration: str | None,
    area: str | None,
    language: str,
    has_meaningful_name: bool = True,
) -> str | None:
    """Call LLM to generate a short TTS announcement for a finished timer.

    Returns the generated message string, or None if the timer has no
    meaningful name (chime-only) or if the LLM call fails.
    """
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
    """Play a notification chime on the media_player before TTS."""
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
    """Play TTS message on a media_player entity."""
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


async def _notify_persistent(
    ha_client: Any,
    timer_name: str,
    message: str,
) -> None:
    """Create a persistent notification in HA UI."""
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
    """Send push notifications to mobile_app targets."""
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
    """Load notification profile from settings repository."""
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
    """Find the assist_satellite entity_id for the given area.

    FLOW-HIGH-6: HA ``state.attributes`` does not carry ``area_id``,
    so the old state-scan loop never matched in production. The
    EntityIndex tracks area assignments from the device/entity
    registry and is authoritative. If an EntityIndex is provided, use
    it; otherwise fall back to the (mostly non-functional) state scan
    so callers without an index still behave as before.
    """
    if not area:
        return None

    if entity_index is not None:
        try:
            entries = await entity_index.list_entries_async(
                domains={"assist_satellite"},
            )
            for e in entries:
                if getattr(e, "area", None) == area:
                    return e.entity_id
        except Exception:
            logger.warning(
                "EntityIndex satellite lookup failed for area %s",
                area,
                exc_info=True,
            )

    try:
        states = await ha_client.get_states()
        for s in states:
            eid = s.get("entity_id", "")
            if eid.startswith("assist_satellite.") and s.get("attributes", {}).get("area_id") == area:
                return eid
    except Exception:
        logger.warning("Failed to resolve satellite for area %s", area, exc_info=True)
    return None


async def _resolve_ha_device_id(
    ha_client: Any,
    entity_id: str,
) -> str | None:
    """Look up the HA device registry id for ``entity_id`` via the
    template endpoint.

    FLOW-HIGH-5: HA's ``assist_pipeline.run`` validates ``device_id``
    as a registry UUID, not an entity_id. We resolve it by rendering
    ``{{ device_id('<entity_id>') }}`` server-side. Returns ``None``
    on any error or when HA renders ``"None"`` / empty text (entity
    unknown or not tied to a device).
    """
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

    if not rendered:
        return None
    if rendered.lower() == "none":
        return None
    return rendered


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


async def _resolve_media_player_from_area(
    ha_client: Any,
    area: str | None,
    entity_index: Any = None,
) -> str | None:
    if not area:
        return None

    if entity_index is not None:
        try:
            entries = await entity_index.list_entries_async(domains={"media_player"})
            for e in entries:
                if getattr(e, "area", None) == area:
                    return e.entity_id
        except Exception:
            logger.warning(
                "EntityIndex media_player lookup failed for area %s",
                area,
                exc_info=True,
            )

    try:
        states = await ha_client.get_states()
        for s in states:
            eid = s.get("entity_id", "")
            if not eid.startswith("media_player."):
                continue
            if s.get("attributes", {}).get("area_id") == area:
                return eid
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


async def _trigger_conversation_continuation(
    ha_client: Any,
    media_player_entity: str,
    area: str | None,
    profile: dict,
    entity_index: Any = None,
) -> None:
    """After TTS plays, trigger the satellite to listen for a voice follow-up."""
    if not profile.get("voice_followup_enabled", True):
        return

    delay = profile.get("tts_to_listen_delay", _TTS_TO_LISTEN_DELAY)
    await asyncio.sleep(delay)

    # Prefer the assist_satellite entity registered for the origin
    # area; the voice pipeline is tied to satellite devices, not the
    # media_player chosen for TTS playback. Fall back to the
    # media_player entity so single-device setups still resolve.
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
