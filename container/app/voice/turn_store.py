"""In-memory per-conversation store for speaker-identification results.

A voice-turn session registers its ``conversation_id`` when audio starts,
so a conversation request arriving while the embedding is still being
computed can wait (bounded) for the result. Entries are single-use, expire
after :data:`ENTRY_TTL_S` and are capped at :data:`MAX_ENTRIES`. A newer
session for the same ``conversation_id`` (multi-turn follow-up) replaces
the older entry. The container runs a single process, so process memory is
the right scope.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

ENTRY_TTL_S = 120.0
MAX_ENTRIES = 256
RECENT_RESULTS = 20


@dataclass
class _Entry:
    token: int
    created_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class SpeakerLookup:
    """Outcome of :meth:`SpeakerTurnStore.pop`."""

    found: bool
    result: dict[str, Any] | None
    waited_ms: float
    timed_out: bool = False


class SpeakerTurnStore:
    """Single-use, TTL-bounded speaker results keyed by conversation id."""

    def __init__(
        self,
        *,
        ttl_s: float = ENTRY_TTL_S,
        max_entries: int = MAX_ENTRIES,
        recent_size: int = RECENT_RESULTS,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._tokens = itertools.count(1)
        self._recent: deque[dict[str, Any]] = deque(maxlen=recent_size)

    # -- session side --------------------------------------------------

    def begin(self, conversation_id: str) -> int:
        """Register a pending entry and return its session token.

        Replaces (and releases waiters of) an older entry for the same id.
        """
        self._purge_expired()
        old = self._entries.pop(conversation_id, None)
        if old is not None:
            old.event.set()
        while len(self._entries) >= self._max_entries:
            _, evicted = self._entries.popitem(last=False)
            evicted.event.set()
        token = next(self._tokens)
        self._entries[conversation_id] = _Entry(token=token, created_at=time.monotonic())
        return token

    def resolve(self, conversation_id: str, token: int, result: dict[str, Any]) -> bool:
        """Attach ``result`` to the entry created by ``token``.

        Returns False when the entry was replaced, consumed or expired.
        """
        entry = self._entries.get(conversation_id)
        if entry is None or entry.token != token:
            return False
        entry.result = dict(result)
        entry.event.set()
        return True

    def discard(self, conversation_id: str, token: int | None = None) -> None:
        """Remove the entry (only the one created by ``token`` when given) and release waiters."""
        entry = self._entries.get(conversation_id)
        if entry is None or (token is not None and entry.token != token):
            return
        self._entries.pop(conversation_id, None)
        entry.event.set()

    # -- conversation side ---------------------------------------------

    def has(self, conversation_id: str | None) -> bool:
        if not conversation_id:
            return False
        self._purge_expired()
        return conversation_id in self._entries

    async def pop(self, conversation_id: str, wait_s: float) -> SpeakerLookup:
        """Consume the entry, waiting up to ``wait_s`` while it is still pending.

        The entry is removed in every outcome (result, timeout, cancellation)
        so a late result never attaches to a later turn.
        """
        self._purge_expired()
        entry = self._entries.get(conversation_id)
        if entry is None:
            return SpeakerLookup(found=False, result=None, waited_ms=0.0)
        t0 = time.perf_counter()
        timed_out = False
        try:
            if not entry.event.is_set() and wait_s > 0:
                try:
                    await asyncio.wait_for(entry.event.wait(), timeout=wait_s)
                except TimeoutError:
                    timed_out = True
            elif not entry.event.is_set():
                timed_out = True
        finally:
            if self._entries.get(conversation_id) is entry:
                self._entries.pop(conversation_id, None)
        waited_ms = round((time.perf_counter() - t0) * 1000, 2)
        return SpeakerLookup(found=True, result=entry.result, waited_ms=waited_ms, timed_out=timed_out)

    # -- calibration ring buffer ---------------------------------------

    def record_recent(self, result: dict[str, Any]) -> None:
        """Remember a finished result for the dashboard (no audio, no embeddings)."""
        self._recent.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "status": result.get("status"),
                "profile_id": result.get("profile_id"),
                "name": result.get("name"),
                "score": result.get("score"),
                "margin": result.get("margin"),
                "duration_s": result.get("duration_s"),
            }
        )

    def recent(self) -> list[dict[str, Any]]:
        """Most recent results first."""
        return list(reversed(self._recent))

    # -- housekeeping ---------------------------------------------------

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [cid for cid, entry in self._entries.items() if now - entry.created_at > self._ttl_s]
        for cid in expired:
            entry = self._entries.pop(cid)
            entry.event.set()

    def __len__(self) -> int:
        return len(self._entries)

    def reset(self) -> None:
        for entry in self._entries.values():
            with contextlib.suppress(Exception):
                entry.event.set()
        self._entries.clear()
        self._recent.clear()
