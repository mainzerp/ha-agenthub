"""Speaker identification core: PCM handling, matching, turn store, enrollment, engine wrapper."""

from __future__ import annotations

import asyncio
import struct
import threading
from pathlib import Path

import numpy as np
import pytest

from app.voice import turn_store as turn_store_module
from app.voice.audio import (
    MAX_AUDIO_BYTES,
    PcmBuffer,
    find_wav_data_offset,
    pcm_duration_s,
    pcm_s16le_to_float32,
)
from app.voice.enrollment import EnrollmentState
from app.voice.profiles import ProfileCentroid, build_centroids, l2_normalize, match_embedding
from app.voice.speaker_engine import SpeakerEngine, SpeakerEngineUnavailableError
from app.voice.turn_store import SpeakerTurnStore


def _wav_header(extra_chunk: bytes = b"") -> bytes:
    fmt = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + extra_chunk + b"data" + struct.pack("<I", 0)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _profile(pid: int, vector: list[float], name: str | None = None) -> ProfileCentroid:
    return ProfileCentroid(pid, name or f"p{pid}", None, l2_normalize(np.array(vector, dtype=np.float32)))


# ---------------------------------------------------------------------------
# PCM / WAV handling
# ---------------------------------------------------------------------------


class TestPcmBuffer:
    def test_raw_pcm_passes_through(self):
        buf = PcmBuffer()
        buf.append(b"\x01\x00\x02\x00")
        buf.append(b"\x03\x00")
        assert buf.finish() == b"\x01\x00\x02\x00\x03\x00"

    def test_wav_header_is_skipped(self):
        buf = PcmBuffer()
        buf.append(_wav_header() + b"\x10\x00\x20\x00")
        assert buf.finish() == b"\x10\x00\x20\x00"

    def test_wav_header_with_extra_chunk_is_parsed(self):
        # A LIST chunk with an odd size (pad byte) before "data".
        header = _wav_header(b"LIST" + struct.pack("<I", 3) + b"abc\x00")
        assert find_wav_data_offset(header) == len(header)
        buf = PcmBuffer()
        buf.append(header + b"\x05\x00")
        assert buf.finish() == b"\x05\x00"

    def test_wav_header_split_across_frames(self):
        data = _wav_header() + b"\x07\x00\x08\x00"
        buf = PcmBuffer()
        buf.append(data[:10])
        buf.append(data[10:30])
        buf.append(data[30:])
        assert buf.finish() == b"\x07\x00\x08\x00"

    def test_malformed_wave_falls_back_to_44_bytes(self):
        data = b"RIFF" + b"\x00" * 4 + b"XXXX" + b"\x00" * 32 + b"\x09\x00"
        assert find_wav_data_offset(data) == 44
        buf = PcmBuffer()
        buf.append(data)
        assert buf.finish() == b"\x09\x00"

    def test_odd_trailing_byte_is_dropped(self):
        buf = PcmBuffer()
        buf.append(b"\x01\x00\x02")
        assert buf.finish() == b"\x01\x00"

    def test_audio_is_capped(self):
        buf = PcmBuffer(max_bytes=8)
        buf.append(b"\x00" * 6)
        buf.append(b"\x01" * 6)
        assert len(buf.finish()) == 8
        assert buf.truncated

    def test_default_cap_is_thirty_seconds(self):
        assert MAX_AUDIO_BYTES == 30 * 16000 * 2


def test_s16le_conversion_range():
    pcm = struct.pack("<3h", -32768, 0, 16384)
    wave = pcm_s16le_to_float32(pcm)
    assert wave.dtype == np.float32
    assert wave.tolist() == [-1.0, 0.0, 0.5]


def test_pcm_duration():
    assert pcm_duration_s(b"\x00" * 32000) == pytest.approx(1.0)
    assert pcm_duration_s(b"\x00" * 32000, 0) == 0.0


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class TestMatching:
    def test_identified_above_threshold_and_margin(self):
        centroids = [_profile(1, [1, 0, 0]), _profile(2, [0, 1, 0])]
        result = match_embedding(np.array([0.9, 0.1, 0.0]), centroids, threshold=0.55, margin=0.05)
        assert result.identified
        assert result.best is not None and result.best.profile_id == 1
        assert result.score is not None and result.score > 0.9
        assert result.margin is not None and result.margin > 0.5

    def test_below_threshold_is_unknown(self):
        centroids = [_profile(1, [1, 0, 0])]
        result = match_embedding(np.array([0.3, 1.0, 0.0]), centroids, threshold=0.55, margin=0.05)
        assert not result.identified
        assert result.best is not None and result.score is not None and result.score < 0.55

    def test_margin_too_small_is_unknown(self):
        centroids = [_profile(1, [1, 0.0, 0]), _profile(2, [1, 0.05, 0])]
        result = match_embedding(np.array([1.0, 0.02, 0.0]), centroids, threshold=0.55, margin=0.05)
        assert not result.identified
        assert result.margin is not None and result.margin < 0.05

    def test_single_profile_margin_uses_zero_second_best(self):
        centroids = [_profile(1, [1, 0, 0])]
        result = match_embedding(np.array([1.0, 0.0, 0.0]), centroids, threshold=0.55, margin=0.05)
        assert result.identified
        assert result.margin == pytest.approx(1.0)

    def test_no_centroids(self):
        result = match_embedding(np.array([1.0, 0.0]), [], threshold=0.5, margin=0.0)
        assert not result.identified and result.best is None and result.score is None

    def test_build_centroids_means_normalised_samples(self):
        rows = [
            {
                "profile_id": 1,
                "name": "Anna",
                "ha_user_id": "u1",
                "embedding": np.array([2, 0], "<f4").tobytes(),
                "dim": 2,
            },
            {
                "profile_id": 1,
                "name": "Anna",
                "ha_user_id": "u1",
                "embedding": np.array([0, 5], "<f4").tobytes(),
                "dim": 2,
            },
            {
                "profile_id": 2,
                "name": "Ben",
                "ha_user_id": None,
                "embedding": np.array([1, 1, 1], "<f4").tobytes(),
                "dim": 3,
            },
            {
                "profile_id": 3,
                "name": "Bad",
                "ha_user_id": None,
                "embedding": np.array([1, 1], "<f4").tobytes(),
                "dim": 5,
            },
        ]
        centroids = build_centroids(rows)
        assert [c.profile_id for c in centroids] == [1]
        anna = centroids[0]
        assert anna.name == "Anna" and anna.ha_user_id == "u1"
        assert np.allclose(anna.centroid, np.array([1, 1]) / np.sqrt(2), atol=1e-6)


# ---------------------------------------------------------------------------
# Turn store
# ---------------------------------------------------------------------------


class TestTurnStore:
    async def test_resolve_then_pop_is_single_use(self):
        store = SpeakerTurnStore()
        token = store.begin("c1")
        assert store.resolve("c1", token, {"status": "identified"})
        lookup = await store.pop("c1", 0.5)
        assert lookup.found and lookup.result == {"status": "identified"}
        assert not store.has("c1")
        again = await store.pop("c1", 0.5)
        assert not again.found and again.result is None

    async def test_pop_waits_for_pending_result(self):
        store = SpeakerTurnStore()
        token = store.begin("c1")

        async def _later():
            await asyncio.sleep(0.02)
            store.resolve("c1", token, {"status": "unknown"})

        resolver = asyncio.create_task(_later())
        lookup = await store.pop("c1", 1.0)
        await resolver
        assert lookup.result == {"status": "unknown"}
        assert lookup.waited_ms > 0
        assert not lookup.timed_out

    async def test_pop_wait_timeout_removes_entry(self):
        store = SpeakerTurnStore()
        token = store.begin("c1")
        lookup = await store.pop("c1", 0.01)
        assert lookup.found and lookup.timed_out and lookup.result is None
        # A late result never lands on a later turn.
        assert not store.resolve("c1", token, {"status": "identified"})
        assert not store.has("c1")

    async def test_newer_session_replaces_older(self):
        store = SpeakerTurnStore()
        old = store.begin("c1")
        new = store.begin("c1")
        assert not store.resolve("c1", old, {"status": "stale"})
        assert store.resolve("c1", new, {"status": "fresh"})
        assert (await store.pop("c1", 0)).result == {"status": "fresh"}

    async def test_replacement_releases_waiter(self):
        store = SpeakerTurnStore()
        store.begin("c1")
        waiter = asyncio.create_task(store.pop("c1", 2.0))
        await asyncio.sleep(0)
        store.begin("c1")
        lookup = await asyncio.wait_for(waiter, 1.0)
        assert lookup.found and lookup.result is None

    async def test_ttl_expiry(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(turn_store_module.time, "monotonic", lambda: now[0])
        store = SpeakerTurnStore(ttl_s=120)
        token = store.begin("c1")
        store.resolve("c1", token, {"status": "identified"})
        now[0] += 121
        assert not store.has("c1")
        assert not (await store.pop("c1", 0)).found

    def test_max_entries_evicts_oldest(self):
        store = SpeakerTurnStore(max_entries=2)
        store.begin("a")
        store.begin("b")
        store.begin("c")
        assert not store.has("a")
        assert store.has("b") and store.has("c")
        assert len(store) == 2

    def test_discard_respects_token(self):
        store = SpeakerTurnStore()
        old = store.begin("c1")
        store.begin("c1")
        store.discard("c1", old)
        assert store.has("c1")
        store.discard("c1")
        assert not store.has("c1")

    def test_recent_ring_buffer(self):
        store = SpeakerTurnStore(recent_size=3)
        for i in range(5):
            store.record_recent({"status": "unknown", "score": i / 10, "duration_s": 2.0, "embedding": [1, 2]})
        recent = store.recent()
        assert len(recent) == 3
        assert recent[0]["score"] == 0.4
        assert "embedding" not in recent[0]
        assert set(recent[0]) == {
            "timestamp",
            "status",
            "profile_id",
            "name",
            "score",
            "margin",
            "second_name",
            "second_score",
            "scores",
            "duration_s",
            "speech_s",
            "vad",
            "reason",
            "compute_ms",
            "latency_ms",
        }
        assert recent[0]["compute_ms"] is None and recent[0]["latency_ms"] is None

    def test_recent_keeps_timing(self):
        store = SpeakerTurnStore()
        store.record_recent({"status": "identified", "compute_ms": 12.3, "latency_ms": 15.0})
        entry = store.recent()[0]
        assert entry["compute_ms"] == 12.3 and entry["latency_ms"] == 15.0


# ---------------------------------------------------------------------------
# Enrollment state
# ---------------------------------------------------------------------------


class TestEnrollmentState:
    def test_arm_collect_auto_disarm(self):
        state = EnrollmentState()
        armed = state.arm(7, "Anna", samples=2, timeout_s=60)
        assert armed["profile_id"] == 7 and armed["remaining"] == 2 and armed["expires_at"]
        assert state.record_sample(7) == 1
        assert state.current()["remaining"] == 1
        assert state.record_sample(7) == 0
        assert state.current() is None

    def test_expiry(self, monkeypatch):
        from app.voice import enrollment as enrollment_module

        now = [50.0]
        monkeypatch.setattr(enrollment_module.time, "monotonic", lambda: now[0])
        state = EnrollmentState()
        state.arm(1, "Ben", samples=5, timeout_s=30)
        now[0] += 31
        assert state.current() is None

    def test_only_one_armed_and_cancel(self):
        state = EnrollmentState()
        state.arm(1, "A")
        state.arm(2, "B")
        assert state.current()["profile_id"] == 2
        assert state.record_sample(1) == 0  # not the armed profile
        assert state.cancel()
        assert not state.cancel()

    def test_disarm_and_rename(self):
        state = EnrollmentState()
        state.arm(3, "Old")
        state.rename(3, "New")
        assert state.current()["name"] == "New"
        state.disarm_profile(4)
        assert state.current() is not None
        state.disarm_profile(3)
        assert state.current() is None


# ---------------------------------------------------------------------------
# Engine wrapper
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self) -> None:
        self.waveform = None
        self.finished = False

    def accept_waveform(self, sample_rate, waveform):
        assert sample_rate == 16000
        self.waveform = waveform

    def input_finished(self):
        self.finished = True


class _FakeExtractor:
    dim = 3

    def create_stream(self):
        return _FakeStream()

    def compute(self, stream):
        assert stream.finished
        return [float(len(stream.waveform)), 0.0, 1.0]


async def _fake_model_path() -> Path:
    return Path("model.onnx")


class TestSpeakerEngine:
    async def test_lazy_load_and_embed(self):
        calls: list[tuple[Path, int]] = []

        def factory(path, threads):
            calls.append((path, threads))
            return _FakeExtractor()

        engine = SpeakerEngine(extractor_factory=factory, model_path_resolver=_fake_model_path, model_id="m1")
        assert engine.status == "unavailable"
        vector = await engine.embed(b"\x00\x00" * 4, 16000, num_threads=3)
        assert engine.status == "ready"
        assert vector.dtype == np.float32 and vector.tolist() == [4.0, 0.0, 1.0]
        await engine.embed(b"\x00\x00", 16000, num_threads=3)
        assert calls == [(Path("model.onnx"), 3)]

    async def test_load_failure_reports_unavailable_with_backoff(self):
        attempts = []

        def factory(path, threads):
            attempts.append(1)
            raise SpeakerEngineUnavailableError("sherpa_onnx is not installed")

        engine = SpeakerEngine(extractor_factory=factory, model_path_resolver=_fake_model_path)
        with pytest.raises(SpeakerEngineUnavailableError):
            await engine.embed(b"\x00\x00", 16000)
        assert engine.status == "unavailable"
        assert "sherpa_onnx" in (engine.last_error or "")
        # Backoff: no second load attempt right away.
        assert not await engine.ensure_ready()
        assert len(attempts) == 1

    async def test_model_download_failure_is_unavailable(self):
        async def failing_resolver():
            raise RuntimeError("network down")

        engine = SpeakerEngine(extractor_factory=lambda p, t: _FakeExtractor(), model_path_resolver=failing_resolver)
        assert not await engine.ensure_ready()
        assert engine.status == "unavailable"


class _TaggedExtractor(_FakeExtractor):
    def __init__(self, tag: float) -> None:
        self.tag = tag

    def compute(self, stream):
        assert stream.finished
        return [self.tag, 0.0, 0.0]


class TestSpeakerEngineThreadChange:
    """A changed ``num_threads`` never reloads inside a voice turn."""

    async def test_turn_uses_loaded_extractor_while_reload_runs_in_background(self):
        calls: list[int] = []
        gate = threading.Event()

        def factory(path, threads):
            calls.append(threads)
            if threads == 4:
                assert gate.wait(5.0)
            return _TaggedExtractor(float(threads))

        engine = SpeakerEngine(extractor_factory=factory, model_path_resolver=_fake_model_path)
        assert (await engine.embed(b"\x00\x00", 16000, num_threads=2)).tolist()[0] == 2.0

        # Changed thread count: served at once by the loaded extractor (the reload is blocked).
        vector = await asyncio.wait_for(engine.embed(b"\x00\x00", 16000, num_threads=4), 1.0)
        assert vector.tolist()[0] == 2.0
        assert engine.status == "ready"
        reload_task = engine._reload_task
        assert reload_task is not None and not reload_task.done()
        # A second turn does not start another reload for the same count.
        assert engine.schedule_reload(4) is False

        gate.set()
        assert await asyncio.wait_for(reload_task, 5.0) is True
        assert (await engine.embed(b"\x00\x00", 16000, num_threads=4)).tolist()[0] == 4.0
        assert calls == [2, 4]

    async def test_failed_reload_keeps_loaded_extractor(self):
        calls: list[int] = []

        def factory(path, threads):
            calls.append(threads)
            if threads == 4:
                raise RuntimeError("load failed")
            return _TaggedExtractor(float(threads))

        engine = SpeakerEngine(extractor_factory=factory, model_path_resolver=_fake_model_path)
        await engine.embed(b"\x00\x00", 16000, num_threads=2)
        assert (await engine.embed(b"\x00\x00", 16000, num_threads=4)).tolist()[0] == 2.0
        await asyncio.wait_for(engine._reload_task, 5.0)

        assert engine.status == "ready" and engine.last_error is None
        assert (await engine.embed(b"\x00\x00", 16000, num_threads=4)).tolist()[0] == 2.0
        # Backoff: no immediate retry for the failed thread count.
        assert engine.schedule_reload(4) is False
        assert calls == [2, 4]

    def test_schedule_reload_noop_without_loaded_extractor(self):
        engine = SpeakerEngine(extractor_factory=lambda path, threads: _FakeExtractor())
        assert engine.schedule_reload(4) is False
