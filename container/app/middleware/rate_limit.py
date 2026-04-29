"""Lightweight in-memory rate limiter for FastAPI dependencies."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import HTTPException, Request

from app.config import settings

logger = logging.getLogger(__name__)

# In-memory store: key -> (count, window_start)
# Not distributed; sufficient for single-container deployments.
_rate_limit_store: dict[str, tuple[int, float]] = {}
_store_lock = asyncio.Lock()

DEFAULT_WINDOW_SECONDS = 60


# Parse once at module load; entries are IP strings or networks.
_TRUSTED_PROXIES: set[str] = set()
if settings.trusted_proxies:
    _TRUSTED_PROXIES = {p.strip() for p in settings.trusted_proxies.split(",") if p.strip()}


def _make_key(identifier: str, scope: str) -> str:
    return f"{scope}:{identifier}"


def reset_rate_limit_store() -> None:
    """Clear all rate limit counters. Useful for tests."""
    _rate_limit_store.clear()


async def _check_rate_limit(identifier: str, max_requests: int, window_seconds: float, scope: str) -> None:
    """Increment counter for identifier and raise HTTPException 429 if exceeded."""
    if not identifier:
        return
    key = _make_key(identifier, scope)
    now = time.time()
    async with _store_lock:
        count, window_start = _rate_limit_store.get(key, (0, now))
        if now - window_start > window_seconds:
            count = 0
            window_start = now
        count += 1
        _rate_limit_store[key] = (count, window_start)
        if count > max_requests:
            logger.warning("Rate limit exceeded for %s", key)
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please try again later.",
            )


def _get_client_ip(request: Request) -> str:
    direct = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and direct in _TRUSTED_PROXIES:
        return forwarded.split(",")[0].strip()
    return direct


async def rate_limit_conversation(request: Request) -> None:
    """30 requests per minute per IP for /api/conversation REST endpoints."""
    ip = _get_client_ip(request)
    await _check_rate_limit(ip, max_requests=30, window_seconds=60, scope="conversation")


async def rate_limit_login(request: Request) -> None:
    """5 requests per 15 minutes per IP for /dashboard/login."""
    ip = _get_client_ip(request)
    await _check_rate_limit(ip, max_requests=5, window_seconds=900, scope="login")


async def rate_limit_admin(request: Request) -> None:
    """60 requests per minute per IP for /api/admin/* endpoints."""
    ip = _get_client_ip(request)
    await _check_rate_limit(ip, max_requests=60, window_seconds=60, scope="admin")


class WsMessageRateLimiter:
    """Simple token bucket for per-connection WebSocket message rate limiting.

    Defaults to 10 messages/sec with a burst capacity of 20.
    """

    def __init__(self, rate: float = 10.0, burst: int = 20) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Return True if a message is allowed, False if rate limit exceeded."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_update
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last_update = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False
