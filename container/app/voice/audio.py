"""PCM helpers for voice-turn audio (16-bit little-endian, mono)."""

from __future__ import annotations

import struct

import numpy as np

# The only audio format the speaker-identification engine accepts.
SUPPORTED_SAMPLE_RATE = 16000
SUPPORTED_BIT_RATE = 16
SUPPORTED_CHANNELS = 1
SUPPORTED_CODEC = "pcm"
BYTES_PER_SAMPLE = SUPPORTED_BIT_RATE // 8

# Hard cap on buffered audio per voice turn; excess bytes are ignored.
MAX_AUDIO_SECONDS = 30.0
MAX_AUDIO_BYTES = int(MAX_AUDIO_SECONDS * SUPPORTED_SAMPLE_RATE * BYTES_PER_SAMPLE)

# Canonical RIFF/WAVE header length used when the chunk list cannot be parsed.
_DEFAULT_WAV_HEADER_BYTES = 44
# A WAV header larger than this is treated as unparseable (fallback to 44 bytes).
_MAX_WAV_HEADER_BYTES = 4096


def find_wav_data_offset(data: bytes) -> int | None:
    """Return the byte offset of the PCM payload in a RIFF/WAVE buffer.

    Walks the RIFF chunk list to the ``data`` chunk. Returns ``None`` when
    the buffer does not yet contain the complete ``data`` chunk header (the
    caller may wait for more bytes) and the canonical 44-byte offset when
    the header is malformed.
    """
    if len(data) < 12:
        return None
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return _DEFAULT_WAV_HEADER_BYTES
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        (chunk_size,) = struct.unpack("<I", data[offset + 4 : offset + 8])
        if chunk_id == b"data":
            return offset + 8
        # RIFF chunks are word-aligned: odd sizes carry one pad byte.
        offset += 8 + chunk_size + (chunk_size & 1)
        if offset > _MAX_WAV_HEADER_BYTES:
            return _DEFAULT_WAV_HEADER_BYTES
    return None


class PcmBuffer:
    """Accumulates raw PCM bytes for one voice turn.

    If the very first bytes start with ``RIFF`` the WAV header is skipped
    (chunk list parsed to the ``data`` chunk, falling back to 44 bytes).
    Audio beyond :data:`MAX_AUDIO_BYTES` is ignored.
    """

    def __init__(self, max_bytes: int = MAX_AUDIO_BYTES) -> None:
        self._max_bytes = max_bytes
        self._data = bytearray()
        # Bytes held back while deciding whether a WAV header is present.
        self._pending_header: bytearray | None = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._pending_header is not None:
            self._pending_header.extend(chunk)
            self._resolve_header(final=False)
            return
        self._extend(chunk)

    def finish(self) -> bytes:
        """Flush any held-back header bytes and return the PCM payload."""
        if self._pending_header is not None:
            self._resolve_header(final=True)
        # An odd trailing byte is not a complete s16le sample.
        usable = len(self._data) - (len(self._data) % BYTES_PER_SAMPLE)
        return bytes(self._data[:usable])

    @property
    def byte_count(self) -> int:
        pending = len(self._pending_header) if self._pending_header is not None else 0
        return len(self._data) + pending

    def _resolve_header(self, *, final: bool) -> None:
        buf = self._pending_header
        assert buf is not None
        if len(buf) < 4 and not final:
            return
        if buf[:4] != b"RIFF":
            self._pending_header = None
            self._extend(bytes(buf))
            return
        offset = find_wav_data_offset(bytes(buf))
        if offset is None:
            if not final and len(buf) < _MAX_WAV_HEADER_BYTES:
                return
            offset = _DEFAULT_WAV_HEADER_BYTES
        self._pending_header = None
        self._extend(bytes(buf[offset:]))

    def _extend(self, chunk: bytes) -> None:
        room = self._max_bytes - len(self._data)
        if room <= 0:
            self.truncated = True
            return
        if len(chunk) > room:
            self.truncated = True
            chunk = chunk[:room]
        self._data.extend(chunk)


def pcm_duration_s(pcm: bytes, sample_rate: int = SUPPORTED_SAMPLE_RATE) -> float:
    """Duration in seconds of a mono s16le buffer."""
    if sample_rate <= 0:
        return 0.0
    return len(pcm) / (BYTES_PER_SAMPLE * sample_rate)


def pcm_s16le_to_float32(pcm: bytes) -> np.ndarray:
    """Convert mono s16le bytes to a float32 waveform in [-1, 1)."""
    usable = len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)
    samples = np.frombuffer(pcm[:usable], dtype="<i2")
    return samples.astype(np.float32) / 32768.0
