"""Speaker-embedding model provisioning (CAM++ English VoxCeleb, 16 kHz).

The container image ships the model under :data:`IMAGE_MODEL_DIR`. When
it is missing (dev/test environments), the model is downloaded once into
the writable data directory next to the SQLite database.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)

# Stable model identifier stored with every voice sample: samples computed
# by a different model are never mixed into a profile centroid.
MODEL_ID = "3dspeaker_speech_campplus_sv_en_voxceleb_16k"
MODEL_FILENAME = f"{MODEL_ID}.onnx"
# Keep in sync with the pre-download step in container/Dockerfile.
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL_FILENAME}"
MODEL_SHA256 = "357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b"
MODEL_SIZE_BYTES = 29_596_978

IMAGE_MODEL_DIR = Path("/opt/models/speaker")

_DOWNLOAD_TIMEOUT_S = 300.0
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def data_model_dir() -> Path:
    """Writable fallback directory: ``<data dir>/models/speaker``."""
    return Path(settings.sqlite_db_path).parent / "models" / "speaker"


def _candidate_paths() -> list[Path]:
    return [IMAGE_MODEL_DIR / MODEL_FILENAME, data_model_dir() / MODEL_FILENAME]


def _find_existing_model() -> Path | None:
    for path in _candidate_paths():
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


async def resolve_model_path() -> Path | None:
    """Return the first existing model file (image path, then data path)."""
    return await asyncio.to_thread(_find_existing_model)


def _write_model_atomically(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


async def download_model(url: str = MODEL_URL, target: Path | None = None) -> Path:
    """Download the model into the data directory and return its path.

    Verifies size and SHA-256 before an atomic rename, so a partial or
    tampered download never lands at the final path. Raises on failure.
    """
    target = target or data_model_dir() / MODEL_FILENAME
    timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_S)
    logger.info("Downloading speaker-embedding model %s", MODEL_FILENAME)
    payload = bytearray()
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(1 << 16):
            payload.extend(chunk)
            if len(payload) > _MAX_DOWNLOAD_BYTES:
                raise RuntimeError("Speaker model download exceeds the size limit")
    if len(payload) != MODEL_SIZE_BYTES:
        raise RuntimeError(f"Speaker model size mismatch: got {len(payload)} bytes, expected {MODEL_SIZE_BYTES}")
    digest = await asyncio.to_thread(lambda: hashlib.sha256(payload).hexdigest())
    if digest != MODEL_SHA256:
        raise RuntimeError("Speaker model checksum mismatch")
    await asyncio.to_thread(_write_model_atomically, target, bytes(payload))
    logger.info("Speaker-embedding model stored at %s", target)
    return target


async def ensure_model_path() -> Path:
    """Return an existing model path, downloading the model when missing."""
    existing = await resolve_model_path()
    if existing is not None:
        return existing
    return await download_model()
