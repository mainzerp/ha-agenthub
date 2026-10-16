"""Tests for app.security.middleware -- CSRF guard, security headers, API docs toggle."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from app.config import settings
from app.security.auth import SESSION_COOKIE_NAME
from app.security.middleware import (
    CSRF_BLOCKED_DETAIL,
    FetchMetadataCSRFMiddleware,
    SecurityHeadersMiddleware,
    build_frame_ancestors,
    parse_trusted_origins,
)

BASE_URL = "http://agenthub.local:8080"
SESSION = {"cookie": f"{SESSION_COOKIE_NAME}=signed-session-value"}


async def _ok(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def _custom_headers(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        "custom",
        headers={"Content-Security-Policy": "default-src 'none'", "Referrer-Policy": "no-referrer"},
    )


async def _stream(request: Request) -> StreamingResponse:
    async def gen():
        yield b"data: one\n\n"
        yield b"data: two\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _ws(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text("hello")
    await websocket.close()


def _make_app(*, trusted_origins=(), frame_ancestors: str = "'self'") -> Starlette:
    app = Starlette(
        routes=[
            Route("/api/admin/rotate", _ok, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
            Route("/custom", _custom_headers),
            Route("/stream", _stream),
            WebSocketRoute("/ws", _ws),
        ]
    )
    app.add_middleware(FetchMetadataCSRFMiddleware, trusted_origins=trusted_origins)
    app.add_middleware(SecurityHeadersMiddleware, frame_ancestors=frame_ancestors)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_make_app(), base_url=BASE_URL)


def _assert_blocked(response) -> None:
    assert response.status_code == 403
    assert response.json() == {"detail": CSRF_BLOCKED_DETAIL}


# ---------------------------------------------------------------------------
# FetchMetadataCSRFMiddleware
# ---------------------------------------------------------------------------


class TestFetchMetadataCSRF:
    def test_cross_site_blocked(self, client):
        response = client.post(
            "/api/admin/rotate",
            headers={**SESSION, "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        _assert_blocked(response)

    def test_same_site_cross_port_blocked(self, client):
        # Home Assistant on :8123 is "same-site" with the dashboard on :8080.
        response = client.post(
            "/api/admin/rotate",
            headers={**SESSION, "Sec-Fetch-Site": "same-site", "Origin": "http://agenthub.local:8123"},
        )
        _assert_blocked(response)

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
    def test_other_unsafe_methods_blocked(self, client, method):
        response = client.request(method, "/api/admin/rotate", headers={**SESSION, "Sec-Fetch-Site": "cross-site"})
        _assert_blocked(response)

    def test_same_origin_allowed(self, client):
        response = client.post(
            "/api/admin/rotate",
            headers={**SESSION, "Sec-Fetch-Site": "same-origin", "Origin": BASE_URL},
        )
        assert response.status_code == 200

    def test_user_initiated_none_allowed(self, client):
        response = client.post("/api/admin/rotate", headers={**SESSION, "Sec-Fetch-Site": "none"})
        assert response.status_code == 200

    def test_origin_mismatch_without_fetch_metadata_blocked(self, client):
        response = client.post("/api/admin/rotate", headers={**SESSION, "Origin": "http://agenthub.local:8123"})
        _assert_blocked(response)

    def test_null_origin_without_fetch_metadata_blocked(self, client):
        response = client.post("/api/admin/rotate", headers={**SESSION, "Origin": "null"})
        _assert_blocked(response)

    def test_matching_origin_without_fetch_metadata_allowed(self, client):
        response = client.post("/api/admin/rotate", headers={**SESSION, "Origin": BASE_URL})
        assert response.status_code == 200

    def test_default_port_origin_matches_host_without_port(self):
        client = TestClient(_make_app(), base_url="https://agenthub.example")
        response = client.post(
            "/api/admin/rotate",
            headers={**SESSION, "Origin": "https://agenthub.example:443", "Host": "agenthub.example"},
        )
        assert response.status_code == 200

    def test_forwarded_host_origin_allowed(self, client):
        response = client.post(
            "/api/admin/rotate",
            headers={
                **SESSION,
                "Origin": "https://agenthub.example.com",
                "X-Forwarded-Host": "agenthub.example.com, internal:8080",
            },
        )
        assert response.status_code == 200

    def test_no_fetch_metadata_and_no_origin_allowed(self, client):
        # Non-browser clients (curl with a cookie jar) send neither header.
        response = client.post("/api/admin/rotate", headers=SESSION)
        assert response.status_code == 200

    def test_bearer_request_allowed(self, client):
        response = client.post(
            "/api/admin/rotate",
            headers={**SESSION, "Authorization": "Bearer key", "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 200

    def test_get_allowed(self, client):
        response = client.get("/api/admin/rotate", headers={**SESSION, "Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 200

    def test_no_session_cookie_allowed(self, client):
        response = client.post(
            "/api/admin/rotate",
            headers={"cookie": "other=1", "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert response.status_code == 200

    def test_explicit_cors_origin_allowed(self):
        client = TestClient(_make_app(trusted_origins=["https://ha.example:8123"]), base_url=BASE_URL)
        response = client.post(
            "/api/admin/rotate",
            headers={**SESSION, "Sec-Fetch-Site": "cross-site", "Origin": "https://ha.example:8123"},
        )
        assert response.status_code == 200
        other = client.post(
            "/api/admin/rotate",
            headers={**SESSION, "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        _assert_blocked(other)

    def test_wildcard_cors_origin_not_trusted(self):
        assert parse_trusted_origins("*, https://HA.example:443 ,") == frozenset({"https://ha.example"})
        client = TestClient(_make_app(trusted_origins=["*"]), base_url=BASE_URL)
        response = client.post(
            "/api/admin/rotate",
            headers={**SESSION, "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        _assert_blocked(response)

    def test_websocket_untouched(self, client):
        with client.websocket_connect("/ws", headers={**SESSION, "Origin": "https://evil.example"}) as ws:
            assert ws.receive_text() == "hello"


# ---------------------------------------------------------------------------
# SecurityHeadersMiddleware
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    def test_default_headers_present(self, client):
        response = client.get("/api/admin/rotate")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["content-security-policy"] == "frame-ancestors 'self'"

    def test_headers_on_csrf_block(self, client):
        response = client.post("/api/admin/rotate", headers={**SESSION, "Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_route_headers_not_overwritten(self, client):
        response = client.get("/custom")
        assert response.headers["content-security-policy"] == "default-src 'none'"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_custom_frame_ancestors(self):
        client = TestClient(_make_app(frame_ancestors="'self' http://homeassistant.local:8123"), base_url=BASE_URL)
        response = client.get("/api/admin/rotate")
        assert response.headers["content-security-policy"] == "frame-ancestors 'self' http://homeassistant.local:8123"

    def test_streaming_response_passes(self, client):
        response = client.get("/stream")
        assert response.status_code == 200
        assert "data: two" in response.text
        assert response.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", "'self'"),
            ("  'self'   https://ha.example  ", "'self' https://ha.example"),
            ("'self'; script-src *", "'self'"),
            ("'none'", "'none'"),
        ],
    )
    def test_build_frame_ancestors(self, value, expected):
        assert build_frame_ancestors(value) == expected


# ---------------------------------------------------------------------------
# create_app wiring
# ---------------------------------------------------------------------------


class TestCreateAppWiring:
    def test_api_docs_disabled_by_default(self, monkeypatch):
        from app.main import create_app

        monkeypatch.setattr(settings, "enable_api_docs", False)
        app = create_app()
        assert app.docs_url is None
        assert app.redoc_url is None
        assert app.openapi_url is None
        paths = {getattr(route, "path", None) for route in app.routes}
        assert not {"/docs", "/redoc", "/openapi.json"} & paths

    def test_api_docs_enabled_via_setting(self, monkeypatch):
        from app.main import create_app

        monkeypatch.setattr(settings, "enable_api_docs", True)
        app = create_app()
        assert app.docs_url == "/docs"
        assert app.redoc_url == "/redoc"
        assert app.openapi_url == "/openapi.json"
        paths = {getattr(route, "path", None) for route in app.routes}
        assert {"/docs", "/redoc", "/openapi.json"} <= paths

    def test_security_middlewares_registered(self):
        from app.main import create_app

        app = create_app()
        classes = [m.cls for m in app.user_middleware]
        assert FetchMetadataCSRFMiddleware in classes
        # Security headers are the outermost user middleware.
        assert classes[0] is SecurityHeadersMiddleware

    def test_env_setting_defaults(self, monkeypatch):
        from app.config import Settings

        monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
        monkeypatch.delenv("FRAME_ANCESTORS", raising=False)
        defaults = Settings(_env_file=None)
        assert defaults.enable_api_docs is False
        assert defaults.frame_ancestors == "'self'"

    def test_env_settings_loaded(self, monkeypatch):
        from app.config import Settings

        monkeypatch.setenv("ENABLE_API_DOCS", "true")
        monkeypatch.setenv("FRAME_ANCESTORS", "'self' http://ha.local:8123")
        loaded = Settings(_env_file=None)
        assert loaded.enable_api_docs is True
        assert loaded.frame_ancestors == "'self' http://ha.local:8123"

    def test_real_app_blocks_cross_site_admin_post(self):
        from tests.conftest import build_integration_test_app

        app = build_integration_test_app(override_admin_session=True)
        with TestClient(app, base_url=BASE_URL) as client:
            response = client.post(
                "/api/admin/container-api-key/rotate",
                headers={**SESSION, "Sec-Fetch-Site": "same-site", "Origin": "http://agenthub.local:8123"},
            )
        _assert_blocked(response)
        assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
        assert response.headers["referrer-policy"] == "same-origin"
