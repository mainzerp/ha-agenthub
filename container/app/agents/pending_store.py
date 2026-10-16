"""In-memory, TTL-bounded store for per-conversation pending decisions.

Shared by the automation-change confirmation and the sensitive-action
approval flows. Entries are keyed by ``conversation_id``. An entry is
``active`` until its TTL; after that it becomes a tombstone (the payload is
replaced by ``tombstone(value)``, ``None`` by default, so secrets such as
alarm codes do not linger) so a late answer gets an honest "that expired"
instead of being treated as a new request. Tombstones are dropped after the
tombstone TTL; the store holds at most ``max_entries`` conversations.

State is in memory only: a container restart drops every pending entry,
which is the safe direction.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Literal


class _Tombstone:
    """Marks an expired entry; ``value`` is the tombstone payload (may be ``None``)."""

    __slots__ = ("value",)

    def __init__(self, value: object | None) -> None:
        self.value = value


StoreStatus = Literal["active", "expired", "none"]

DEFAULT_TOMBSTONE_TTL_SECONDS = 3600.0
DEFAULT_MAX_ENTRIES = 256


class PendingStore[T]:
    """Pending-entry map keyed by conversation_id (TTL + tombstone + size bound)."""

    def __init__(
        self,
        ttl_seconds: float,
        *,
        tombstone_ttl_seconds: float = DEFAULT_TOMBSTONE_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        tombstone: Callable[[T], object | None] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._tombstone_ttl = tombstone_ttl_seconds
        self._max_entries = max_entries
        self._tombstone = tombstone
        # (stored_at, payload); an expired payload is wrapped in ``_Tombstone``
        # (a plain ``None`` payload also counts as expired).
        self._entries: OrderedDict[str, tuple[float, object | None]] = OrderedDict()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def put(self, conversation_id: str, value: T) -> None:
        self._purge()
        self._entries[conversation_id] = (time.monotonic(), value)
        self._entries.move_to_end(conversation_id)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def get_entry(self, conversation_id: str | None) -> tuple[StoreStatus, object | None]:
        """Return ``(status, payload)``; an ``expired`` payload is the tombstone value."""
        if not conversation_id:
            return "none", None
        entry = self._entries.get(conversation_id)
        if entry is None:
            return "none", None
        stored_at, payload = entry
        age = time.monotonic() - stored_at
        active = _is_active(payload)
        if active and age <= self._ttl:
            return "active", payload
        if age <= self._tombstone_ttl:
            tombstone = payload if isinstance(payload, _Tombstone) else self._make_tombstone(payload)
            self._entries[conversation_id] = (stored_at, tombstone)
            return "expired", tombstone.value
        self._entries.pop(conversation_id, None)
        return "none", None

    def pop(self, conversation_id: str | None) -> T | None:
        """Remove the entry; returns the payload only when it was still active."""
        if not conversation_id:
            return None
        entry = self._entries.pop(conversation_id, None)
        if entry is None:
            return None
        stored_at, payload = entry
        if _is_active(payload) and time.monotonic() - stored_at <= self._ttl:
            return payload  # type: ignore[return-value]
        return None

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def _make_tombstone(self, payload: object | None) -> _Tombstone:
        if self._tombstone is None or payload is None:
            return _Tombstone(None)
        return _Tombstone(self._tombstone(payload))  # type: ignore[arg-type]

    def _purge(self) -> None:
        now = time.monotonic()
        for key in list(self._entries):
            stored_at, payload = self._entries[key]
            age = now - stored_at
            if age > self._tombstone_ttl:
                self._entries.pop(key, None)
            elif _is_active(payload) and age > self._ttl:
                self._entries[key] = (stored_at, self._make_tombstone(payload))


def _is_active(payload: object | None) -> bool:
    return payload is not None and not isinstance(payload, _Tombstone)
