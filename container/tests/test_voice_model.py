"""Speaker-embedding model provisioning: path resolution and verified download (no real network)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock

import aiohttp
import pytest

from app.voice import model as voice_model
from app.voice.speaker_engine import SpeakerEngine

_PAYLOAD = b"fake-onnx-model-" * 64


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            await asyncio.sleep(0)
            yield chunk


class _FakeResponse:
    def __init__(self, chunks: list[bytes], *, status_error: Exception | None = None) -> None:
        self.content = _FakeContent(chunks)
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeClientSession:
    """Stands in for ``aiohttp.ClientSession``; records the requested URLs."""

    requested: ClassVar[list[str]] = []
    chunks: ClassVar[list[bytes]] = []
    get_error: ClassVar[Exception | None] = None
    status_error: ClassVar[Exception | None] = None

    def __init__(self, *args, **kwargs) -> None:
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self) -> _FakeClientSession:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def get(self, url: str) -> _FakeResponse:
        type(self).requested.append(url)
        if type(self).get_error is not None:
            raise type(self).get_error
        return _FakeResponse(list(type(self).chunks), status_error=type(self).status_error)


@pytest.fixture()
def model_dirs(tmp_path, monkeypatch):
    """Point the image and data model directories at temporary folders."""
    image_dir = tmp_path / "image" / "speaker"
    data_db = tmp_path / "data" / "agent_assist.db"
    monkeypatch.setattr(voice_model, "IMAGE_MODEL_DIR", image_dir)
    monkeypatch.setattr(voice_model.settings, "sqlite_db_path", str(data_db))
    return image_dir, tmp_path / "data" / "models" / "speaker"


@pytest.fixture()
def fake_http(monkeypatch):
    """Replace aiohttp.ClientSession inside the model module; the payload verifies by default."""
    _FakeClientSession.requested = []
    _FakeClientSession.chunks = [_PAYLOAD[:500], _PAYLOAD[500:]]
    _FakeClientSession.get_error = None
    _FakeClientSession.status_error = None
    monkeypatch.setattr(voice_model.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(voice_model, "MODEL_SIZE_BYTES", len(_PAYLOAD))
    monkeypatch.setattr(voice_model, "MODEL_SHA256", hashlib.sha256(_PAYLOAD).hexdigest())
    return _FakeClientSession


def _leftovers(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".part"))


class TestModelPathResolution:
    def test_data_dir_is_next_to_the_database(self, model_dirs):
        _, data_dir = model_dirs
        assert voice_model.data_model_dir() == data_dir

    async def test_no_model_anywhere(self, model_dirs):
        assert await voice_model.resolve_model_path() is None

    async def test_data_path_used_when_image_lacks_model(self, model_dirs):
        _, data_dir = model_dirs
        data_dir.mkdir(parents=True)
        (data_dir / voice_model.MODEL_FILENAME).write_bytes(b"data")
        assert await voice_model.resolve_model_path() == data_dir / voice_model.MODEL_FILENAME

    async def test_image_path_preferred_over_data_path(self, model_dirs):
        image_dir, data_dir = model_dirs
        for directory in (image_dir, data_dir):
            directory.mkdir(parents=True)
            (directory / voice_model.MODEL_FILENAME).write_bytes(b"model")
        assert await voice_model.resolve_model_path() == image_dir / voice_model.MODEL_FILENAME

    async def test_empty_file_is_ignored(self, model_dirs):
        image_dir, data_dir = model_dirs
        image_dir.mkdir(parents=True)
        (image_dir / voice_model.MODEL_FILENAME).write_bytes(b"")
        data_dir.mkdir(parents=True)
        (data_dir / voice_model.MODEL_FILENAME).write_bytes(b"data")
        assert await voice_model.resolve_model_path() == data_dir / voice_model.MODEL_FILENAME

    async def test_ensure_model_path_skips_download_when_present(self, model_dirs, fake_http):
        image_dir, _ = model_dirs
        image_dir.mkdir(parents=True)
        (image_dir / voice_model.MODEL_FILENAME).write_bytes(b"model")
        assert await voice_model.ensure_model_path() == image_dir / voice_model.MODEL_FILENAME
        assert fake_http.requested == []


class TestModelDownload:
    async def test_successful_download_is_verified_and_renamed_atomically(self, model_dirs, fake_http):
        _, data_dir = model_dirs
        path = await voice_model.ensure_model_path()
        target = data_dir / voice_model.MODEL_FILENAME
        assert path == target
        assert target.read_bytes() == _PAYLOAD
        assert _leftovers(data_dir) == []
        assert fake_http.requested == [voice_model.MODEL_URL]

    async def test_checksum_mismatch_stores_nothing(self, model_dirs, fake_http, monkeypatch):
        _, data_dir = model_dirs
        monkeypatch.setattr(voice_model, "MODEL_SHA256", "0" * 64)
        with pytest.raises(RuntimeError, match="checksum"):
            await voice_model.download_model()
        assert not (data_dir / voice_model.MODEL_FILENAME).exists()
        assert _leftovers(data_dir) == []

    async def test_size_mismatch_stores_nothing(self, model_dirs, fake_http):
        _, data_dir = model_dirs
        fake_http.chunks = [_PAYLOAD[:100]]
        with pytest.raises(RuntimeError, match="size mismatch"):
            await voice_model.download_model()
        assert not (data_dir / voice_model.MODEL_FILENAME).exists()

    async def test_oversized_download_is_aborted(self, model_dirs, fake_http, monkeypatch):
        monkeypatch.setattr(voice_model, "_MAX_DOWNLOAD_BYTES", 600)
        with pytest.raises(RuntimeError, match="size limit"):
            await voice_model.download_model()

    async def test_failed_rename_removes_partial_file(self, tmp_path, monkeypatch):
        target = tmp_path / "speaker" / "model.onnx"

        def _fail_replace(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(voice_model.os, "replace", _fail_replace)
        with pytest.raises(OSError):
            voice_model._write_model_atomically(target, b"payload")
        assert not target.exists()
        assert _leftovers(target.parent) == []

    @pytest.mark.parametrize(
        "error",
        [TimeoutError(), aiohttp.ClientConnectionError("connection refused")],
    )
    async def test_network_failure_raises(self, model_dirs, fake_http, error):
        fake_http.get_error = error
        with pytest.raises(type(error)):
            await voice_model.download_model()


class TestEngineUnavailableOnDownloadFailure:
    """A failed provisioning marks the engine ``unavailable``; nothing crashes."""

    @staticmethod
    def _engine() -> SpeakerEngine:
        return SpeakerEngine(
            extractor_factory=lambda path, threads: pytest.fail("extractor must not load without a model"),
            model_path_resolver=voice_model.ensure_model_path,
        )

    @pytest.mark.parametrize(
        "error",
        [TimeoutError(), aiohttp.ClientConnectionError("connection refused")],
    )
    async def test_network_error(self, model_dirs, fake_http, error):
        fake_http.get_error = error
        engine = self._engine()
        assert await engine.ensure_ready() is False
        assert engine.status == "unavailable"
        assert engine.last_error

    async def test_http_error_status(self, model_dirs, fake_http):
        fake_http.status_error = aiohttp.ClientResponseError(MagicMock(real_url=voice_model.MODEL_URL), (), status=404)
        engine = self._engine()
        assert await engine.ensure_ready() is False
        assert engine.status == "unavailable"

    async def test_checksum_mismatch(self, model_dirs, fake_http, monkeypatch):
        _, data_dir = model_dirs
        monkeypatch.setattr(voice_model, "MODEL_SHA256", "f" * 64)
        engine = self._engine()
        assert await engine.ensure_ready() is False
        assert engine.status == "unavailable"
        assert "checksum" in (engine.last_error or "")
        assert not (data_dir / voice_model.MODEL_FILENAME).exists()
