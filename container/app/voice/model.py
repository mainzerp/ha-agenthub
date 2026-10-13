"""Voice model provisioning: CAM++ speaker embeddings and Silero VAD (16 kHz).

The container image ships both models under :data:`IMAGE_MODEL_DIR`. When a
model is missing (dev/test environments), it is downloaded once into the
writable data directory next to the SQLite database. Every download is
size- and SHA-256-verified before an atomic rename.
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

# Silero voice-activity detector used to trim silence before embedding.
# Keep in sync with the pre-download step in container/Dockerfile.
VAD_MODEL_FILENAME = "silero_vad.onnx"
VAD_MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{VAD_MODEL_FILENAME}"
VAD_MODEL_SHA256 = "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"
VAD_MODEL_SIZE_BYTES = 643_854

IMAGE_MODEL_DIR = Path("/opt/models/speaker")

_DOWNLOAD_TIMEOUT_S = 300.0
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def data_model_dir() -> Path:
    """Writable fallback directory: ``<data dir>/models/speaker``."""
    return Path(settings.sqlite_db_path).parent / "models" / "speaker"


def _find_existing(filename: str) -> Path | None:
    for path in (IMAGE_MODEL_DIR / filename, data_model_dir() / filename):
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def _find_existing_model() -> Path | None:
    return _find_existing(MODEL_FILENAME)


async def resolve_model_path() -> Path | None:
    """Return the first existing speaker model file (image path, then data path)."""
    return await asyncio.to_thread(_find_existing_model)


async def resolve_vad_model_path() -> Path | None:
    """Return the first existing VAD model file (image path, then data path)."""
    return await asyncio.to_thread(_find_existing, VAD_MODEL_FILENAME)


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


async def _download_verified(
    url: str,
    target: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> Path:
    """Download ``url`` to ``target`` after a size and SHA-256 check. Raises on failure."""
    timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_S)
    title = label[:1].upper() + label[1:]
    logger.info("Downloading %s model %s", label, target.name)
    payload = bytearray()
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(1 << 16):
            payload.extend(chunk)
            if len(payload) > _MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"{title} model download exceeds the size limit")
    if len(payload) != expected_size:
        raise RuntimeError(f"{title} model size mismatch: got {len(payload)} bytes, expected {expected_size}")
    digest = await asyncio.to_thread(lambda: hashlib.sha256(payload).hexdigest())
    if digest != expected_sha256:
        raise RuntimeError(f"{title} model checksum mismatch")
    await asyncio.to_thread(_write_model_atomically, target, bytes(payload))
    logger.info("%s model stored at %s", title, target)
    return target


async def download_model(url: str = MODEL_URL, target: Path | None = None) -> Path:
    """Download the speaker model into the data directory and return its path.

    Verifies size and SHA-256 before an atomic rename, so a partial or
    tampered download never lands at the final path. Raises on failure.
    """
    return await _download_verified(
        url,
        target or data_model_dir() / MODEL_FILENAME,
        expected_size=MODEL_SIZE_BYTES,
        expected_sha256=MODEL_SHA256,
        label="speaker",
    )


async def download_vad_model(url: str = VAD_MODEL_URL, target: Path | None = None) -> Path:
    """Download the Silero VAD model into the data directory (same checks as the speaker model)."""
    return await _download_verified(
        url,
        target or data_model_dir() / VAD_MODEL_FILENAME,
        expected_size=VAD_MODEL_SIZE_BYTES,
        expected_sha256=VAD_MODEL_SHA256,
        label="VAD",
    )


async def ensure_model_path() -> Path:
    """Return an existing speaker model path, downloading the model when missing."""
    existing = await resolve_model_path()
    if existing is not None:
        return existing
    return await download_model()


async def ensure_vad_model_path() -> Path:
    """Return an existing VAD model path, downloading the model when missing."""
    existing = await resolve_vad_model_path()
    if existing is not None:
        return existing
    return await download_vad_model()
