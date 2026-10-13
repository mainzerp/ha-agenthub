"""Opt-in integration test for the real sherpa-onnx Silero VAD.

Runs only when ``sherpa_onnx`` is installed AND the model file exists:
``AGENTHUB_VAD_MODEL`` (path to ``silero_vad.onnx``), else the image copy
under ``/opt/models/speaker``. Otherwise it is skipped -- tests never
download the model. The signal is synthetic (a voiced harmonic burst with
syllable-rate modulation), so the bounds are generous.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import numpy as np
import pytest

from app.voice import model as voice_model

pytestmark = pytest.mark.integration

_RATE = 16000


def _model_path() -> Path | None:
    configured = os.environ.get("AGENTHUB_VAD_MODEL")
    candidates = [Path(configured)] if configured else []
    candidates.append(voice_model.IMAGE_MODEL_DIR / voice_model.VAD_MODEL_FILENAME)
    return next((path for path in candidates if path.is_file()), None)


def _voiced(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * _RATE)) / _RATE
    f0 = 120 + 20 * np.sin(2 * np.pi * 3 * t)
    phase = 2 * np.pi * np.cumsum(f0) / _RATE
    wave = sum(np.sin(k * phase) / k for k in range(1, 15)) * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t) ** 2)
    return (0.3 * wave / np.max(np.abs(wave))).astype(np.float32)


@pytest.fixture(scope="module")
def detect():
    model = _model_path()
    if model is None:
        pytest.skip("silero_vad.onnx not found (set AGENTHUB_VAD_MODEL)")
    if importlib.util.find_spec("sherpa_onnx") is None:
        pytest.skip("sherpa_onnx is not installed")
    from app.voice.vad import _sherpa_vad_factory

    return _sherpa_vad_factory(model)


def test_speech_offsets_and_reset(detect):
    silence = np.zeros(_RATE, dtype=np.float32)
    wave = np.concatenate([silence, _voiced(1.5), silence])
    spans = detect(wave)
    assert spans, "no speech detected"
    start = spans[0][0]
    end = spans[-1][0] + spans[-1][1]
    assert abs(start - _RATE) < 0.3 * _RATE
    assert abs(end - 2.5 * _RATE) < 0.4 * _RATE
    # The detector is reset per call: the same input gives the same spans.
    assert detect(wave) == spans
    assert detect(np.zeros(2 * _RATE, dtype=np.float32)) == []


def test_flush_emits_a_trailing_segment(detect):
    # Speech runs to the very end of the buffer: only ``flush`` can close the segment.
    wave = np.concatenate([np.zeros(_RATE, dtype=np.float32), _voiced(1.2)])
    spans = detect(wave)
    assert spans, "trailing speech was not flushed"
    assert spans[-1][0] + spans[-1][1] > len(wave) - 0.3 * _RATE


async def test_trimmer_keeps_inner_pauses(detect):
    from unittest.mock import AsyncMock

    from app.voice.vad import VoiceActivityTrimmer

    silence = np.zeros(_RATE, dtype=np.float32)
    pause = np.zeros(int(0.8 * _RATE), dtype=np.float32)
    wave = np.concatenate([silence, _voiced(1.0), pause, _voiced(1.0), silence])
    pcm = (wave * 32767).astype("<i2").tobytes()
    trimmer = VoiceActivityTrimmer(
        detector_factory=lambda path: detect, model_path_resolver=AsyncMock(return_value=Path("m"))
    )
    assert await trimmer.ensure_ready()
    trim = await trimmer.trim(pcm, _RATE)
    assert trim.vad is True and trim.embedded_s is not None
    # Both bursts and the 0.8 s pause between them are embedded; most of the outer silence is not.
    assert trim.embedded_s > 2.5
    assert trim.embedded_s < len(wave) / _RATE - 1.0
    assert trim.speech_s < trim.embedded_s
