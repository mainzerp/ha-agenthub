"""HA WebSocket client for real-time state updates."""

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Callable
from typing import Any

import aiohttp

from app.db.repository import SettingsRepository
from app.ha_client.auth import get_ha_token

logger = logging.getLogger(__name__)

BASE_DELAY = 1.0
MAX_DELAY = 60.0
MAX_JITTER = 1.0
HEARTBEAT_INTERVAL = 15.0
IDLE_TIMEOUT = 2 * HEARTBEAT_INTERVAL
# P3-2: bound the auth handshake receives. Without this, a half-open
# socket that accepted ``ws_connect`` but never sends the
# ``auth_required`` / ``auth_ok`` frames blocks ``connect()`` (and
# therefore the whole receive loop) forever.
AUTH_HANDSHAKE_TIMEOUT = 10.0
# FLOW-RECONN-1 (P2-4): cap the tight reconnect loop so a permanently
# unreachable HA does not burn CPU and log noise forever. After the cap
# we pause, then reset the attempt counter and resume. ``_use_rest_fallback``
# stays ON during the pause so callers keep short-circuiting WS waits.
MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_PAUSE_DURATION = 300.0


class WebSocketResetError(Exception):
    """Raised on pending state waiters when the WebSocket is torn down.

    P3-5: callers awaiting :meth:`HAWebSocketClient.register_state_waiter`
    receive this exception instead of hanging forever once a reconnect
    drops the underlying socket. Catchers should fall back to REST polling
    or treat the action as unverified.
    """


class HAWebSocketClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running: bool = False
        self._use_rest_fallback: bool = False
        self._message_id: int = 0
        self._listeners: dict[str, list[Callable]] = {}
        self._logger = logging.getLogger("ha_client.websocket")
        self._ws_lock: asyncio.Lock = asyncio.Lock()
        self._ws_last_active: float = time.monotonic()
        # FLOW-VERIFY-1: per-entity state waiters, resolved from the single
        # WebSocket receive task. Key = entity_id, value = list of
        # (future, expected_state_or_None) tuples. Ordered FIFO so multiple
        # concurrent actions on the same entity resolve in the order they
        # registered.
        self._state_waiters: dict[str, list[tuple[asyncio.Future[str], str | None]]] = {}
        # PENDING-RESP: track in-flight command/result pairs for
        # request-response style WebSocket calls (e.g. entity_registry).
        self._pending_responses: dict[int, asyncio.Future[dict]] = {}

    def is_connected(self) -> bool:
        """Return True if the WebSocket connection is active and running."""
        return self._running and self._ws is not None and not self._ws.closed

    def _next_id(self) -> int:
        self._message_id += 1
        return self._message_id

    async def connect(self) -> bool:
        ha_url = await SettingsRepository.get_value("ha_url")
        token = await get_ha_token()
        if not ha_url or not token:
            self._logger.warning("HA URL or token not configured, skipping connection")
            return False

        ha_url = ha_url.rstrip("/")
        if ha_url.startswith("https://"):
            ws_url = ha_url.replace("https://", "wss://") + "/api/websocket"
        else:
            ws_url = ha_url.replace("http://", "ws://") + "/api/websocket"

        try:
            async with self._ws_lock:
                connector = aiohttp.TCPConnector(limit=10, limit_per_host=5, enable_cleanup_closed=True)
                self._session = aiohttp.ClientSession(connector=connector)
                self._ws = await self._session.ws_connect(ws_url, heartbeat=HEARTBEAT_INTERVAL)
                self._ws_last_active = time.monotonic()

                # P3-2: bound both handshake receives. A half-open HA that
                # never replies must not block the receive loop forever.
                try:
                    msg = await asyncio.wait_for(self._ws.receive_json(), timeout=AUTH_HANDSHAKE_TIMEOUT)
                except TimeoutError:
                    self._logger.error("HA WebSocket handshake timed out waiting for auth_required")
                    await self._close_session()
                    return False
                if msg.get("type") != "auth_required":
                    self._logger.error("Unexpected initial message from HA WebSocket")
                    await self._close_session()
                    return False

                await self._ws.send_json({"type": "auth", "access_token": token})
                try:
                    auth_response = await asyncio.wait_for(self._ws.receive_json(), timeout=AUTH_HANDSHAKE_TIMEOUT)
                except TimeoutError:
                    self._logger.error("HA WebSocket handshake timed out waiting for auth_ok")
                    await self._close_session()
                    return False

                if auth_response.get("type") != "auth_ok":
                    self._logger.error("HA WebSocket auth failed")
                    await self._close_session()
                    return False

                self._running = True
                self._logger.info("Connected to HA WebSocket")

                # Auto-subscribe to all registered event types
                for event_type in self._listeners:
                    await self.subscribe_events(event_type)

            return True
        except (aiohttp.ClientError, TimeoutError, ConnectionError):
            self._logger.error("Failed to connect to HA WebSocket", exc_info=True)
            await self._close_session()
            return False

    async def _close_session(self) -> None:
        # P3-5: fail any pending state waiters so callers in
        # ``expect_state`` / verifier paths unblock immediately and
        # can fall back to REST polling instead of hanging until the
        # next reconnect.
        self._cancel_all_state_waiters("websocket_closed")
        async with self._ws_lock:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            self._ws = None
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    def _cancel_all_state_waiters(self, reason: str) -> None:
        """Resolve every pending state waiter with ``WebSocketResetError``.

        P3-5: invoked from ``_close_session`` so a reconnect (admin URL
        change, idle timeout, transport error) does not leave verifier
        callers waiting for an event that will never arrive on the new
        connection.
        """
        if not self._state_waiters:
            return
        snapshot = self._state_waiters
        self._state_waiters = {}
        for waiters in snapshot.values():
            for future, _expected in waiters:
                if future.done():
                    continue
                with contextlib.suppress(asyncio.InvalidStateError):
                    future.set_exception(WebSocketResetError(reason))

    async def disconnect(self) -> None:
        self._running = False
        await self._close_session()
        self._logger.info("Disconnected from HA WebSocket")

    async def drop_connection(self) -> None:
        """Close the live socket without stopping ``run()``.

        Used after admin updates ``ha_url`` / token so the receive loop
        exits and ``run()`` reconnects with fresh settings from the DB.
        """
        if not self._running:
            return
        await self._close_session()
        self._logger.info("HA WebSocket connection dropped for reconnect")

    async def run(self) -> None:
        self._running = True
        while self._running:
            connected = await self.connect()
            if not connected:
                if not self._running:
                    return
                await self._reconnect_loop()
                continue
            try:
                await self._receive_loop()
            except Exception:
                self._logger.error("WebSocket receive loop error", exc_info=True)
            if self._running:
                await self._close_session()
                await self._reconnect_loop()

    async def _reconnect_loop(self) -> None:
        attempt = 0
        max_delay = MAX_DELAY
        try:
            val = await SettingsRepository.get_value("communication.ws_reconnect_interval")
            if val is not None:
                max_delay = float(val)
        except Exception:
            self._logger.debug("Failed to read ws_reconnect_interval, using default", exc_info=True)
        while self._running:
            delay = min(BASE_DELAY * (2**attempt), max_delay) + random.uniform(0, MAX_JITTER)
            self._logger.info("Reconnecting in %.1fs (attempt %d)", delay, attempt + 1)
            await asyncio.sleep(delay)
            try:
                if await self.connect():
                    self._use_rest_fallback = False
                    return
            except Exception:
                self._logger.error("Reconnect attempt failed", exc_info=True)
            attempt += 1
            if attempt >= 5 and not self._use_rest_fallback:
                self._use_rest_fallback = True
                self._logger.warning(
                    "WebSocket reconnect failed after %d attempts, enabling REST fallback",
                    attempt,
                )
            # FLOW-RECONN-1 (P2-4): after MAX_RECONNECT_ATTEMPTS cap the
            # loop with a long pause. This prevents an offline HA from
            # spinning the loop at max_delay + jitter indefinitely. We
            # then reset the attempt counter and resume the tight loop
            # so recovery is still quick once HA comes back.
            if attempt >= MAX_RECONNECT_ATTEMPTS:
                self._logger.warning(
                    "WebSocket reconnect capped at %d attempts; pausing for %.0fs",
                    MAX_RECONNECT_ATTEMPTS,
                    RECONNECT_PAUSE_DURATION,
                )
                try:
                    await asyncio.sleep(RECONNECT_PAUSE_DURATION)
                except asyncio.CancelledError:
                    raise
                if not self._running:
                    return
                self._logger.info("Resuming WebSocket reconnect attempts after pause")
                attempt = 0

    async def _receive_loop(self) -> None:
        while self._running and self._ws and not self._ws.closed:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=IDLE_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._logger.warning(
                    "HA WebSocket idle for >%.0fs, forcing reconnect",
                    IDLE_TIMEOUT,
                )
                break
            self._ws_last_active = time.monotonic()
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    event_type = data.get("type", "")
                    if event_type == "event":
                        event = data.get("event", {})
                        et = event.get("event_type", "")
                        if et == "state_changed":
                            self._dispatch_state_waiters(event)
                        for callback in self._listeners.get(et, []):
                            try:
                                result = callback(event)
                                if asyncio.iscoroutine(result):
                                    await result
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                self._logger.error("Event callback error", exc_info=True)
                    elif event_type == "result":
                        msg_id = data.get("id")
                        self._logger.debug(
                            "_receive_loop: got result id=%d pending=%s", msg_id, msg_id in self._pending_responses
                        )
                        if isinstance(msg_id, int) and msg_id in self._pending_responses:
                            future = self._pending_responses.pop(msg_id)
                            if not future.done():
                                with contextlib.suppress(asyncio.InvalidStateError):
                                    future.set_result(data)
                except json.JSONDecodeError:
                    self._logger.warning("Received non-JSON WebSocket message")
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                break

    async def subscribe_events(self, event_type: str | None = None) -> int:
        msg_id = self._next_id()
        payload: dict[str, Any] = {"id": msg_id, "type": "subscribe_events"}
        if event_type:
            payload["event_type"] = event_type
        if self._ws and not self._ws.closed:
            await self._ws.send_json(payload)
        return msg_id

    async def send_command(self, msg_type: str, **kwargs: Any) -> dict | None:
        """Send a request-response command over the WebSocket and await the result.

        Returns the ``result`` payload dict on success, or ``None`` if the
        command could not be sent or the response indicated an error.
        """
        if not self._ws or self._ws.closed:
            return None
        msg_id = self._next_id()
        payload: dict[str, Any] = {"id": msg_id, "type": msg_type, **kwargs}
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending_responses[msg_id] = future
        try:
            async with self._ws_lock:
                await self._ws.send_json(payload)
            # PENDING-RESP: HA typically answers within a few hundred ms.
            # Cap at 10s so a lost message does not hang the sync forever.
            result_data = await asyncio.wait_for(future, timeout=10.0)
            self._logger.debug("send_command: received result for id=%d success=%s", msg_id, result_data.get("success"))
        except TimeoutError:
            self._pending_responses.pop(msg_id, None)
            self._logger.debug("WebSocket command %s (id=%d) timed out", msg_type, msg_id)
            return None
        except Exception:
            self._pending_responses.pop(msg_id, None)
            self._logger.debug("WebSocket command %s (id=%d) failed", msg_type, msg_id, exc_info=True)
            return None
        if result_data.get("success"):
            return result_data.get("result")
        self._logger.debug(
            "WebSocket command %s (id=%d) returned error: %s",
            msg_type,
            msg_id,
            result_data.get("error"),
        )
        return None

    async def get_hidden_entity_ids(self) -> set[str]:
        """Query HA's entity registry via WebSocket for hidden/disabled entities."""
        result = await self.send_command("config/entity_registry/list")
        if not isinstance(result, list):
            return set()
        hidden: set[str] = set()
        for row in result:
            if not isinstance(row, dict):
                continue
            if row.get("hidden_by") or row.get("disabled_by"):
                eid = row.get("entity_id")
                if isinstance(eid, str) and eid:
                    hidden.add(eid)
        self._logger.info("Fetched %d hidden/disabled entities via WebSocket", len(hidden))
        return hidden

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | None = None,
        service_data: dict[str, Any] | None = None,
        *,
        return_response: bool = False,
    ) -> dict[str, Any] | None:
        """Call a Home Assistant service via WebSocket.

        Returns the response dict on success, or ``None`` if the call could not
        be sent, timed out, or returned an error result.
        """
        if not self.is_connected():
            return None
        payload = {
            "domain": domain,
            "service": service,
            "service_data": service_data or {},
            "return_response": return_response,
        }
        if entity_id:
            payload["target"] = {"entity_id": entity_id}
        self._logger.debug("call_service: sending %s.%s for %s", domain, service, entity_id)
        result_data = await self.send_command("call_service", **payload)
        self._logger.debug("call_service: result for %s.%s = %s", domain, service, result_data)
        if not isinstance(result_data, dict):
            return None
        return result_data.get("response", result_data)

    def on_event(self, event_type: str, callback: Callable) -> None:
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(callback)

    # -----------------------------------------------------------------
    # FLOW-VERIFY-1: state-change waiters for post-action verification
    # -----------------------------------------------------------------
    def register_state_waiter(
        self,
        entity_id: str,
        *,
        expected: str | None = None,
    ) -> asyncio.Future[str]:
        """Register a waiter that resolves on the next matching state_changed.

        Call this BEFORE issuing the action that is expected to cause the
        state change, to avoid a race where the event arrives before the
        waiter is registered.

        Args:
            entity_id: Entity to watch (e.g. ``"light.keller"``).
            expected: If given, only events whose ``new_state.state`` equals
                this string resolve the waiter. ``None`` resolves on the
                first ``state_changed`` event for the entity.

        Returns:
            An ``asyncio.Future`` that resolves with the observed
            ``new_state.state`` string. The caller is responsible for
            awaiting it with a timeout and for cancelling on its own error
            paths if desired.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._state_waiters.setdefault(entity_id, []).append((future, expected))
        return future

    def _dispatch_state_waiters(self, event: dict) -> None:
        """Resolve pending waiters for a ``state_changed`` event."""
        data = event.get("data") or {}
        entity_id = data.get("entity_id") or ""
        new_state = data.get("new_state") or {}
        if not entity_id or not isinstance(new_state, dict):
            return
        state_value = new_state.get("state")
        if not isinstance(state_value, str):
            return
        waiters = self._state_waiters.get(entity_id)
        if not waiters:
            return
        kept: list[tuple[asyncio.Future[str], str | None]] = []
        for future, expected in waiters:
            if future.done():
                continue
            if expected is not None and state_value != expected:
                kept.append((future, expected))
                continue
            with contextlib.suppress(asyncio.InvalidStateError):
                future.set_result(state_value)
        if kept:
            self._state_waiters[entity_id] = kept
        else:
            self._state_waiters.pop(entity_id, None)

    def cancel_state_waiter(self, entity_id: str, future: asyncio.Future[str]) -> None:
        """Remove a pending waiter (e.g. on timeout) without resolving it."""
        waiters = self._state_waiters.get(entity_id)
        if not waiters:
            return
        remaining = [(f, exp) for f, exp in waiters if f is not future]
        if remaining:
            self._state_waiters[entity_id] = remaining
        else:
            self._state_waiters.pop(entity_id, None)
        if not future.done():
            future.cancel()
