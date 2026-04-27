"""Shared thread-safe state wrapper for cache tiers (P1-3).

Both :class:`RoutingCache` and :class:`ResponseCache` need the same
bookkeeping primitives:

* an ``_store_count`` that advances on every write and decides when
  ``_enforce_lru`` is allowed to run,
* a ``_hit_since_flush`` counter plus a ``_pending_updates`` dict that
  batches metadata updates (hit count / last_accessed) so the read
  path does not issue one Chroma write per hit,
* an ``_invalidation_generation`` counter so a worker thread that was
  preparing a write can notice that an admin flush ran while the call
  was in flight and abort before re-populating the collection.

Pre-P1-3 each cache class hand-rolled this with a bare ``threading.Lock``
and some fields were mutated without the lock being held. The tier code
also lost pending updates when the Chroma flush raised (the dict was
swapped out before the Chroma call). :class:`_CacheState` centralises
all of that:

* every read/write of the counters and the pending map happens under
  the lock,
* :meth:`swap_pending` returns the currently-queued updates and clears
  the internal dict,
* :meth:`requeue_failed` puts entries back without clobbering newer
  updates that arrived in the meantime.

ChromaDB I/O itself must stay *outside* the lock; callers obtain the
pending batch via ``swap_pending``, run the Chroma update, and on
failure call ``requeue_failed``.
"""

from __future__ import annotations

import threading


class _CacheState:
    """Thread-safe counter + pending-updates holder for a single cache tier."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store_count: int = 0
        self._hit_since_flush: int = 0
        self._pending_updates: dict[str, tuple[str, dict]] = {}
        self._invalidation_generation: int = 0

    # --- store-path helpers -------------------------------------------------

    def record_store(self, eviction_interval: int) -> bool:
        """Increment the store counter; return True iff LRU should run now."""
        with self._lock:
            self._store_count += 1
            if self._store_count >= eviction_interval:
                self._store_count = 0
                return True
            return False

    def current_generation(self) -> int:
        with self._lock:
            return self._invalidation_generation

    def matches_generation(self, generation: int) -> bool:
        with self._lock:
            return generation == self._invalidation_generation

    # --- hit-path helpers ---------------------------------------------------

    def record_pending_update(
        self,
        entry_id: str,
        document: str,
        metadata: dict,
        flush_interval: int,
    ) -> bool:
        """Queue a metadata update; return True iff caller should flush now."""
        with self._lock:
            self._pending_updates[entry_id] = (document, metadata)
            self._hit_since_flush += 1
            return self._hit_since_flush >= flush_interval

    def swap_pending(self) -> dict[str, tuple[str, dict]]:
        """Atomically take ownership of the queued updates."""
        with self._lock:
            pending = self._pending_updates
            self._pending_updates = {}
            self._hit_since_flush = 0
            return pending

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending_updates)

    def requeue_failed(self, pending: dict[str, tuple[str, dict]]) -> None:
        """Restore updates that failed to flush without clobbering newer writes."""
        if not pending:
            return
        with self._lock:
            for entry_id, value in pending.items():
                # A newer hit may have superseded this entry while the
                # flush was running; keep the newer one.
                self._pending_updates.setdefault(entry_id, value)
            # The caller already counted these hits once; do not double
            # count by bumping ``_hit_since_flush`` again. The swap that
            # preceded this reset it to zero.

    # --- invalidation -------------------------------------------------------

    def invalidate(self) -> None:
        """Admin flush in progress: drop pending and bump generation."""
        with self._lock:
            self._invalidation_generation += 1
            self._pending_updates.clear()
            self._hit_since_flush = 0

    def discard_pending(self, entry_id: str) -> None:
        """Drop a queued metadata update for one entry, if present."""
        with self._lock:
            removed = self._pending_updates.pop(entry_id, None)
            if removed is not None and self._hit_since_flush > 0:
                self._hit_since_flush -= 1

    # --- introspection used by tests ---------------------------------------

    def snapshot_pending(self) -> dict[str, tuple[str, dict]]:
        with self._lock:
            return dict(self._pending_updates)

    def hit_count(self) -> int:
        with self._lock:
            return self._hit_since_flush
