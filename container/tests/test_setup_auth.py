"""Auth tests for setup wizard POST endpoints (CRIT-1).

Once the setup wizard has been completed, every POST under ``/setup/*``
must require an authenticated admin session. While setup is still in
progress those endpoints remain anonymous so the bootstrap flow can run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from tests.conftest import build_integration_test_app
from tests.helpers import csrf_post


def _build_app(*, setup_complete: bool = False):
    return build_integration_test_app(setup_complete=setup_complete)


@pytest_asyncio.fixture()
async def incomplete_client(db_repository):
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
async def complete_client(db_repository):
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
class TestSetupRoutesOpenWhileIncomplete:
    async def test_step1_open_during_setup(self, incomplete_client):
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
            resp = await csrf_post(
                incomplete_client,
                "/setup/step/1",
                {"username": "admin", "password": "password123"},
                get_url="/setup/step/1",
            )
            assert resp.status_code == 303


@pytest.mark.integration
class TestSetupRoutesLockedAfterCompletion:
    async def _post(self, client, url, data=None):
        return await client.post(url, data=data or {})

    async def test_step1_requires_session(self, complete_client):
        resp = await self._post(
            complete_client,
            "/setup/step/1",
            {"username": "admin", "password": "pw"},
        )
        assert resp.status_code == 401

    async def test_step2_requires_session(self, complete_client):
        resp = await self._post(
            complete_client,
            "/setup/step/2",
            {"ha_url": "http://x", "ha_token": "t"},
        )
        assert resp.status_code == 401

    async def test_step3_requires_session(self, complete_client):
        resp = await self._post(complete_client, "/setup/step/3")
        assert resp.status_code == 401

    async def test_step4_requires_session(self, complete_client):
        resp = await self._post(
            complete_client,
            "/setup/step/4",
            {
                "openrouter_key": "",
                "groq_key": "",
                "ollama_url": "",
                "custom_provider_name": "",
                "custom_provider_url": "",
                "custom_provider_key": "",
                "custom_provider_headers": "",
            },
        )
        assert resp.status_code == 401

    async def test_step5_requires_session(self, complete_client):
        resp = await self._post(complete_client, "/setup/step/5")
        assert resp.status_code == 401

    async def test_test_ha_requires_session(self, complete_client):
        resp = await self._post(
            complete_client,
            "/setup/test/ha",
            {"ha_url": "http://x", "ha_token": "t"},
        )
        assert resp.status_code == 401

    async def test_test_llm_requires_session(self, complete_client):
        resp = await self._post(
            complete_client,
            "/setup/test/llm",
            {"provider": "openrouter", "api_key": "k"},
        )
        assert resp.status_code == 401


@pytest.mark.integration
class TestSetupRoutesLockedWithValidCsrf:
    """SEC-7: even when a valid CSRF cookie+token is supplied, an admin
    session is still required once setup has been completed."""

    async def test_step1_with_csrf_still_requires_session(self, complete_client):
        resp = await csrf_post(
            complete_client,
            "/setup/step/1",
            {"username": "admin", "password": "pw"},
            get_url="/setup/step/1",
        )
        assert resp.status_code == 401

    async def test_test_llm_with_csrf_still_requires_session(self, complete_client):
        resp = await csrf_post(
            complete_client,
            "/setup/test/llm",
            {"provider": "openrouter", "api_key": "k"},
            get_url="/setup/step/4",
        )
        assert resp.status_code == 401
