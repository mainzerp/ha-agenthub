"""Tests for app.api.routes.cache_api."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from tests.conftest import build_integration_test_app


def _build_app(**kwargs):
    return build_integration_test_app(
        setup_complete=True,
        override_api_key=True,
        override_admin_session=True,
        **kwargs,
    )


async def _client_for(app):
    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=True,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.mark.asyncio
class TestBrowseFlushValidate:
    async def test_browse_flush_and_validate_cache(self, db_repository):
        """browse, flush, and validate endpoints return expected shapes."""
        app = _build_app()

        cache_manager = MagicMock()
        cache_manager._routing_cache = MagicMock()
        cache_manager._action_cache = MagicMock()
        cache_manager._cache_store = MagicMock()
        cache_manager.flush = MagicMock()

        # browse: non-search path
        cache_manager._cache_store.count = MagicMock(return_value=1)
        cache_manager._cache_store.get = MagicMock(
            return_value={
                "ids": ["id1"],
                "metadatas": [{"agent_id": "light-agent"}],
                "documents": ["turn on kitchen light"],
            }
        )
        app.state.cache_manager = cache_manager

        validator = MagicMock()
        validator.run_once = AsyncMock(return_value={"scanned": 5, "invalid": 0})
        app.state.cache_validator = validator

        with patch(
            "app.api.routes.cache_api.ensure_setup_runtime_initialized",
            new_callable=AsyncMock,
        ):
            async for client in _client_for(app):
                # Browse
                resp = await client.get("/api/admin/cache/entries?tier=routing")
                assert resp.status_code == 200
                data = resp.json()
                assert data["entries"][0]["id"] == "id1"

                # Flush
                resp = await client.post("/api/admin/cache/flush", json={"tier": "routing"})
                assert resp.status_code == 200
                assert resp.json()["flushed"] == "routing"

                # Validate
                resp = await client.post("/api/admin/cache/validate")
                assert resp.status_code == 200
                assert resp.json()["scanned"] == 5


@pytest.mark.asyncio
class TestExportCache:
    async def test_export_cache(self, db_repository):
        """export returns a StreamingResponse with the correct headers."""
        app = _build_app()

        cache_manager = MagicMock()

        def _chunk_gen():
            yield b'{"version": "1"}'

        with (
            patch(
                "app.api.routes.cache_api.iter_export_chunks",
                return_value=_chunk_gen(),
            ),
            patch(
                "app.api.routes.cache_api.build_export_filename",
                return_value="cache_export.json",
            ),
            patch(
                "app.api.routes.cache_api.ensure_setup_runtime_initialized",
                new_callable=AsyncMock,
            ),
        ):
            app.state.cache_manager = cache_manager
            async for client in _client_for(app):
                resp = await client.get("/api/admin/cache/export?tier=all")

        assert resp.status_code == 200
        assert resp.headers["content-disposition"] == 'attachment; filename="cache_export.json"'
