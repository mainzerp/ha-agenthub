"""Analytics admin API endpoints.

Returns data in Chart.js-compatible format for dashboard charts.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query

from app.db.repository import AnalyticsRepository, ConversationRepository
from app.security.auth import require_admin_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/analytics",
    tags=["admin-analytics"],
    dependencies=[Depends(require_admin_session)],
)


def _compute_percentiles(values: list[float], percentiles: list[int]) -> dict[str, float]:
    """Compute percentiles from a list of values. Returns dict like {"p50": 12.3}."""
    if not values:
        return {f"p{p}": 0.0 for p in percentiles}
    values_sorted = sorted(values)
    n = len(values_sorted)
    result = {}
    for p in percentiles:
        idx = int(n * p / 100)
        idx = min(idx, n - 1)
        result[f"p{p}"] = round(values_sorted[idx], 2)
    return result


@router.get("/overview")
async def analytics_overview(
    hours: int = Query(24, ge=1, le=720),
):
    """Summary metrics for the analytics dashboard."""
    start = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    requests = await AnalyticsRepository.query_by_range(
        event_type="request",
        start=start,
        limit=10000,
    )
    cache_events = await AnalyticsRepository.query_by_range(
        event_type=None,
        start=start,
        limit=10000,
    )

    total_requests = len(requests)
    latencies = [
        r["data"]["latency_ms"]
        for r in requests
        if r.get("data") and isinstance(r["data"], dict) and "latency_ms" in r["data"]
    ]
    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0
    latency_percentiles = _compute_percentiles(latencies, [50, 95, 99])

    # Cache hit rate
    hits = sum(1 for e in cache_events if e.get("event_type") in ("routing_hit", "action_hit"))
    misses = sum(1 for e in cache_events if e.get("event_type") == "miss")
    total_cache = hits + misses
    hit_rate = round(hits / total_cache * 100, 1) if total_cache > 0 else 0

    # Total conversations
    total_conversations = await ConversationRepository.count()

    return {
        "total_requests": total_requests,
        "avg_latency_ms": avg_latency,
        "cache_hit_rate": hit_rate,
        "total_conversations": total_conversations,
        "period_hours": hours,
        **latency_percentiles,
    }


@router.get("/requests")
async def analytics_requests(
    hours: int = Query(24, ge=1, le=720),
    bucket_minutes: int = Query(60, ge=5, le=1440),
):
    """Time-series request counts in Chart.js format."""
    start = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    events = await AnalyticsRepository.query_by_range(
        event_type="request",
        start=start,
        limit=10000,
    )

    # Bucket by time interval
    buckets: dict[str, int] = defaultdict(int)
    for e in events:
        ts = e.get("created_at", "")
        try:
            dt = datetime.fromisoformat(ts)
            # Truncate to bucket
            bucket_secs = bucket_minutes * 60
            ts_epoch = int(dt.timestamp())
            bucket_start = ts_epoch - (ts_epoch % bucket_secs)
            bucket_label = datetime.fromtimestamp(bucket_start, tz=UTC).strftime("%H:%M")
            buckets[bucket_label] += 1
        except (ValueError, TypeError):
            logger.debug("Failed to parse event timestamp %s", ts, exc_info=True)

    labels = sorted(buckets.keys())
    data = [buckets[lb] for lb in labels]

    return {
        "labels": labels,
        "datasets": [{"label": "Requests", "data": data}],
    }


@router.get("/agents")
async def analytics_agents(
    hours: int = Query(24, ge=1, le=720),
):
    """Per-agent metrics with p50/p95/p99 latencies."""
    start = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    events = await AnalyticsRepository.query_by_range(
        event_type="request",
        start=start,
        limit=10000,
    )

    agent_latencies: dict[str, list[float]] = defaultdict(list)
    agent_counts: dict[str, int] = defaultdict(int)

    for e in events:
        agent = e.get("agent_id") or "unknown"
        agent_counts[agent] += 1
        data = e.get("data")
        if isinstance(data, dict) and "latency_ms" in data:
            agent_latencies[agent].append(data["latency_ms"])

    agents = []
    for agent_id in sorted(agent_counts.keys()):
        latencies = agent_latencies.get(agent_id, [])
        percentiles = _compute_percentiles(latencies, [50, 95, 99])
        agents.append(
            {
                "agent_id": agent_id,
                "request_count": agent_counts[agent_id],
                "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
                **percentiles,
            }
        )

    return {"agents": agents, "period_hours": hours}


@router.get("/cache")
async def analytics_cache(
    hours: int = Query(24, ge=1, le=720),
    bucket_minutes: int = Query(60, ge=5, le=1440),
):
    """Cache hit rate time-series in Chart.js format."""
    start = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    events = await AnalyticsRepository.query_by_range(start=start, limit=10000)

    hit_types = {"routing_hit", "action_hit"}
    miss_types = {"miss"}

    hits_per_bucket: dict[str, int] = defaultdict(int)
    total_per_bucket: dict[str, int] = defaultdict(int)

    for e in events:
        et = e.get("event_type", "")
        if et not in hit_types and et not in miss_types:
            continue
        ts = e.get("created_at", "")
        try:
            dt = datetime.fromisoformat(ts)
            bucket_secs = bucket_minutes * 60
            ts_epoch = int(dt.timestamp())
            bucket_start = ts_epoch - (ts_epoch % bucket_secs)
            bucket_label = datetime.fromtimestamp(bucket_start, tz=UTC).strftime("%H:%M")
            total_per_bucket[bucket_label] += 1
            if et in hit_types:
                hits_per_bucket[bucket_label] += 1
        except (ValueError, TypeError):
            logger.debug("Failed to parse cache event timestamp %s", ts, exc_info=True)

    labels = sorted(total_per_bucket.keys())
    data = [
        round(hits_per_bucket.get(lb, 0) / total_per_bucket[lb] * 100, 1) if total_per_bucket[lb] > 0 else 0
        for lb in labels
    ]

    return {
        "labels": labels,
        "datasets": [{"label": "Cache Hit Rate (%)", "data": data}],
    }


@router.get("/tokens")
async def analytics_tokens(
    hours: int = Query(24, ge=1, le=720),
):
    """Token usage per agent/provider."""
    start = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    events = await AnalyticsRepository.query_by_range(
        event_type="token_usage",
        start=start,
        limit=10000,
    )

    by_agent: dict[str, dict] = defaultdict(
        lambda: {"tokens_in": 0, "tokens_out": 0, "calls": 0, "ttft_ms_values": [], "tps_values": []}
    )
    by_provider: dict[str, dict] = defaultdict(
        lambda: {"tokens_in": 0, "tokens_out": 0, "calls": 0, "ttft_ms_values": [], "tps_values": []}
    )

    for e in events:
        data = e.get("data")
        if not isinstance(data, dict):
            continue
        agent = e.get("agent_id") or "unknown"
        provider = data.get("provider", "unknown")
        tokens_in = data.get("tokens_in", 0)
        tokens_out = data.get("tokens_out", 0)

        by_agent[agent]["tokens_in"] += tokens_in
        by_agent[agent]["tokens_out"] += tokens_out
        by_agent[agent]["calls"] += 1

        by_provider[provider]["tokens_in"] += tokens_in
        by_provider[provider]["tokens_out"] += tokens_out
        by_provider[provider]["calls"] += 1

        ttft = data.get("ttft_ms")
        tps_val = data.get("tps")
        if ttft is not None:
            by_agent[agent]["ttft_ms_values"].append(ttft)
            by_provider[provider]["ttft_ms_values"].append(ttft)
        if tps_val is not None:
            by_agent[agent]["tps_values"].append(tps_val)
            by_provider[provider]["tps_values"].append(tps_val)

    for bucket in (by_agent, by_provider):
        for key in bucket:
            ttft_vals = bucket[key].pop("ttft_ms_values", [])
            tps_vals = bucket[key].pop("tps_values", [])
            bucket[key]["avg_ttft_ms"] = round(sum(ttft_vals) / len(ttft_vals), 2) if ttft_vals else None
            bucket[key]["avg_tps"] = round(sum(tps_vals) / len(tps_vals), 2) if tps_vals else None

    return {
        "by_agent": dict(by_agent),
        "by_provider": dict(by_provider),
        "period_hours": hours,
    }


@router.get("/errors")
async def analytics_errors(
    hours: int = Query(24, ge=1, le=720),
):
    """Error counts per agent and per error type."""
    start = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    events = await AnalyticsRepository.query_by_range(
        event_type="error",
        start=start,
        limit=10000,
    )

    by_agent: dict[str, int] = defaultdict(int)
    by_error_type: dict[str, int] = defaultdict(int)

    for e in events:
        agent = e.get("agent_id") or "unknown"
        by_agent[agent] += 1
        data = e.get("data")
        if isinstance(data, dict):
            error_type = data.get("error_type", "unknown")
            by_error_type[error_type] += 1

    return {
        "labels": sorted(by_error_type.keys()),
        "datasets": [
            {"label": "Errors by Type", "data": [by_error_type[k] for k in sorted(by_error_type.keys())]},
        ],
        "by_agent": dict(by_agent),
        "period_hours": hours,
    }


@router.get("/cache/tiers")
async def analytics_cache_tiers(
    hours: int = Query(24, ge=1, le=720),
    bucket_minutes: int = Query(60, ge=5, le=1440),
):
    """Cache tier time-series with separate datasets per tier."""
    start = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    events = await AnalyticsRepository.query_by_range(start=start, limit=10000)

    routing_hits_per_bucket: dict[str, int] = defaultdict(int)
    action_hits_per_bucket: dict[str, int] = defaultdict(int)
    misses_per_bucket: dict[str, int] = defaultdict(int)

    for e in events:
        et = e.get("event_type", "")
        ts = e.get("created_at", "")
        try:
            dt = datetime.fromisoformat(ts)
            bucket_secs = bucket_minutes * 60
            ts_epoch = int(dt.timestamp())
            bucket_start = ts_epoch - (ts_epoch % bucket_secs)
            bucket_label = datetime.fromtimestamp(bucket_start, tz=UTC).strftime("%H:%M")
            if et == "routing_hit":
                routing_hits_per_bucket[bucket_label] += 1
            elif et == "action_hit":
                action_hits_per_bucket[bucket_label] += 1
            elif et == "miss":
                misses_per_bucket[bucket_label] += 1
        except (ValueError, TypeError):
            logger.debug("Failed to parse cache event timestamp %s", ts, exc_info=True)

    labels = sorted(set(routing_hits_per_bucket) | set(action_hits_per_bucket) | set(misses_per_bucket))

    return {
        "labels": labels,
        "datasets": [
            {"label": "Routing Hits", "data": [routing_hits_per_bucket.get(lb, 0) for lb in labels]},
            {"label": "Action Hits", "data": [action_hits_per_bucket.get(lb, 0) for lb in labels]},
            {"label": "Misses", "data": [misses_per_bucket.get(lb, 0) for lb in labels]},
        ],
    }


@router.get("/rewrite")
async def analytics_rewrite(
    hours: int = Query(24, ge=1, le=720),
):
    """Rewrite invocation stats."""
    start = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    events = await AnalyticsRepository.query_by_range(
        event_type="rewrite_invocation",
        start=start,
        limit=10000,
    )

    total = len(events)
    successes = 0
    failures = 0
    latencies = []

    for e in events:
        data = e.get("data")
        if not isinstance(data, dict):
            continue
        if data.get("success"):
            successes += 1
        else:
            failures += 1
        if "latency_ms" in data:
            latencies.append(data["latency_ms"])

    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0

    return {
        "total": total,
        "successes": successes,
        "failures": failures,
        "avg_latency_ms": avg_latency,
        "period_hours": hours,
    }
