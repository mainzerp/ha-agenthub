"""Send agent -- delivers content to devices via HA notify or TTS services."""

from __future__ import annotations

import logging
import re

import httpx

from app.agents.base import BaseAgent, _render_prompt_template
from app.agents.decorator import agent
from app.analytics.tracer import _optional_span
from app.db.repository import SendDeviceMappingRepository, SettingsRepository
from app.models.agent import (
    AgentCard,
    AgentErrorCode,
    DispatchTask,
    TaskResult,
)

logger = logging.getLogger(__name__)

# Prefix used by orchestrator to pass content in the condensed task
_CONTENT_SEPARATOR = "|||CONTENT|||"

# Shared by the @agent registration and agent_card so both stay identical.
_SEND_AGENT_DESCRIPTION = (
    "Sends or delivers researched content, information, or messages "
    "to a person or device, including messages the user dictates verbatim. "
    "Use when the user asks to send, deliver, or forward something to a person name or device name. "
    "Examples: 'send the recipe to Laura Handy', "
    "'send Anna the message: I am running late'."
)

# User-facing send-flow speech keyed by message id, then language key.
# English is the default; resolve via ``localized_send_speech``.
_SEND_SPEECH: dict[str, dict[str, str]] = {
    "sent": {
        "en": "Content sent to {name}.",
        "de": "Inhalt an {name} gesendet.",
    },
    "no_content": {
        "en": "No content provided for delivery.",
        "de": "Es wurde kein Inhalt zum Senden übergeben.",
    },
    "no_target": {
        "en": "Could not determine target device from request.",
        "de": "Ich konnte das Zielgerät nicht erkennen.",
    },
    "no_mapping": {
        "en": "No matching send device mapping is configured. Please add it in the dashboard under Send Devices.",
        "de": "Für dieses Ziel ist kein passendes Sendegerät eingerichtet. "
        "Bitte lege es im Dashboard unter Send Devices an.",
    },
    "unknown_device_type": {
        "en": "Cannot deliver to {name}: unknown device type '{device_type}'. "
        "Please reconfigure it in the dashboard under Send Devices.",
        "de": "Ich kann nicht an {name} senden: unbekannter Gerätetyp '{device_type}'. "
        "Bitte richte es im Dashboard unter Send Devices neu ein.",
    },
    "ha_unavailable": {
        "en": "I could not reach the smart home system to deliver to {name}.",
        "de": "Ich konnte das Smart-Home-System nicht erreichen, um an {name} zu senden.",
    },
    "delivery_failed": {
        "en": "Sorry, I could not deliver the content to {name}.",
        "de": "Entschuldigung, ich konnte den Inhalt nicht an {name} senden.",
    },
    # Orchestrator sequential-send fallbacks (content leg failed or empty).
    "no_content_available": {
        "en": "No content available to send.",
        "de": "Es ist kein Inhalt zum Senden vorhanden.",
    },
    "content_unavailable": {
        "en": "I could not prepare the content to send.",
        "de": "Ich konnte den Inhalt zum Senden nicht vorbereiten.",
    },
}


_TEMPLATE_OPENERS_RE = re.compile(r"\{(?=[{%#])")


def neutralize_ha_template(text: str) -> str:
    """Break Jinja delimiters so HA renders user/LLM content literally.

    The legacy ``notify.*`` services treat ``message`` as a template, so
    delivered content containing ``{{ ... }}`` / ``{% ... %}`` would be
    evaluated by Home Assistant (reading arbitrary entity states).
    Inserting a space after the opening brace keeps the text readable.
    """
    return _TEMPLATE_OPENERS_RE.sub("{ ", text or "")


def localized_send_speech(message_id: str, language: str | None, **values: str) -> str:
    """Return the send-flow speech for ``message_id`` in ``language`` (English fallback)."""
    lang_key = "de" if (language or "en").lower().startswith("de") else "en"
    templates = _SEND_SPEECH[message_id]
    return templates.get(lang_key, templates["en"]).format(**values)


@agent(
    agent_id="send-agent",
    name="Send Agent",
    description=_SEND_AGENT_DESCRIPTION,
    skills=["send_message", "deliver_content", "notify_device"],
    needs_entity_matcher=False,
    db_gated=True,
    expected_latency="low",
)
class SendAgent(BaseAgent):
    """Delivers pre-produced content to a target device.

    Supports two delivery channels:
    - notify: smartphone push via HA notify.* service
    - tts: satellite speaker via HA tts.speak service
    """

    def __init__(self, ha_client=None, entity_index=None) -> None:
        super().__init__(ha_client=ha_client, entity_index=entity_index)

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="send-agent",
            name="Send Agent",
            description=_SEND_AGENT_DESCRIPTION,
            skills=["send_message", "deliver_content", "notify_device"],
            endpoint="local://send-agent",
            expected_latency="low",
        )

    async def handle_task(self, task: DispatchTask) -> TaskResult:
        """Deliver content to the target device."""
        description = task.description or ""
        span_collector = task.span_collector
        language = (task.context.language if task.context else "en") or "en"

        # Parse target and content from the orchestrator-assembled description
        if _CONTENT_SEPARATOR in description:
            target_part, content = description.split(_CONTENT_SEPARATOR, 1)
        else:
            return self._error_result(
                AgentErrorCode.PARSE_ERROR,
                localized_send_speech("no_content", language),
            )

        target_text = target_part.strip()
        if not target_text:
            return self._error_result(
                AgentErrorCode.PARSE_ERROR,
                localized_send_speech("no_target", language),
            )

        # Look up device mapping. The speech never echoes the raw target
        # text: it is condensed classifier output, not a device name.
        mapping = await self._resolve_mapping(target_text)
        if not mapping:
            logger.info("No send-device mapping matched target text %r", target_text)
            return self._error_result(
                AgentErrorCode.ENTITY_NOT_FOUND,
                localized_send_speech("no_mapping", language),
            )

        # Format content for channel (optional LLM call)
        formatted_content = await self._format_content(
            content.strip(),
            mapping["device_type"],
            mapping["display_name"],
            span_collector=span_collector,
        )

        # Deliver
        device_type = mapping["device_type"]
        if device_type not in ("notify", "tts"):
            logger.error("Unknown device_type %r in send-device mapping for %r", device_type, mapping["display_name"])
            return self._error_result(
                AgentErrorCode.ACTION_FAILED,
                localized_send_speech(
                    "unknown_device_type", language, name=mapping["display_name"], device_type=str(device_type)
                ),
            )
        try:
            if device_type == "notify":
                async with _optional_span(span_collector, "ha_call", agent_id="send-agent") as span:
                    await self._deliver_notify(mapping["ha_service_target"], formatted_content)
                    span["metadata"]["service"] = "notify"
                    span["metadata"]["target"] = mapping["ha_service_target"]
            else:
                async with _optional_span(span_collector, "ha_call", agent_id="send-agent") as span:
                    await self._deliver_tts(mapping["ha_service_target"], formatted_content)
                    span["metadata"]["service"] = "tts"
                    span["metadata"]["target"] = mapping["ha_service_target"]
        except httpx.HTTPError:
            logger.warning("Delivery to %s failed: HA unreachable", mapping["display_name"], exc_info=True)
            return self._error_result(
                AgentErrorCode.HA_UNAVAILABLE,
                localized_send_speech("ha_unavailable", language, name=mapping["display_name"]),
                recoverable=False,
            )
        except Exception:
            logger.exception("Delivery to %s failed", mapping["display_name"])
            return self._error_result(
                AgentErrorCode.ACTION_FAILED,
                localized_send_speech("delivery_failed", language, name=mapping["display_name"]),
            )

        return TaskResult(speech=localized_send_speech("sent", language, name=mapping["display_name"]))

    async def _resolve_mapping(self, target_text: str) -> dict | None:
        """Resolve the send-device mapping for the target text, deterministically.

        Order: exact/normalized name on the full text, then on the
        regex-extracted name, then a word-boundary scan of all configured
        mappings inside the text (longest match wins, ties are ambiguous).
        """
        mapping = await SendDeviceMappingRepository.find_by_name(target_text)
        if mapping:
            return mapping
        extracted = self._extract_target_name(target_text)
        if extracted and extracted != target_text:
            mapping = await SendDeviceMappingRepository.find_by_name(extracted)
            if mapping:
                return mapping
        return await SendDeviceMappingRepository.find_in_text(target_text)

    def _extract_target_name(self, text: str) -> str | None:
        """Extract target device name from condensed task text."""
        patterns = [
            r"(?:sende|schicke|senden|schicken)\s+an\s+(.+)",
            r"(?:send|deliver)\s+(?:to|an)\s+(.+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return text.strip() if text.strip() else None

    async def _format_content(
        self,
        content: str,
        delivery_type: str,
        target_name: str,
        span_collector=None,
    ) -> str:
        """Optionally format content via LLM for the delivery channel."""
        try:
            prompt_template = await self._load_prompt_async("send")
            wrapped_content = self._wrap_user_input(content)
            prompt = _render_prompt_template(
                prompt_template,
                delivery_type=delivery_type,
                target_name=target_name,
                content=wrapped_content,
            )
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": wrapped_content},
            ]
            async with _optional_span(span_collector, "llm_call", agent_id="send-agent") as span:
                result = await self._call_llm(
                    messages, span_collector=span_collector, max_tokens=1024 if delivery_type == "notify" else 512
                )
                span["metadata"]["model"] = "send-agent"
                span["metadata"]["delivery_type"] = delivery_type
                span["metadata"]["llm_response"] = (result or "")[:500]
            if result and result.strip():
                return result.strip()
        except Exception:
            logger.warning("LLM formatting failed, using raw content", exc_info=True)
        return content

    async def _deliver_notify(self, service_target: str, content: str) -> None:
        """Send via HA notify.* service (smartphone push)."""
        await self._ha_client.call_service(
            "notify",
            service_target,
            None,
            {"message": neutralize_ha_template(content), "title": "HA-AgentHub"},
        )
        logger.info("Notify sent to %s", service_target)

    async def _deliver_tts(self, media_player_entity: str, content: str) -> None:
        """Send via TTS to a satellite media_player entity."""
        tts_engine = await SettingsRepository.get_value("tts.engine", "tts.google_translate_say")
        await self._ha_client.call_service(
            "tts",
            "speak",
            tts_engine,
            {"media_player_entity_id": media_player_entity, "message": content},
        )
        logger.info("TTS sent to %s", media_player_entity)
