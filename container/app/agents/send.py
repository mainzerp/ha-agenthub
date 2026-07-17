"""Send agent -- delivers content to devices via HA notify or TTS services."""

from __future__ import annotations

import logging
import re

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


@agent(
    agent_id="send-agent",
    name="Send Agent",
    description=(
        "Sends or delivers researched content, information, or messages "
        "to a person or device. Use when the user says 'send to', "
        "'schicke an', 'sende an' followed by a person name or device name. "
        "Examples: 'send the recipe to Laura Handy', "
        "'sende das Rezept an Satellite Kueche'."
    ),
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
            description=(
                "Sends or delivers researched content, information, or messages "
                "to a person or device. Use when the user says 'send to', "
                "'schicke an', 'sende an' followed by a person name or device name. "
                "Examples: 'send the recipe to Laura Handy', "
                "'sende das Rezept an Satellite Kueche'."
            ),
            skills=["send_message", "deliver_content", "notify_device"],
            endpoint="local://send-agent",
            expected_latency="low",
        )

    async def handle_task(self, task: DispatchTask) -> TaskResult:
        """Deliver content to the target device."""
        description = task.description or ""
        span_collector = task.span_collector

        # Parse target and content from the orchestrator-assembled description
        if _CONTENT_SEPARATOR in description:
            target_part, content = description.split(_CONTENT_SEPARATOR, 1)
        else:
            return self._error_result(
                AgentErrorCode.PARSE_ERROR,
                "No content provided for delivery.",
            )

        # Extract target name from target_part
        target_name = self._extract_target_name(target_part)
        if not target_name:
            return self._error_result(
                AgentErrorCode.PARSE_ERROR,
                "Could not determine target device from request.",
            )

        # Look up device mapping
        mapping = await SendDeviceMappingRepository.find_by_name(target_name)
        if not mapping:
            return self._error_result(
                AgentErrorCode.ENTITY_NOT_FOUND,
                f"No device mapping found for '{target_name}'. "
                "Please configure it in the dashboard under Send Devices.",
            )

        # Format content for channel (optional LLM call)
        formatted_content = await self._format_content(
            content.strip(),
            mapping["device_type"],
            mapping["display_name"],
            span_collector=span_collector,
        )

        # Deliver
        if mapping["device_type"] == "notify":
            async with _optional_span(span_collector, "ha_call", agent_id="send-agent") as span:
                await self._deliver_notify(mapping["ha_service_target"], formatted_content)
                span["metadata"]["service"] = "notify"
                span["metadata"]["target"] = mapping["ha_service_target"]
        elif mapping["device_type"] == "tts":
            async with _optional_span(span_collector, "ha_call", agent_id="send-agent") as span:
                await self._deliver_tts(mapping["ha_service_target"], formatted_content)
                span["metadata"]["service"] = "tts"
                span["metadata"]["target"] = mapping["ha_service_target"]

        language = (task.context.language if task.context else "en") or "en"
        if language.startswith("de"):
            speech = f"Inhalt an {mapping['display_name']} gesendet."
        else:
            speech = f"Content sent to {mapping['display_name']}."

        return TaskResult(speech=speech)

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
            {"message": content, "title": "HA-AgentHub"},
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
