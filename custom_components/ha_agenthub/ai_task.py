"""AI Task entity that forwards generate-data tasks to the HA-AgentHub container."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from homeassistant.components import ai_task, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from voluptuous_openapi import convert

from .client import (
    AgentHubAuthError,
    AgentHubConnectionError,
    AgentHubResponseError,
    async_post_json,
)
from .const import DOMAIN

logger = logging.getLogger(__name__)

AI_TASK_PATH = "/api/ai_task"
# Limits mirror the container's validation of POST /api/ai_task. They are
# enforced here first so oversized payloads never leave Home Assistant.
MAX_INSTRUCTIONS_LENGTH = 4000
MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
AI_TASK_TIMEOUT = 180.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the AI Task entity from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HaAgentHubAITaskEntity(entry, data["url"], data["api_key"])])


def _normalize_mime_type(mime_type: str | None, path: Path) -> str:
    """Return a lower-case MIME type, guessed from the file name when missing."""
    if not mime_type:
        mime_type = mimetypes.guess_type(path.name)[0] or ""
    mime_type = mime_type.strip().lower()
    if mime_type == "image/jpg":
        mime_type = "image/jpeg"
    return mime_type


def _read_attachment_files(files: list[tuple[Path, str]]) -> list[dict[str, str]]:
    """Read and base64-encode attachment files. Runs in the executor."""
    encoded: list[dict[str, str]] = []
    for path, mime_type in files:
        try:
            size = path.stat().st_size
        except OSError as err:
            raise HomeAssistantError(
                f"Attachment {path.name} cannot be read: {err.strerror or err}"
            ) from err
        if size > MAX_ATTACHMENT_BYTES:
            raise HomeAssistantError(
                f"Attachment {path.name} is too large ({size} bytes); "
                f"the maximum is {MAX_ATTACHMENT_BYTES} bytes"
            )
        try:
            raw = path.read_bytes()
        except OSError as err:
            raise HomeAssistantError(
                f"Attachment {path.name} cannot be read: {err.strerror or err}"
            ) from err
        # Re-check after reading: the file may have grown since stat().
        if len(raw) > MAX_ATTACHMENT_BYTES:
            raise HomeAssistantError(
                f"Attachment {path.name} is too large ({len(raw)} bytes); "
                f"the maximum is {MAX_ATTACHMENT_BYTES} bytes"
            )
        encoded.append(
            {"mime_type": mime_type, "data": base64.b64encode(raw).decode("ascii")}
        )
    return encoded


class HaAgentHubAITaskEntity(ai_task.AITaskEntity):
    """AI Task entity backed by the HA-AgentHub container."""

    _attr_has_entity_name = True
    _attr_translation_key = "ai_task"
    _attr_supported_features = (
        ai_task.AITaskEntityFeature.GENERATE_DATA
        | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
    )

    def __init__(self, entry: ConfigEntry, url: str, api_key: str) -> None:
        self._entry = entry
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._attr_unique_id = f"{entry.entry_id}_ai_task"
        # Must stay identical to the conversation entity's device info
        # (conversation.py) so both entities share one service device.
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="HA-AgentHub",
            model="Conversation bridge",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Send a generate-data task to the container and record the answer."""
        instructions = task.instructions or ""
        if len(instructions) > MAX_INSTRUCTIONS_LENGTH:
            raise HomeAssistantError(
                f"AI Task instructions are too long ({len(instructions)} "
                f"characters); the maximum is {MAX_INSTRUCTIONS_LENGTH}"
            )

        attachments = await self._async_prepare_attachments(task.attachments or [])
        structure = self._convert_structure(task.structure, chat_log)

        payload: dict[str, Any] = {
            "task_name": task.name,
            "instructions": instructions,
            "structure": structure,
            "attachments": attachments,
            "conversation_id": chat_log.conversation_id,
            "language": self.hass.config.language,
            # AI Task chat logs carry no user or device context (core builds
            # the LLM context with context=None and device_id=None).
            "user_id": None,
            "device_id": None,
        }
        logger.debug(
            "Sending AI task %r to container (structured=%s, attachments=%d)",
            task.name,
            structure is not None,
            len(attachments),
        )

        try:
            response = await async_post_json(
                self.hass,
                self._url,
                self._api_key,
                AI_TASK_PATH,
                payload,
                timeout=AI_TASK_TIMEOUT,
            )
        except AgentHubAuthError as err:
            raise HomeAssistantError(
                f"HA-AgentHub API key rejected by the container (HTTP {err.status}); "
                "reconfigure the integration with a valid API key"
            ) from err
        except AgentHubResponseError as err:
            message = f"HA-AgentHub AI task failed (HTTP {err.status})"
            if err.detail:
                message = f"{message}: {err.detail}"
            raise HomeAssistantError(message) from err
        except AgentHubConnectionError as err:
            raise HomeAssistantError(
                f"Cannot reach the HA-AgentHub container: {err}"
            ) from err

        if "data" not in response:
            raise HomeAssistantError(
                "HA-AgentHub AI task response does not contain data"
            )
        data = response["data"]
        if structure is not None and isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError as err:
                raise HomeAssistantError(
                    "HA-AgentHub AI task returned invalid structured data"
                ) from err

        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=self.entity_id, content=text)
        )
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id,
            data=data,
        )

    async def _async_prepare_attachments(
        self, attachments: list[conversation.Attachment]
    ) -> list[dict[str, str]]:
        """Validate attachments and return them base64-encoded."""
        if len(attachments) > MAX_ATTACHMENTS:
            raise HomeAssistantError(
                f"Too many attachments ({len(attachments)}); "
                f"the maximum is {MAX_ATTACHMENTS}"
            )
        files: list[tuple[Path, str]] = []
        for attachment in attachments:
            path = Path(attachment.path)
            mime_type = _normalize_mime_type(attachment.mime_type, path)
            if not mime_type.startswith("image/"):
                raise HomeAssistantError(
                    f"Unsupported attachment type {mime_type or 'unknown'} for "
                    f"{path.name}; only images are supported"
                )
            files.append((path, mime_type))
        if not files:
            return []
        return await self.hass.async_add_executor_job(_read_attachment_files, files)

    @staticmethod
    def _convert_structure(
        structure: Any, chat_log: conversation.ChatLog
    ) -> dict[str, Any] | None:
        """Convert the task's voluptuous structure to a JSON schema."""
        if structure is None:
            return None
        llm_api = getattr(chat_log, "llm_api", None)
        return convert(
            structure,
            custom_serializer=(
                llm_api.custom_serializer if llm_api else llm.selector_serializer
            ),
        )
