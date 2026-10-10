"""Admin/API loose ends of issue #132: home-context cache, streamed dispatch closing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.a2a.dispatcher import Dispatcher
from app.a2a.protocol import JsonRpcRequest
from app.ha_client.home_context import HomeContextProvider
from app.models.agent import IngressTask


@pytest.mark.asyncio
async def test_home_context_invalidate_forces_refetch():
    provider = HomeContextProvider()
    ha_client = MagicMock()
    ha_client.get_config = AsyncMock(return_value={"time_zone": "Europe/Berlin", "location_name": "Home"})
    with patch.object(provider, "_load_overrides", new=AsyncMock(return_value=None)):
        await provider.get(ha_client)
        await provider.get(ha_client)
        assert ha_client.get_config.await_count == 1

        provider.invalidate()
        ctx = await provider.get(ha_client)

    assert ha_client.get_config.await_count == 2
    assert ctx.timezone == "Europe/Berlin"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keys", "expected"),
    [(("home.timezone",), True), (("home.location_name", "language"), True), (("language",), False)],
)
async def test_settings_write_invalidates_home_context(keys, expected):
    from app.api.routes.admin import _settings

    with patch("app.ha_client.home_context.home_context_provider") as provider:
        _settings._invalidate_home_context_if_changed(keys)

    assert provider.invalidate.called is expected


@pytest.mark.asyncio
async def test_update_single_setting_invalidates_home_context():
    from app.api.routes.admin import _settings

    existing = {"value_type": "str", "category": "home", "description": None}
    with (
        patch.object(_settings.SettingsRepository, "get", new=AsyncMock(return_value=existing)),
        patch.object(_settings.SettingsRepository, "set", new=AsyncMock()),
        patch.object(_settings, "_validate_setting_value"),
        patch("app.ha_client.home_context.home_context_provider") as provider,
    ):
        result = await _settings.update_single_setting("home.timezone", {"value": "Europe/Berlin"})

    assert result == {"status": "ok", "key": "home.timezone"}
    provider.invalidate.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_stream_closes_inner_stream_on_early_exit():
    """The inner transport stream is closed when the consumer stops early."""
    closed = {"value": False}

    async def _inner(agent_id, task, request_id):
        try:
            yield {"token": "a", "done": False}
            yield {"token": "b", "done": False}
            yield {"token": "", "done": True}
        finally:
            closed["value"] = True

    transport = MagicMock()
    transport.stream = _inner
    dispatcher = Dispatcher(registry=MagicMock(), transport=transport)
    request = JsonRpcRequest(
        method="message/stream",
        params={"agent_id": "orchestrator", "task": IngressTask(description="hi", conversation_id="c1")},
        id="r1",
    )

    stream = dispatcher.dispatch_stream(request)
    first = await anext(stream)
    await stream.aclose()

    assert first["token"] == "a"
    assert closed["value"] is True
