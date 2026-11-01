#!/usr/bin/env python3
"""Generate the timer and alarm ring sounds served from the dashboard static directory.

Both files are synthesized with the standard library only, so the output is deterministic and no encoder is
needed:

* ``timer.wav``: a soft two-note chime, played three times.
* ``alarm.wav``: a more insistent rising four-beep pattern, played four times (longer, clearly different).

The notification dispatcher hands the served URL to ``assist_satellite.announce`` as ``preannounce_media_id``
(see ``docs/configuration.md``, notification profile). Run ``python scripts/gen_ring_sounds.py`` to rebuild them.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 16000
OUT_DIR = (
    Path(__file__).resolve().parent.parent
    / "container"
    / "app"
    / "dashboard"
    / "static"
    / "sounds"
)


def _tone(
    freq: float,
    seconds: float,
    *,
    decay: float,
    amp: float = 0.6,
    harmonics: tuple[float, ...] = (1.0,),
) -> list[float]:
    """A decaying bell-like tone: the sum of harmonics under an exponential envelope with a short attack."""
    count = int(SAMPLE_RATE * seconds)
    attack = int(SAMPLE_RATE * 0.005)
    samples: list[float] = []
    for i in range(count):
        t = i / SAMPLE_RATE
        env = math.exp(-decay * t) * (min(1.0, i / attack) if attack else 1.0)
        value = sum(
            weight * math.sin(2 * math.pi * freq * (n + 1) * t)
            for n, weight in enumerate(harmonics)
        )
        samples.append(amp * env * value / sum(harmonics))
    return samples


def _silence(seconds: float) -> list[float]:
    return [0.0] * int(SAMPLE_RATE * seconds)


def _mix_at(track: list[float], start_seconds: float, tone: list[float]) -> None:
    """Add ``tone`` into ``track`` at an offset, growing the track when needed."""
    start = int(SAMPLE_RATE * start_seconds)
    end = start + len(tone)
    if end > len(track):
        track.extend([0.0] * (end - len(track)))
    for i, value in enumerate(tone):
        track[start + i] += value


def timer_sound() -> list[float]:
    """Three repetitions of a descending two-note chime (about 6 s)."""
    track: list[float] = []
    for rep in range(3):
        base = rep * 2.0
        _mix_at(track, base, _tone(880.0, 1.2, decay=3.5, harmonics=(1.0, 0.4, 0.15)))
        _mix_at(
            track,
            base + 0.45,
            _tone(659.25, 1.5, decay=3.0, harmonics=(1.0, 0.4, 0.15)),
        )
    return track


def alarm_sound() -> list[float]:
    """Four repetitions of a rising four-beep burst (about 7.5 s)."""
    track: list[float] = []
    beep_notes = (1046.5, 1318.5, 1568.0, 2093.0)
    for rep in range(4):
        base = rep * 2.2
        for n, freq in enumerate(beep_notes):
            _mix_at(
                track,
                base + n * 0.22,
                _tone(freq, 0.2, decay=6.0, amp=0.55, harmonics=(1.0, 0.25)),
            )
    return track


def write_wav(path: Path, samples: list[float]) -> None:
    peak = max(1e-9, max(abs(s) for s in samples))
    scale = 0.85 / peak if peak > 0.85 else 1.0
    frames = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, s * scale)) * 32767)) for s in samples
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, build in (("timer.wav", timer_sound), ("alarm.wav", alarm_sound)):
        path = OUT_DIR / name
        samples = build()
        write_wav(path, samples)
        print(
            f"{path}: {len(samples) / SAMPLE_RATE:.1f} s, {path.stat().st_size} bytes"
        )


if __name__ == "__main__":
    main()
