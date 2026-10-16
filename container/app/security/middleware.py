"""HTTP security middleware: fetch-metadata CSRF guard and security headers.

Both middlewares are pure ASGI (not ``BaseHTTPMiddleware``) so they never
buffer streaming responses (SSE) and pass WebSocket scopes through untouched.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.requests import cookie_parser

from app.security.auth import API_KEY_HEADER, SESSION_COOKIE_NAME

logger = logging.getLogger(__name__)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
CSRF_BLOCKED_DETAIL = "Cross-site request blocked"
DEFAULT_FRAME_ANCESTORS = "'self'"

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    """Return the first value of header *name* (lower-case bytes) or None."""
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _normalize_origin(value: str) -> str | None:
    """Normalize an origin string to ``scheme://host[:port]`` (default port dropped)."""
    parts = urlsplit(value.strip())
    scheme = (parts.scheme or "").lower()
    if not scheme or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _normalize_host(value: str, scheme: str) -> str:
    """Lower-case a ``host[:port]`` value and drop the port when it is the scheme default."""
    host = value.strip().lower()
    default_port = _DEFAULT_PORTS.get(scheme)
    if default_port and host.endswith(f":{default_port}"):
        host = host[: -(len(default_port) + 1)]
    return host


def _origin_matches_request(origin: str, headers: list[tuple[bytes, bytes]]) -> bool:
    """Return True when *origin* names the same ``host[:port]`` the request was sent to."""
    if origin.strip().lower() == "null":
        return False
    parts = urlsplit(origin.strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS or not parts.netloc:
        return False
    origin_host = _normalize_host(parts.netloc, scheme)

    candidates: list[str] = []
    host = _header(headers, b"host")
    if host:
        candidates.append(host)
    forwarded_host = _header(headers, b"x-forwarded-host")
    if forwarded_host:
        # A proxy chain may append values; the first one is the client-facing host.
        candidates.append(forwarded_host.split(",")[0])
    return any(_normalize_host(candidate, scheme) == origin_host for candidate in candidates if candidate.strip())


def parse_trusted_origins(raw: str | Iterable[str]) -> frozenset[str]:
    """Build the explicit trusted-origin set from a comma-separated string or iterable.

    The wildcard ``*`` is never treated as trusted for CSRF purposes.
    """
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    trusted: set[str] = set()
    for item in items:
        item = item.strip()
        if not item or item == "*":
            continue
        normalized = _normalize_origin(item)
        if normalized:
            trusted.add(normalized)
    return frozenset(trusted)


class FetchMetadataCSRFMiddleware:
    """Reject cross-site, cookie-authenticated, state-changing HTTP requests.

    Scope: unsafe methods (POST, PUT, PATCH, DELETE) that carry the dashboard
    session cookie and no ``Authorization`` header. Bearer/API-key clients are
    not CSRF-prone (browsers never attach that header cross-site on their own)
    and are passed through.

    Decision:

    * ``Sec-Fetch-Site`` present: only ``same-origin`` and ``none`` pass.
    * ``Sec-Fetch-Site`` absent, ``Origin`` present: the Origin's
      ``host[:port]`` must equal the ``Host`` (or ``X-Forwarded-Host``) header.
    * Both absent (non-browser client such as curl): pass.

    An Origin listed explicitly in ``trusted_origins`` (the operator's
    ``CORS_ORIGINS`` allowlist, wildcard excluded) also passes, because the
    operator already granted that origin credentialed CORS access.

    The existing double-submit ``verify_csrf`` check on HTML form POSTs is
    independent of this middleware and still applies.
    """

    def __init__(self, app, trusted_origins: Iterable[str] = ()) -> None:
        self.app = app
        self.trusted_origins = parse_trusted_origins(trusted_origins)

    def _is_blocked(self, headers: list[tuple[bytes, bytes]]) -> bool:
        if _header(headers, API_KEY_HEADER.lower().encode("latin-1")) is not None:
            return False
        cookie_header = _header(headers, b"cookie")
        if not cookie_header or SESSION_COOKIE_NAME not in cookie_parser(cookie_header):
            return False

        origin = _header(headers, b"origin")
        if origin and self.trusted_origins:
            normalized = _normalize_origin(origin)
            if normalized and normalized in self.trusted_origins:
                return False

        fetch_site = _header(headers, b"sec-fetch-site")
        if fetch_site is not None:
            return fetch_site.strip().lower() not in ALLOWED_FETCH_SITES
        if origin is not None:
            return not _origin_matches_request(origin, headers)
        return False

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        if not self._is_blocked(headers):
            await self.app(scope, receive, send)
            return

        logger.warning(
            "CSRF guard blocked %s %s (sec-fetch-site=%s, origin=%s)",
            scope.get("method"),
            scope.get("path"),
            _header(headers, b"sec-fetch-site"),
            _header(headers, b"origin"),
        )
        body = json.dumps({"detail": CSRF_BLOCKED_DETAIL}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_frame_ancestors(value: str | None) -> str:
    """Return a safe ``frame-ancestors`` source list, falling back to ``'self'``.

    Rejects values that could inject further CSP directives or header lines.
    """
    candidate = " ".join((value or "").split())
    if not candidate:
        return DEFAULT_FRAME_ANCESTORS
    if any(ch in candidate for ch in ";,") or any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in candidate):
        logger.warning("Invalid FRAME_ANCESTORS value %r; falling back to %s", value, DEFAULT_FRAME_ANCESTORS)
        return DEFAULT_FRAME_ANCESTORS
    return candidate


class SecurityHeadersMiddleware:
    """Add baseline security headers to every HTTP response.

    Headers already set by a route are left untouched. WebSocket scopes are
    passed through.
    """

    def __init__(self, app, frame_ancestors: str = DEFAULT_FRAME_ANCESTORS) -> None:
        self.app = app
        self.default_headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"same-origin"),
            (
                b"content-security-policy",
                f"frame-ancestors {build_frame_ancestors(frame_ancestors)}".encode("latin-1"),
            ),
        ]

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {key.lower() for key, _ in headers}
                for name, value in self.default_headers:
                    if name not in present:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
