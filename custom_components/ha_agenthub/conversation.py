"""Conversation entity for HA-AgentHub (I/O bridge)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import aiohttp
from homeassistant.components import assist_pipeline, conversation
from homeassistant.components.conversation import ConversationEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_WS_RECEIVE_TIMEOUT,
    DOMAIN,
    MAX_REQUEST_TEXT_LENGTH,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    WS_HEARTBEAT_INTERVAL,
    WS_IDLE_THRESHOLD,
    WS_PATH,
    resolve_ws_receive_timeout,
)
from .log_shipper import current_conversation_id, current_trace_id

logger = logging.getLogger(__name__)

# User-facing texts. Raw container error strings are never spoken; details
# go to the log together with the container trace id.
_MSG_UNAVAILABLE = (
    "Sorry, the assistant container is unavailable. "
    "Check that the container is running and reachable from Home Assistant."
)
_MSG_TIMEOUT = (
    "Sorry, the assistant did not answer in time. "
    "If the action may have run, check your devices."
)
_MSG_DROPPED = (
    "The connection dropped before the reply finished. "
    "If the action may have run, check your devices."
)
_MSG_STREAM_ERROR = "The assistant could not complete that request."
_MSG_TOO_LONG = (
    "Sorry, that request is too long. "
    f"Please keep it under {MAX_REQUEST_TEXT_LENGTH} characters."
)
_MSG_INVALID_REQUEST = (
    "Sorry, the assistant could not accept that request. "
    "It may be too long or contain unsupported values."
)
_MSG_RATE_LIMITED = (
    "Sorry, the assistant is receiving too many requests right now. "
    "Please try again in a moment."
)

# Ingress rejections the container sends as terminal WS error frames
# (container/app/api/routes/conversation.py ``_ws_terminal_error``).
_WS_INGRESS_ERROR_MESSAGES: dict[str, str] = {
    "Rate limit exceeded": _MSG_RATE_LIMITED,
    "Invalid request": _MSG_INVALID_REQUEST,
    "Message too large": _MSG_INVALID_REQUEST,
}

# aiohttp message types that end a WebSocket. Looked up by name so test
# doubles of ``aiohttp.WSMsgType`` that define only some members still work.
_WS_CLOSE_TYPE_NAMES = ("CLOSE", "CLOSING", "CLOSED", "ERROR")


def _is_ws_close_message(msg_type: Any) -> bool:
    """Return True for a close/closing/closed/error WebSocket message type."""
    return any(
        hasattr(aiohttp.WSMsgType, name)
        and msg_type == getattr(aiohttp.WSMsgType, name)
        for name in _WS_CLOSE_TYPE_NAMES
    )


class _WsDroppedAfterSendError(Exception):
    """Request was written to the WebSocket; REST fallback would duplicate server work."""

    def __init__(self, *, timed_out: bool = False) -> None:
        super().__init__("receive timed out" if timed_out else "connection dropped")
        self.timed_out = timed_out


class _WsNotDeliveredError(Exception):
    """The socket closed before the turn received its first frame.

    The container answers every received request with frames on the same
    socket, including a terminal error frame when dispatch fails, so a close
    or error message before any frame is treated as "request not delivered"
    (typically an idle socket the container had already closed). The bridge
    retries such a turn over REST.
    """


@dataclass(slots=True)
class _BridgeState:
    """One backend request and the callers currently observing it."""

    started: float
    task: asyncio.Task[Any]
    waiters: int = 0


def _turn_conversation_id(
    user_input: conversation.ConversationInput,
    chat_log: conversation.ChatLog,
) -> str | None:
    """Return the HA chat session id for this turn.

    HA resolves the session before calling the entity: a missing id gets a
    fresh ULID and an unknown non-ULID id is replaced, so
    ``chat_log.conversation_id`` (HA >= 2025.4) is authoritative. The input
    id is only a fallback for chat logs without the attribute.
    """
    chat_log_id = getattr(chat_log, "conversation_id", None)
    if isinstance(chat_log_id, str) and chat_log_id:
        return chat_log_id
    return user_input.conversation_id


def _rest_fallback_error_message(status_code: int | None) -> str:
    """Return an actionable fallback message for REST error responses."""
    if status_code in {401, 403}:
        return (
            "Sorry, the HA-AgentHub integration API key was rejected. "
            "Update the API key in the HA-AgentHub integration settings."
        )
    if status_code == 422:
        return _MSG_INVALID_REQUEST
    if status_code == 429:
        return _MSG_RATE_LIMITED
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
    # Responses are streamed into the chat log as content deltas; HA >= 2025.7
    # pipelines then feed them to streaming TTS. Inert on older cores.
    _attr_supports_streaming = True

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
        # Background reader on the idle shared socket: aiohttp answers the
        # container's pings only inside ``receive()``. Enabled once the
        # entity is attached to hass (background tasks need the entry).
        self._ws_idle_reader_enabled = False
        self._idle_reader_task: asyncio.Task[None] | None = None
        self._idle_reader_ws: aiohttp.ClientWebSocketResponse | None = None
        # Coalesce parallel HA calls with the same conversation_id + text (duplicate
        # pipeline invocations or WS+REST overlap) into a single bridge request.
        self._coalesce_lock = asyncio.Lock()
        self._bridge_shutdown = False
        # FLOW-COALESCE-1 (P2-3): each value records start time, task, and
        # waiter ownership. The
        # started-timestamp guards a legitimate repeat of the same utterance
        # that arrives after the original response was already rendered --
        # without it we would short-circuit the second request onto the
        # first completed task forever.
        self._inflight_bridge: dict[tuple[str, str], _BridgeState] = {}
        # Keep every request alive here, including an older request replaced
        # under the same coalescing key after the time window expires.
        self._bridge_tasks: set[asyncio.Task[Any]] = set()
        self._coalesce_window_sec: float = 0.25
        # Debounced reconnect request flag for the background reconnect loop.
        self._reconnect_requested = asyncio.Event()
        # Guards the automatic reauth trigger so a persistent 401 starts at
        # most one reauth flow per failure episode (reset on next success).
        self._reauth_triggered = False
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
        except (AttributeError, ValueError, KeyError):
            logger.debug("Pipeline engine migration skipped (not critical)")
        self._ws_idle_reader_enabled = True
        self._reconnect_task = self._entry.async_create_background_task(
            self.hass,
            self._reconnect_loop(),
            name="ha_agenthub_ws_reconnect",
        )

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        self._bridge_shutdown = True
        reconnect_task = getattr(self, "_reconnect_task", None)
        if reconnect_task:
            reconnect_task.cancel()
            if isinstance(reconnect_task, asyncio.Future):
                await asyncio.gather(reconnect_task, return_exceptions=True)
            self._reconnect_task = None
        async with self._coalesce_lock:
            bridge_tasks = set(getattr(self, "_bridge_tasks", set()))
            bridge_tasks.update(state.task for state in self._inflight_bridge.values())
            self._inflight_bridge.clear()
        for task in bridge_tasks:
            if not task.done():
                task.cancel()
        if bridge_tasks:
            await asyncio.gather(*bridge_tasks, return_exceptions=True)
        async with self._coalesce_lock:
            self._bridge_tasks.difference_update(bridge_tasks)
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
        if self._ws is not None:
            # A closed socket is still installed: drop it (and its reader).
            await self._disconnect_ws_locked()
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()

            parsed = urlparse(self._url)
            ws_scheme = "wss" if parsed.scheme == "https" else "ws"
            ws_url = parsed._replace(scheme=ws_scheme).geturl()
            ws = await self._session.ws_connect(
                f"{ws_url}{WS_PATH}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=aiohttp.ClientTimeout(total=10),
                heartbeat=WS_HEARTBEAT_INTERVAL,
            )
            self._ws = ws
            self._reconnect_delay = RECONNECT_BASE_DELAY
            self._ws_last_active = time.monotonic()
            self._start_idle_reader_locked(ws)
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
        await self._stop_idle_reader()
        ws = self._ws
        self._ws = None
        if ws is not None and not ws.closed:
            await ws.close()

    def _start_idle_reader_locked(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Start the background reader for the idle shared socket ``ws``.

        Caller MUST hold ``self._ws_lock`` and ``ws`` must be ``self._ws``.
        No-op before the entity is attached to hass, after shutdown, or when
        a reader for ``ws`` is already running.
        """
        if not self._ws_idle_reader_enabled or self._bridge_shutdown:
            return
        task = self._idle_reader_task
        if task is not None and not task.done():
            if self._idle_reader_ws is ws:
                return
            # Defensive: a reader still bound to an older socket.
            task.cancel()
        self._idle_reader_ws = ws
        self._idle_reader_task = self._entry.async_create_background_task(
            self.hass,
            self._idle_read_loop(ws),
            name="ha_agenthub_ws_idle_reader",
        )

    async def _stop_idle_reader(self) -> None:
        """Stop the idle reader so a turn (or a close) can use the socket.

        Cancelling a pending ``receive()`` loses no data: aiohttp only pops
        a message from its queue after the wait completes.
        """
        task = self._idle_reader_task
        self._idle_reader_task = None
        self._idle_reader_ws = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _idle_read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Read the idle shared socket until it closes.

        ``receive()`` answers server pings internally and returns only real
        messages. While no turn owns the socket, any returned message ends
        it: a close/error means the container dropped the socket, and a
        stray data frame means the socket no longer has a clean turn
        boundary. Either way the socket is detached, closed, and a
        reconnect is requested.
        """
        try:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                logger.warning(
                    "ha-agenthub: unexpected frame on the idle WebSocket; discarding the socket"
                )
            else:
                logger.debug(
                    "ha-agenthub: idle WebSocket closed by the container (type=%s)",
                    msg.type,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("ha-agenthub: idle WebSocket reader failed", exc_info=True)
        if self._idle_reader_task is asyncio.current_task():
            self._idle_reader_task = None
            self._idle_reader_ws = None
        if self._ws is ws:
            self._ws = None
        await self._close_local_ws(ws)
        if not self._bridge_shutdown:
            self._schedule_reconnect()

    async def _ensure_idle_reader(self) -> None:
        """Restart the idle reader if the shared socket has none."""
        async with self._ws_lock:
            ws = self._ws
            if ws is not None and not ws.closed:
                self._start_idle_reader_locked(ws)

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
                # Connection is alive -- make sure its idle reader runs, then
                # wait until a reconnect is explicitly requested or the
                # keep-alive poll interval elapses.
                await self._ensure_idle_reader()
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

        On success the idle reader is stopped, so the caller may send and
        then read the socket itself.
        """
        ws = self._ws
        if ws is not None:
            # Take the socket out of idle reading first; the reader may
            # just have detected that the container closed it.
            await self._stop_idle_reader()
            if self._ws is ws and not ws.closed:
                if time.monotonic() - self._ws_last_active <= WS_IDLE_THRESHOLD:
                    return True
                try:
                    await asyncio.wait_for(ws.ping(), timeout=2.0)
                    self._ws_last_active = time.monotonic()
                    return True
                except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
                    logger.warning("WebSocket idle ping failed, reconnecting")
            await self._disconnect_ws_locked()
        connected = await self._connect_ws_locked()
        if connected:
            await self._stop_idle_reader()
        else:
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
        back-to-back from production HA setups. Coalesced duplicates share
        the first invocation's ``chat_log`` (same conversation_id by
        construction), so streamed content lands in that chat log. A
        coalesced caller's own chat-log copy stays unchanged; HA core
        discards unchanged copies on exit, and its speech arrives through
        the shared ``ConversationResult``.
        """
        cid = user_input.conversation_id or ""
        text = (user_input.text or "").strip()
        device_id = getattr(user_input, "device_id", None)
        # Expose the HA conversation id to the log shipper for this turn.
        # The bridge task created below copies the current task context, so
        # log records from the whole turn (including the delta-stream
        # generator) carry the id.
        cid_token = current_conversation_id.set(
            _turn_conversation_id(user_input, chat_log) or None
        )
        try:
            logger.debug(
                "ha-agenthub: turn-entry cid=%s device_id=%s text_len=%d",
                cid,
                device_id,
                len(text),
            )
            # The coalescing key uses the caller-supplied id: duplicate
            # invocations without an id get distinct HA session ids.
            key = (cid, text)

            async with self._coalesce_lock:
                if getattr(self, "_bridge_shutdown", False):
                    raise asyncio.CancelledError
                if not hasattr(self, "_bridge_tasks"):
                    self._bridge_tasks = set()
                existing = self._inflight_bridge.get(key)
                now = time.monotonic()
                if (
                    existing is not None
                    and not existing.task.done()
                    and (now - existing.started) < self._coalesce_window_sec
                ):
                    state = existing
                else:
                    bridge_task = self.hass.async_create_task(
                        self._async_bridge_with_cleanup(user_input, key, chat_log)
                    )
                    state = _BridgeState(now, bridge_task)
                    self._inflight_bridge[key] = state
                    self._bridge_tasks.add(bridge_task)
                    bridge_task.add_done_callback(self._consume_bridge_task)
                state.waiters += 1
            if state is existing:
                logger.info(
                    "HA-AgentHub: coalescing duplicate request (same conversation + text) onto in-flight bridge"
                )
            try:
                # A pipeline cancellation only removes this waiter. The
                # backend request remains available to other coalesced callers.
                return await asyncio.shield(state.task)
            finally:
                await self._release_bridge_waiter(key, state)
        finally:
            current_conversation_id.reset(cid_token)

    def _consume_bridge_task(self, task: asyncio.Task[Any]) -> None:
        """Release ownership and consume an unobserved backend exception."""
        bridge_tasks = getattr(self, "_bridge_tasks", None)
        if bridge_tasks is not None:
            bridge_tasks.discard(task)
        for key, state in list(self._inflight_bridge.items()):
            if state.task is task:
                self._inflight_bridge.pop(key, None)
                break
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            return

    async def _release_bridge_waiter(
        self, key: tuple[str, str], state: _BridgeState
    ) -> None:
        """Drop one observer and cancel an unobserved request if it is last."""
        cancel_task = False
        async with self._coalesce_lock:
            if state.waiters:
                state.waiters -= 1
            if state.waiters == 0:
                current = self._inflight_bridge.get(key)
                if current is state:
                    self._inflight_bridge.pop(key, None)
                cancel_task = not state.task.done()
        # Cancellation happens after releasing the lock. The bridge's own
        # finally block also takes this lock to remove its identity safely.
        if cancel_task:
            state.task.cancel()
            await asyncio.gather(state.task, return_exceptions=True)

    async def _async_bridge_with_cleanup(
        self,
        user_input: conversation.ConversationInput,
        key: tuple[str, str],
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        task = asyncio.current_task()
        try:
            return await self._async_bridge_to_container(user_input, chat_log)
        finally:
            async with self._coalesce_lock:
                existing = self._inflight_bridge.get(key)
                if task is not None:
                    bridge_tasks = getattr(self, "_bridge_tasks", None)
                    if bridge_tasks is not None:
                        bridge_tasks.discard(task)
                if task is not None and existing is not None and existing.task is task:
                    self._inflight_bridge.pop(key, None)

    async def _async_bridge_to_container(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Single WS (preferred) or REST attempt to the HA-AgentHub container.

        P1: ``self._ws_lock`` covers only the connectivity check and the
        ``send_json`` write. The streaming read runs unlocked on a socket
        the turn exclusively owns (detached from ``self._ws`` at send
        time), so a concurrent satellite turn no longer queues behind a
        slow read -- it simply connects its own socket.
        """
        conversation_id = _turn_conversation_id(user_input, chat_log)
        if len(user_input.text or "") > MAX_REQUEST_TEXT_LENGTH:
            # The container would reject it with a validation error; answer
            # locally instead of truncating (a cut command could act on the
            # wrong target).
            logger.warning(
                "ha-agenthub: request text exceeds %d characters (%d); not forwarded",
                MAX_REQUEST_TEXT_LENGTH,
                len(user_input.text or ""),
            )
            return await self._rest_result(
                user_input, chat_log, _MSG_TOO_LONG, conversation_id
            )
        try:
            turn_ws: aiohttp.ClientWebSocketResponse | None = None
            async with self._ws_lock:
                if await self._ensure_connected_locked():
                    try:
                        turn_ws = await self._ws_send_locked(
                            user_input, conversation_id
                        )
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                        logger.warning("WebSocket send failed, falling back to REST")
                        await self._disconnect_ws_locked()
            if turn_ws is not None:
                try:
                    return await self._process_via_ws_read(
                        user_input, chat_log, turn_ws
                    )
                except _WsDroppedAfterSendError as err:
                    logger.warning(
                        "WebSocket failed after the request was sent; skipping REST "
                        "(avoids duplicate container work)",
                        exc_info=True,
                    )
                    # On mid-stream failure the delta stream added nothing
                    # to the chat log; add the canned message so display
                    # and speech stay consistent.
                    drop_speech = _MSG_TIMEOUT if err.timed_out else _MSG_DROPPED
                    await self._add_assistant_chat_log_content(
                        chat_log, user_input, drop_speech
                    )
                    return self._build_result(
                        drop_speech,
                        conversation_id,
                        user_input.language,
                    )
                except _WsNotDeliveredError:
                    logger.warning(
                        "WebSocket closed before the container answered; "
                        "retrying the turn over REST"
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            logger.warning(
                "Unexpected WS dispatch failure, falling back to REST", exc_info=True
            )

        result = await self._process_via_rest(user_input, chat_log)
        self._schedule_reconnect()
        return result

    def _resolve_origin_context(
        self, user_input: conversation.ConversationInput
    ) -> dict[str, str]:
        """Forward raw device_id, user_id and area_id to the container.

        The container maintains its own entity index and resolves
        human-readable names from its synced copy.  The bridge must
        not perform entity resolution on behalf of the container
        (Prime Directive 1).
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

    async def _ws_send_locked(
        self,
        user_input: conversation.ConversationInput,
        conversation_id: str | None = None,
    ) -> aiohttp.ClientWebSocketResponse:
        """Send the request payload and hand socket ownership to the turn.

        Caller MUST hold ``self._ws_lock`` (except single-threaded test
        doubles). ``self._ws`` is cleared *before* the write, so no other
        turn can send on -- or read from -- this socket while the streaming
        read runs unlocked; the read phase offers the socket back via
        :meth:`_reuse_shared_ws` after a clean done frame. If the send
        fails or the task is cancelled during it, the bytes may already be
        on the wire, so the socket is closed instead of staying shared.
        """
        cid = (
            conversation_id
            if conversation_id is not None
            else user_input.conversation_id
        )
        logger.debug(
            "ha-agenthub: ws-entry cid=%s ws_open=%s",
            cid,
            self._ws is not None and not self._ws.closed,
        )
        payload: dict[str, Any] = {
            "text": user_input.text,
            "conversation_id": cid,
            "language": user_input.language or "en",
        }
        payload.update(self._resolve_origin_context(user_input))
        turn_ws = self._ws
        if turn_ws is None:
            raise aiohttp.ClientError("WebSocket not connected")
        self._ws = None
        try:
            await turn_ws.send_json(payload)
        except BaseException:
            await self._close_local_ws(turn_ws)
            raise
        return turn_ws

    async def _reuse_shared_ws(self, turn_ws: aiohttp.ClientWebSocketResponse) -> None:
        """Offer a finished turn's socket back as the shared connection.

        If another turn (or the reconnect loop) already installed a fresh
        socket, the extra one is closed instead -- only one shared
        connection is kept. A reused socket gets an idle reader again.
        """
        async with self._ws_lock:
            if self._ws is None and not turn_ws.closed:
                self._ws = turn_ws
                self._ws_last_active = time.monotonic()
                self._start_idle_reader_locked(turn_ws)
                return
        await self._close_local_ws(turn_ws)

    async def _close_local_ws(self, turn_ws: aiohttp.ClientWebSocketResponse) -> None:
        """Best-effort close of a turn-owned socket; never touches ``self._ws``."""
        try:
            if not turn_ws.closed:
                await turn_ws.close()
        except (aiohttp.ClientError, OSError):
            logger.debug("ha-agenthub: error closing turn socket", exc_info=True)

    async def _process_via_ws_read(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        turn_ws: aiohttp.ClientWebSocketResponse,
    ) -> conversation.ConversationResult:
        """Read the streaming response from a turn-owned socket (unlocked).

        The turn owns ``turn_ws`` exclusively (see :meth:`_ws_send_locked`),
        so this loop never touches ``self._ws`` and never holds
        ``self._ws_lock``. The received frames are streamed into the chat
        log as content deltas via
        :meth:`chat_log.async_add_delta_content_stream`: a ``filler_push``
        frame becomes the in-stream preamble (the first content of the
        assistant message), ``token`` frames concatenate, and the terminal
        done frame ends the stream. Socket disposition:
          - clean done frame: the socket is offered back as the shared
            connection via :meth:`_reuse_shared_ws`;
          - any failure or cancellation: the socket is closed.

        Every WS path returns in the same turn with
        ``continue_conversation=voice_followup`` from the done frame, so HA
        keeps the chat session and the satellite re-listens natively.

        Failures: a close/error message before the first frame raises
        :class:`_WsNotDeliveredError` (the caller retries over REST); any
        later transport failure or a receive timeout raises
        :class:`_WsDroppedAfterSendError` (no REST retry).
        """
        box: dict[str, Any] = {
            "filler": "",
            "tokens": "",
            "mediated": "",
            "canned_error": "",
            "conversation_id": _turn_conversation_id(user_input, chat_log),
            "sanitized": False,
            "voice_followup": False,
            "trace_id": None,
            "frames": 0,
        }

        async def _delta_stream(
            box: dict[str, Any],
        ) -> AsyncIterator[conversation.AssistantContentDeltaDict]:
            message_open = False

            def _receive_timeout() -> float:
                return resolve_ws_receive_timeout(
                    self._entry.options.get(CONF_WS_RECEIVE_TIMEOUT)
                )

            while True:
                msg = await asyncio.wait_for(
                    turn_ws.receive(), timeout=_receive_timeout()
                )
                if msg.type == aiohttp.WSMsgType.TEXT:
                    box["frames"] += 1
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning(
                            "ha-agenthub: ignoring malformed WS message in stream"
                        )
                        continue
                    if not isinstance(data, dict):
                        logger.warning(
                            "ha-agenthub: ignoring non-object WS message in stream"
                        )
                        continue

                    # Filler frames become the in-stream preamble: the first
                    # content of the assistant message, so streaming TTS
                    # speaks it early and the final result speech (filler +
                    # answer) agrees with the chat-log content.
                    filler_text = data.get("filler_push")
                    if filler_text is not None:
                        stripped_filler = _strip_markdown(str(filler_text).strip())
                        if stripped_filler:
                            box["filler"] = stripped_filler + " "
                            logger.info(
                                "ha-agenthub: filler preamble filler_chars=%d",
                                len(stripped_filler),
                            )
                            yield {"role": "assistant", "content": box["filler"]}
                            message_open = True
                        continue

                    token_text = data.get("token", "")
                    if token_text:
                        if not message_open:
                            yield {"role": "assistant"}
                            message_open = True
                        yield {"content": token_text}
                        box["tokens"] += token_text

                    if data.get("done", False):
                        # The container's conversation_id is deliberately NOT
                        # forwarded into the result: HA owns chat sessions and
                        # regenerates unknown-but-valid ULIDs, so adopting the
                        # container id would silently break session continuity
                        # (box starts from the HA chat-log session id).
                        # P3-1: the backend signals sanitization on the done
                        # frame. Honour it for both ``mediated_speech`` and
                        # accumulated tokens (the orchestrator strips both
                        # before emitting).
                        box["sanitized"] = bool(data.get("sanitized", False))
                        box["voice_followup"] = bool(data.get("voice_followup", False))
                        # Per-turn container trace id (present once the
                        # container ships it; None against older containers).
                        box["trace_id"] = data.get("trace_id")
                        current_trace_id.set(box["trace_id"])
                        # The terminal frame carries ``mediated_speech`` only
                        # when no tokens were streamed.
                        mediated = data.get("mediated_speech")
                        if mediated and not box["tokens"]:
                            box["mediated"] = mediated
                            if not message_open:
                                yield {"role": "assistant"}
                                message_open = True
                            yield {"content": mediated}
                        stream_err = data.get("error")
                        if stream_err:
                            # Application-level error from the container (done
                            # chunk), not a transport failure -- do not raise
                            # (would become _WsDroppedAfterSendError). The raw
                            # error is logged, never spoken.
                            logger.warning(
                                "Container reported error in stream done chunk "
                                "(trace_id=%s): %s",
                                box["trace_id"],
                                stream_err,
                            )
                            if not (box["mediated"] or box["tokens"]).strip():
                                box["canned_error"] = _WS_INGRESS_ERROR_MESSAGES.get(
                                    str(stream_err), _MSG_STREAM_ERROR
                                )
                                if not message_open:
                                    yield {"role": "assistant"}
                                    message_open = True
                                yield {"content": box["canned_error"]}
                        return
                elif _is_ws_close_message(msg.type):
                    is_error = hasattr(aiohttp.WSMsgType, "ERROR") and (
                        msg.type == aiohttp.WSMsgType.ERROR
                    )
                    kind = "error" if is_error else "closed"
                    if not box["frames"]:
                        raise _WsNotDeliveredError(
                            f"WebSocket {kind} before the first frame"
                        )
                    raise aiohttp.ClientError(f"WebSocket {kind} mid-stream")

        done_ok = False
        try:
            async for _ in chat_log.async_add_delta_content_stream(
                self.entity_id or user_input.agent_id, _delta_stream(box)
            ):
                pass
            done_ok = True
            self._ws_last_active = time.monotonic()
            speech = box["filler"] + (
                box["mediated"] or box["tokens"] or box["canned_error"]
            )
            return self._build_result(
                speech,
                box["conversation_id"],
                user_input.language,
                sanitized=box["sanitized"],
                continue_conversation=box["voice_followup"],
            )
        except _WsNotDeliveredError:
            raise
        except asyncio.TimeoutError as err:
            raise _WsDroppedAfterSendError(timed_out=True) from err
        except aiohttp.ClientError as err:
            raise _WsDroppedAfterSendError() from err
        finally:
            if done_ok:
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
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Fallback: send request via REST and get the full response.

        The response speech is added to the chat log so REST turns appear
        in the chat history like WS turns (which stream via deltas). The
        request is bounded by the same configured response timeout as the
        WebSocket path.
        """
        conversation_id = _turn_conversation_id(user_input, chat_log)
        timeout = resolve_ws_receive_timeout(
            self._entry.options.get(CONF_WS_RECEIVE_TIMEOUT)
        )
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            headers = {"Authorization": f"Bearer {self._api_key}"}
            payload: dict[str, Any] = {
                "text": user_input.text,
                "conversation_id": conversation_id,
                "language": user_input.language or "en",
            }
            payload.update(self._resolve_origin_context(user_input))
            async with self._session.post(
                f"{self._url}/api/conversation",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    if resp.status in {401, 403}:
                        self._start_reauth_once()
                    logger.warning(
                        "ha-agenthub: REST conversation request failed with HTTP %s",
                        resp.status,
                    )
                    return await self._rest_result(
                        user_input,
                        chat_log,
                        _rest_fallback_error_message(resp.status),
                        conversation_id,
                    )
                self._reauth_triggered = False
                # The container's tracing middleware returns a per-request
                # trace id on every response; expose it to the log shipper.
                current_trace_id.set(resp.headers.get("X-Trace-Id"))
                data = await resp.json()
                if not isinstance(data, dict):
                    logger.warning(
                        "ha-agenthub: REST conversation response is not a JSON object"
                    )
                    return await self._rest_result(
                        user_input,
                        chat_log,
                        _rest_fallback_error_message(None),
                        conversation_id,
                    )
                speech = data.get("speech")
                return await self._rest_result(
                    user_input,
                    chat_log,
                    speech if isinstance(speech, str) else "",
                    # HA owns chat sessions; the container's conversation_id
                    # is only the container-internal correlation key and is
                    # deliberately not forwarded into the result.
                    conversation_id,
                    sanitized=bool(data.get("sanitized", False)),
                    continue_conversation=bool(data.get("voice_followup", False)),
                )
        except asyncio.TimeoutError:
            logger.warning(
                "ha-agenthub: REST conversation request timed out after %.1fs", timeout
            )
            return await self._rest_result(
                user_input, chat_log, _MSG_TIMEOUT, conversation_id
            )
        except (aiohttp.ClientError, json.JSONDecodeError, OSError):
            logger.warning(
                "ha-agenthub: REST conversation request failed", exc_info=True
            )
            return await self._rest_result(
                user_input, chat_log, _MSG_UNAVAILABLE, conversation_id
            )

    async def _rest_result(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        speech: str,
        conversation_id: str | None,
        *,
        sanitized: bool = False,
        continue_conversation: bool = False,
    ) -> conversation.ConversationResult:
        """Build the REST result and mirror its speech into the chat log."""
        result = self._build_result(
            speech,
            conversation_id,
            user_input.language,
            sanitized=sanitized,
            continue_conversation=continue_conversation,
        )
        await self._add_assistant_chat_log_content(chat_log, user_input, speech)
        return result

    async def _add_assistant_chat_log_content(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        content: str,
    ) -> None:
        """Add assistant content to the chat log for non-streaming paths.

        WS turns stream their content via
        :meth:`chat_log.async_add_delta_content_stream`; REST turns and the
        canned connection-drop message use this so the chat history matches
        the spoken response. ``async_add_assistant_content_without_tools``
        is a sync ``@callback`` in HA core (2025.4+): awaiting it raises
        ``TypeError`` and surfaces as ``intent-failed`` in the pipeline.
        """
        if not content:
            return
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=self.entity_id or user_input.agent_id,
                content=content,
            )
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
        return conversation.ConversationResult(
            response=response,
            conversation_id=conversation_id,
            continue_conversation=continue_conversation,
        )
