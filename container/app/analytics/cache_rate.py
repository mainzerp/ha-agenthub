"""Cache hit-rate semantics shared by the analytics and dashboard APIs.

The cache hit rate is a PER-REQUEST rate: requests served from the cache
divided by tracked requests (``request`` analytics events) in the same
window. Raw cache events cannot be the denominator: one live turn emits a
``miss`` per tier it consulted (action tier, routing tier, plus the combined
``both_miss``), so a per-event ratio under-reports the hit rate and does not
match the request count shown next to it.

Numerator: one hit event per served lookup (``action_hit``,
``routing_hit``, ``semantic_hit``; each lookup emits at most one). The rate
is capped at 100 %.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

CACHE_HIT_EVENT_TYPES: frozenset[str] = frozenset({"routing_hit", "semantic_hit", "action_hit"})


def count_cache_hits(events: Iterable[dict[str, Any]]) -> int:
    """Number of cache-hit analytics events in ``events``."""
    return sum(1 for event in events if event.get("event_type") in CACHE_HIT_EVENT_TYPES)


def per_request_hit_rate(hits: int, requests: int) -> float:
    """Cache hit rate in percent (one decimal): ``hits / requests``, capped at 100."""
    if requests <= 0:
        return 0.0
    return round(min(hits, requests) / requests * 100, 1)
