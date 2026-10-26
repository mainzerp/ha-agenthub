"""Opt-in recording of voice turns for later assignment to a voice profile.

With ``voice.speaker_id.keep_audio`` on, every identification turn that
produced an embedding (``identified`` or ``unknown``) is kept: the exact
voice-activity-trimmed PCM that was embedded is written as a 16 kHz mono
16-bit WAV to ``<data dir>/voice_turns/<id>.wav`` and a ``voice_turns`` row
stores the result and the embedding. Enrollment turns are never recorded.
Recordings expire after ``voice.speaker_id.audio_retention_days`` (see
:func:`cleanup_recordings`). The audio never leaves the container.

File names are always ``<32 hex chars>.wav``; every path from the database
is validated against that pattern before it touches the file system.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from app import config
from app.db.repositories.voice import VoiceTurnRepository
from app.voice.audio import BYTES_PER_SAMPLE, SUPPORTED_CHANNELS

logger = logging.getLogger(__name__)

RECORDINGS_DIR_NAME = "voice_turns"
# Files without a database row are deleted only after this grace period: a
# recording writes its file before it inserts the row.
ORPHAN_GRACE_S = 3600.0

_TURN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_AUDIO_NAME_RE = re.compile(r"^[0-9a-f]{32}\.wav$")


def recordings_dir() -> Path:
    """``<data dir>/voice_turns`` (next to the SQLite database)."""
    # Read through the module so a patched ``app.config.settings`` applies.
    return Path(config.settings.sqlite_db_path).parent / RECORDINGS_DIR_NAME


def new_turn_id() -> str:
    return uuid.uuid4().hex


def is_valid_turn_id(value: str) -> bool:
    return bool(_TURN_ID_RE.match(value or ""))


def audio_file_name(turn_id: str) -> str:
    return f"{turn_id}.wav"


def audio_file_path(audio_path: str | None) -> Path | None:
    """Absolute path of a stored file name, or None for anything but ``<hex id>.wav``."""
    if not audio_path or not _AUDIO_NAME_RE.match(audio_path):
        return None
    return recordings_dir() / audio_path


def encode_wav(pcm: bytes, sample_rate: int) -> bytes:
    """PCM s16le mono -> RIFF/WAVE bytes."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(SUPPORTED_CHANNELS)
        wav.setsampwidth(BYTES_PER_SAMPLE)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def read_wav_pcm(path: Path) -> tuple[bytes, int]:
    """Read a recorded WAV file (blocking). Returns ``(pcm, sample_rate)``."""
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != SUPPORTED_CHANNELS or wav.getsampwidth() != BYTES_PER_SAMPLE:
            raise ValueError("unsupported WAV format")
        return wav.readframes(wav.getnframes()), wav.getframerate()


def _write_atomically(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _unlink_files(names: list[str]) -> int:
    removed = 0
    for name in names:
        path = audio_file_path(name)
        if path is None:
            continue
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("Deleting voice recording %s failed", name, exc_info=True)
    return removed


async def delete_audio_files(names: list[str]) -> int:
    """Delete recorded WAV files by stored name (off the event loop). Returns the number removed."""
    if not names:
        return 0
    return await asyncio.to_thread(_unlink_files, list(names))


@dataclass(frozen=True)
class TurnRecording:
    """Everything needed to persist one recorded voice turn."""

    turn_id: str
    pcm: bytes
    sample_rate: int
    vector: np.ndarray
    model_id: str
    status: str
    conversation_id: str | None = None
    duration_s: float | None = None
    speech_s: float | None = None
    profile_id: int | None = None
    score: float | None = None
    margin: float | None = None


def _write_recording(path: Path, pcm: bytes, sample_rate: int) -> None:
    _write_atomically(path, encode_wav(pcm, sample_rate))


async def store_recording(recording: TurnRecording) -> bool:
    """Write the WAV file and insert the row. Never raises: failures are logged, nothing is left behind."""
    name = audio_file_name(recording.turn_id)
    path = recordings_dir() / name
    try:
        await asyncio.to_thread(_write_recording, path, recording.pcm, recording.sample_rate)
    except Exception:
        logger.warning("Writing voice recording %s failed", name, exc_info=True)
        return False
    vector = recording.vector.astype("float32", copy=False).reshape(-1)
    try:
        await VoiceTurnRepository.add_turn(
            recording.turn_id,
            embedding=vector.tobytes(),
            dim=int(vector.size),
            model=recording.model_id,
            status=recording.status,
            audio_path=name,
            conversation_id=recording.conversation_id,
            duration_s=recording.duration_s,
            speech_s=recording.speech_s,
            speaker_profile_id=recording.profile_id,
            score=recording.score,
            margin=recording.margin,
        )
    except Exception:
        logger.warning("Storing voice recording %s failed", name, exc_info=True)
        await delete_audio_files([name])
        return False
    return True


def _sweep_orphans(known: set[str], grace_s: float) -> int:
    """Delete WAV and partial files without a row, older than ``grace_s`` (blocking)."""
    directory = recordings_dir()
    if not directory.is_dir():
        return 0
    cutoff = time.time() - grace_s
    removed = 0
    for entry in directory.iterdir():
        name = entry.name
        is_audio = bool(_AUDIO_NAME_RE.match(name))
        is_partial = name.startswith(".") and name.endswith(".part")
        if not (is_audio or is_partial) or (is_audio and name in known):
            continue
        try:
            if not entry.is_file() or entry.stat().st_mtime > cutoff:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            logger.warning("Deleting orphaned voice recording %s failed", name, exc_info=True)
    return removed


async def cleanup_recordings(
    retention_days: int,
    *,
    now: datetime | None = None,
    orphan_grace_s: float = ORPHAN_GRACE_S,
) -> dict[str, int]:
    """Delete recordings older than ``retention_days`` and orphaned files.

    Returns ``{"expired": rows deleted, "orphans": orphan files deleted}``.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    expired = await VoiceTurnRepository.delete_older_than(cutoff.isoformat())
    await delete_audio_files(expired)
    known = await VoiceTurnRepository.list_audio_paths()
    orphans = await asyncio.to_thread(_sweep_orphans, known, orphan_grace_s)
    return {"expired": len(expired), "orphans": orphans}
