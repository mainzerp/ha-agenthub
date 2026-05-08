"""CSRF protection regression tests (SEC-1).

Covers ``/dashboard/login`` and ``/setup/step/*`` form POSTs:
- GET render sets the ``agent_assist_csrf`` cookie and embeds a token in the
  HTML form.
- POST without cookie+token is rejected with 401.
- POST with mismatched token is rejected with 401.
- POST with matching cookie+token is accepted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from tests.conftest import build_integration_test_app


def _build_app(*, setup_complete: bool = False):
    return build_integration_test_app(setup_complete=setup_complete)


@pytest_asyncio.fixture()
async def setup_client(db_repository):
    app = _build_app(setup_complete=False)
    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=False,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            yield client


@pytest_asyncio.fixture()
async def dashboard_client(db_repository):
    app = _build_app(setup_complete=True)
    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=True,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            yield client


@pytest.mark.integration
class TestCsrfSetupForm:
    async def test_get_sets_cookie_and_embeds_token(self, setup_client):
        resp = await setup_client.get("/setup/step/1")
        assert resp.status_code == 200
        token = setup_client.cookies.get("agent_assist_csrf")
        assert token, "agent_assist_csrf cookie missing"
        assert f'value="{token}"' in resp.text
        assert 'name="csrf_token"' in resp.text

    async def test_post_without_cookie_or_token_rejected(self, setup_client):
        resp = await setup_client.post(
            "/setup/step/1",
            data={"username": "admin", "password": "pw"},
        )
        assert resp.status_code == 401

    async def test_post_with_mismatched_token_rejected(self, setup_client):
        await setup_client.get("/setup/step/1")
        cookie_token = setup_client.cookies.get("agent_assist_csrf")
        assert cookie_token
        resp = await setup_client.post(
            "/setup/step/1",
            data={
                "username": "admin",
                "password": "pw",
                "csrf_token": cookie_token + "tampered",
            },
        )
        assert resp.status_code == 401

    async def test_post_with_matching_token_accepted(self, setup_client):
        await setup_client.get("/setup/step/1")
        cookie_token = setup_client.cookies.get("agent_assist_csrf")
        assert cookie_token
        with (
            patch(
                "app.setup.routes.hash_password",
                return_value="hashed-password",
            ),
            patch(
                "app.setup.routes.AdminAccountRepository.create",
                new_callable=AsyncMock,
            ),
            patch(
                "app.setup.routes.SetupStateRepository.set_step_completed",
                new_callable=AsyncMock,
            ),
        ):
            resp = await setup_client.post(
                "/setup/step/1",
                data={
                    "username": "admin",
                    "password": "password123",
                    "csrf_token": cookie_token,
                },
            )
            assert resp.status_code == 303


@pytest.mark.integration
class TestCsrfDashboardLogin:
    async def test_login_get_sets_cookie(self, dashboard_client):
        resp = await dashboard_client.get("/dashboard/login")
        assert resp.status_code == 200
        assert dashboard_client.cookies.get("agent_assist_csrf")
        assert 'name="csrf_token"' in resp.text

    async def test_login_post_without_cookie_rejected(self, dashboard_client):
        resp = await dashboard_client.post(
            "/dashboard/login",
            data={"username": "admin", "password": "pw"},
        )
        assert resp.status_code == 401

    async def test_login_post_with_wrong_token_rejected(self, dashboard_client):
        await dashboard_client.get("/dashboard/login")
        token = dashboard_client.cookies.get("agent_assist_csrf")
        assert token
        resp = await dashboard_client.post(
            "/dashboard/login",
            data={
                "username": "admin",
                "password": "pw",
                "csrf_token": token + "x",
            },
        )
        assert resp.status_code == 401

    async def test_login_post_with_matching_token_proceeds(self, dashboard_client):
        await dashboard_client.get("/dashboard/login")
        token = dashboard_client.cookies.get("agent_assist_csrf")
        assert token
        with patch(
            "app.dashboard.routes.authenticate_admin",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = await dashboard_client.post(
                "/dashboard/login",
                data={
                    "username": "admin",
                    "password": "wrong",
                    "csrf_token": token,
                },
            )
            # Reaches the handler (renders error page) instead of CSRF 401.
            assert resp.status_code == 200
            assert "Invalid credentials" in resp.text


class TestCsrfTokenRotation:
    def test_ensure_csrf_token_returns_existing_when_session_present(self):
        """CRIT-2: calling ensure_csrf_token twice with the same request must return the same token."""
        from unittest.mock import MagicMock

        from app.security.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, ensure_csrf_token

        token = "existing-token-value"
        request = MagicMock()
        request.cookies = {
            CSRF_COOKIE_NAME: token,
            SESSION_COOKIE_NAME: "session-value",
        }

        first = ensure_csrf_token(request)
        second = ensure_csrf_token(request)
        assert first == token
        assert second == token
        assert first == second
