"""Tests for app.middleware -- auth/setup redirect and tracing middleware."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.auth import SetupRedirectMiddleware, apply_auth_dependencies
from app.middleware.tracing import TracingMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_app(middleware_cls, setup_complete: bool = True):
    """Build a minimal Starlette app with the given middleware."""

    async def homepage(request: Request) -> PlainTextResponse:
        trace_id = getattr(request.state, "trace_id", None)
        return PlainTextResponse(f"ok:{trace_id}")

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("healthy")

    async def setup_page(request: Request) -> PlainTextResponse:
        return PlainTextResponse("setup")

    app = Starlette(
        routes=[
            Route("/", homepage),
            Route("/api/health", health),
            Route("/setup/", setup_page),
            Route("/dashboard/settings", homepage),
        ],
    )

    if middleware_cls in (SetupRedirectMiddleware, TracingMiddleware):
        app.add_middleware(middleware_cls)

    return app


# ---------------------------------------------------------------------------
# SetupRedirectMiddleware
# ---------------------------------------------------------------------------


class TestSetupRedirectMiddleware:
    @patch("app.middleware.auth.SetupStateRepository")
    def test_redirects_when_setup_incomplete(self, mock_repo):
        mock_repo.is_complete = AsyncMock(return_value=False)
        app = _make_test_app(SetupRedirectMiddleware, setup_complete=False)
        client = TestClient(app, follow_redirects=False)

        response = client.get("/dashboard/settings")
        assert response.status_code == 302
        assert "/setup/" in response.headers.get("location", "")

    @patch("app.middleware.auth.SetupStateRepository")
    def test_allows_setup_route_when_incomplete(self, mock_repo):
        mock_repo.is_complete = AsyncMock(return_value=False)
        app = _make_test_app(SetupRedirectMiddleware, setup_complete=False)
        client = TestClient(app)

        response = client.get("/setup/")
        assert response.status_code == 200
        assert response.text == "setup"

    @patch("app.middleware.auth.SetupStateRepository")
    def test_allows_health_when_incomplete(self, mock_repo):
        mock_repo.is_complete = AsyncMock(return_value=False)
        app = _make_test_app(SetupRedirectMiddleware, setup_complete=False)
        client = TestClient(app)

        response = client.get("/api/health")
        assert response.status_code == 200

    @patch("app.middleware.auth.SetupStateRepository")
    def test_passes_through_when_setup_complete(self, mock_repo):
        mock_repo.is_complete = AsyncMock(return_value=True)
        app = _make_test_app(SetupRedirectMiddleware, setup_complete=True)
        client = TestClient(app)

        response = client.get("/")
        assert response.status_code == 200
        assert "ok" in response.text

    @patch("app.middleware.auth.SetupStateRepository")
    def test_caches_completion_state(self, mock_repo):
        mock_repo.is_complete = AsyncMock(return_value=True)
        app = _make_test_app(SetupRedirectMiddleware)
        client = TestClient(app)

        # First request checks DB
        client.get("/")
        # Second request should use cache
        client.get("/")
        # is_complete called exactly once (then cached since True)
        assert mock_repo.is_complete.await_count == 1


# ---------------------------------------------------------------------------
# TracingMiddleware
# ---------------------------------------------------------------------------


class TestTracingMiddleware:
    @patch("app.middleware.tracing.SpanCollector")
    def test_assigns_trace_id_header(self, mock_collector_cls):
        mock_collector_cls.return_value = MagicMock(
            _spans=[],
            flush=AsyncMock(),
        )
        app = _make_test_app(TracingMiddleware)
        client = TestClient(app)

        response = client.get("/")
        assert "X-Trace-Id" in response.headers
        trace_id = response.headers["X-Trace-Id"]
        assert len(trace_id) == 16

    @patch("app.middleware.tracing.SpanCollector")
    def test_trace_id_unique_per_request(self, mock_collector_cls):
        mock_collector_cls.return_value = MagicMock(
            _spans=[],
            flush=AsyncMock(),
        )
        app = _make_test_app(TracingMiddleware)
        client = TestClient(app)

        r1 = client.get("/")
        r2 = client.get("/")
        assert r1.headers["X-Trace-Id"] != r2.headers["X-Trace-Id"]

    @patch("app.middleware.tracing.SpanCollector")
    def test_span_collector_flushed(self, mock_collector_cls):
        collector = MagicMock()
        collector._spans = []
        collector.flush = AsyncMock()
        mock_collector_cls.return_value = collector

        app = _make_test_app(TracingMiddleware)
        client = TestClient(app)
        client.get("/")
        collector.flush.assert_awaited_once()

    @patch("app.middleware.tracing.SpanCollector")
    def test_request_gets_200(self, mock_collector_cls):
        mock_collector_cls.return_value = MagicMock(
            _spans=[],
            flush=AsyncMock(),
        )
        app = _make_test_app(TracingMiddleware)
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# apply_auth_dependencies
# ---------------------------------------------------------------------------


class TestRateLimitClientIp:
    def test_get_client_ip_uses_direct_when_no_forwarded(self):
        from app.middleware.rate_limit import _get_client_ip

        request = MagicMock()
        request.client.host = "192.168.1.1"
        request.headers = {}
        assert _get_client_ip(request) == "192.168.1.1"

    def test_get_client_ip_ignores_forwarded_when_direct_not_trusted(self):
        from app.middleware.rate_limit import _get_client_ip

        request = MagicMock()
        request.client.host = "192.168.1.1"
        request.headers = {"x-forwarded-for": "10.0.0.1, 10.0.0.2"}
        assert _get_client_ip(request) == "192.168.1.1"

    @patch("app.middleware.rate_limit._TRUSTED_PROXIES", {"10.0.0.1", "10.0.0.2"})
    def test_get_client_ip_walks_rightmost_non_trusted(self):
        from app.middleware.rate_limit import _get_client_ip

        request = MagicMock()
        request.client.host = "10.0.0.2"
        request.headers = {"x-forwarded-for": "spoofed, 192.168.1.1, 10.0.0.1"}
        assert _get_client_ip(request) == "192.168.1.1"

    @patch("app.middleware.rate_limit._TRUSTED_PROXIES", {"10.0.0.1", "10.0.0.2"})
    def test_get_client_ip_skips_all_trusted_proxies(self):
        from app.middleware.rate_limit import _get_client_ip

        request = MagicMock()
        request.client.host = "10.0.0.2"
        request.headers = {"x-forwarded-for": "10.0.0.1"}
        assert _get_client_ip(request) == "10.0.0.1"


class TestApplyAuthDependencies:
    def test_registers_exception_handlers(self):
        from fastapi import FastAPI, HTTPException
        from starlette.testclient import TestClient

        app = FastAPI()
        apply_auth_dependencies(app)
        # HTTPException and generic Exception should be registered
        assert len(app.exception_handlers) >= 2

        @app.get("/raise-http")
        async def raise_http():
            raise HTTPException(status_code=418)

        @app.get("/raise-value")
        async def raise_value():
            raise ValueError("oops")

        client = TestClient(app, raise_server_exceptions=False)
        r1 = client.get("/raise-http")
        assert r1.status_code == 418
        r2 = client.get("/raise-value")
        assert r2.status_code == 500
