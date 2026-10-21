"""Recently used agents: in-memory recency tracker plus the ``recent_agents`` query.

The tracker is fed from the tracing hook on every dispatch-type span start,
whether or not anybody listens to the activity stream, so the "recently used"
list stays correct for clients that connect later. It stores only agent ids and
UTC timestamps (never span metadata or user text).

``recent_agents`` returns registered, user-facing agents most recent first. When
the in-memory tracker (empty after a restart) cannot fill the requested limit it
is topped up from persisted dispatch spans, then from request usage counts.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from app.models.agent import CANCEL_INTERACTION_AGENT, INTERNAL_ONLY_AGENTS, NOISE_AGENT

logger = logging.getLogger(__name__)

# Span names whose ``agent_id`` is the agent that executes the work.
DISPATCH_SPAN_NAMES: frozenset[str] = frozenset({"dispatch", "dispatch_content", "dispatch_send", "dispatch_fallback"})

# Pseudo ids that are never a user-facing agent (pipeline internals and
# pipeline-level directives); never tracked and never returned.
EXCLUDED_AGENT_IDS: frozenset[str] = INTERNAL_ONLY_AGENTS | {NOISE_AGENT, CANCEL_INTERACTION_AGENT}

MAX_TRACKED_AGENTS = 64
# Look-back window of the database top-up queries.
DB_LOOKBACK_DAYS = 30
MAX_RECENT_LIMIT = 20

# agent_id -> last used ISO-8601 UTC timestamp, least recent first.
_recent: OrderedDict[str, str] = OrderedDict()
# Spans can be started from other threads (``asyncio.run`` in a worker), so the
# tiny critical sections are guarded.
_lock = threading.Lock()


def record_agent_use(span_name: Any, agent_id: Any, timestamp: Any = None) -> None:
    """Remember that ``agent_id`` was just dispatched to. Never raises.

    Only dispatch-type spans with a string agent id count; internal pseudo ids
    are ignored. ``timestamp`` defaults to now (UTC).
    """
    try:
        if span_name not in DISPATCH_SPAN_NAMES:
            return
        if not isinstance(agent_id, str) or not agent_id or agent_id in EXCLUDED_AGENT_IDS:
            return
        ts = timestamp if isinstance(timestamp, str) and timestamp else datetime.now(UTC).isoformat()
        with _lock:
            _recent[agent_id] = ts
            _recent.move_to_end(agent_id)
            while len(_recent) > MAX_TRACKED_AGENTS:
                _recent.popitem(last=False)
    except Exception:
        logger.debug("Failed to record agent use", exc_info=True)


def tracked_agents() -> list[tuple[str, str]]:
    """Return ``(agent_id, last_used)`` pairs from memory, most recent first."""
    with _lock:
        return list(reversed(_recent.items()))


def reset_recency() -> None:
    """Forget every tracked agent (tests)."""
    with _lock:
        _recent.clear()


def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, MAX_RECENT_LIMIT))


async def _registered_cards(registry: Any) -> dict[str, Any]:
    if registry is None:
        from app.a2a.registry import registry as default_registry

        registry = default_registry
    cards = await registry.list_agents()
    return {card.agent_id: card for card in cards if card.agent_id not in EXCLUDED_AGENT_IDS}


async def recent_agents(limit: int = 7, registry: Any = None) -> list[dict[str, Any]]:
    """Return the most recently used registered agents, most recent first.

    Each entry is ``{"agent_id", "name", "last_used"}`` where ``name`` is the
    A2A agent card name and ``last_used`` an ISO-8601 UTC string or ``None``
    (usage-count fallback has no timestamp). Sources, in order: the in-memory
    tracker, persisted dispatch spans of the last 30 days, request usage
    counts. Agents that are not registered (disabled, removed) and internal
    pseudo ids are never returned. Database failures only shorten the result.
    """
    wanted = _clamp_limit(limit)
    if wanted == 0:
        return []
    cards = await _registered_cards(registry)
    if not cards:
        return []

    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(agent_id: Any, last_used: str | None) -> None:
        if len(result) >= wanted or not isinstance(agent_id, str) or agent_id in seen:
            return
        card = cards.get(agent_id)
        if card is None:
            return
        seen.add(agent_id)
        result.append({"agent_id": agent_id, "name": card.name, "last_used": last_used})

    for agent_id, last_used in tracked_agents():
        _add(agent_id, last_used)

    if len(result) < wanted:
        try:
            from app.db.repository import TraceSpanRepository

            rows = await TraceSpanRepository.recent_dispatch_agents(
                span_names=sorted(DISPATCH_SPAN_NAMES),
                excluded_ids=sorted(EXCLUDED_AGENT_IDS),
                days=DB_LOOKBACK_DAYS,
                limit=MAX_TRACKED_AGENTS,
            )
            for row in rows:
                _add(row.get("agent_id"), row.get("last_used"))
        except Exception:
            logger.debug("Recent agents: span top-up failed", exc_info=True)

    if len(result) < wanted:
        try:
            from app.db.repository import AnalyticsRepository

            rows = await AnalyticsRepository.agent_usage_counts(
                excluded_ids=sorted(EXCLUDED_AGENT_IDS),
                days=DB_LOOKBACK_DAYS,
                limit=MAX_TRACKED_AGENTS,
            )
            for row in rows:
                _add(row.get("agent_id"), None)
        except Exception:
            logger.debug("Recent agents: usage fallback failed", exc_info=True)

    return result
