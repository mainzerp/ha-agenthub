"""Conversation entity for HA-AgentHub (I/O bridge)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp

from homeassistant.components import assist_pipeline, conversation
from homeassistant.components.conversation import ConversationEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, CONF_API_KEY, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

try:
    from homeassistant.helpers.event import async_track_state_change_event
except ModuleNotFoundError:
    async_track_state_change_event = None

from ._filler_gate import FillerGate
from .const import (
    DOMAIN,
    WS_PATH,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    WS_HEARTBEAT_INTERVAL,
    WS_IDLE_THRESHOLD,
    CONF_NATIVE_PLAIN_TIMERS,
    DEFAULT_NATIVE_PLAIN_TIMERS,
    NATIVE_HA_AGENT_ID,
    NATIVE_PLAIN_TIMER_DIRECTIVE,
    NATIVE_PLAIN_TIMER_ELIGIBLE_FIELD,
    NATIVE_PLAIN_TIMER_ELIGIBLE_HEADER,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Native plain-timer delegation (0.25.1)
# ---------------------------------------------------------------------------
#
# The integration no longer classifies utterances locally. Instead, when the
# per-config-entry ``CONF_NATIVE_PLAIN_TIMERS`` opt-in is enabled, every
# bridge request is marked eligible (additive JSON field + REST header).
# The container timer-agent owns the semantic decision and may return a
# ``directive=delegate_native_plain_timer`` response through the normal
# orchestrator path. The integration honours the directive by calling the
# proven native seam (``conversation.async_converse(...,
# agent_id=NATIVE_HA_AGENT_ID)``).
#
# Recursion safety: ``_async_delegate_to_native`` falls back to the bridge
# on pre-handler errors. To prevent that fallback from triggering a second
# directive loop, eligibility is suppressed via a task-local ContextVar
# while a directive is being honoured.


@dataclass(slots=True)
class _BridgeDirective:
    """Internal carrier returned by bridge senders when the container
    instructs the integration to delegate to native HA Assist."""

    directive: str
    reason: str | None = None
    conversation_id: str | None = None


# Task-local suppression of the eligibility flag/header. When True, neither
# the WebSocket payload nor the REST request includes the eligibility
# signal so the bridge cannot
# emit a second native directive for the same turn.
_suppress_native_plain_timer_eligibility: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ha_agenthub_suppress_native_plain_timer_eligibility",
    default=False,
)

MAX_FILLER_WAIT_SECONDS = 6.0
_FILLER_PLAYING_STATES = frozenset({"playing"})
_FILLER_FINISHED_STATES = frozenset({"idle", "off", "paused"})


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
        self._filler_gates: dict[str, FillerGate] = {}
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
                    pong = await self._ws.ping()
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

        0.25.1: the integration no longer classifies utterances locally.
        Native plain-timer delegation is decided by the container's timer-agent
        path and surfaces as a directive on the bridge response;
        ``_async_bridge_with_cleanup`` honours the directive inside the
        coalesced task so duplicate suppression still applies.
        """
        cid = user_input.conversation_id or ""
        text = (user_input.text or "").strip()
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
            outcome = await self._async_bridge_to_container(user_input)
            if isinstance(outcome, _BridgeDirective):
                return await self._handle_bridge_directive(user_input, outcome)
            return outcome
        finally:
            async with self._coalesce_lock:
                existing = self._inflight_bridge.get(key)
                if task is not None and existing is not None and existing[1] is task:
                    self._inflight_bridge.pop(key, None)

    async def _handle_bridge_directive(
        self,
        user_input: conversation.ConversationInput,
        directive: _BridgeDirective,
    ) -> conversation.ConversationResult:
        """Honour a directive returned by the container bridge.

        Currently only ``delegate_native_plain_timer`` is supported. The
        eligibility flag is suppressed for the duration of the native
        attempt so that ``_async_delegate_to_native``'s own pre-handler
        bridge fallback cannot trigger a second directive loop.
        """
        if directive.directive == NATIVE_PLAIN_TIMER_DIRECTIVE:
            native_callable = self._resolve_native_delegate()
            reason = directive.reason or "native"
            if native_callable is None:
                logger.warning(
                    "HA-AgentHub: native delegate unavailable, retrying bridge "
                    "(path=agenthub, reason=native_unavailable)"
                )
                token = _suppress_native_plain_timer_eligibility.set(True)
                try:
                    fallback = await self._async_bridge_to_container(user_input)
                finally:
                    _suppress_native_plain_timer_eligibility.reset(token)
                if isinstance(fallback, _BridgeDirective):
                    # Bridge unexpectedly emitted a second directive even with
                    # suppression on. Surface a benign error instead of looping.
                    return self._build_result(
                        "Sorry, the assistant could not complete that request.",
                        user_input.conversation_id,
                        user_input.language,
                    )
                return fallback
            logger.debug(
                "HA-AgentHub: honouring native plain-timer directive "
                "(path=native, reason=%s)",
                reason,
            )
            token = _suppress_native_plain_timer_eligibility.set(True)
            try:
                return await self._async_delegate_to_native(
                    user_input, native_callable, reason
                )
            finally:
                _suppress_native_plain_timer_eligibility.reset(token)

        # Unknown directive: log and run one bridge fallback with eligibility
        # suppressed. Never recurse on unknown values.
        logger.warning(
            "HA-AgentHub: ignoring unknown bridge directive %r (path=agenthub)",
            directive.directive,
        )
        token = _suppress_native_plain_timer_eligibility.set(True)
        try:
            fallback = await self._async_bridge_to_container(user_input)
        finally:
            _suppress_native_plain_timer_eligibility.reset(token)
        if isinstance(fallback, _BridgeDirective):
            return self._build_result(
                "Sorry, the assistant could not complete that request.",
                user_input.conversation_id,
                user_input.language,
            )
        return fallback

    # ------------------------------------------------------------------
    # Native plain-timer delegation helpers (0.25.0)
    # ------------------------------------------------------------------

    def _is_native_plain_timers_enabled(self) -> bool:
        """Return True if the integration is opted into native plain-timer
        delegation. Default False keeps existing behavior unchanged when the
        flag is absent or the entry data is missing."""
        try:
            data = getattr(self._entry, "data", None) or {}
            return bool(data.get(CONF_NATIVE_PLAIN_TIMERS, DEFAULT_NATIVE_PLAIN_TIMERS))
        except Exception:
            return DEFAULT_NATIVE_PLAIN_TIMERS

    def _resolve_native_delegate(self):
        """Resolve the HA conversation delegate seam.

        Phase 1 proven seam: ``conversation.async_converse(..., agent_id=
        NATIVE_HA_AGENT_ID)``. The ``agent_id`` ensures HA core dispatches
        directly to the built-in default agent, never re-entering this
        custom entity. Returns the callable or None if the API is missing
        on the running HA core (e.g., very old core or the stub used in
        tests).
        """
        try:
            return getattr(conversation, "async_converse", None)
        except Exception:
            return None

    async def _async_delegate_to_native(
        self,
        user_input: conversation.ConversationInput,
        native_callable,
        reason_code: str,
    ) -> conversation.ConversationResult:
        """Delegate the request to HA's built-in default conversation agent.

        On success the native ConversationResult is returned directly; per
        plan D9 we never retry the request through AgentHub once native
        has produced a definitive response. Only delegate-side construction
        errors (raised before the native handler runs) fall through to the
        AgentHub bridge as a safety net.
        """
        context = getattr(user_input, "context", None)
        try:
            result = await native_callable(
                self.hass,
                user_input.text,
                conversation_id=user_input.conversation_id,
                context=context,
                language=user_input.language,
                agent_id=NATIVE_HA_AGENT_ID,
            )
            logger.info(
                "HA-AgentHub: native Assist handled plain timer "
                "(path=native, reason=%s)",
                reason_code,
            )
            return result
        except Exception:
            # Definitive native handler errors (e.g., intent-not-matched)
            # are surfaced inside ConversationResult, not raised. A raised
            # exception here means we never reached the native handler --
            # safe to fall back to AgentHub once.
            logger.warning(
                "HA-AgentHub: native delegation failed before handler ran, "
                "falling back to AgentHub (path=agenthub, reason=native_error)",
                exc_info=True,
            )
            return await self._async_bridge_to_container(user_input)

    async def _async_bridge_to_container(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult | _BridgeDirective:
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
        """Resolve device_id/area_id and their human-readable names.

        FLOW-CTX-1 (0.18.6): IDs alone were not enough for traces or
        area-aware entity resolution. Adding the display names here
        means the container can annotate speech and the trace UI
        with "Kitchen Satellite / Kitchen" instead of opaque UUIDs.
        Lookup failures degrade silently -- the IDs stay authoritative.
        """
        extra: dict[str, str] = {}
        device_id = getattr(user_input, "device_id", None)
        if not device_id:
            return extra
        extra["device_id"] = device_id
        try:
            device_reg = dr.async_get(self.hass)
            device = device_reg.async_get(device_id)
        except Exception:
            device = None
        if not device:
            return extra

        device_name = device.name_by_user or device.name
        if device_name:
            extra["device_name"] = device_name

        area_id = device.area_id
        if not area_id:
            return extra
        extra["area_id"] = area_id
        try:
            area_reg = ar.async_get(self.hass)
            area = area_reg.async_get_area(area_id)
            if area and area.name:
                extra["area_name"] = area.name
        except Exception:
            logger.debug("area_registry lookup failed for %s", area_id, exc_info=True)
        return extra

    def _filler_gate_key(self, user_input) -> str:
        """Return the per-origin key used to gate filler completion."""
        device_id = getattr(user_input, "device_id", None)
        if isinstance(device_id, str) and device_id:
            return f"device:{device_id}"
        area_id = getattr(user_input, "area_id", None)
        if isinstance(area_id, str) and area_id:
            return f"area:{area_id}"
        return "__global__"

    async def _process_via_ws(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult | _BridgeDirective:
        """Send request via WebSocket and accumulate streaming tokens."""
        gate_key = HaAgentHubConversationEntity._filler_gate_key(self, user_input)
        payload: dict[str, Any] = {
            "text": user_input.text,
            "conversation_id": user_input.conversation_id,
            "language": user_input.language or "en",
        }
        payload.update(self._resolve_origin_context(user_input))
        if (
            self._is_native_plain_timers_enabled()
            and not _suppress_native_plain_timer_eligibility.get()
        ):
            payload[NATIVE_PLAIN_TIMER_ELIGIBLE_FIELD] = True
        await self._ws.send_json(payload)

        try:
            speech_parts: list[str] = []
            final_conversation_id = user_input.conversation_id

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

                    # Handle filler tokens -- speak immediately via TTS, do not accumulate
                    if data.get("is_filler", False):
                        filler_text = data.get("token", "")
                        if filler_text:
                            await self._speak_filler(filler_text, user_input)
                        continue

                    token_text = data.get("token", "")
                    if token_text:
                        speech_parts.append(token_text)
                    if data.get("done", False):
                        received_done = True
                        stream_err = data.get("error")
                        final_conversation_id = data.get("conversation_id", final_conversation_id)
                        # 0.25.1: directive on the final frame short-circuits the
                        # bridge response. The integration delegates to native
                        # Assist instead of returning the (empty) speech.
                        directive_value = data.get("directive")
                        if directive_value:
                            try:
                                await HaAgentHubConversationEntity._await_filler_gate(self, gate_key)
                            except Exception:
                                logger.debug("Filler gate await raised; proceeding", exc_info=True)
                            self._ws_last_active = time.monotonic()
                            return _BridgeDirective(
                                directive=str(directive_value),
                                reason=data.get("reason"),
                                conversation_id=final_conversation_id,
                            )
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

            try:
                await HaAgentHubConversationEntity._await_filler_gate(self, gate_key)
            except Exception:
                logger.debug("Filler gate await raised; proceeding", exc_info=True)
            self._ws_last_active = time.monotonic()
            speech = "".join(speech_parts)
            return self._build_result(speech, final_conversation_id, user_input.language, sanitized=stream_sanitized)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as err:
            gates = getattr(self, "_filler_gates", None)
            if not isinstance(gates, dict):
                gates = {}
                self._filler_gates = gates
            stale = gates.pop(gate_key, None)
            if stale is not None:
                try:
                    if stale.cleanup:
                        stale.cleanup()
                except Exception:
                    logger.debug("Failed to clean up filler gate after dropped stream", exc_info=True)
                finally:
                    stale.event.set()
            raise _WsDroppedAfterSendError() from err

    async def _process_via_rest(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult | _BridgeDirective:
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
            if (
                self._is_native_plain_timers_enabled()
                and not _suppress_native_plain_timer_eligibility.get()
            ):
                payload[NATIVE_PLAIN_TIMER_ELIGIBLE_FIELD] = True
                headers[NATIVE_PLAIN_TIMER_ELIGIBLE_HEADER] = "1"
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
                directive_value = data.get("directive")
                if directive_value:
                    return _BridgeDirective(
                        directive=str(directive_value),
                        reason=data.get("reason"),
                        conversation_id=data.get("conversation_id", user_input.conversation_id),
                    )
                return self._build_result(
                    data.get("speech", ""),
                    data.get("conversation_id", user_input.conversation_id),
                    user_input.language,
                    sanitized=bool(data.get("sanitized", False)),
                )
        except (aiohttp.ClientError, TimeoutError):
            return self._build_result(
                "Sorry, the assistant container is unavailable. Check that the container is running and reachable from Home Assistant.",
                user_input.conversation_id,
                user_input.language,
            )

    def _build_result(self, speech: str, conversation_id: str | None, language: str | None, *, sanitized: bool = False) -> conversation.ConversationResult:
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

    def _resolve_satellite_entity(self, user_input) -> str | None:
        """Resolve the originating assist_satellite entity from device or area context."""
        try:
            entity_reg = er.async_get(self.hass)
            device_reg = dr.async_get(self.hass)

            device_id = getattr(user_input, "device_id", None)
            area_id = getattr(user_input, "area_id", None)
            if isinstance(device_id, str) and device_id:
                for entry in entity_reg.entities.values():
                    if entry.domain == "assist_satellite" and entry.device_id == device_id:
                        return entry.entity_id
                device = device_reg.async_get(device_id)
                if device and device.area_id:
                    area_id = device.area_id

            if not isinstance(area_id, str) or not area_id:
                return None

            for entry in entity_reg.entities.values():
                if entry.domain != "assist_satellite" or not entry.device_id:
                    continue
                sat_device = device_reg.async_get(entry.device_id)
                if sat_device and sat_device.area_id == area_id:
                    return entry.entity_id
        except Exception:
            logger.debug("Failed to resolve assist satellite entity", exc_info=True)
        return None

    def _arm_filler_gate(self, key: str, *, mechanism, cleanup=None) -> FillerGate:
        """Create or replace the filler gate for a given origin key."""
        gates = getattr(self, "_filler_gates", None)
        if not isinstance(gates, dict):
            gates = {}
            self._filler_gates = gates

        stale = gates.pop(key, None)
        if stale is not None:
            stale.event.set()
            if stale.cleanup:
                try:
                    stale.cleanup()
                except Exception:
                    logger.debug("Failed to clean up stale filler gate", exc_info=True)

        gate = FillerGate(
            event=asyncio.Event(),
            deadline=time.monotonic() + MAX_FILLER_WAIT_SECONDS,
            mechanism=mechanism,
            cleanup=cleanup,
        )
        gates[key] = gate
        return gate

    def _make_media_player_state_callback(self, gate: FillerGate):
        """Build the media_player state callback for filler completion."""

        def _handle_state_change(event) -> None:
            new_state = event.data.get("new_state") if event else None
            new_state_value = getattr(new_state, "state", None)
            if new_state_value in _FILLER_PLAYING_STATES:
                gate.observed_playing = True
            elif new_state_value in _FILLER_FINISHED_STATES and gate.observed_playing:
                gate.event.set()

        return _handle_state_change

    async def _await_filler_gate(self, key: str) -> None:
        """Wait for filler playback to finish for the given origin key."""
        gates = getattr(self, "_filler_gates", None)
        if not isinstance(gates, dict):
            gates = {}
            self._filler_gates = gates

        gate = gates.get(key)
        if gate is None:
            return

        remaining = max(0.0, gate.deadline - time.monotonic())
        try:
            await asyncio.wait_for(gate.event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            gate.event.set()
            logger.warning(
                "Filler completion signal not received within %.1fs cap; releasing gate",
                MAX_FILLER_WAIT_SECONDS,
            )
        finally:
            if gate.cleanup:
                try:
                    gate.cleanup()
                except Exception:
                    logger.debug("Failed to clean up filler gate subscription", exc_info=True)
            current = gates.get(key)
            if current is gate:
                gates.pop(key, None)

    async def _speak_filler(self, text: str, user_input) -> None:
        """Speak filler text immediately via TTS, bypassing the
        conversation result.

        FLOW-HIGH-7: ``tts.speak`` validates ``entity_id`` against the
        ``tts`` domain -- passing a ``media_player.*`` entity (old
        behavior) makes HA drop the call with ``vol.Invalid`` and no
        audio is ever produced. The correct schema is:
        ``entity_id`` = a ``tts.*`` engine entity, and
        ``media_player_entity_id`` = the target media_player.
        """
        gate_key = self._filler_gate_key(user_input)
        try:
            satellite = self._resolve_satellite_entity(user_input)
            if satellite:
                gate = self._arm_filler_gate(gate_key, mechanism="announce")
                try:
                    await asyncio.wait_for(
                        self.hass.services.async_call(
                            "assist_satellite",
                            "announce",
                            {
                                "entity_id": satellite,
                                "message": _strip_markdown(text),
                            },
                            blocking=True,
                        ),
                        timeout=MAX_FILLER_WAIT_SECONDS,
                    )
                except Exception:
                    logger.debug("assist_satellite.announce failed", exc_info=True)
                finally:
                    gate.event.set()
                return

            device_id = getattr(user_input, "device_id", None)
            if not isinstance(device_id, str) or not device_id:
                return
            media_player = self._resolve_tts_entity(device_id)
            tts_engine = self._resolve_tts_engine_entity()
            if not media_player or not tts_engine or async_track_state_change_event is None:
                return

            gate = self._arm_filler_gate(gate_key, mechanism="media_player_state")
            callback = self._make_media_player_state_callback(gate)
            gate.cleanup = async_track_state_change_event(self.hass, [media_player], callback)

            try:
                await self.hass.services.async_call(
                    "tts",
                    "speak",
                    {
                        "entity_id": tts_engine,
                        "media_player_entity_id": media_player,
                        "message": _strip_markdown(text),
                    },
                    blocking=False,
                )
            except Exception:
                logger.debug("tts.speak failed", exc_info=True)
                gate.event.set()
        except Exception:
            logger.debug("Failed to speak filler text", exc_info=True)
            gates = getattr(self, "_filler_gates", None)
            if isinstance(gates, dict):
                stale = gates.get(gate_key)
                if stale is not None:
                    stale.event.set()

    def _resolve_tts_engine_entity(self) -> str | None:
        """Return a configured TTS engine entity_id (``tts.*``).

        Preferred source: the TTS engine configured on the Assist
        pipeline currently bound to this conversation entity. If
        pipeline_select is not reachable, fall back to the first
        ``tts.*`` entity in the entity registry so filler audio still
        plays on single-engine installations (the common case).
        """
        try:
            pipeline = None
            try:
                get_pipeline = getattr(assist_pipeline, "async_get_pipeline", None)
                if get_pipeline is not None:
                    pipeline = get_pipeline(self.hass)
            except Exception:
                pipeline = None
            if pipeline is not None:
                engine = getattr(pipeline, "tts_engine", None)
                if isinstance(engine, str) and engine.startswith("tts."):
                    return engine

            entity_reg = er.async_get(self.hass)
            for entry in entity_reg.entities.values():
                if entry.domain == "tts":
                    return entry.entity_id
        except Exception:
            logger.debug("Failed to resolve TTS engine entity", exc_info=True)
        return None

    def _resolve_tts_entity(self, device_id: str) -> str | None:
        """Resolve a device_id to a TTS-capable media_player entity in the same area."""
        try:
            device_reg = dr.async_get(self.hass)
            device = device_reg.async_get(device_id)
            if not device or not device.area_id:
                return None
            entity_reg = er.async_get(self.hass)
            for entry in entity_reg.entities.values():
                if (
                    entry.domain == "media_player"
                    and entry.device_id
                ):
                    mp_device = device_reg.async_get(entry.device_id)
                    if mp_device and mp_device.area_id == device.area_id:
                        return entry.entity_id
            return None
        except Exception:
            logger.debug("Failed to resolve TTS entity for device %s", device_id)
            return None
