"""Tests for the Lovelace card hosting (card.py): registration and the view."""

import hashlib
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The view tests drive a real aiohttp server; skip when only the stand-in exists.
web = pytest.importorskip("aiohttp.web")
test_utils = pytest.importorskip("aiohttp.test_utils")

from custom_components.tests._lazy import lazy

card = lazy("custom_components.ha_agenthub.card")
const = lazy("custom_components.ha_agenthub.const")


def _expected_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:12]


def _card_hass():
    hass = MagicMock()
    hass.data = {}
    hass.http.register_view = MagicMock()

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_executor)
    return hass


@pytest.fixture
def card_file(tmp_path):
    path = tmp_path / "agenthub-activity-orb-card.js"
    path.write_bytes(b"// card v1\n")
    with patch.object(card, "CARD_PATH", path):
        yield path


def _integration(version="3.13.0"):
    return patch.object(
        card,
        "async_get_integration",
        new=AsyncMock(return_value=SimpleNamespace(version=version)),
    )


class TestCardRegistration:
    @pytest.mark.asyncio
    async def test_extra_js_url_carries_the_content_hash(self, card_file):
        hass = _card_hass()
        with patch.object(card, "add_extra_js_url") as add_js, _integration():
            await card.async_register_card(hass)

        expected = f"{const.CARD_URL_PATH}?v={_expected_hash(b'// card v1\n')}"
        add_js.assert_called_once_with(hass, expected)
        assert len(expected.split("?v=")[1]) == 12

    @pytest.mark.asyncio
    async def test_hash_changes_with_the_content(self, card_file):
        urls = []
        for content in (b"// card v1\n", b"// card v2\n"):
            card_file.write_bytes(content)
            hass = _card_hass()
            with patch.object(card, "add_extra_js_url") as add_js, _integration():
                await card.async_register_card(hass)
            urls.append(add_js.call_args.args[1])
        assert urls[0] != urls[1]
        assert urls[1].endswith(_expected_hash(b"// card v2\n"))

    @pytest.mark.asyncio
    async def test_view_is_registered_with_the_card_path(self, card_file):
        hass = _card_hass()
        with patch.object(card, "add_extra_js_url"), _integration():
            await card.async_register_card(hass)

        hass.http.register_view.assert_called_once()
        (view,) = hass.http.register_view.call_args.args
        assert isinstance(view, card.AgentHubCardView)
        assert view.url == const.CARD_URL_PATH
        assert view.requires_auth is False
        assert view.cors_allowed is False
        assert view._path == card_file

    @pytest.mark.asyncio
    async def test_file_access_runs_in_the_executor(self, card_file):
        hass = _card_hass()
        with patch.object(card, "add_extra_js_url"), _integration():
            await card.async_register_card(hass)
        hass.async_add_executor_job.assert_awaited_once_with(
            card._inspect_card, card_file
        )

    @pytest.mark.asyncio
    async def test_unreadable_file_falls_back_to_the_manifest_version(self, card_file):
        hass = _card_hass()
        with (
            patch.object(card, "_inspect_card", return_value=(True, None)),
            patch.object(card, "add_extra_js_url") as add_js,
            _integration("3.13.0"),
        ):
            await card.async_register_card(hass)
        add_js.assert_called_once_with(hass, f"{const.CARD_URL_PATH}?v=3.13.0")
        hass.http.register_view.assert_called_once()

    def test_inspect_reports_unreadable_file(self, card_file):
        with patch.object(
            pathlib.Path, "read_bytes", side_effect=PermissionError("denied")
        ):
            assert card._inspect_card(card_file) == (True, None)

    def test_inspect_reports_missing_file(self, tmp_path):
        assert card._inspect_card(tmp_path / "nope.js") == (False, None)

    @pytest.mark.asyncio
    async def test_version_lookup_failure_falls_back_to_zero(self, card_file):
        hass = _card_hass()
        with (
            patch.object(card, "_inspect_card", return_value=(True, None)),
            patch.object(card, "add_extra_js_url") as add_js,
            patch.object(
                card,
                "async_get_integration",
                new=AsyncMock(side_effect=RuntimeError("no")),
            ),
        ):
            await card.async_register_card(hass)
        add_js.assert_called_once_with(hass, f"{const.CARD_URL_PATH}?v=0")

    @pytest.mark.asyncio
    async def test_registration_is_idempotent(self, card_file):
        hass = _card_hass()
        with patch.object(card, "add_extra_js_url") as add_js, _integration():
            await card.async_register_card(hass)
            await card.async_register_card(hass)
        hass.http.register_view.assert_called_once()
        add_js.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_file_skips_registration_and_warns(self, tmp_path, caplog):
        hass = _card_hass()
        with (
            patch.object(card, "CARD_PATH", tmp_path / "missing.js"),
            patch.object(card, "add_extra_js_url") as add_js,
        ):
            await card.async_register_card(hass)
        hass.http.register_view.assert_not_called()
        add_js.assert_not_called()
        assert "missing" in caplog.text

    @pytest.mark.asyncio
    async def test_extra_js_error_never_raises_and_view_is_not_repeated(
        self, card_file
    ):
        hass = _card_hass()
        with (
            patch.object(card, "add_extra_js_url", side_effect=RuntimeError("boom")),
            _integration(),
        ):
            await card.async_register_card(hass)
            await card.async_register_card(hass)
        hass.http.register_view.assert_called_once()

    @pytest.mark.asyncio
    async def test_view_registration_failure_never_raises(self, card_file):
        hass = _card_hass()
        hass.http.register_view = MagicMock(side_effect=RuntimeError("dup"))
        with patch.object(card, "add_extra_js_url") as add_js, _integration():
            await card.async_register_card(hass)
        add_js.assert_not_called()


# ---------------------------------------------------------------------------
# The view served by a real aiohttp server
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(card_file):
    view = card.AgentHubCardView(card_file)
    app = web.Application()
    # Same routes HomeAssistantView.register() creates for get and head.
    app.router.add_route("GET", view.url, view.get)
    app.router.add_route("HEAD", view.url, view.head)
    server = test_utils.TestServer(app)
    async with test_utils.TestClient(server) as test_client:
        yield test_client


class TestCardView:
    @pytest.mark.asyncio
    async def test_get_returns_the_file_with_revalidation_headers(
        self, client, card_file
    ):
        resp = await client.get(const.CARD_URL_PATH)
        assert resp.status == 200
        assert await resp.read() == b"// card v1\n"
        assert resp.headers["Cache-Control"] == "no-cache"
        assert resp.headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert resp.headers["ETag"]
        assert resp.headers["Last-Modified"]

    @pytest.mark.asyncio
    async def test_matching_if_none_match_returns_304(self, client):
        first = await client.get(const.CARD_URL_PATH)
        etag = first.headers["ETag"]

        resp = await client.get(const.CARD_URL_PATH, headers={"If-None-Match": etag})

        assert resp.status == 304
        assert await resp.read() == b""
        assert resp.headers["ETag"] == etag

    @pytest.mark.asyncio
    async def test_matching_if_modified_since_returns_304(self, client):
        first = await client.get(const.CARD_URL_PATH)
        resp = await client.get(
            const.CARD_URL_PATH,
            headers={"If-Modified-Since": first.headers["Last-Modified"]},
        )
        assert resp.status == 304

    @pytest.mark.asyncio
    async def test_changed_file_is_served_again_with_a_new_etag(
        self, client, card_file
    ):
        first = await client.get(const.CARD_URL_PATH)
        old_etag = first.headers["ETag"]
        card_file.write_bytes(b"// card v2, longer\n")

        resp = await client.get(
            const.CARD_URL_PATH, headers={"If-None-Match": old_etag}
        )

        assert resp.status == 200
        assert await resp.read() == b"// card v2, longer\n"
        assert resp.headers["ETag"] != old_etag

    @pytest.mark.asyncio
    async def test_head_returns_headers_without_body(self, client):
        resp = await client.head(const.CARD_URL_PATH)
        assert resp.status == 200
        assert await resp.read() == b""
        assert resp.headers["Cache-Control"] == "no-cache"
        assert resp.headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert resp.headers["Content-Length"] == str(len(b"// card v1\n"))

    @pytest.mark.asyncio
    async def test_query_string_does_not_change_the_response(self, client):
        resp = await client.get(f"{const.CARD_URL_PATH}?x=1")
        assert resp.status == 200
        assert resp.headers["Cache-Control"] == "no-cache"
