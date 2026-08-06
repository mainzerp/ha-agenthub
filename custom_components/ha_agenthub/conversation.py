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
from urllib.parse import urlparse

import aiohttp
from homeassistant.components import assist_pipeline, conversation
from homeassistant.components.conversation import ConversationEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

try:
    from homeassistant.helpers.event import async_track_state_change_event
except (ImportError, ModuleNotFoundError):
    async_track_state_change_event = None

from .const import (
    CONF_WS_RECEIVE_TIMEOUT,
    DEFAULT_WS_RECEIVE_TIMEOUT,
    DOMAIN,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    WS_HEARTBEAT_INTERVAL,
    WS_IDLE_THRESHOLD,
    WS_PATH,
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


def _is_sentence_boundary(text: str) -> bool:
    """Return True if text ends with a sentence boundary and has at least one word."""
    if not text or not text.strip():
        return False
    stripped = text.rstrip()
    return stripped[-1] in ".!?\n"


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
    with the backend so containers that do not advertise ``sanitized``
    and filler tokens (which are emitted unsanitized) still produce
    TTS-friendly output.
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
        # P3: memoized device_id -> assist_satellite entity_id resolution
        # (None = known to have no satellite). Cleared on entity-registry
        # updates for assist_satellite entities.
        self._satellite_cache: dict[str, str | None] = {}
        # Debounced reconnect request flag for the background reconnect loop.
        self._reconnect_requested = asyncio.Event()
        # Guards the automatic reauth trigger so a persistent 401 starts at
        # most one reauth flow per failure episode (reset on next success).
        self._reauth_triggered = False
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

        # P3: invalidate the memoized device->satellite mapping when an
        # assist_satellite entity is created/removed/updated (e.g. the
        # satellite integration is replaced or re-assigned to a device).
        def _on_entity_registry_updated(event) -> None:
            entity_id = (event.data or {}).get("entity_id") if event else None
            if entity_id is None or entity_id.startswith("assist_satellite."):
                self._satellite_cache.clear()

        self.async_on_remove(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, _on_entity_registry_updated
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        if hasattr(self, "_reconnect_task") and self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        for sat_id, task in list(self._inflight_pushes.items()):
            task.cancel()
        self._inflight_pushes.clear()
        for key, (_, task) in list(self._inflight_bridge.items()):
            if not task.done():
                task.cancel()
        self._inflight_bridge.clear()
        await self._disconnect_ws()
        # P3: the shared session survives disconnects; close it exactly
        # once when the entity is removed.
        await self._close_session()
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
            # P3 (minimal WS reuse): keep the session on connect failure --
            # a failed ws_connect does not poison the ClientSession, and
            # reusing it across reconnect attempts avoids paying connection
            # pool setup per retry. A dead session is replaced by the
            # ``self._session.closed`` check above.
            self._ws = None
            return False

    async def _disconnect_ws(self) -> None:
        """Close the WebSocket (the shared session survives, see below)."""
        async with self._ws_lock:
            await self._disconnect_ws_locked()

    async def _disconnect_ws_locked(self) -> None:
        """Locked body of :meth:`_disconnect_ws`. Caller MUST hold
        ``self._ws_lock``.

        P3 (minimal WS reuse): only the WebSocket is closed here. The
        shared ``aiohttp.ClientSession`` survives reconnects (a fresh
        session per reconnect pays connection-pool setup for no benefit)
        and is closed exactly once on entity removal via
        :meth:`_close_session`.
        """
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def _close_session(self) -> None:
        """Close the shared aiohttp session (entity removal only)."""
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except (aiohttp.ClientError, OSError):
                pass
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
                        await self._wait_for_reconnect(delay)
                        continue
                # Connection is alive -- wait until a reconnect is explicitly
                # requested or the keep-alive poll interval elapses.
                await self._wait_for_reconnect(30)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error in reconnect loop")
                await asyncio.sleep(5)

    async def _wait_for_reconnect(self, timeout: float) -> None:
        """Wait for a reconnect request, but time out after ``timeout`` seconds."""
        try:
            await asyncio.wait_for(self._reconnect_requested.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        else:
            self._reconnect_requested.clear()

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
        """Signal the background reconnect loop to try again soon.

        Instead of spawning a competing immediate task, this resets the
        backoff and wakes the existing reconnect loop.  That prevents
        overlapping reconnect attempts and reduces log noise after a WS
        failure that falls back to REST.
        """
        self._reconnect_delay = RECONNECT_BASE_DELAY
        self._reconnect_requested.set()

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
        """Single WS (preferred) or REST attempt to the HA-AgentHub container.

        P1: ``self._ws_lock`` covers only the connectivity check and the
        ``send_json`` write. The streaming read runs unlocked on a socket
        the turn exclusively owns (detached from ``self._ws`` at send
        time), so a concurrent satellite turn no longer queues behind a
        slow read -- it simply connects its own socket.
        """
        try:
            turn_ws: aiohttp.ClientWebSocketResponse | None = None
            async with self._ws_lock:
                if await self._ensure_connected_locked():
                    try:
                        turn_ws = await self._ws_send_locked(user_input)
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        logger.warning("WebSocket send failed, falling back to REST")
                        await self._disconnect_ws_locked()
            if turn_ws is not None:
                try:
                    return await self._process_via_ws_read(user_input, turn_ws)
                except _WsDroppedAfterSendError:
                    logger.warning(
                        "WebSocket failed after the request was sent; skipping REST "
                        "(avoids duplicate container traces)",
                        exc_info=True,
                    )
                    return self._build_result(
                        "The connection dropped before the reply finished. "
                        "If the action may have run, check your devices.",
                        user_input.conversation_id,
                        user_input.language,
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    logger.warning("WebSocket error, falling back to REST")
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
        """Forward raw device_id, user_id and area_id to the container.

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
        # M-5: forward the HA user ID so the container can attribute the
        # request to a person.  Defensive getattr chain: older HA versions
        # or custom callers may not populate ``context``.
        user_id = getattr(getattr(user_input, "context", None), "user_id", None)
        if user_id:
            extra["user_id"] = user_id
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
        # P3: the registry scan is memoized per device (invalidated on
        # assist_satellite registry updates) so streamed turns no longer
        # scan -- and log -- the whole registry entry list every time.
        if device_id in self._satellite_cache:
            return self._satellite_cache[device_id]
        resolved: str | None = None
        try:
            entity_registry = er.async_get(self.hass)
            entries = er.async_entries_for_device(entity_registry, device_id)
            logger.debug(
                "filler_push: device %s has %d registry entries",
                device_id,
                len(entries),
            )
            for entry in entries:
                logger.debug(
                    "filler_push: entry domain=%s entity_id=%s",
                    entry.domain,
                    entry.entity_id,
                )
                if entry.domain == "assist_satellite":
                    resolved = entry.entity_id
                    break
            if resolved is None:
                logger.warning(
                    "filler_push: no assist_satellite entity found for device %s",
                    device_id,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            logger.warning(
                "filler_push: failed to resolve satellite entity for device %s",
                device_id,
                exc_info=True,
            )
        self._satellite_cache[device_id] = resolved
        return resolved

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
        """Read the post-filler final response and push it after idle.

        V4b: supports incremental mediated tokens. Sentences are buffered
        until the satellite returns to idle, then announced as they arrive.
        """
        sentence_buffer = ""
        pending_sentences: list[str] = []
        observed_idle = asyncio.Event()
        aborted_new_turn = False
        voice_followup = False
        stream_sanitized = False
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

            announced_any = False
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
                        sentence_buffer += token_text
                        if _is_sentence_boundary(sentence_buffer):
                            clean = sentence_buffer.strip()
                            if observed_idle.is_set() and not aborted_new_turn:
                                await self._announce_sentence(
                                    satellite_entity_id,
                                    clean,
                                    stream_sanitized,
                                    gate_key,
                                )
                            else:
                                pending_sentences.append(clean)
                            sentence_buffer = ""
                            announced_any = True

                    if data.get("done", False):
                        mediated = data.get("mediated_speech")
                        if (
                            mediated
                            and not sentence_buffer.strip()
                            and not announced_any
                        ):
                            sentence_buffer = mediated
                        stream_sanitized = bool(data.get("sanitized", False))
                        voice_followup = bool(data.get("voice_followup", False))
                        if sentence_buffer.strip():
                            clean = sentence_buffer.strip()
                            if observed_idle.is_set() and not aborted_new_turn:
                                await self._announce_sentence(
                                    satellite_entity_id,
                                    clean,
                                    stream_sanitized,
                                    gate_key,
                                )
                            else:
                                pending_sentences.append(clean)
                        logger.info(
                            "ha-agenthub: post-filler push received final key=%s sat=%s pending_sentences=%d",
                            gate_key,
                            satellite_entity_id,
                            len(pending_sentences),
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

            # Flush any pending sentences that arrived before idle
            for sentence in pending_sentences:
                await self._announce_sentence(
                    satellite_entity_id, sentence, stream_sanitized, gate_key
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
                except (aiohttp.ClientError, OSError):
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
        except (HomeAssistantError, aiohttp.ClientError, OSError, asyncio.TimeoutError):
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

    def _spawn_sentence_stream(
        self,
        *,
        local_ws: aiohttp.ClientWebSocketResponse,
        gate_key: str,
        satellite_entity_id: str | None,
        initial_buffer: str = "",
    ) -> None:
        """Spawn a background task to stream remaining sentences via satellite announce."""
        key = satellite_entity_id or f"__no_sat__:{gate_key}"
        previous = self._inflight_pushes.get(key)
        if previous is not None and not previous.done():
            logger.info(
                "ha-agenthub: cancelling previous sentence stream key=%s sat=%s",
                gate_key,
                satellite_entity_id,
            )
            previous.cancel()
        task = self._entry.async_create_background_task(
            self.hass,
            self._sentence_stream_task(
                local_ws=local_ws,
                satellite_entity_id=satellite_entity_id,
                gate_key=gate_key,
                key=key,
                initial_buffer=initial_buffer,
            ),
            name=f"ha_agenthub_sentence_stream:{key}",
        )
        self._inflight_pushes[key] = task

    async def _sentence_stream_task(
        self,
        *,
        local_ws: aiohttp.ClientWebSocketResponse,
        satellite_entity_id: str | None,
        gate_key: str,
        key: str,
        initial_buffer: str = "",
    ) -> None:
        """Read remaining mediated tokens and announce sentence by sentence."""
        sentence_buffer = initial_buffer
        voice_followup = False
        stream_sanitized = False

        try:
            announced_any = False
            deadline_final = time.monotonic() + PUSH_FINAL_WAIT_SECONDS
            while True:
                remaining = deadline_final - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "ha-agenthub: sentence stream timed out waiting for final frame key=%s sat=%s",
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
                            "ha-agenthub: ignoring malformed WS message in sentence stream key=%s",
                            gate_key,
                        )
                        continue

                    if data.get("filler_push") is not None:
                        logger.info(
                            "ha-agenthub: ignoring filler in sentence stream key=%s",
                            gate_key,
                        )
                        continue
                    if data.get("directive"):
                        logger.info(
                            "ha-agenthub: sentence stream received directive, skipping announce key=%s sat=%s",
                            gate_key,
                            satellite_entity_id,
                        )
                        break

                    token_text = data.get("token", "")
                    if token_text:
                        sentence_buffer += token_text
                        if _is_sentence_boundary(sentence_buffer):
                            await self._announce_sentence(
                                satellite_entity_id,
                                sentence_buffer.strip(),
                                stream_sanitized,
                                gate_key,
                            )
                            sentence_buffer = ""
                            announced_any = True

                    if data.get("done", False):
                        stream_sanitized = bool(data.get("sanitized", False))
                        voice_followup = bool(data.get("voice_followup", False))
                        mediated = data.get("mediated_speech")
                        if (
                            mediated
                            and not sentence_buffer.strip()
                            and not announced_any
                        ):
                            sentence_buffer = mediated
                        if sentence_buffer.strip():
                            await self._announce_sentence(
                                satellite_entity_id,
                                sentence_buffer.strip(),
                                stream_sanitized,
                                gate_key,
                            )
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning(
                        "ha-agenthub: sentence stream WS closed before final key=%s sat=%s type=%s",
                        gate_key,
                        satellite_entity_id,
                        msg.type,
                    )
                    break

            if voice_followup and satellite_entity_id:
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
                        "ha-agenthub: sentence stream triggered voice follow-up key=%s sat=%s",
                        gate_key,
                        satellite_entity_id,
                    )
                except (aiohttp.ClientError, OSError):
                    logger.warning(
                        "ha-agenthub: assist_satellite.start_conversation failed in sentence stream key=%s sat=%s",
                        gate_key,
                        satellite_entity_id,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            logger.info(
                "ha-agenthub: sentence stream cancelled key=%s sat=%s",
                gate_key,
                satellite_entity_id,
            )
            raise
        except (HomeAssistantError, aiohttp.ClientError, OSError, asyncio.TimeoutError):
            logger.warning(
                "ha-agenthub: sentence stream raised unexpectedly key=%s sat=%s",
                gate_key,
                satellite_entity_id,
                exc_info=True,
            )
        finally:
            try:
                if local_ws is not None and not local_ws.closed:
                    await local_ws.close()
            except (aiohttp.ClientError, OSError):
                logger.exception(
                    "ha-agenthub: local_ws close raised in sentence stream"
                )
            current = self._inflight_pushes.get(key)
            if current is asyncio.current_task():
                self._inflight_pushes.pop(key, None)

    async def _announce_sentence(
        self,
        satellite_entity_id: str | None,
        text: str,
        sanitized: bool,
        gate_key: str,
    ) -> None:
        """Announce a single sentence via assist_satellite."""
        if not satellite_entity_id:
            logger.warning(
                "ha-agenthub: no satellite to announce sentence on key=%s",
                gate_key,
            )
            return
        try:
            clean = text if sanitized else _strip_markdown(text)
            clean = (clean or "").strip()
            if not clean:
                return
            logger.info(
                "ha-agenthub: announcing sentence key=%s sat=%s chars=%d",
                gate_key,
                satellite_entity_id,
                len(clean),
            )
            await self.hass.services.async_call(
                "assist_satellite",
                "announce",
                {
                    "entity_id": satellite_entity_id,
                    "message": clean,
                    "preannounce": False,
                },
                blocking=False,
            )
        except (aiohttp.ClientError, OSError):
            logger.warning(
                "ha-agenthub: assist_satellite.announce failed for sentence key=%s sat=%s",
                gate_key,
                satellite_entity_id,
                exc_info=True,
            )

    async def _ws_send_locked(
        self, user_input: conversation.ConversationInput
    ) -> aiohttp.ClientWebSocketResponse:
        """Send the request payload and hand socket ownership to the turn.

        Caller MUST hold ``self._ws_lock`` (except single-threaded test
        doubles). On success ``self._ws`` is cleared so no other turn can
        send on -- or read from -- this socket while the streaming read
        runs unlocked; the read phase offers the socket back via
        :meth:`_reuse_shared_ws` after a clean done frame. On send failure
        ``self._ws`` is left untouched so the caller can disconnect under
        the lock (pre-P1 semantics).
        """
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
        turn_ws = self._ws
        if turn_ws is None:
            raise aiohttp.ClientError("WebSocket not connected")
        await turn_ws.send_json(payload)
        self._ws = None
        return turn_ws

    async def _reuse_shared_ws(self, turn_ws: aiohttp.ClientWebSocketResponse) -> None:
        """Offer a finished turn's socket back as the shared connection.

        If another turn (or the reconnect loop) already installed a fresh
        socket, the extra one is closed instead -- only one shared
        connection is kept.
        """
        async with self._ws_lock:
            if self._ws is None and not turn_ws.closed:
                self._ws = turn_ws
                self._ws_last_active = time.monotonic()
                return
        await self._close_local_ws(turn_ws)

    async def _close_local_ws(self, turn_ws: aiohttp.ClientWebSocketResponse) -> None:
        """Best-effort close of a turn-owned socket; never touches ``self._ws``."""
        try:
            if not turn_ws.closed:
                await turn_ws.close()
        except (aiohttp.ClientError, OSError):
            logger.debug("ha-agenthub: error closing turn socket", exc_info=True)

    async def _process_via_ws(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Send + read on the current shared socket (legacy combined form).

        Production turns go through :meth:`_async_bridge_to_container`,
        which splits the locked send phase from the unlocked read phase.
        This wrapper keeps the pre-existing single-call contract used by
        the bridge test-suite.
        """
        turn_ws = await self._ws_send_locked(user_input)
        return await self._process_via_ws_read(user_input, turn_ws)

    async def _process_via_ws_read(
        self,
        user_input: conversation.ConversationInput,
        turn_ws: aiohttp.ClientWebSocketResponse,
    ) -> conversation.ConversationResult:
        """Read the streaming response from a turn-owned socket (unlocked).

        The turn owns ``turn_ws`` exclusively (see :meth:`_ws_send_locked`),
        so this loop never touches ``self._ws`` and never holds
        ``self._ws_lock``. Socket disposition:
          - early filler/sentence handoff: ownership moves to the
            background push task (which closes it), exactly as before;
          - clean done frame: the socket is offered back as the shared
            connection via :meth:`_reuse_shared_ws`;
          - any failure or cancellation: the socket is closed.
        """
        handoff = False
        done_ok = False
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
            voice_followup = False
            sentence_buffer = ""

            while True:
                try:
                    timeout = float(
                        self._entry.options.get(
                            CONF_WS_RECEIVE_TIMEOUT, DEFAULT_WS_RECEIVE_TIMEOUT
                        )
                    )
                except (TypeError, ValueError):
                    timeout = float(DEFAULT_WS_RECEIVE_TIMEOUT)
                msg = await asyncio.wait_for(turn_ws.receive(), timeout=timeout)
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
                            self._spawn_post_filler_push(
                                local_ws=turn_ws,
                                satellite_entity_id=satellite,
                                gate_key=gate_key,
                            )
                            handoff = True
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
                        sentence_buffer += token_text
                        if _is_sentence_boundary(sentence_buffer):
                            response = intent.IntentResponse(
                                language=user_input.language or "en"
                            )
                            response.async_set_speech(
                                _strip_markdown(sentence_buffer.strip())
                            )
                            satellite = self._resolve_satellite_entity(device_id)
                            self._spawn_sentence_stream(
                                local_ws=turn_ws,
                                gate_key=gate_key,
                                satellite_entity_id=satellite,
                                initial_buffer="",
                            )
                            handoff = True
                            self._ws_last_active = time.monotonic()
                            return conversation.ConversationResult(
                                response=response,
                                conversation_id=user_input.conversation_id,
                            )

                    if data.get("done", False):
                        received_done = True
                        stream_err = data.get("error")
                        final_conversation_id = data.get(
                            "conversation_id", final_conversation_id
                        )
                        mediated = data.get("mediated_speech")
                        if mediated:
                            speech_parts = [mediated]
                        elif sentence_buffer:
                            speech_parts.append(sentence_buffer)
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
                                    (
                                        "The assistant could not complete that request. "
                                        f"({stream_err})"
                                    )
                                ]
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise aiohttp.ClientError(
                        f"WebSocket {'closed' if msg.type == aiohttp.WSMsgType.CLOSED else 'error'} mid-stream"
                    )

            if not received_done:
                raise aiohttp.ClientError("WebSocket stream ended without done token")

            done_ok = True
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
        finally:
            if handoff:
                # The background push task owns the socket now and closes it.
                pass
            elif done_ok:
                await self._reuse_shared_ws(turn_ws)
            else:
                await self._close_local_ws(turn_ws)

    def _start_reauth_once(self) -> None:
        """Start the reauth flow once per auth-failure episode.

        The flag is reset on the next successful request so a recovered
        connection can trigger reauth again if the key is rotated later.
        """
        if self._reauth_triggered:
            return
        self._reauth_triggered = True
        self._entry.async_start_reauth(self.hass)

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
                    if resp.status in {401, 403}:
                        self._start_reauth_once()
                    return self._build_result(
                        _rest_fallback_error_message(resp.status),
                        user_input.conversation_id,
                        user_input.language,
                    )
                self._reauth_triggered = False
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
