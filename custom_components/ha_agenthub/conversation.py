"""Conversation entity for HA-AgentHub (I/O bridge)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Literal

import aiohttp

from homeassistant.components import assist_pipeline, conversation
from homeassistant.components.conversation import ConversationEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DOMAIN,
    WS_PATH,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    WS_HEARTBEAT_INTERVAL,
    WS_IDLE_THRESHOLD,
)

logger = logging.getLogger(__name__)


class _WsDroppedAfterSendError(Exception):
    """Request was written to the WebSocket; REST fallback would duplicate server work."""


def _rest_fallback_error_message(status_code: int | None) -> str:
    """Return an actionable fallback message for REST error responses."""
    if status_code in {401, 403}:
        return (
            "Sorry, the HA-AgentHub integration API key was rejected. "
            "Update the API key in the HA-AgentHub integration settings."
        )
    if status_code is not None and status_code >= 500:
        return (
            "Sorry, the assistant container returned an error. "
            "Check the configured container URL and the container logs."
        )
    return (
        "Sorry, the assistant container returned an unexpected response. "
        "Check the configured container URL and the container logs."
    )


def _strip_markdown(text: str) -> str:
    """Remove Markdown formatting for TTS-friendly output.

    FLOW-MED-4 / P3-1: this function is now a *defensive fallback only*.
    The container backend strips Markdown via
    ``container/app/agents/sanitize.strip_markdown`` and advertises the
    fact through the ``sanitized`` field on its REST/WebSocket responses
    (see ``ConversationResponse`` / ``StreamToken``). When that flag is
    True, ``_build_result`` skips this pass and treats the backend as
    the single source of truth. The implementation is kept in lock-step
    with the backend so legacy containers (< 0.18.35) and filler tokens
    (which are emitted unsanitized) still produce TTS-friendly output.
    """
    if not text:
        return text
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    text = re.sub(r"^[\s]*([-*_]){3,}\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the conversation entity from a config entry."""
    # Migrate legacy unique_id formats (incl. pre-0.5 domain ``agent_assist``)
    entity_registry = er.async_get(hass)
    _legacy_domain = "agent_assist"
    migration_pairs = [
        (_legacy_domain, "agent_assist"),
        (_legacy_domain, "agent_assist_conversation"),
        (_legacy_domain, _legacy_domain),
        (DOMAIN, DOMAIN),
        (DOMAIN, f"{DOMAIN}_conversation"),
    ]
    for int_domain, old_uid in migration_pairs:
        entity_id = entity_registry.async_get_entity_id("conversation", int_domain, old_uid)
        if entity_id:
            entity_registry.async_update_entity(entity_id, new_unique_id=entry.entry_id)
            logger.info(
                "Migrated entity %s unique_id from %s/%s to %s",
                entity_id, int_domain, old_uid, entry.entry_id,
            )

    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HaAgentHubConversationEntity(entry, data["url"], data["api_key"])])


class HaAgentHubConversationEntity(
    conversation.ConversationEntity,
):
    """Conversation entity that bridges HA voice to the HA-AgentHub container."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, entry: ConfigEntry, url: str, api_key: str) -> None:
        self._entry = entry
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._attr_unique_id = entry.entry_id
        self._reconnect_delay = RECONNECT_BASE_DELAY
        self._ws_lock = asyncio.Lock()
        self._ws_last_active: float = 0.0
        # Coalesce parallel HA calls with the same conversation_id + text (duplicate
        # pipeline invocations or WS+REST overlap) into a single bridge request.
        self._coalesce_lock = asyncio.Lock()
        # FLOW-COALESCE-1 (P2-3): value is (started_monotonic, task). The
        # started-timestamp guards a legitimate repeat of the same utterance
        # that arrives after the original response was already rendered --
        # without it we would short-circuit the second request onto the
        # first completed task forever.
        self._inflight_bridge: dict[tuple[str, str], tuple[float, asyncio.Task]] = {}
        self._coalesce_window_sec: float = 0.25
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="HA-AgentHub",
            model="Conversation bridge",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()
        try:
            assist_pipeline.async_migrate_engine(
                self.hass, "conversation", self._entry.entry_id, self.entity_id
            )
        except Exception:
            logger.debug("Pipeline engine migration skipped (not critical)")
        self._reconnect_task = self._entry.async_create_background_task(
            self.hass,
            self._reconnect_loop(),
            name="ha_agenthub_ws_reconnect",
        )

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        if hasattr(self, "_reconnect_task") and self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        await self._disconnect_ws()
        await super().async_will_remove_from_hass()

    async def _connect_ws(self) -> bool:
        """Establish persistent WebSocket connection to the container."""
        async with self._ws_lock:
            return await self._connect_ws_locked()

    async def _connect_ws_locked(self) -> bool:
        """Locked body of :meth:`_connect_ws`. Caller MUST hold
        ``self._ws_lock``. See FLOW-HIGH-8."""
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession()

            ws_url = self._url.replace("http://", "ws://").replace("https://", "wss://")
            self._ws = await self._session.ws_connect(
                f"{ws_url}{WS_PATH}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=aiohttp.ClientTimeout(total=10),
                heartbeat=WS_HEARTBEAT_INTERVAL,
            )
            self._reconnect_delay = RECONNECT_BASE_DELAY
            self._ws_last_active = time.monotonic()
            logger.info("Connected to HA-AgentHub container at %s", self._url)
            return True
        except (aiohttp.ClientError, TimeoutError):
            logger.warning("Failed to connect to container at %s", self._url)
            if self._session:
                try:
                    await self._session.close()
                except Exception:
                    pass
                self._session = None
            self._ws = None
            return False

    async def _disconnect_ws(self) -> None:
        """Close the WebSocket and session."""
        async with self._ws_lock:
            await self._disconnect_ws_locked()

    async def _disconnect_ws_locked(self) -> None:
        """Locked body of :meth:`_disconnect_ws`. Caller MUST hold
        ``self._ws_lock``."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session:
            await self._session.close()
            self._session = None

    async def _reconnect_loop(self) -> None:
        """Background loop that maintains the WebSocket connection."""
        while True:
            try:
                if self._ws is None or self._ws.closed:
                    connected = await self._connect_ws()
                    if not connected:
                        delay = self._reconnect_delay
                        self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX_DELAY)
                        logger.debug("Reconnect in %.1fs", delay)
                        await asyncio.sleep(delay)
                        continue
                # Connection is alive -- sleep before checking again
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error in reconnect loop")
                await asyncio.sleep(5)

    async def _ensure_connected(self) -> bool:
        """Ensure WebSocket is connected, reconnect if needed."""
        async with self._ws_lock:
            return await self._ensure_connected_locked()

    async def _ensure_connected_locked(self) -> bool:
        """Body of :meth:`_ensure_connected` that assumes the caller
        already holds ``self._ws_lock``.

        FLOW-HIGH-8 extracts this so ``_async_handle_message`` can
        hold the lock across both the connectivity check and the
        subsequent send -- closing the race where the WS flips to
        closed between the two calls.
        """
        if self._ws is not None and not self._ws.closed:
            if time.monotonic() - self._ws_last_active > WS_IDLE_THRESHOLD:
                try:
                    pong = self._ws.ping()
                    await asyncio.wait_for(pong, timeout=2.0)
                    self._ws_last_active = time.monotonic()
                except (asyncio.TimeoutError, Exception):
                    logger.warning("WebSocket idle ping failed, reconnecting")
                    await self._disconnect_ws_locked()
                    return await self._connect_ws_locked()
            return True
        connected = await self._connect_ws_locked()
        if not connected:
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX_DELAY)
        return connected

    def _schedule_reconnect(self) -> None:
        """Schedule an immediate background WS reconnect."""
        self._reconnect_delay = RECONNECT_BASE_DELAY
        self._entry.async_create_background_task(
            self.hass,
            self._connect_ws(),
            name="ha_agenthub_ws_immediate_reconnect",
        )

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Process a conversation turn by forwarding to the container.

        FLOW-HIGH-8: hold ``self._ws_lock`` across both the
        connectivity probe and the actual send so the socket cannot
        flip to closed between the two steps. All REST-fallback paths
        run *outside* the lock to avoid serializing fallback traffic
        behind a hung WS send.

        Duplicate invocations with the same ``conversation_id`` and user
        text are coalesced so only one WebSocket/REST round-trip runs;
        this matches traces where the container saw two identical turns
        back-to-back from production HA setups.
        """
        cid = user_input.conversation_id or ""
        text = (user_input.text or "").strip()
        device_id = getattr(user_input, "device_id", None)
        logger.debug(
            "ha-agenthub: turn-entry cid=%s device_id=%s text_len=%d",
            cid, device_id, len(text),
        )
        key = (cid, text)

        coalesced = False
        async with self._coalesce_lock:
            existing = self._inflight_bridge.get(key)
            now = time.monotonic()
            if existing is not None and (now - existing[0]) < self._coalesce_window_sec:
                bridge_task = existing[1]
                coalesced = True
            else:
                bridge_task = self.hass.async_create_task(
                    self._async_bridge_with_cleanup(user_input, key)
                )
                self._inflight_bridge[key] = (now, bridge_task)
        if coalesced:
            logger.info(
                "HA-AgentHub: coalescing duplicate request (same conversation + text) onto in-flight bridge"
            )
        return await bridge_task

    async def _async_bridge_with_cleanup(
        self,
        user_input: conversation.ConversationInput,
        key: tuple[str, str],
    ) -> conversation.ConversationResult:
        task = asyncio.current_task()
        try:
            return await self._async_bridge_to_container(user_input)
        finally:
            async with self._coalesce_lock:
                existing = self._inflight_bridge.get(key)
                if task is not None and existing is not None and existing[1] is task:
                    self._inflight_bridge.pop(key, None)

    async def _async_bridge_to_container(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Single WS (preferred) or REST attempt to the HA-AgentHub container."""
        try:
            async with self._ws_lock:
                if await self._ensure_connected_locked():
                    try:
                        return await self._process_via_ws(user_input)
                    except _WsDroppedAfterSendError:
                        logger.warning(
                            "WebSocket failed after the request was sent; skipping REST "
                            "(avoids duplicate container traces)",
                            exc_info=True,
                        )
                        await self._disconnect_ws_locked()
                        return self._build_result(
                            "The connection dropped before the reply finished. "
                            "If the action may have run, check your devices.",
                            user_input.conversation_id,
                            user_input.language,
                        )
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        logger.warning("WebSocket error, falling back to REST")
                        await self._disconnect_ws_locked()
        except Exception:
            logger.warning("Unexpected WS dispatch failure, falling back to REST", exc_info=True)

        result = await self._process_via_rest(user_input)
        self._schedule_reconnect()
        return result

    def _resolve_origin_context(self, user_input: conversation.ConversationInput) -> dict[str, str]:
        """Forward raw device_id and area_id to the container.

        The container maintains its own entity index and resolves
        human-readable names from its synced copy.  The bridge must
        not perform entity resolution (Prime Directive 1).
        """
        extra: dict[str, str] = {}
        device_id = getattr(user_input, "device_id", None)
        if device_id:
            extra["device_id"] = device_id
            # HA ConversationInput does not expose area_id directly;
            # the container resolves it from its own entity index via
            # the device_id we forward above.
        return extra

    def _resolve_satellite_entity(self, device_id: str | None) -> str | None:
        """Find the assist_satellite entity_id associated with a device.

        This is used solely to route container-directed filler_push
        directives to the correct satellite for audio playback.  It
        does not perform entity resolution on behalf of the container
        (Prime Directive 1).
        """
        if not device_id:
            logger.warning("filler_push: no device_id in ConversationInput, cannot resolve satellite")
            return None
        try:
            entity_registry = er.async_get(self.hass)
            entries = er.async_entries_for_device(entity_registry, device_id)
            logger.info("filler_push: device %s has %d registry entries", device_id, len(entries))
            for entry in entries:
                logger.info("filler_push: entry domain=%s entity_id=%s", entry.domain, entry.entity_id)
                if entry.domain == "assist_satellite":
                    return entry.entity_id
            logger.warning("filler_push: no assist_satellite entity found for device %s", device_id)
        except Exception:
            logger.warning(
                "filler_push: failed to resolve satellite entity for device %s", device_id, exc_info=True
            )
        return None

    async def _post_filler_push(
        self, filler_text: str, device_id: str | None
    ) -> None:
        """Execute a container-directed filler via assist_satellite.announce.

        Blocks until the announcement has finished playing on the device.
        The container decides when filler is needed; the integration merely
        executes the service call (Prime Directive 1 boundary).
        """
        satellite_entity = self._resolve_satellite_entity(device_id)
        if not satellite_entity:
            logger.warning(
                "filler_push: no satellite entity for device %s, skipping", device_id
            )
            return
        try:
            await self.hass.services.async_call(
                "assist_satellite",
                "announce",
                {
                    "message": filler_text,
                    "preannounce": False,
                },
                target={"entity_id": satellite_entity},
                blocking=False,
            )
            logger.info(
                "filler_push: successfully announced on %s: %s", satellite_entity, filler_text[:80]
            )
        except Exception:
            logger.warning(
                "filler_push: failed to announce on %s", satellite_entity, exc_info=True
            )

    async def _process_via_ws(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult:
        """Send request via WebSocket and accumulate streaming tokens."""
        logger.debug(
            "ha-agenthub: ws-entry cid=%s ws_open=%s",
            user_input.conversation_id,
            self._ws is not None and not self._ws.closed,
        )
        payload: dict[str, Any] = {
            "text": user_input.text,
            "conversation_id": user_input.conversation_id,
            "language": user_input.language or "en",
        }
        payload.update(self._resolve_origin_context(user_input))
        await self._ws.send_json(payload)

        try:
            speech_parts: list[str] = []
            buffered_filler_tasks: list[asyncio.Task] = []
            final_conversation_id = user_input.conversation_id
            device_id = getattr(user_input, "device_id", None)

            received_done = False
            # P3-1: track per-stream sanitization. The orchestrator emits
            # token / mediated_speech chunks already stripped by
            # ``app.agents.sanitize.strip_markdown``; the done frame
            # carries the flag explicitly. Default False so legacy
            # backends fall through the local strip pass.
            stream_sanitized = False

            while True:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=30.0)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)

                    # Container-directed filler_push directive: play via
                    # assist_satellite.announce outside the pipeline so the
                    # user hears interim audio while the real response is
                    # still being prepared.
                    filler_text = data.get("filler_push")
                    if filler_text is not None:
                        stripped_filler = _strip_markdown(str(filler_text).strip())
                        logger.info("filler_push: received from container (%s): %s", device_id, stripped_filler[:80])
                        if stripped_filler:
                            # Launch as background task so we keep reading
                            # the stream (the container may send more tokens
                            # while the filler is playing).
                            task = self.hass.async_create_task(
                                self._post_filler_push(stripped_filler, device_id)
                            )
                            buffered_filler_tasks.append(task)
                        continue

                    token_text = data.get("token", "")
                    if token_text:
                        speech_parts.append(token_text)
                    if data.get("done", False):
                        received_done = True
                        stream_err = data.get("error")
                        final_conversation_id = data.get("conversation_id", final_conversation_id)
                        mediated = data.get("mediated_speech")
                        if mediated:
                            speech_parts = [mediated]
                        # P3-1: backend signals sanitization on the done
                        # frame. Honour it for both ``mediated_speech``
                        # and accumulated tokens (the orchestrator
                        # strips both before emitting).
                        stream_sanitized = bool(data.get("sanitized", False))
                        if stream_err:
                            # Application-level error from the container (done chunk), not a
                            # transport failure — do not raise (would become _WsDroppedAfterSend).
                            logger.warning(
                                "Container reported error in stream done chunk: %s", stream_err
                            )
                            if not "".join(speech_parts).strip():
                                speech_parts = [
                                    "The assistant could not complete that request. "
                                    f"({stream_err})"
                                ]
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    self._ws = None
                    raise aiohttp.ClientError(
                        f"WebSocket {'closed' if msg.type == aiohttp.WSMsgType.CLOSED else 'error'} mid-stream"
                    )

            if not received_done:
                self._ws = None
                raise aiohttp.ClientError("WebSocket stream ended without done token")

            self._ws_last_active = time.monotonic()

            # Wait for any in-flight filler announcements to finish before
            # returning the final response, preventing TTS overlap.
            for task in buffered_filler_tasks:
                try:
                    await asyncio.wait_for(task, timeout=30.0)
                except Exception:
                    logger.warning("Filler task failed or timed out", exc_info=True)

            speech = "".join(speech_parts)
            return self._build_result(
                speech, final_conversation_id, user_input.language, sanitized=stream_sanitized
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as err:
            raise _WsDroppedAfterSendError() from err

    async def _process_via_rest(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult:
        """Fallback: send request via REST and get full response."""
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession()
            headers = {"Authorization": f"Bearer {self._api_key}"}
            payload: dict[str, Any] = {
                "text": user_input.text,
                "conversation_id": user_input.conversation_id,
                "language": user_input.language or "en",
            }
            payload.update(self._resolve_origin_context(user_input))
            async with self._session.post(
                f"{self._url}/api/conversation",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return self._build_result(
                        _rest_fallback_error_message(resp.status),
                        user_input.conversation_id,
                        user_input.language,
                    )
                data = await resp.json()
                return self._build_result(
                    data.get("speech", ""),
                    data.get("conversation_id", user_input.conversation_id),
                    user_input.language,
                    sanitized=bool(data.get("sanitized", False)),
                )
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
            return self._build_result(
                (
                    "Sorry, the assistant container is unavailable. "
                    "Check that the container is running and reachable from Home Assistant."
                ),
                user_input.conversation_id,
                user_input.language,
            )

    def _build_result(
        self,
        speech: str,
        conversation_id: str | None,
        language: str | None,
        *,
        sanitized: bool = False,
    ) -> conversation.ConversationResult:
        """Assemble a ConversationResult from the response.

        P3-1: ``sanitized`` indicates that the backend already stripped
        Markdown for TTS. When True we trust the backend (single source
        of truth) and skip the local ``_strip_markdown`` pass. Older
        backends that do not advertise the flag default to False so the
        defensive fallback still runs.
        """
        response = intent.IntentResponse(language=language or "en")
        response.async_set_speech(speech if sanitized else _strip_markdown(speech))
        return conversation.ConversationResult(response=response, conversation_id=conversation_id)
