"""Phase 2 (P1): fire-and-forget analytics helpers.

The ``track_*_background`` wrappers in ``app.analytics.collector`` schedule
the awaited trackers as tracked tasks so hot request paths never await a
SQLite insert. Failures must be logged and swallowed -- never raised into
the request path.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.analytics import collector
from app.analytics.collector import track_cache_event_background, track_request_background


@pytest.mark.asyncio
async def test_track_request_background_schedules_insert():
    """The helper returns immediately and the insert runs as a task."""
    with patch.object(collector.AnalyticsRepository, "insert", new_callable=AsyncMock) as mock_insert:
        result = track_request_background("light-agent", cache_hit=False, latency_ms=12.34)
        assert result is None  # fire-and-forget: nothing to await
        await asyncio.sleep(0.01)
    mock_insert.assert_awaited_once()
    kwargs = mock_insert.await_args.kwargs
    assert kwargs["event_type"] == "request"
    assert kwargs["agent_id"] == "light-agent"
    assert kwargs["data"]["cache_hit"] is False
    assert kwargs["data"]["latency_ms"] == 12.3


@pytest.mark.asyncio
async def test_track_cache_event_background_schedules_insert():
    with patch.object(collector.AnalyticsRepository, "insert", new_callable=AsyncMock) as mock_insert:
        track_cache_event_background(tier="routing", hit_type="routing_hit", agent_id="light-agent", similarity=0.97)
        await asyncio.sleep(0.01)
    mock_insert.assert_awaited_once()
    kwargs = mock_insert.await_args.kwargs
    assert kwargs["event_type"] == "routing_hit"
    assert kwargs["agent_id"] == "light-agent"
    assert kwargs["data"]["tier"] == "routing"
    assert kwargs["data"]["similarity"] == 0.97


@pytest.mark.asyncio
async def test_background_track_does_not_delay_caller():
    """The caller continues while the insert is still blocked in-flight."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_insert(**_kwargs):
        started.set()
        await release.wait()

    with patch.object(collector.AnalyticsRepository, "insert", side_effect=_blocking_insert):
        # Must return even though the insert has not completed (and never
        # will until we release it below).
        track_cache_event_background(tier="routing", hit_type="miss")
        await asyncio.wait_for(started.wait(), timeout=1.0)
        # Caller reached here while the insert is still in-flight.
        release.set()
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_background_track_swallows_db_errors(caplog):
    """A failing insert is logged and never raised into the request path."""
    with (
        patch.object(
            collector.AnalyticsRepository,
            "insert",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
        caplog.at_level(logging.WARNING, logger="app.analytics.collector"),
    ):
        track_request_background("light-agent", cache_hit=True, latency_ms=1.0)  # must not raise
        await asyncio.sleep(0.01)
    assert "Failed to track request event" in caplog.text
