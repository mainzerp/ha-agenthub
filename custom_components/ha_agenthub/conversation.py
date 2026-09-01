"""Conversation entity for HA-AgentHub (I/O bridge)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
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
    DEFAULT_WS_RECEIVE_TIMEOUT,
    DOMAIN,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    WS_HEARTBEAT_INTERVAL,
    WS_IDLE_THRESHOLD,
    WS_PATH,
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
        back-to-back from production HA setups. Coalesced duplicates share
        the first invocation's ``chat_log`` (same conversation_id by
        construction), so streamed content lands in that chat log.
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
                    self._async_bridge_with_cleanup(user_input, key, chat_log)
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
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        task = asyncio.current_task()
        try:
            return await self._async_bridge_to_container(user_input, chat_log)
        finally:
            async with self._coalesce_lock:
                existing = self._inflight_bridge.get(key)
                if task is not None and existing is not None and existing[1] is task:
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
                    return await self._process_via_ws_read(
                        user_input, chat_log, turn_ws
                    )
                except _WsDroppedAfterSendError:
                    logger.warning(
                        "WebSocket failed after the request was sent; skipping REST "
                        "(avoids duplicate container traces)",
                        exc_info=True,
                    )
                    # On mid-stream failure the delta stream added nothing
                    # to the chat log; add the canned message so display
                    # and speech stay consistent.
                    drop_speech = (
                        "The connection dropped before the reply finished. "
                        "If the action may have run, check your devices."
                    )
                    await self._add_assistant_chat_log_content(
                        chat_log, user_input, drop_speech
                    )
                    return self._build_result(
                        drop_speech,
                        user_input.conversation_id,
                        user_input.language,
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    logger.warning("WebSocket error, falling back to REST")
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
        """
        box: dict[str, Any] = {
            "filler": "",
            "tokens": "",
            "mediated": "",
            "canned_error": "",
            "conversation_id": user_input.conversation_id,
            "sanitized": False,
            "voice_followup": False,
        }

        async def _delta_stream(
            box: dict[str, Any],
        ) -> AsyncIterator[conversation.AssistantContentDeltaDict]:
            message_open = False

            def _receive_timeout() -> float:
                try:
                    return float(
                        self._entry.options.get(
                            CONF_WS_RECEIVE_TIMEOUT, DEFAULT_WS_RECEIVE_TIMEOUT
                        )
                    )
                except (TypeError, ValueError):
                    return float(DEFAULT_WS_RECEIVE_TIMEOUT)

            while True:
                msg = await asyncio.wait_for(
                    turn_ws.receive(), timeout=_receive_timeout()
                )
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning(
                            "ha-agenthub: ignoring malformed WS message in stream"
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
                        box["conversation_id"] = data.get(
                            "conversation_id", box["conversation_id"]
                        )
                        # P3-1: the backend signals sanitization on the done
                        # frame. Honour it for both ``mediated_speech`` and
                        # accumulated tokens (the orchestrator strips both
                        # before emitting).
                        box["sanitized"] = bool(data.get("sanitized", False))
                        box["voice_followup"] = bool(data.get("voice_followup", False))
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
                            # (would become _WsDroppedAfterSendError).
                            logger.warning(
                                "Container reported error in stream done chunk: %s",
                                stream_err,
                            )
                            if not (box["mediated"] or box["tokens"]).strip():
                                box["canned_error"] = (
                                    "The assistant could not complete that request. "
                                    f"({stream_err})"
                                )
                                if not message_open:
                                    yield {"role": "assistant"}
                                    message_open = True
                                yield {"content": box["canned_error"]}
                        return
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise aiohttp.ClientError(
                        f"WebSocket {'closed' if msg.type == aiohttp.WSMsgType.CLOSED else 'error'} mid-stream"
                    )

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
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
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
        in the chat history like WS turns (which stream via deltas).
        """
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
                    return await self._rest_result(
                        user_input,
                        chat_log,
                        _rest_fallback_error_message(resp.status),
                        user_input.conversation_id,
                    )
                self._reauth_triggered = False
                data = await resp.json()
                return await self._rest_result(
                    user_input,
                    chat_log,
                    data.get("speech", ""),
                    data.get("conversation_id", user_input.conversation_id),
                    sanitized=bool(data.get("sanitized", False)),
                    continue_conversation=bool(data.get("voice_followup", False)),
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return await self._rest_result(
                user_input,
                chat_log,
                (
                    "Sorry, the assistant container is unavailable. "
                    "Check that the container is running and reachable from Home Assistant."
                ),
                user_input.conversation_id,
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
