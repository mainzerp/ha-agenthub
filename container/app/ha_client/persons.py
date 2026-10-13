"""Home Assistant person lookup: HA user id -> ``person.*`` entity and display name.

A ``person.*`` entity links a Home Assistant user through
``attributes.user_id``. :func:`find_person_by_user_id` is the shared matcher
(also used by the calendar identity resolver); :class:`PersonDirectory`
caches the user-id map built from one ``get_states`` call so the
orchestrator can name the requesting user once per turn without a state
fetch on every request.

Personalization only: the result shapes wording (prompt line, mediation,
send sender name) and is never used for visibility or authorization.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.util.tasks import spawn

logger = logging.getLogger(__name__)

PERSON_ENTITY_PREFIX = "person."

# A successful fetch is reused for this long; ``state_changed`` events that
# touch a person's name or linked user invalidate it earlier.
DEFAULT_TTL_SECONDS = 300.0
# After a failed fetch, lookups serve only a never-invalidated map (or
# nothing) and retry no sooner than this, so an HA outage does not cost a
# fetch per turn.
FAILURE_RETRY_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class PersonMatch:
    """A ``person.*`` entity linked to a Home Assistant user."""

    entity_id: str
    # ``attributes.friendly_name``; None when the person has none.
    name: str | None


def _person_name(attributes: dict[str, Any]) -> str | None:
    name = attributes.get("friendly_name")
    if not isinstance(name, str):
        return None
    return name.strip() or None


def build_person_map(states: Iterable[Any] | None) -> dict[str, PersonMatch]:
    """Map HA user id -> :class:`PersonMatch` for every linked ``person.*`` state.

    When several persons claim the same user id, the first one wins.
    """
    persons: dict[str, PersonMatch] = {}
    for state in states or ():
        if not isinstance(state, dict):
            continue
        entity_id = state.get("entity_id", "")
        if not isinstance(entity_id, str) or not entity_id.startswith(PERSON_ENTITY_PREFIX):
            continue
        attributes = state.get("attributes", {})
        if not isinstance(attributes, dict):
            continue
        user_id = attributes.get("user_id")
        if not isinstance(user_id, str) or not user_id or user_id in persons:
            continue
        persons[user_id] = PersonMatch(entity_id=entity_id, name=_person_name(attributes))
    return persons


def find_person_by_user_id(states: Iterable[Any] | None, user_id: str | None) -> PersonMatch | None:
    """Return the first ``person.*`` state whose ``attributes.user_id`` equals ``user_id``."""
    if not user_id:
        return None
    return build_person_map(states).get(user_id)


def person_identity_changed(old_state: dict[str, Any] | None, new_state: dict[str, Any] | None) -> bool:
    """True when a person state change can alter the user-id map (name, user link, add/remove)."""
    if old_state is None or new_state is None:
        return True
    old_attrs = old_state.get("attributes") or {}
    new_attrs = new_state.get("attributes") or {}
    return old_attrs.get("user_id") != new_attrs.get("user_id") or old_attrs.get("friendly_name") != new_attrs.get(
        "friendly_name"
    )


class PersonDirectory:
    """TTL cache of the HA user id -> person map, bound to one HA client.

    Freshness rules:

    * Within the TTL the cached map is served without a fetch.
    * After the TTL expires the previous map is served at once while one
      background refresh runs (stale-while-revalidate).
    * :meth:`invalidate` (a person was added, removed, renamed or relinked;
      WebSocket reconnect) drops the map: the pre-invalidation map is never
      served again, and the next lookup waits (bounded by ``timeout``) for
      a fresh fetch.
    * A failed fetch serves only a map that was never invalidated (or
      nothing) and is retried no sooner than ``failure_retry_seconds``.

    Refreshes are single-flight per invalidation generation, tracked via
    :func:`app.util.tasks.spawn` and shielded from the waiting turn: a
    lookup that times out returns None while the fetch keeps running and
    fills the cache for later turns. Lookups never raise for HA failures.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        failure_retry_seconds: float = FAILURE_RETRY_SECONDS,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._failure_retry_seconds = failure_retry_seconds
        self._client: Any = None
        # Last map that may be served (None = nothing servable).
        self._persons: dict[str, PersonMatch] | None = None
        self._fresh_until = 0.0
        self._retry_after = 0.0
        self._generation = 0
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_generation = -1
        self._refresh_tasks: set[asyncio.Task[None]] = set()

    def invalidate(self) -> None:
        """Drop the cached map; the next lookup waits for a fresh fetch."""
        self._persons = None
        self._fresh_until = 0.0
        self._retry_after = 0.0
        self._generation += 1

    def reset(self) -> None:
        """Drop all cached state (tests, HA client swap).

        In-flight refreshes keep running to completion (their results are
        discarded); :meth:`aclose` cancels them.
        """
        self.invalidate()
        self._client = None
        self._refresh_task = None
        self._refresh_generation = -1

    def _bind(self, ha_client: Any) -> None:
        if ha_client is not self._client:
            self.reset()
            self._client = ha_client

    def _serve(self, user_id: str) -> PersonMatch | None:
        persons = self._persons
        return persons.get(user_id) if persons is not None else None

    async def lookup(self, ha_client: Any, user_id: str | None, *, timeout: float | None = None) -> PersonMatch | None:
        """Return the person linked to ``user_id`` (None when unknown or HA fails).

        ``timeout`` bounds the wait for a fetch when no servable map exists;
        on expiry None is returned and the fetch continues in the background.
        """
        if not user_id or ha_client is None:
            return None
        self._bind(ha_client)
        now = time.monotonic()
        if self._persons is not None and now < self._fresh_until:
            return self._serve(user_id)
        if now < self._retry_after:
            # Failure backoff: serve what is still valid, no new fetch.
            return self._serve(user_id)

        refresh = self._ensure_refresh(ha_client)
        if self._persons is not None:
            # Stale-while-revalidate: answer now, refresh in the background.
            return self._serve(user_id)
        try:
            if timeout is None:
                await asyncio.shield(refresh)
            else:
                await asyncio.wait_for(asyncio.shield(refresh), timeout)
        except TimeoutError:
            logger.debug("Person lookup timed out after %.2fs; continuing without a name", timeout)
        return self._serve(user_id)

    def lookup_cached(self, ha_client: Any, user_id: str | None) -> PersonMatch | None:
        """Non-blocking :meth:`lookup`: answer only from the servable map, never wait.

        When the map is stale or missing (and no failure backoff is active)
        one background refresh is started so later turns find it warm. Must
        be called from the event loop.
        """
        if not user_id or ha_client is None:
            return None
        self._bind(ha_client)
        now = time.monotonic()
        fresh = self._persons is not None and now < self._fresh_until
        if not fresh and now >= self._retry_after:
            self._ensure_refresh(ha_client)
        return self._serve(user_id)

    def _ensure_refresh(self, ha_client: Any) -> asyncio.Task[None]:
        task = self._refresh_task
        if task is None or task.done() or self._refresh_generation != self._generation:
            task = spawn(self._refresh(ha_client, self._generation), name="ha-person-directory-refresh")
            self._refresh_task = task
            self._refresh_generation = self._generation
            self._refresh_tasks.add(task)
            task.add_done_callback(self._refresh_tasks.discard)
        return task

    async def _refresh(self, ha_client: Any, generation: int) -> None:
        persons: dict[str, PersonMatch] | None = None
        try:
            persons = build_person_map(await ha_client.get_states())
        except Exception:
            logger.warning("Failed to fetch person entities from Home Assistant", exc_info=True)
        if ha_client is not self._client or generation != self._generation:
            # Rebound to another client or invalidated meanwhile: the result
            # may predate the change, so it is never served.
            return
        now = time.monotonic()
        if persons is None:
            self._retry_after = now + self._failure_retry_seconds
            return
        self._persons = persons
        self._fresh_until = now + self._ttl_seconds
        self._retry_after = 0.0

    async def aclose(self) -> None:
        """Cancel in-flight refreshes (application shutdown, tests)."""
        tasks = [task for task in self._refresh_tasks if not task.done()]
        self._refresh_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


_person_directory = PersonDirectory()


def get_person_directory() -> PersonDirectory:
    """Process-wide person directory."""
    return _person_directory
