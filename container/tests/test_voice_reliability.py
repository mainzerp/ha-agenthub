"""Speaker-identification reliability: VAD trimming, enrollment guard, sample management, diagnostics, migration 51."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import numpy as np
import pytest

from app.analytics.tracer import SpanCollector, sanitize_trace_metadata
from app.db.repository import SettingsRepository, VoiceProfileRepository
from app.db.schema import _create_indexes, _create_tables, _seed_defaults
from app.db.schema._migrations import _run_migrations
from app.defaults import VOICE_DEFAULTS
from app.models.agent import TaskContext
from app.voice import model as voice_model
from app.voice import service as voice_service
from app.voice.profiles import (
    REJECTED_INCONSISTENT,
    REJECTED_OTHER_SPEAKER,
    ProfileCentroid,
    build_centroids,
    check_enrollment_sample,
    l2_normalize,
    sample_diagnostics,
)
from app.voice.service import SpeakerIdService, VoiceConfig, attach_speaker_to_task
from app.voice.vad import (
    _EDGE_PAD_S,
    SpeechTrim,
    VoiceActivityTrimmer,
    speech_region,
    speech_sample_count,
    untrimmed,
)
from tests.conftest import build_integration_test_app
from tests.helpers import make_ingress_task, shutdown_aiosqlite

_MODEL = "test-model"
_RATE = 16000
# Captured before the autouse fixture replaces it with a failing stub.
_ORIGINAL_ENSURE_VAD = voice_model.ensure_vad_model_path


class FakeEngine:
    """Returns queued vectors and records the PCM length it was given."""

    def __init__(self, vectors: list[list[float]] | None = None) -> None:
        self.model_id = _MODEL
        self.status = "ready"
        self.last_error = None
        self.vectors = list(vectors or [])
        self.pcm_lengths: list[int] = []

    async def ensure_ready(self, num_threads: int = 2) -> bool:
        return True

    async def embed(self, pcm: bytes, sample_rate: int, *, num_threads: int = 2) -> np.ndarray:
        self.pcm_lengths.append(len(pcm))
        return np.array(self.vectors.pop(0), dtype=np.float32)


class FakeVad:
    """Stands in for VoiceActivityTrimmer: keeps ``speech_s`` seconds of every turn."""

    def __init__(self, speech_s: float | None, *, vad: bool = True, state: str | None = None) -> None:
        self.speech_s = speech_s
        self.vad_flag = vad
        self.status = "ready" if vad else "unavailable"
        self.state = state or ("ready" if vad else "failed")
        self.last_error = None
        self.calls = 0
        self.warm_ups = 0

    async def ensure_ready(self) -> bool:
        return self.vad_flag

    def schedule_warm_up(self) -> None:
        self.warm_ups += 1

    async def trim(self, pcm: bytes, sample_rate: int) -> SpeechTrim:
        self.calls += 1
        if not self.vad_flag or self.speech_s is None:
            return untrimmed(pcm, sample_rate)
        samples = int(self.speech_s * sample_rate)
        return SpeechTrim(pcm=pcm[: samples * 2], speech_s=self.speech_s, vad=True)


def _config(**overrides) -> VoiceConfig:
    values = {
        "enabled": True,
        "threshold": 0.55,
        "margin": 0.12,
        "min_duration_s": 1.0,
        "min_enroll_duration_s": 1.5,
        "wait_ms": 500,
        "num_threads": 2,
        "enroll_min_similarity": 0.45,
    }
    values.update(overrides)
    return VoiceConfig(**values)


def _pcm(seconds: float) -> bytes:
    return b"\x01\x00" * int(_RATE * seconds)


def _vec(*values: float) -> bytes:
    return np.array(values, dtype=np.float32).tobytes()


def _unit(*values: float) -> list[float]:
    return [float(v) for v in l2_normalize(np.array(values, dtype=np.float32))]


@pytest.fixture(autouse=True)
def _reset_voice_service():
    voice_service.set_speaker_service(None)
    yield
    voice_service.set_speaker_service(None)


# ---------------------------------------------------------------------------
# VAD trimming
# ---------------------------------------------------------------------------


class TestVoiceActivityTrimmer:
    async def test_outer_silence_is_trimmed_and_inner_pauses_kept(self):
        # Speech 0.50-0.75 s and 2.00-2.50 s of a 3 s buffer: the 1.25 s pause between them stays.
        pcm = np.arange(3 * _RATE, dtype="<i2").tobytes()
        spans = [(_RATE // 2, _RATE // 4), (2 * _RATE, _RATE // 2)]
        seen: list[int] = []

        def factory(path: Path):
            def detect(waveform: np.ndarray):
                seen.append(len(waveform))
                return spans

            return detect

        trimmer = VoiceActivityTrimmer(detector_factory=factory, model_path_resolver=AsyncMock(return_value=Path("m")))
        assert await trimmer.ensure_ready() is True
        assert trimmer.status == "ready"
        trim = await trimmer.trim(pcm, _RATE)
        pad = int(_EDGE_PAD_S * _RATE)
        assert trim.vad is True
        # ``speech_s`` measures the detected speech only; ``embedded_s`` the padded region.
        assert trim.speech_s == pytest.approx(0.75)
        assert trim.embedded_s == pytest.approx(2.0 + 2 * _EDGE_PAD_S)
        # One contiguous slice: every sample of the inner pause is embedded.
        assert trim.pcm == pcm[(_RATE // 2 - pad) * 2 : (int(2.5 * _RATE) + pad) * 2]
        assert seen == [3 * _RATE]

    async def test_single_span_is_padded(self):
        pcm = np.arange(2 * _RATE, dtype="<i2").tobytes()
        trimmer = VoiceActivityTrimmer(
            detector_factory=lambda path: lambda wf: [(_RATE // 2, _RATE)],
            model_path_resolver=AsyncMock(return_value=Path("m")),
        )
        assert await trimmer.ensure_ready()
        trim = await trimmer.trim(pcm, _RATE)
        assert trim.speech_s == pytest.approx(1.0)
        assert trim.embedded_s == pytest.approx(1.0 + 2 * _EDGE_PAD_S)
        pad = int(_EDGE_PAD_S * _RATE)
        assert trim.pcm == pcm[(_RATE // 2 - pad) * 2 : (_RATE // 2 + _RATE + pad) * 2]

    async def test_padding_is_clamped_to_the_buffer(self):
        # Speech touches both buffer edges: no padding is possible, the whole buffer is embedded.
        pcm = np.arange(2 * _RATE, dtype="<i2").tobytes()
        trimmer = VoiceActivityTrimmer(
            detector_factory=lambda path: lambda wf: [(0, _RATE // 2), (_RATE, _RATE)],
            model_path_resolver=AsyncMock(return_value=Path("m")),
        )
        assert await trimmer.ensure_ready()
        trim = await trimmer.trim(pcm, _RATE)
        assert trim.pcm == pcm
        assert trim.speech_s == pytest.approx(1.5) and trim.embedded_s == pytest.approx(2.0)

    def test_speech_region_clamps_and_spans_the_outer_speech(self):
        pcm = np.arange(20, dtype="<i2").tobytes()
        region = speech_region(pcm, [(12, 2), (-3, 5), (30, 4), (5, 1)], pad_samples=1)
        # First span start 0 (clamped), last span end 14, padded by 1; the out-of-range span is ignored.
        assert np.frombuffer(region, dtype="<i2").tolist() == list(range(15))
        assert speech_region(pcm, [], pad_samples=3) == b""
        assert speech_region(pcm, [(25, 4)], pad_samples=3) == b""

    def test_speech_sample_count_counts_overlaps_once(self):
        assert speech_sample_count([(0, 4), (2, 4), (10, 2)], 20) == 8
        assert speech_sample_count([(18, 5), (-2, 3)], 20) == 3
        assert speech_sample_count([], 20) == 0

    async def test_not_ready_falls_back_to_the_whole_buffer_and_warms_up(self):
        trimmer = VoiceActivityTrimmer(detector_factory=lambda path: lambda wf: [])
        with patch.object(trimmer, "schedule_warm_up") as warm_up:
            trim = await trimmer.trim(_pcm(2.0), _RATE)
        warm_up.assert_called_once()
        assert trim.vad is False and trim.speech_s == pytest.approx(2.0) and len(trim.pcm) == len(_pcm(2.0))

    async def test_failed_load_marks_unavailable_without_raising(self):
        trimmer = VoiceActivityTrimmer(
            detector_factory=lambda path: pytest.fail("no detector without a model"),
            model_path_resolver=AsyncMock(side_effect=RuntimeError("download failed")),
        )
        assert await trimmer.ensure_ready() is False
        assert trimmer.status == "unavailable" and "download failed" in (trimmer.last_error or "")
        # Backoff: no retry right away.
        assert await trimmer.ensure_ready() is False

    async def test_detector_error_falls_back(self):
        def factory(path: Path):
            def detect(waveform: np.ndarray):
                raise RuntimeError("onnx failure")

            return detect

        trimmer = VoiceActivityTrimmer(detector_factory=factory, model_path_resolver=AsyncMock(return_value=Path("m")))
        assert await trimmer.ensure_ready()
        trim = await trimmer.trim(_pcm(1.5), _RATE)
        assert trim.vad is False and trim.speech_s == pytest.approx(1.5)

    async def test_no_speech_detected(self):
        trimmer = VoiceActivityTrimmer(
            detector_factory=lambda path: lambda wf: [], model_path_resolver=AsyncMock(return_value=Path("m"))
        )
        assert await trimmer.ensure_ready()
        trim = await trimmer.trim(_pcm(2.0), _RATE)
        assert trim.vad is True and trim.speech_s == 0.0 and trim.pcm == b"" and trim.embedded_s == 0.0


class TestServiceUsesVad:
    async def test_only_speech_is_embedded(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0), 2, _MODEL, 2.0)
        engine = FakeEngine([[1, 0]])
        service = SpeakerIdService(engine=engine, vad=FakeVad(1.25))
        result = await service.process_turn(_pcm(3.0), _RATE, _config())
        assert result["status"] == "identified"
        assert engine.pcm_lengths == [int(1.25 * _RATE) * 2]
        assert result["duration_s"] == pytest.approx(3.0)
        assert result["speech_s"] == pytest.approx(1.25) and result["vad"] is True

    async def test_too_short_counts_speech_seconds(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0), 2, _MODEL, 2.0)
        engine = FakeEngine([[1, 0]])
        service = SpeakerIdService(engine=engine, vad=FakeVad(0.6))
        result = await service.process_turn(_pcm(3.0), _RATE, _config())
        assert result["status"] == "too_short"
        assert result["speech_s"] == pytest.approx(0.6) and result["vad"] is True
        assert result["duration_s"] == pytest.approx(3.0)
        assert engine.pcm_lengths == []

    async def test_no_speech_reports_reason(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0), 2, _MODEL, 2.0)
        service = SpeakerIdService(engine=FakeEngine([[1, 0]]), vad=FakeVad(0.0))
        result = await service.process_turn(_pcm(2.0), _RATE, _config())
        assert result["status"] == "too_short" and result["reason"] == "no_speech"

    async def test_vad_unavailable_embeds_whole_turn(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0), 2, _MODEL, 2.0)
        engine = FakeEngine([[1, 0]])
        service = SpeakerIdService(engine=engine, vad=FakeVad(None, vad=False))
        result = await service.process_turn(_pcm(2.0), _RATE, _config())
        assert result["status"] == "identified"
        assert result["vad"] is False and result["speech_s"] == pytest.approx(2.0)
        assert engine.pcm_lengths == [len(_pcm(2.0))]

    async def test_default_vad_without_model_falls_back(self, db_repository):
        """The real trimmer with the download disabled (autouse fixture) never fails a turn."""
        anna = await VoiceProfileRepository.create_profile("Anna")
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0), 2, _MODEL, 2.0)
        service = SpeakerIdService(engine=FakeEngine([[1, 0]]))
        assert await service.vad.ensure_ready() is False
        result = await service.process_turn(_pcm(2.0), _RATE, _config())
        assert result["status"] == "identified" and result["vad"] is False


# ---------------------------------------------------------------------------
# Enrollment guard
# ---------------------------------------------------------------------------


def _centroid(pid: int, name: str, *values: float, samples: int = 1) -> ProfileCentroid:
    return ProfileCentroid(pid, name, None, l2_normalize(np.array(values, dtype=np.float32)), sample_count=samples)


class TestCheckEnrollmentSample:
    def test_consistent_sample_accepted(self):
        check = check_enrollment_sample(
            np.array([1.0, 0.1, 0.0]),
            1,
            [_centroid(1, "Anna", 1, 0, 0), _centroid(2, "Ben", 0, 1, 0)],
            threshold=0.55,
            margin=0.12,
            min_similarity=0.45,
        )
        assert check.reason is None and check.own_score > 0.9
        assert [item.profile.name for item in check.ranked] == ["Anna", "Ben"]

    def test_other_speaker_rejected(self):
        check = check_enrollment_sample(
            np.array([0.3, 1.0, 0.0]),
            1,
            [_centroid(1, "Anna", 1, 0, 0), _centroid(2, "Ben", 0, 1, 0)],
            threshold=0.55,
            margin=0.12,
            min_similarity=0.1,
        )
        assert check.reason == REJECTED_OTHER_SPEAKER

    def test_inconsistent_rejected_from_the_third_sample_on(self):
        check = check_enrollment_sample(
            np.array([0.4, 0.0, 1.0]),
            1,
            [_centroid(1, "Anna", 1, 0, 0, samples=2), _centroid(2, "Ben", 0, 1, 0)],
            threshold=0.55,
            margin=0.12,
            min_similarity=0.45,
        )
        assert check.reason == REJECTED_INCONSISTENT and check.own_score < 0.45

    def test_inconsistent_second_sample_accepted(self):
        # One accepted sample is no reliable anchor: the consistency rule waits for two.
        check = check_enrollment_sample(
            np.array([0.4, 0.0, 1.0]),
            1,
            [_centroid(1, "Anna", 1, 0, 0, samples=1), _centroid(2, "Ben", 0, 1, 0)],
            threshold=0.55,
            margin=0.12,
            min_similarity=0.45,
        )
        assert check.reason is None and check.own_score < 0.45

    def test_other_speaker_rule_applies_to_the_second_sample(self):
        check = check_enrollment_sample(
            np.array([0.3, 1.0, 0.0]),
            1,
            [_centroid(1, "Anna", 1, 0, 0, samples=1), _centroid(2, "Ben", 0, 1, 0)],
            threshold=0.55,
            margin=0.12,
            min_similarity=0.45,
        )
        assert check.reason == REJECTED_OTHER_SPEAKER

    def test_build_centroids_counts_samples(self):
        rows = [
            {"profile_id": 1, "name": "Anna", "embedding": _vec(1, 0), "dim": 2},
            {"profile_id": 1, "name": "Anna", "embedding": _vec(0.9, 0.1), "dim": 2},
            {"profile_id": 2, "name": "Ben", "embedding": _vec(0, 1), "dim": 2},
        ]
        counts = {c.profile_id: c.sample_count for c in build_centroids(rows)}
        assert counts == {1: 2, 2: 1}

    def test_first_sample_rejected_when_identified_as_someone_else(self):
        centroids = [_centroid(2, "Ben", 0, 1, 0), _centroid(3, "Cleo", 0, 0, 1)]
        rejected = check_enrollment_sample(
            np.array([0.1, 1.0, 0.0]), 1, centroids, threshold=0.55, margin=0.12, min_similarity=0.45
        )
        assert rejected.reason == REJECTED_OTHER_SPEAKER and rejected.own_score is None
        # Close to Ben but ambiguous with Cleo (margin rule): accepted as a new voice.
        accepted = check_enrollment_sample(
            np.array([0.0, 1.0, 0.95]), 1, centroids, threshold=0.55, margin=0.12, min_similarity=0.45
        )
        assert accepted.reason is None

    def test_first_sample_without_other_profiles_accepted(self):
        check = check_enrollment_sample(np.array([1.0, 0.0]), 1, [], threshold=0.55, margin=0.12, min_similarity=0.45)
        assert check.reason is None and check.own_score is None and check.ranked == ()


class TestEnrollmentGuardService:
    async def test_rejected_samples_do_not_count(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        ben = await VoiceProfileRepository.create_profile("Ben")
        # Two accepted samples: the consistency rule applies.
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0, 0), 3, _MODEL, 2.0, 1.8)
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0.05, 0), 3, _MODEL, 2.0, 1.8)
        await VoiceProfileRepository.add_sample(ben["id"], _vec(0, 1, 0), 3, _MODEL, 2.0, 1.8)
        engine = FakeEngine([_unit(0.2, 1, 0), _unit(0.3, 0, 1), _unit(1, 0.1, 0)])
        service = SpeakerIdService(engine=engine, vad=FakeVad(2.0))
        service.enrollment.arm(anna["id"], "Anna", samples=1, timeout_s=60)

        other = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert other["status"] == "rejected_sample" and other["reason"] == "rejected_other_speaker"
        assert other["name"] == "Anna" and other["scores"][0]["name"] == "Ben"
        assert service.enrollment.current()["remaining"] == 1

        inconsistent = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert inconsistent["status"] == "rejected_sample" and inconsistent["reason"] == "rejected_inconsistent"
        armed = service.enrollment.current()
        assert armed["remaining"] == 1 and armed["rejected"] == 2
        assert armed["last_rejection"]["reason"] == "rejected_inconsistent"
        assert await VoiceProfileRepository.count_samples(anna["id"], _MODEL) == 2

        accepted = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert accepted["status"] == "enrolled_sample" and accepted["score"] > 0.9
        assert accepted["speech_s"] == pytest.approx(2.0) and accepted["vad"] is True
        assert service.enrollment.current() is None
        rows = await VoiceProfileRepository.list_samples(anna["id"])
        assert len(rows) == 3 and rows[-1]["speech_s"] == pytest.approx(2.0)

        recent = service.store.recent()
        assert [r["status"] for r in recent] == ["enrolled_sample", "rejected_sample", "rejected_sample"]
        assert recent[1]["reason"] == "rejected_inconsistent"

    async def test_consistency_rule_waits_for_two_accepted_samples(self, db_repository):
        """A poor first sample must not block the session: the 2nd sample is never ``rejected_inconsistent``."""
        anna = await VoiceProfileRepository.create_profile("Anna")
        ben = await VoiceProfileRepository.create_profile("Ben")
        await VoiceProfileRepository.add_sample(ben["id"], _vec(0, 1, 0), 3, _MODEL, 2.0, 1.8)
        engine = FakeEngine([_unit(1, 0, 0), _unit(0.4, 0, 1), _unit(0.5, 0, -0.3), _unit(1, 0.1, 0.3)])
        service = SpeakerIdService(engine=engine, vad=FakeVad(2.0))
        service.enrollment.arm(anna["id"], "Anna", samples=3, timeout_s=60)

        first = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert first["status"] == "enrolled_sample"
        # Cosine ~0.37 to the single first sample (below 0.45): accepted, only one sample to compare with.
        second = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert second["status"] == "enrolled_sample" and second["score"] < 0.45
        # Cosine ~0.42 to the centroid of the two accepted samples: rejected.
        third = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert third["status"] == "rejected_sample" and third["reason"] == "rejected_inconsistent"
        assert await VoiceProfileRepository.count_samples(anna["id"], _MODEL) == 2
        fourth = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert fourth["status"] == "enrolled_sample"
        assert service.enrollment.current() is None
        assert await VoiceProfileRepository.count_samples(anna["id"], _MODEL) == 3

    async def test_first_sample_of_new_profile_identified_as_other_is_rejected(self, db_repository):
        ben = await VoiceProfileRepository.create_profile("Ben")
        cleo = await VoiceProfileRepository.create_profile("Cleo")
        await VoiceProfileRepository.add_sample(ben["id"], _vec(0, 1, 0), 3, _MODEL, 2.0)
        laura = await VoiceProfileRepository.create_profile("Laura")
        service = SpeakerIdService(engine=FakeEngine([_unit(0.1, 1, 0), _unit(1, 0, 0)]), vad=FakeVad(2.0))
        service.enrollment.arm(laura["id"], "Laura", samples=1, timeout_s=60)

        rejected = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert rejected["reason"] == "rejected_other_speaker"
        accepted = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert accepted["status"] == "enrolled_sample" and accepted["score"] is None
        assert await VoiceProfileRepository.count_samples(laura["id"], _MODEL) == 1
        assert await VoiceProfileRepository.count_samples(cleo["id"], _MODEL) == 0

    async def test_too_little_speech_rejected(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        engine = FakeEngine([[1, 0]])
        service = SpeakerIdService(engine=engine, vad=FakeVad(1.0))
        service.enrollment.arm(anna["id"], "Anna", samples=2, timeout_s=60)
        result = await service.process_turn(_pcm(4.0), _RATE, _config())
        assert result["status"] == "too_short" and result["reason"] == "rejected_too_short"
        assert result["speech_s"] == pytest.approx(1.0)
        assert engine.pcm_lengths == []
        armed = service.enrollment.current()
        assert armed["remaining"] == 2 and armed["last_rejection"]["reason"] == "rejected_too_short"


# ---------------------------------------------------------------------------
# Per-sample management
# ---------------------------------------------------------------------------


class TestSampleDiagnostics:
    def test_leave_one_out_and_best_other(self):
        rows = [
            {"id": 1, "embedding": _vec(1, 0, 0), "dim": 3},
            {"id": 2, "embedding": _vec(1, 0.1, 0), "dim": 3},
            {"id": 3, "embedding": _vec(0, 1, 0), "dim": 3},
        ]
        others = [_centroid(9, "Ben", 0, 1, 0)]
        result = {item["id"]: item for item in sample_diagnostics(rows, others)}
        # Sample 3 is someone else's voice: low leave-one-out, high score against Ben.
        assert result[3]["loo_score"] < 0.2
        assert result[3]["best_other_name"] == "Ben" and result[3]["best_other_score"] == pytest.approx(1.0)
        assert result[1]["loo_score"] > 0.6 and result[1]["best_other_score"] < 0.1

    def test_single_sample_has_no_leave_one_out(self):
        result = sample_diagnostics([{"id": 1, "embedding": _vec(1, 0), "dim": 2}], [])
        assert result == [{"id": 1, "loo_score": None, "best_other_name": None, "best_other_score": None}]


@pytest.fixture()
async def admin_client(db_repository):
    service = SpeakerIdService(engine=FakeEngine(), vad=FakeVad(None, vad=False))
    voice_service.set_speaker_service(service)
    app = build_integration_test_app(override_admin_session=True)
    with patch("app.db.repository.SetupStateRepository.is_complete", new_callable=AsyncMock, return_value=True):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client, service


class TestSampleApi:
    async def test_list_and_delete_single_sample(self, admin_client):
        client, _service = admin_client
        anna = await VoiceProfileRepository.create_profile("Anna")
        ben = await VoiceProfileRepository.create_profile("Ben")
        good = await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0, 0), 3, _MODEL, 2.5, 1.9)
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0.1, 0), 3, _MODEL, 2.2, 1.6)
        bad = await VoiceProfileRepository.add_sample(anna["id"], _vec(0, 1, 0), 3, _MODEL, 3.0, 2.4)
        legacy = await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0), 2, "old-model", 2.0)
        await VoiceProfileRepository.add_sample(ben["id"], _vec(0, 1, 0), 3, _MODEL, 2.0)

        response = await client.get(f"/api/admin/voice/profiles/{anna['id']}/samples")
        assert response.status_code == 200
        body = response.json()
        assert body["profile_id"] == anna["id"] and body["model"] == _MODEL
        samples = {item["id"]: item for item in body["samples"]}
        assert set(samples) == {good, bad, legacy, good + 1}
        assert samples[good]["created_at"] and samples[good]["duration_s"] == 2.5
        assert samples[good]["speech_s"] == pytest.approx(1.9)
        assert samples[good]["loo_score"] > 0.6
        assert samples[bad]["loo_score"] < 0.2
        assert samples[bad]["best_other_name"] == "Ben" and samples[bad]["best_other_score"] == pytest.approx(1.0)
        assert samples[legacy]["current_model"] is False and samples[legacy]["loo_score"] is None
        assert "embedding" not in samples[good]
        # Stored without silence trimming (speech_s NULL): flagged for re-enrollment.
        assert samples[legacy]["pre_vad"] is True and samples[good]["pre_vad"] is False

        deleted = await client.delete(f"/api/admin/voice/profiles/{anna['id']}/samples/{bad}")
        assert deleted.status_code == 200 and deleted.json() == {"ok": True}
        assert await VoiceProfileRepository.count_samples(anna["id"]) == 3
        # Wrong profile, already deleted, unknown profile.
        assert (await client.delete(f"/api/admin/voice/profiles/{ben['id']}/samples/{good}")).status_code == 404
        assert (await client.delete(f"/api/admin/voice/profiles/{anna['id']}/samples/{bad}")).status_code == 404
        assert (await client.delete("/api/admin/voice/profiles/999/samples/1")).status_code == 404
        assert (await client.get("/api/admin/voice/profiles/999/samples")).status_code == 404

    async def test_delete_invalidates_centroids(self, admin_client):
        client, service = admin_client
        anna = await VoiceProfileRepository.create_profile("Anna")
        sample = await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0), 2, _MODEL, 2.0)
        assert len(await service.centroids.get(_MODEL)) == 1
        await client.delete(f"/api/admin/voice/profiles/{anna['id']}/samples/{sample}")
        assert await service.centroids.get(_MODEL) == []

    async def test_sample_endpoints_require_admin_session(self, db_repository):
        voice_service.set_speaker_service(SpeakerIdService(engine=FakeEngine(), vad=FakeVad(None, vad=False)))
        app = build_integration_test_app(override_admin_session=False)
        with patch("app.db.repository.SetupStateRepository.is_complete", new_callable=AsyncMock, return_value=True):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                assert (await client.get("/api/admin/voice/profiles/1/samples")).status_code == 401
                assert (await client.delete("/api/admin/voice/profiles/1/samples/1")).status_code == 401


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics:
    async def test_unknown_turn_records_all_scores_without_a_name(self, db_repository):
        patric = await VoiceProfileRepository.create_profile("Patric")
        laura = await VoiceProfileRepository.create_profile("Laura")
        await VoiceProfileRepository.add_sample(patric["id"], _vec(1, 0, 0), 3, _MODEL, 2.0)
        await VoiceProfileRepository.add_sample(laura["id"], _vec(0, 1, 0), 3, _MODEL, 2.0)
        # Laura 0.75 vs Patric 0.66 (margin 0.09): the live confusion case.
        engine = FakeEngine([[0.66, 0.75, float(np.sqrt(1 - 0.66**2 - 0.75**2))]])
        service = SpeakerIdService(engine=engine, vad=FakeVad(1.4))
        result = await service.process_turn(_pcm(2.0), _RATE, _config())
        assert result["status"] == "unknown"
        assert result["name"] is None and result["profile_id"] is None
        assert [s["name"] for s in result["scores"]] == ["Laura", "Patric"]
        assert result["scores"][0]["score"] == pytest.approx(0.75, abs=1e-3)
        # The runner-up is named only next to an identified speaker; ``scores`` lists everyone.
        assert result["second_name"] is None and result["second_score"] is None
        assert result["scores"][1]["name"] == "Patric"
        assert result["scores"][1]["score"] == pytest.approx(0.66, abs=1e-3)
        assert result["margin"] == pytest.approx(0.09, abs=1e-3)
        assert result["speech_s"] == pytest.approx(1.4) and result["vad"] is True

        recent = service.store.recent()[0]
        assert recent["name"] is None and recent["status"] == "unknown"
        for key in ("scores", "second_name", "second_score", "speech_s", "vad", "duration_s"):
            assert recent[key] == result[key]

    async def test_status_api_exposes_vad_and_recent_diagnostics(self, admin_client):
        client, service = admin_client
        service.store.record_recent(
            voice_service.make_result(
                "unknown",
                duration_s=2.5,
                speech_s=1.2,
                vad=True,
                score=0.75,
                margin=0.09,
                scores=[{"name": "Laura", "score": 0.75}, {"name": "Patric", "score": 0.66}],
                second_name="Patric",
                second_score=0.66,
            )
        )
        status = (await client.get("/api/admin/voice/status")).json()
        assert status["vad"] == "unavailable" and "vad_error" in status
        recent = status["recent"][0]
        assert recent["name"] is None
        assert recent["scores"][1] == {"name": "Patric", "score": 0.66}
        assert recent["second_name"] == "Patric" and recent["speech_s"] == 1.2 and recent["vad"] is True

    async def test_span_metadata_carries_diagnostics_and_survives_sanitization(self):
        service = SpeakerIdService(engine=FakeEngine(), vad=FakeVad(None, vad=False))
        voice_service.set_speaker_service(service)
        result = voice_service.make_result(
            "identified",
            duration_s=2.4,
            profile_id=4,
            name="Laura",
            score=0.81,
            margin=0.2,
            speech_s=1.7,
            vad=True,
            scores=[{"name": "Laura", "score": 0.81}, {"name": "Patric", "score": 0.61}],
            second_name="Patric",
            second_score=0.61,
        )
        token = service.store.begin("conv-diag")
        service.store.resolve("conv-diag", token, result)
        task = make_ingress_task("hi", "conv-diag", TaskContext())
        collector = SpanCollector("trace-diag", source="ha")
        with patch.object(voice_service, "load_voice_config", AsyncMock(return_value=_config())):
            await attach_speaker_to_task(task, "conv-diag", collector)
        span = next(s for s in collector.get_spans() if s["span_name"] == "speaker_id")
        metadata = span["metadata"]
        for key in ("status", "profile_id", "score", "margin", "duration_s", "speaker_name"):
            assert key in metadata
        sanitized = sanitize_trace_metadata(metadata)
        assert sanitized["scores"] == [{"name": "Laura", "score": 0.81}, {"name": "Patric", "score": 0.61}]
        assert sanitized["second_name"] == "Patric" and sanitized["second_score"] == 0.61
        assert sanitized["speech_s"] == 1.7 and sanitized["vad"] is True and sanitized["duration_s"] == 2.4
        assert sanitized["speaker_name"] == "Laura"


# ---------------------------------------------------------------------------
# Defaults and migration 51
# ---------------------------------------------------------------------------


def test_new_defaults():
    assert VOICE_DEFAULTS["voice.speaker_id.margin"] == 0.12
    assert VOICE_DEFAULTS["voice.speaker_id.min_duration_s"] == 1.0
    assert VOICE_DEFAULTS["voice.speaker_id.min_enroll_duration_s"] == 1.5
    assert VOICE_DEFAULTS["voice.speaker_id.enroll_min_similarity"] == 0.45


async def test_enroll_min_similarity_is_loaded_and_clamped(db_repository):
    await SettingsRepository.set("voice.speaker_id.enroll_min_similarity", "1.7", "float", "voice")
    assert (await voice_service.load_voice_config()).enroll_min_similarity == 1.0


async def _settings(db: aiosqlite.Connection) -> dict[str, str]:
    cursor = await db.execute("SELECT key, value FROM settings WHERE key LIKE 'voice.speaker_id.%'")
    return {row[0]: row[1] for row in await cursor.fetchall()}


class TestMigration51:
    @staticmethod
    async def _install_at_schema_50(db: aiosqlite.Connection, values: dict[str, str]) -> None:
        await _create_tables(db)
        await _create_indexes(db)
        await _seed_defaults(db)
        await _run_migrations(db)
        # Simulate an install at schema 50: old stored values, no speech_s, no guard setting.
        for key, value in values.items():
            await db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
        await db.execute("DELETE FROM settings WHERE key = 'voice.speaker_id.enroll_min_similarity'")
        await db.execute("ALTER TABLE voice_samples DROP COLUMN speech_s")
        await db.execute("DELETE FROM schema_version WHERE version >= 51")
        await db.commit()

    async def test_updates_only_old_defaults(self, tmp_path: Path):
        db = await aiosqlite.connect(str(tmp_path / "upgrade.db"))
        try:
            await self._install_at_schema_50(
                db,
                {
                    "voice.speaker_id.margin": "0.05",
                    "voice.speaker_id.min_duration_s": "1.5",
                    "voice.speaker_id.min_enroll_duration_s": "2.5",
                },
            )
            await _run_migrations(db)
            await db.commit()
            values = await _settings(db)
            assert values["voice.speaker_id.margin"] == "0.12"
            assert values["voice.speaker_id.min_duration_s"] == "1.0"
            # Customized value preserved.
            assert values["voice.speaker_id.min_enroll_duration_s"] == "2.5"
            assert values["voice.speaker_id.enroll_min_similarity"] == "0.45"
            cursor = await db.execute("PRAGMA table_info(voice_samples)")
            assert "speech_s" in {row[1] for row in await cursor.fetchall()}
            cursor = await db.execute("SELECT MAX(version) FROM schema_version")
            assert (await cursor.fetchone())[0] >= 51
        finally:
            await shutdown_aiosqlite(db)

    async def test_custom_margin_untouched_and_idempotent(self, tmp_path: Path):
        db = await aiosqlite.connect(str(tmp_path / "custom.db"))
        try:
            await self._install_at_schema_50(
                db,
                {
                    "voice.speaker_id.margin": "0.15",
                    "voice.speaker_id.min_duration_s": "2.0",
                    "voice.speaker_id.min_enroll_duration_s": "2.0",
                },
            )
            await _run_migrations(db)
            await db.execute("UPDATE settings SET value = '0.3' WHERE key = 'voice.speaker_id.enroll_min_similarity'")
            await db.commit()
            first = await _settings(db)
            # Re-running is a no-op.
            await db.execute("DELETE FROM schema_version WHERE version >= 51")
            await _run_migrations(db)
            await db.commit()
            assert await _settings(db) == first
            assert first["voice.speaker_id.margin"] == "0.15"
            assert first["voice.speaker_id.min_duration_s"] == "2.0"
            assert first["voice.speaker_id.min_enroll_duration_s"] == "1.5"
            assert first["voice.speaker_id.enroll_min_similarity"] == "0.3"
        finally:
            await shutdown_aiosqlite(db)

    async def test_fresh_install_gets_new_defaults(self, tmp_path: Path):
        db = await aiosqlite.connect(str(tmp_path / "fresh.db"))
        try:
            await _create_tables(db)
            await _create_indexes(db)
            await _seed_defaults(db)
            await _run_migrations(db)
            await db.commit()
            values = await _settings(db)
            assert values["voice.speaker_id.margin"] == "0.12"
            assert values["voice.speaker_id.min_duration_s"] == "1.0"
            assert values["voice.speaker_id.min_enroll_duration_s"] == "1.5"
            assert values["voice.speaker_id.enroll_min_similarity"] == "0.45"
        finally:
            await shutdown_aiosqlite(db)


# ---------------------------------------------------------------------------
# VAD model provisioning
# ---------------------------------------------------------------------------


class TestVadModelProvisioning:
    async def test_existing_vad_model_used_without_download(self, tmp_path, monkeypatch):
        image_dir = tmp_path / "image"
        monkeypatch.setattr(voice_model, "IMAGE_MODEL_DIR", image_dir)
        monkeypatch.setattr(voice_model.settings, "sqlite_db_path", str(tmp_path / "data" / "agent_assist.db"))
        image_dir.mkdir(parents=True)
        (image_dir / voice_model.VAD_MODEL_FILENAME).write_bytes(b"vad")
        download = AsyncMock()
        monkeypatch.setattr(voice_model, "download_vad_model", download)
        assert await _ORIGINAL_ENSURE_VAD() == image_dir / voice_model.VAD_MODEL_FILENAME
        download.assert_not_awaited()

    async def test_vad_download_uses_the_verified_path(self, tmp_path, monkeypatch):
        captured: dict[str, object] = {}

        async def fake_download(url, target, *, expected_size, expected_sha256, label):
            captured.update(url=url, size=expected_size, sha=expected_sha256, label=label, target=target)
            return target

        monkeypatch.setattr(voice_model, "_download_verified", fake_download)
        monkeypatch.setattr(voice_model.settings, "sqlite_db_path", str(tmp_path / "agent_assist.db"))
        target = await voice_model.download_vad_model()
        assert target == tmp_path / "models" / "speaker" / voice_model.VAD_MODEL_FILENAME
        assert captured["url"] == voice_model.VAD_MODEL_URL
        assert captured["size"] == voice_model.VAD_MODEL_SIZE_BYTES
        assert captured["sha"] == voice_model.VAD_MODEL_SHA256


# ---------------------------------------------------------------------------
# Review fixes: VAD readiness, deferred rejections, empty speech, near ties
# ---------------------------------------------------------------------------


class TestEnrollmentVadReadiness:
    async def test_pending_vad_stores_nothing(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        engine = FakeEngine([[1, 0]])
        vad = FakeVad(2.0, state="pending")
        service = SpeakerIdService(engine=engine, vad=vad)
        service.enrollment.arm(anna["id"], "Anna", samples=2, timeout_s=60)
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config())
        assert analysis.pending is None and analysis.rejection is None
        assert analysis.result["status"] == "unavailable" and analysis.result["reason"] == "vad_not_ready"
        assert engine.pcm_lengths == [] and vad.calls == 0 and vad.warm_ups == 1
        armed = service.enrollment.current()
        assert armed["remaining"] == 2 and armed["rejected"] == 0

    async def test_failed_vad_stores_untrimmed_sample_flagged_pre_vad(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        service = SpeakerIdService(engine=FakeEngine([[1, 0]]), vad=FakeVad(None, vad=False))
        service.enrollment.arm(anna["id"], "Anna", samples=2, timeout_s=60)
        result = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert result["status"] == "enrolled_sample" and result["vad"] is False
        rows = await VoiceProfileRepository.list_samples(anna["id"])
        assert len(rows) == 1 and rows[0]["speech_s"] is None
        assert service.enrollment.current()["remaining"] == 1

    async def test_real_trimmer_states(self):
        injected = VoiceActivityTrimmer(
            detector_factory=lambda path: pytest.fail("no detector"),
            model_path_resolver=AsyncMock(side_effect=RuntimeError("offline")),
        )
        assert injected.state == "pending"
        assert await injected.ensure_ready() is False
        assert injected.state == "failed"
        # Default factory without sherpa-onnx (forced by the autouse fixture): failed right away.
        assert VoiceActivityTrimmer().state == "failed"


class TestDeferredRejection:
    async def test_rejection_recorded_only_on_commit(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        ben = await VoiceProfileRepository.create_profile("Ben")
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0, 0), 3, _MODEL, 2.0, 1.8)
        await VoiceProfileRepository.add_sample(ben["id"], _vec(0, 1, 0), 3, _MODEL, 2.0, 1.8)
        service = SpeakerIdService(engine=FakeEngine([_unit(0.2, 1, 0)]), vad=FakeVad(2.0))
        service.enrollment.arm(anna["id"], "Anna", samples=2, timeout_s=60)
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config())
        assert analysis.pending is None and analysis.rejection is not None
        assert analysis.result["reason"] == "rejected_other_speaker"
        assert service.store.recent() == []
        assert service.enrollment.current()["rejected"] == 0

        service.commit_rejection(analysis.rejection)
        assert [r["status"] for r in service.store.recent()] == ["rejected_sample"]
        armed = service.enrollment.current()
        assert armed["rejected"] == 1 and armed["last_rejection"]["reason"] == "rejected_other_speaker"
        assert armed["remaining"] == 2


class TestEmptySpeech:
    async def test_identification_never_embeds_empty_speech(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0), 2, _MODEL, 2.0)
        engine = FakeEngine([[1, 0]])
        service = SpeakerIdService(engine=engine, vad=FakeVad(0.0))
        result = await service.process_turn(_pcm(2.0), _RATE, _config(min_duration_s=0.0))
        assert result["status"] == "too_short" and result["reason"] == "no_speech"
        assert engine.pcm_lengths == []

    async def test_enrollment_never_embeds_empty_speech(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        engine = FakeEngine([[1, 0]])
        service = SpeakerIdService(engine=engine, vad=FakeVad(0.0))
        service.enrollment.arm(anna["id"], "Anna", samples=2, timeout_s=60)
        result = await service.process_turn(_pcm(2.0), _RATE, _config(min_enroll_duration_s=0.0))
        assert result["status"] == "too_short" and result["reason"] == "rejected_too_short"
        assert engine.pcm_lengths == []
        assert await VoiceProfileRepository.count_samples(anna["id"]) == 0


class TestRunnerUpOnlyWhenIdentified:
    async def test_identified_names_the_runner_up(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        ben = await VoiceProfileRepository.create_profile("Ben")
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0, 0), 3, _MODEL, 2.0)
        await VoiceProfileRepository.add_sample(ben["id"], _vec(0, 1, 0), 3, _MODEL, 2.0)
        service = SpeakerIdService(engine=FakeEngine([_unit(1, 0.2, 0)]), vad=FakeVad(1.5))
        result = await service.process_turn(_pcm(2.0), _RATE, _config())
        assert result["status"] == "identified"
        assert result["second_name"] == "Ben" and result["second_score"] == pytest.approx(0.1961, abs=1e-3)

    async def test_enrollment_results_name_no_runner_up(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        ben = await VoiceProfileRepository.create_profile("Ben")
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0, 0), 3, _MODEL, 2.0, 1.8)
        await VoiceProfileRepository.add_sample(ben["id"], _vec(0, 1, 0), 3, _MODEL, 2.0, 1.8)
        service = SpeakerIdService(engine=FakeEngine([_unit(1, 0.1, 0)]), vad=FakeVad(2.0))
        service.enrollment.arm(anna["id"], "Anna", samples=2, timeout_s=60)
        result = await service.process_turn(_pcm(2.5), _RATE, _config())
        assert result["status"] == "enrolled_sample"
        assert result["second_name"] is None and len(result["scores"]) == 2


class TestNearTieGuard:
    @staticmethod
    def _check(margin: float):
        own_and_other = np.array([0.6, 0.56, float(np.sqrt(1 - 0.6**2 - 0.56**2))])
        return check_enrollment_sample(
            own_and_other,
            1,
            [_centroid(1, "Anna", 1, 0, 0), _centroid(2, "Ben", 0, 1, 0)],
            threshold=0.55,
            margin=margin,
            min_similarity=0.45,
        )

    def test_near_tie_rejected(self):
        # Own 0.60 vs Ben 0.56: a lead of 0.04 is below half the default margin (0.06).
        assert self._check(0.12).reason == REJECTED_OTHER_SPEAKER

    def test_clear_lead_accepted(self):
        # Half of margin 0.05 is 0.025: a lead of 0.04 is enough.
        assert self._check(0.05).reason is None


class TestTrimmerRobustness:
    async def test_other_sample_rate_is_not_trimmed(self):
        calls: list[int] = []

        def factory(path: Path):
            def detect(waveform: np.ndarray):
                calls.append(len(waveform))
                return [(0, 10)]

            return detect

        trimmer = VoiceActivityTrimmer(detector_factory=factory, model_path_resolver=AsyncMock(return_value=Path("m")))
        assert await trimmer.ensure_ready()
        trim = await trimmer.trim(bytes(2 * 8000), 8000)
        assert trim.vad is False and trim.speech_s == pytest.approx(1.0) and calls == []

    async def test_failed_load_deletes_only_the_data_copy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(voice_model.settings, "sqlite_db_path", str(tmp_path / "data" / "agent_assist.db"))
        data_copy = voice_model.data_model_dir() / voice_model.VAD_MODEL_FILENAME
        data_copy.parent.mkdir(parents=True)
        data_copy.write_bytes(b"corrupt")
        image_copy = tmp_path / "image" / voice_model.VAD_MODEL_FILENAME
        image_copy.parent.mkdir(parents=True)
        image_copy.write_bytes(b"corrupt")

        def failing_factory(path: Path):
            raise RuntimeError("invalid model")

        from_data = VoiceActivityTrimmer(
            detector_factory=failing_factory, model_path_resolver=AsyncMock(return_value=data_copy)
        )
        assert await from_data.ensure_ready() is False
        assert not data_copy.exists()
        assert from_data.state == "failed" and "invalid model" in (from_data.last_error or "")
        # Backoff kept: no immediate retry.
        assert await from_data.ensure_ready() is False

        from_image = VoiceActivityTrimmer(
            detector_factory=failing_factory, model_path_resolver=AsyncMock(return_value=image_copy)
        )
        assert await from_image.ensure_ready() is False
        assert image_copy.exists()
