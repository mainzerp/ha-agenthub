"""Fire-and-forget analytics event tracking.

All functions catch exceptions internally and log warnings.
They never raise, so callers can safely await without error handling.
"""

from __future__ import annotations

import logging

from app.db.repository import AnalyticsRepository
from app.util.tasks import spawn

logger = logging.getLogger(__name__)


def track_request_background(agent_id: str, cache_hit: bool, latency_ms: float) -> None:
    """Schedule :func:`track_request` without blocking the caller.

    For hot request paths where awaiting the SQLite insert would couple
    analytics DB health to request latency (P1 fire-and-forget analytics).
    The underlying tracker logs and swallows insert failures; ``spawn``
    holds a strong reference to the task until completion and logs
    unexpected errors, so tasks neither leak nor vanish silently.
    Accepted trade-off: telemetry events can be lost on crash.
    """
    spawn(track_request(agent_id, cache_hit, latency_ms), name="analytics-track-request")


def track_cache_event_background(
    tier: str,
    hit_type: str,
    agent_id: str | None = None,
    similarity: float | None = None,
) -> None:
    """Schedule :func:`track_cache_event` without blocking the caller.

    See :func:`track_request_background` for the rationale and safety
    properties.
    """
    spawn(
        track_cache_event(tier, hit_type, agent_id=agent_id, similarity=similarity),
        name="analytics-track-cache-event",
    )


async def track_request(agent_id: str, cache_hit: bool, latency_ms: float) -> None:
    """Track a completed request dispatch."""
    try:
        await AnalyticsRepository.insert(
            event_type="request",
            agent_id=agent_id,
            data={"cache_hit": cache_hit, "latency_ms": round(latency_ms, 1)},
        )
    except Exception:
        logger.warning("Failed to track request event", exc_info=True)


async def track_cache_event(
    tier: str,
    hit_type: str,
    agent_id: str | None = None,
    similarity: float | None = None,
) -> None:
    """Track a cache hit or miss."""
    try:
        await AnalyticsRepository.insert(
            event_type=hit_type,
            agent_id=agent_id,
            data={"tier": tier, "similarity": similarity},
        )
    except Exception:
        logger.warning("Failed to track cache event", exc_info=True)


async def track_rewrite(latency_ms: float, success: bool) -> None:
    """Track a rewrite invocation."""
    try:
        await AnalyticsRepository.insert(
            event_type="rewrite_invocation",
            data={"latency_ms": round(latency_ms, 1), "success": success},
        )
    except Exception:
        logger.warning("Failed to track rewrite event", exc_info=True)


async def track_token_usage(
    agent_id: str,
    provider: str,
    tokens_in: int,
    tokens_out: int,
    ttft_ms: float | None = None,
    tps: float | None = None,
    latency_ms: float | None = None,
) -> None:
    """Track LLM token usage.

    ``latency_ms`` is the whole-call latency (recorded on every path).
    ``ttft_ms``/``tps`` are only populated on streaming paths, where
    first-chunk timing actually exists -- on non-streaming calls the
    whole-call value used to be mislabeled as ``ttft_ms``.
    """
    try:
        data = {"provider": provider, "tokens_in": tokens_in, "tokens_out": tokens_out}
        if latency_ms is not None:
            data["latency_ms"] = round(latency_ms, 2)
        if ttft_ms is not None:
            data["ttft_ms"] = round(ttft_ms, 2)
        if tps is not None:
            data["tps"] = round(tps, 2)
        await AnalyticsRepository.insert(
            event_type="token_usage",
            agent_id=agent_id,
            data=data,
        )
    except Exception:
        logger.warning("Failed to track token usage event", exc_info=True)


async def track_agent_timeout(agent_id: str, timeout_s: int) -> None:
    """Track an agent timeout occurrence."""
    try:
        await AnalyticsRepository.insert(
            event_type="agent_timeout",
            agent_id=agent_id,
            data={"timeout_s": timeout_s},
        )
    except Exception:
        logger.warning("Failed to track agent timeout event", exc_info=True)


async def track_error(agent_id: str, error_type: str, endpoint: str | None = None) -> None:
    """Track an error occurrence."""
    try:
        await AnalyticsRepository.insert(
            event_type="error",
            agent_id=agent_id,
            data={"error_type": error_type, "endpoint": endpoint},
        )
    except Exception:
        logger.warning("Failed to track error event", exc_info=True)
