"""Conversation entity for HA-AgentHub (I/O bridge)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Literal

import aiohttp
from urllib.parse import urlparse

from homeassistant.components import assist_pipeline, conversation
from homeassistant.components.conversation import ConversationEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

try:
    from homeassistant.helpers.event import async_track_state_change_event
except (ImportError, ModuleNotFoundError):
    async_track_state_change_event = None

from .const import (
    DOMAIN,
    WS_PATH,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    WS_HEARTBEAT_INTERVAL,
    WS_IDLE_THRESHOLD,
)

logger = logging.getLogger(__name__)

_RESULT_SUPPORTS_CONTINUE_CONVERSATION = (
    "continue_conversation"
    in inspect.signature(conversation.ConversationResult).parameters
)

# V4: satellite states that indicate the device is busy or idle
_SAT_BUSY_STATES = frozenset({"listening", "processing", "responding"})
_SAT_IDLE_STATES = frozenset({"idle"})

# How long the background push task waits for the final frame after filler
PUSH_FINAL_WAIT_SECONDS = 45.0
# How long to wait for the satellite to return to idle before announcing
MAX_POST_FILLER_WAIT_SECONDS = 8.0


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


# Pre-compiled regex patterns for _strip_markdown (LOW-15)
_STRIP_MARKDOWN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"```[a-zA-Z]*\n?"), ""),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\[[^\]]*\]"), r"\1"),
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),
    (re.compile(r"\*{1,3}([^*]+)\*{1,3}"), r"\1"),
    (re.compile(r"_{1,3}([^_]+)_{1,3}"), r"\1"),
    (re.compile(r"~~([^~]+)~~"), r"\1"),
    (re.compile(r"^[\s]*([-*_]){3,}\s*$", re.MULTILINE), ""),
    (re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE), ""),
    (re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE), ""),
    (re.compile(r"^>\s?", re.MULTILINE), ""),
    (re.compile(r"<[^>]+>"), ""),
    (re.compile(r"https?://\S+"), ""),
    (re.compile(r"\n{3,}"), "\n\n"),
    (re.compile(r" {2,}"), " "),
]


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
    for pattern, replacement in _STRIP_MARKDOWN_PATTERNS:
        text = pattern.sub(replacement, text)
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
        entity_id = entity_registry.async_get_entity_id(
            "conversation", int_domain, old_uid
        )
        if entity_id:
            entity_registry.async_update_entity(entity_id, new_unique_id=entry.entry_id)
            logger.info(
                "Migrated entity %s unique_id from %s/%s to %s",
                entity_id,
                int_domain,
                old_uid,
                entry.entry_id,
            )

    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [HaAgentHubConversationEntity(entry, data["url"], data["api_key"])]
    )


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
        # V4: at most one in-flight post-filler push task per satellite.
        self._inflight_pushes: dict[str, asyncio.Task] = {}
        self._reconnect_immediate_task: asyncio.Task | None = None
        # (removed dead reentrancy guard -- was _push_in_progress_satellites)
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="HA-AgentHub",
            model="Conversation bridge",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

        def _cancel_pushes() -> None:
            for sat_id, task in list(self._inflight_pushes.items()):
                if not task.done():
                    task.cancel()
            self._inflight_pushes.clear()

        self._entry.async_on_unload(_cancel_pushes)

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
        except (AttributeError, ValueError, KeyError):
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
        if (
            hasattr(self, "_reconnect_immediate_task")
            and self._reconnect_immediate_task
        ):
            self._reconnect_immediate_task.cancel()
            self._reconnect_immediate_task = None
        for sat_id, task in list(self._inflight_pushes.items()):
            task.cancel()
        self._inflight_pushes.clear()
        for key, (_, task) in list(self._inflight_bridge.items()):
            if not task.done():
                task.cancel()
        self._inflight_bridge.clear()
        await self._disconnect_ws()
        await super().async_will_remove_from_hass()

    async def _connect_ws(self) -> bool:
        """Establish persistent WebSocket connection to the container."""
        async with self._ws_lock:
            return await self._connect_ws_locked()

    async def _connect_ws_locked(self) -> bool:
        """Locked body of :meth:`_connect_ws`. Caller MUST hold
        ``self._ws_lock``. See FLOW-HIGH-8."""
        if self._ws is not None and not self._ws.closed:
            return True
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()

            parsed = urlparse(self._url)
            ws_scheme = "wss" if parsed.scheme == "https" else "ws"
            ws_url = parsed._replace(scheme=ws_scheme).geturl()
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
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            logger.warning("Failed to connect to container at %s", self._url)
            if self._session:
                try:
                    await self._session.close()
                except (aiohttp.ClientError, OSError):
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
                        self._reconnect_delay = min(
                            self._reconnect_delay * 2, RECONNECT_MAX_DELAY
                        )
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
                except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
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
        if self._reconnect_immediate_task is not None:
            self._reconnect_immediate_task.cancel()
        self._reconnect_immediate_task = self._entry.async_create_background_task(
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
            cid,
            device_id,
            len(text),
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
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            logger.warning(
                "Unexpected WS dispatch failure, falling back to REST", exc_info=True
            )

        result = await self._process_via_rest(user_input)
        self._schedule_reconnect()
        return result

    def _resolve_origin_context(
        self, user_input: conversation.ConversationInput
    ) -> dict[str, str]:
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
            logger.warning(
                "filler_push: no device_id in ConversationInput, cannot resolve satellite"
            )
            return None
        try:
            entity_registry = er.async_get(self.hass)
            entries = er.async_entries_for_device(entity_registry, device_id)
            logger.info(
                "filler_push: device %s has %d registry entries",
                device_id,
                len(entries),
            )
            for entry in entries:
                logger.info(
                    "filler_push: entry domain=%s entity_id=%s",
                    entry.domain,
                    entry.entity_id,
                )
                if entry.domain == "assist_satellite":
                    return entry.entity_id
            logger.warning(
                "filler_push: no assist_satellite entity found for device %s", device_id
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            logger.warning(
                "filler_push: failed to resolve satellite entity for device %s",
                device_id,
                exc_info=True,
            )
        return None

    def _spawn_post_filler_push(
        self,
        *,
        local_ws: aiohttp.ClientWebSocketResponse,
        satellite_entity_id: str | None,
        gate_key: str,
    ) -> None:
        """Spawn the post-filler background push task."""
        key = satellite_entity_id or f"__no_sat__:{gate_key}"
        previous = self._inflight_pushes.get(key)
        if previous is not None and not previous.done():
            logger.info(
                "ha-agenthub: cancelling previous post-filler push key=%s sat=%s",
                gate_key,
                satellite_entity_id,
            )
            previous.cancel()
        task = self._entry.async_create_background_task(
            self.hass,
            self._post_filler_push(
                local_ws=local_ws,
                satellite_entity_id=satellite_entity_id,
                gate_key=gate_key,
                key=key,
            ),
            name=f"ha_agenthub_post_filler_push:{key}",
        )
        self._inflight_pushes[key] = task

    async def _post_filler_push(
        self,
        *,
        local_ws: aiohttp.ClientWebSocketResponse,
        satellite_entity_id: str | None,
        gate_key: str,
        key: str,
    ) -> None:
        """Read the post-filler final response and push it after idle."""
        final_text: str | None = None
        final_parts: list[str] = []
        observed_idle = asyncio.Event()
        aborted_new_turn = False
        voice_followup = False
        unsub: Callable[[], None] | None = None

        def _on_state(event) -> None:
            nonlocal aborted_new_turn
            new_state = event.data.get("new_state") if event else None
            new_state_value = getattr(new_state, "state", None)
            if new_state_value in _SAT_IDLE_STATES:
                observed_idle.set()
            elif new_state_value in _SAT_BUSY_STATES and observed_idle.is_set():
                aborted_new_turn = True
                observed_idle.set()

        try:
            if satellite_entity_id and async_track_state_change_event is not None:
                unsub = async_track_state_change_event(
                    self.hass,
                    [satellite_entity_id],
                    _on_state,
                )
                try:
                    current = self.hass.states.get(satellite_entity_id)
                    if current is not None and current.state in _SAT_IDLE_STATES:
                        observed_idle.set()
                except (ValueError, KeyError):
                    logger.debug("ha-agenthub: state seed lookup failed", exc_info=True)

            deadline_final = time.monotonic() + PUSH_FINAL_WAIT_SECONDS
            while True:
                remaining = deadline_final - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "ha-agenthub: post-filler push timed out waiting for final frame key=%s sat=%s",
                        gate_key,
                        satellite_entity_id,
                    )
                    break
                try:
                    msg = await asyncio.wait_for(local_ws.receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue

                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning(
                            "ha-agenthub: ignoring malformed WS message in push key=%s",
                            gate_key,
                        )
                        continue
                    if data.get("filler_push") is not None:
                        logger.info(
                            "ha-agenthub: ignoring secondary filler in push key=%s",
                            gate_key,
                        )
                        continue
                    if data.get("directive"):
                        logger.info(
                            "ha-agenthub: post-filler push received directive, skipping announce key=%s sat=%s",
                            gate_key,
                            satellite_entity_id,
                        )
                        break

                    token_text = data.get("token", "")
                    if token_text:
                        final_parts.append(token_text)
                    if data.get("done", False):
                        mediated = data.get("mediated_speech")
                        if mediated:
                            final_parts = [mediated]
                        stream_sanitized = bool(data.get("sanitized", False))
                        voice_followup = bool(data.get("voice_followup", False))
                        raw = "".join(final_parts)
                        final_text = raw if stream_sanitized else _strip_markdown(raw)
                        final_text = (final_text or "").strip()
                        logger.info(
                            "ha-agenthub: post-filler push received final key=%s sat=%s final_chars=%d",
                            gate_key,
                            satellite_entity_id,
                            len(final_text),
                        )
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning(
                        "ha-agenthub: post-filler push WS closed before final key=%s sat=%s type=%s",
                        gate_key,
                        satellite_entity_id,
                        msg.type,
                    )
                    break

            if final_text is None or not final_text:
                return

            if not satellite_entity_id:
                logger.warning(
                    "ha-agenthub: post-filler push has final but no satellite to announce on key=%s",
                    gate_key,
                )
                return

            if not observed_idle.is_set():
                try:
                    await asyncio.wait_for(
                        observed_idle.wait(),
                        timeout=MAX_POST_FILLER_WAIT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "ha-agenthub: post-filler push satellite never reached idle within %.1fs key=%s sat=%s",
                        MAX_POST_FILLER_WAIT_SECONDS,
                        gate_key,
                        satellite_entity_id,
                    )
                    return

            if aborted_new_turn:
                logger.info(
                    "ha-agenthub: abandoning post-filler push (new turn detected) key=%s sat=%s",
                    gate_key,
                    satellite_entity_id,
                )
                return

            # Prime Directive 1 exception (tolerated):
            # This assist_satellite.announce call runs after the filler-first
            # pipeline has completed and the satellite has returned to idle.
            # It is part of the HA Assist lifecycle: the primary response was
            # already streamed via the pipeline, and this push delivers the
            # final text to a satellite that is no longer actively listening.
            # The background-task stability is ensured by the observed_idle
            # gate and aborted_new_turn checks above.
            try:
                logger.info(
                    "ha-agenthub: post-filler push dispatching announce key=%s sat=%s final_chars=%d",
                    gate_key,
                    satellite_entity_id,
                    len(final_text),
                )
                await self.hass.services.async_call(
                    "assist_satellite",
                    "announce",
                    {
                        "entity_id": satellite_entity_id,
                        "message": final_text,
                        "preannounce": False,
                    },
                    blocking=False,
                )
            except (aiohttp.ClientError, OSError):
                logger.warning(
                    "ha-agenthub: assist_satellite.announce failed in push key=%s sat=%s",
                    gate_key,
                    satellite_entity_id,
                    exc_info=True,
                )
            if voice_followup and satellite_entity_id and not aborted_new_turn:
                try:
                    await self.hass.services.async_call(
                        "assist_satellite",
                        "start_conversation",
                        {
                            "entity_id": satellite_entity_id,
                            "start_message": "",
                            "preannounce": False,
                        },
                        blocking=False,
                    )
                    logger.info(
                        "ha-agenthub: post-filler push triggered voice follow-up key=%s sat=%s",
                        gate_key,
                        satellite_entity_id,
                    )
                except Exception:
                    logger.warning(
                        "ha-agenthub: assist_satellite.start_conversation failed in push key=%s sat=%s",
                        gate_key,
                        satellite_entity_id,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            logger.info(
                "ha-agenthub: post-filler push cancelled key=%s sat=%s",
                gate_key,
                satellite_entity_id,
            )
            raise
        except Exception:
            logger.warning(
                "ha-agenthub: post-filler push raised unexpectedly key=%s sat=%s",
                gate_key,
                satellite_entity_id,
                exc_info=True,
            )
        finally:
            if unsub is not None:
                try:
                    unsub()
                except (ValueError, KeyError):
                    logger.debug(
                        "ha-agenthub: state listener unsub raised", exc_info=True
                    )
            try:
                if local_ws is not None and not local_ws.closed:
                    await local_ws.close()
            except (aiohttp.ClientError, OSError):
                logger.exception("ha-agenthub: local_ws close raised")
            current = self._inflight_pushes.get(key)
            if current is asyncio.current_task():
                self._inflight_pushes.pop(key, None)

    async def _process_via_ws(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
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
            final_conversation_id = user_input.conversation_id
            device_id = getattr(user_input, "device_id", None)
            gate_key = device_id or f"__no_device__:{user_input.conversation_id}"

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
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning(
                            "ha-agenthub: ignoring malformed WS message in stream"
                        )
                        continue

                    # V4 filler-first return: when the container sends a
                    # filler_push, hand the WebSocket off to a background
                    # push task, return the filler immediately as the
                    # ConversationResult (so the satellite LEDs go idle),
                    # and let the background task announce the final
                    # response after the satellite is observed back in
                    # idle state.
                    filler_text = data.get("filler_push")
                    if filler_text is not None:
                        stripped_filler = _strip_markdown(str(filler_text).strip())
                        logger.info(
                            "ha-agenthub: filler-first return key=%s filler_chars=%d",
                            gate_key,
                            len(stripped_filler),
                        )
                        if stripped_filler:
                            satellite = self._resolve_satellite_entity(device_id)
                            local_ws = self._ws
                            self._ws = None
                            self._spawn_post_filler_push(
                                local_ws=local_ws,
                                satellite_entity_id=satellite,
                                gate_key=gate_key,
                            )
                            self._ws_last_active = time.monotonic()
                            response = intent.IntentResponse(
                                language=user_input.language or "en"
                            )
                            response.async_set_speech(stripped_filler)
                            return conversation.ConversationResult(
                                response=response,
                                conversation_id=user_input.conversation_id,
                            )
                        continue

                    token_text = data.get("token", "")
                    if token_text:
                        speech_parts.append(token_text)
                    if data.get("done", False):
                        received_done = True
                        stream_err = data.get("error")
                        final_conversation_id = data.get(
                            "conversation_id", final_conversation_id
                        )
                        mediated = data.get("mediated_speech")
                        if mediated:
                            speech_parts = [mediated]
                        # P3-1: backend signals sanitization on the done
                        # frame. Honour it for both ``mediated_speech``
                        # and accumulated tokens (the orchestrator
                        # strips both before emitting).
                        stream_sanitized = bool(data.get("sanitized", False))
                        voice_followup = bool(data.get("voice_followup", False))
                        if stream_err:
                            # Application-level error from the container (done chunk), not a
                            # transport failure — do not raise (would become _WsDroppedAfterSend).
                            logger.warning(
                                "Container reported error in stream done chunk: %s",
                                stream_err,
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
            speech = "".join(speech_parts)
            return self._build_result(
                speech,
                final_conversation_id,
                user_input.language,
                sanitized=stream_sanitized,
                continue_conversation=voice_followup,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise _WsDroppedAfterSendError() from err

    async def _process_via_rest(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Fallback: send request via REST and get full response."""
        try:
            if self._session is None or self._session.closed:
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
                voice_followup = bool(data.get("voice_followup", False))
                return self._build_result(
                    data.get("speech", ""),
                    data.get("conversation_id", user_input.conversation_id),
                    user_input.language,
                    sanitized=bool(data.get("sanitized", False)),
                    continue_conversation=voice_followup,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
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
        speech: str | None,
        conversation_id: str | None,
        language: str | None,
        *,
        sanitized: bool = False,
        continue_conversation: bool = False,
    ) -> conversation.ConversationResult:
        """Assemble a ConversationResult from the response.

        P3-1: ``sanitized`` indicates that the backend already stripped
        Markdown for TTS. When True we trust the backend (single source
        of truth) and skip the local ``_strip_markdown`` pass. Older
        backends that do not advertise the flag default to False so the
        defensive fallback still runs.
        """
        speech = speech or ""
        response = intent.IntentResponse(language=language or "en")
        response.async_set_speech(speech if sanitized else _strip_markdown(speech))
        kwargs: dict[str, Any] = {}
        if _RESULT_SUPPORTS_CONTINUE_CONVERSATION and continue_conversation:
            kwargs["continue_conversation"] = True
        return conversation.ConversationResult(
            response=response, conversation_id=conversation_id, **kwargs
        )
