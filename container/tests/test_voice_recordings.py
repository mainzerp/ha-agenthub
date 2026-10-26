"""Recorded voice turns: migration 53, repository, capture, trace link, retention and admin API."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import httpx
import numpy as np
import pytest
from fastapi import HTTPException

from app.analytics.tracer import SpanCollector
from app.api.routes import voice as voice_routes
from app.api.routes.admin import _voice as voice_admin
from app.api.routes.admin._settings import _validate_setting_value
from app.bootstrap import _voice as voice_bootstrap
from app.db.repository import SettingsRepository, VoiceProfileRepository, VoiceTurnRepository
from app.db.schema import get_db_write as real_get_db_write
from app.db.schema._migrations import _migrate_to_53
from app.models.agent import TaskContext
from app.voice import service as voice_service
from app.voice.recordings import (
    TurnRecording,
    audio_file_path,
    cleanup_recordings,
    new_turn_id,
    read_wav_pcm,
    recordings_dir,
    store_recording,
)
from app.voice.service import SpeakerIdService, VoiceConfig, attach_speaker_to_task, load_voice_config
from app.voice.speaker_engine import SpeakerEngineUnavailableError
from app.voice.vad import SpeechTrim
from tests.conftest import build_integration_test_app
from tests.helpers import make_ingress_task, shutdown_aiosqlite
from tests.test_voice_ws import _make_ws, _results, _start, _text

_MODEL = "test-model"
_RATE = 16000


class FakeEngine:
    """Stands in for SpeakerEngine: returns queued vectors (or a fixed one), no sherpa-onnx needed."""

    def __init__(self, vectors: list[list[float]] | None = None, *, status: str = "ready") -> None:
        self.model_id = _MODEL
        self.status = status
        self.last_error = None
        self.vectors = list(vectors or [])
        self.calls = 0
        self.unavailable = False

    async def ensure_ready(self, num_threads: int = 2) -> bool:
        return self.status == "ready"

    def schedule_reload(self, num_threads: int) -> bool:
        return True

    async def embed(self, pcm: bytes, sample_rate: int, *, num_threads: int = 2) -> np.ndarray:
        self.calls += 1
        if self.unavailable:
            raise SpeakerEngineUnavailableError("missing")
        return np.array(self.vectors.pop(0), dtype=np.float32)


def _config(**overrides) -> VoiceConfig:
    values = {
        "enabled": True,
        "threshold": 0.55,
        "margin": 0.05,
        "min_duration_s": 1.0,
        "min_enroll_duration_s": 1.5,
        "wait_ms": 500,
        "num_threads": 2,
        "keep_audio": True,
    }
    values.update(overrides)
    return VoiceConfig(**values)


def _pcm(seconds: float) -> bytes:
    return b"\x10\x00" * int(_RATE * seconds)


def _vec(*values: float) -> bytes:
    return np.array(values, dtype=np.float32).tobytes()


async def _record(
    *,
    status: str = "unknown",
    vector: tuple[float, ...] = (0.0, 1.0, 0.0),
    speech_s: float | None = 2.5,
    model: str = _MODEL,
    profile_id: int | None = None,
    conversation_id: str | None = "conv-1",
) -> str:
    turn_id = new_turn_id()
    stored = await store_recording(
        TurnRecording(
            turn_id=turn_id,
            pcm=_pcm(2.5),
            sample_rate=_RATE,
            vector=np.array(vector, dtype=np.float32),
            model_id=model,
            status=status,
            conversation_id=conversation_id,
            duration_s=3.0,
            speech_s=speech_s,
            profile_id=profile_id,
            score=0.4,
            margin=0.1,
        )
    )
    assert stored
    return turn_id


async def _insert_trace(db_path, trace_id: str, user_input: str) -> None:
    db = await aiosqlite.connect(str(db_path))
    try:
        await db.execute(
            "INSERT INTO trace_summary (trace_id, conversation_id, user_input) VALUES (?, ?, ?)",
            (trace_id, "conv-1", user_input),
        )
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)


@pytest.fixture(autouse=True)
def _reset_voice_service():
    voice_service.set_speaker_service(None)
    yield
    voice_service.set_speaker_service(None)


@pytest.fixture()
async def recording_enabled(db_repository):
    """Speaker identification and recording on in the settings (re-checked when a recording is stored)."""
    await SettingsRepository.set("voice.speaker_id.enabled", "true", "bool", "voice")
    await SettingsRepository.set("voice.speaker_id.keep_audio", "true", "bool", "voice")


async def _drain(service: SpeakerIdService) -> None:
    await service.wait_recordings()


# ---------------------------------------------------------------------------
# Migration 53 and settings
# ---------------------------------------------------------------------------


class TestMigrationAndSettings:
    async def test_migration_53_adds_table_indexes_and_settings(self, db_repository):
        db = await aiosqlite.connect(str(db_repository))
        try:
            await db.execute("DROP TABLE voice_turns")
            await db.execute("DELETE FROM settings WHERE key LIKE 'voice.speaker_id.%audio%'")
            await db.commit()
            await _migrate_to_53(db)
            await _migrate_to_53(db)  # idempotent
            await db.commit()
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%voice_turns%' ORDER BY name")
            names = {row[0] for row in await cursor.fetchall()}
            assert {"voice_turns", "idx_voice_turns_created_at", "idx_voice_turns_trace_id"} <= names
            cursor = await db.execute("SELECT key, value, value_type FROM settings WHERE key LIKE '%audio%'")
            rows = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}
            assert rows == {
                "voice.speaker_id.keep_audio": ("false", "bool"),
                "voice.speaker_id.audio_retention_days": ("30", "int"),
            }
            cursor = await db.execute("SELECT COUNT(*) FROM schema_version WHERE version = 53")
            assert (await cursor.fetchone())[0] == 1
        finally:
            await shutdown_aiosqlite(db)

    async def test_defaults_and_config(self, db_repository):
        assert await SettingsRepository.get_value("voice.speaker_id.keep_audio") == "false"
        assert await SettingsRepository.get_value("voice.speaker_id.audio_retention_days") == "30"
        config = await load_voice_config()
        assert config.keep_audio is False and config.audio_retention_days == 30
        await SettingsRepository.set("voice.speaker_id.keep_audio", "true", "bool", "voice")
        await SettingsRepository.set("voice.speaker_id.audio_retention_days", "9999", "int", "voice")
        config = await load_voice_config()
        assert config.keep_audio is True and config.audio_retention_days == 365

    @pytest.mark.parametrize("value", ["0", "366", "abc"])
    def test_retention_out_of_range_rejected(self, value):
        with pytest.raises(HTTPException):
            _validate_setting_value("voice.speaker_id.audio_retention_days", value, "int")

    @pytest.mark.parametrize("value", ["1", "365"])
    def test_retention_bounds_accepted(self, value):
        _validate_setting_value("voice.speaker_id.audio_retention_days", value, "int")


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class TestVoiceTurnRepository:
    async def test_crud_filters_and_transcript(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        identified = await _record(status="identified", profile_id=anna["id"], vector=(1.0, 0.0, 0.0))
        unknown = await _record(status="unknown")
        assert await VoiceTurnRepository.count() == 2

        assert await VoiceTurnRepository.set_trace_id(identified, "trace-1")
        assert not await VoiceTurnRepository.set_trace_id(new_turn_id(), "trace-x")
        await _insert_trace(db_repository, "trace-1", "turn on the light")

        rows, total = await VoiceTurnRepository.list_turns()
        assert total == 2
        by_id = {row["id"]: row for row in rows}
        assert by_id[identified]["speaker_name"] == "Anna"
        assert by_id[identified]["transcript"] == "turn on the light"
        assert by_id[unknown]["transcript"] is None
        assert "embedding" not in by_id[identified]

        rows, total = await VoiceTurnRepository.list_turns(status="unknown")
        assert total == 1 and rows[0]["id"] == unknown
        rows, total = await VoiceTurnRepository.list_turns(limit=1, offset=1)
        assert total == 2 and len(rows) == 1

        sample_id = await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0, 0), 3, _MODEL, 3.0, 2.5)
        assert await VoiceTurnRepository.mark_assigned(identified, anna["id"], sample_id)
        assert not await VoiceTurnRepository.mark_assigned(identified, anna["id"], sample_id)
        rows, total = await VoiceTurnRepository.list_turns(assigned=False)
        assert total == 1 and rows[0]["id"] == unknown
        rows, _ = await VoiceTurnRepository.list_turns(assigned=True)
        assert rows[0]["assigned_name"] == "Anna"

        turn = await VoiceTurnRepository.get_turn(unknown)
        assert turn is not None and turn["embedding"] == _vec(0, 1, 0) and turn["dim"] == 3
        assert await VoiceTurnRepository.delete_turn(unknown) == f"{unknown}.wav"
        assert await VoiceTurnRepository.delete_turn(unknown) is None
        assert await VoiceTurnRepository.list_audio_paths() == {f"{identified}.wav"}
        assert await VoiceTurnRepository.delete_all() == [f"{identified}.wav"]
        assert await VoiceTurnRepository.count() == 0

    async def test_sample_and_profile_deletion_unlink_assignments(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna")
        ids = [await _record(status="identified", profile_id=anna["id"]) for _ in range(3)]
        samples = [await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0, 0), 3, _MODEL) for _ in ids]
        for turn_id, sample_id in zip(ids, samples, strict=True):
            assert await VoiceTurnRepository.mark_assigned(turn_id, anna["id"], sample_id)

        # One sample deleted: only its turn becomes assignable again.
        assert await VoiceProfileRepository.delete_sample(anna["id"], samples[0])
        assigned = {row["id"] for row in (await VoiceTurnRepository.list_turns(assigned=True))[0]}
        assert assigned == set(ids[1:])
        cleared = await VoiceTurnRepository.get_turn(ids[0])
        assert cleared["assigned_profile_id"] is None and cleared["assigned_at"] is None

        # Reset of the profile's samples: every turn assigned to it is unlinked.
        assert await VoiceProfileRepository.delete_samples(anna["id"]) == 2
        assert (await VoiceTurnRepository.list_turns(assigned=True))[1] == 0

        # Profile deletion: assignment and the identification link are cleared.
        sample_id = await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0, 0), 3, _MODEL)
        assert await VoiceTurnRepository.mark_assigned(ids[0], anna["id"], sample_id)
        assert await VoiceProfileRepository.delete_profile(anna["id"])
        for turn_id in ids:
            turn = await VoiceTurnRepository.get_turn(turn_id)
            assert turn["assigned_profile_id"] is None and turn["assigned_sample_id"] is None
            assert turn["speaker_profile_id"] is None


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class _FakeCentroids:
    def __init__(self, centroids):
        self.centroids = centroids

    async def get(self, model: str):
        return self.centroids

    def invalidate(self) -> None:
        pass


def _capture_service(vectors: list[list[float]]) -> SpeakerIdService:
    from app.voice.profiles import ProfileCentroid, l2_normalize

    centroids = _FakeCentroids(
        [
            ProfileCentroid(1, "Anna", "user-anna", l2_normalize(np.array([1, 0, 0], dtype=np.float32))),
            ProfileCentroid(2, "Ben", None, l2_normalize(np.array([0, 1, 0], dtype=np.float32))),
        ]
    )
    return SpeakerIdService(engine=FakeEngine(vectors), centroids=centroids)


@pytest.mark.usefixtures("recording_enabled")
class TestCapture:
    async def test_identified_turn_is_recorded(self, db_repository):
        service = _capture_service([[0.98, 0.05, 0.0]])
        pcm = _pcm(2.5)
        analysis = await service.analyze_turn(pcm, _RATE, _config(), conversation_id="conv-7")
        result = analysis.result
        assert result["status"] == "identified"
        turn_id = result["voice_turn_id"]
        await service.wait_recordings()

        turn = await VoiceTurnRepository.get_turn(turn_id)
        assert turn is not None
        assert turn["status"] == "identified" and turn["speaker_profile_id"] == 1
        assert turn["conversation_id"] == "conv-7" and turn["model"] == _MODEL
        assert np.allclose(np.frombuffer(turn["embedding"], dtype=np.float32), [0.98, 0.05, 0.0])
        path = audio_file_path(turn["audio_path"])
        assert path is not None and path.parent == recordings_dir()
        assert recordings_dir().parent == db_repository.parent
        # The recorded audio is the PCM that was embedded (whole turn without a VAD model).
        recorded, rate = read_wav_pcm(path)
        assert rate == _RATE and recorded == pcm

    async def test_unknown_turn_is_recorded(self, db_repository):
        service = _capture_service([[0.6, 0.6, 0.5]])
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config())
        assert analysis.result["status"] == "unknown"
        await service.wait_recordings()
        turn = await VoiceTurnRepository.get_turn(analysis.result["voice_turn_id"])
        assert turn["status"] == "unknown" and turn["speaker_profile_id"] is None

    @pytest.mark.parametrize(
        ("config", "record", "seconds"),
        [
            (_config(keep_audio=False), True, 2.5),
            (_config(), False, 2.5),
            (_config(), True, 0.5),
        ],
        ids=["keep_audio_off", "enrollment_turn", "too_short"],
    )
    async def test_turns_not_recorded(self, db_repository, config, record, seconds):
        service = _capture_service([[0.98, 0.05, 0.0]])
        analysis = await service.analyze_turn(_pcm(seconds), _RATE, config, record=record)
        await service.wait_recordings()
        assert "voice_turn_id" not in analysis.result
        assert await VoiceTurnRepository.count() == 0

    async def test_armed_enrollment_turn_is_not_recorded(self, db_repository):
        profile = await VoiceProfileRepository.create_profile("Anna")
        service = _capture_service([[0.98, 0.05, 0.0]])
        service.enrollment.arm(profile["id"], "Anna", 3, 300)
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config())
        await service.wait_recordings()
        assert analysis.pending is not None
        assert await VoiceTurnRepository.count() == 0

    async def test_storage_failure_is_swallowed_and_cleans_up(self, db_repository):
        service = _capture_service([[0.98, 0.05, 0.0]])
        with patch.object(VoiceTurnRepository, "add_turn", AsyncMock(side_effect=aiosqlite.OperationalError("x"))):
            analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config())
            await service.wait_recordings()
        assert analysis.result["status"] == "identified"
        assert not list(recordings_dir().glob("*.wav"))

    async def test_ws_session_passes_conversation_and_enrollment_flag(self, db_repository):
        service = _capture_service([[0.98, 0.05, 0.0], [0.98, 0.05, 0.0]])
        spy = AsyncMock(wraps=service.analyze_turn)
        with (
            patch.object(service, "analyze_turn", spy),
            patch.object(voice_routes, "load_voice_config", AsyncMock(return_value=_config())),
        ):
            ws = _make_ws([_start(), {"type": "websocket.receive", "bytes": _pcm(2.5)}, _text({"type": "end"})])
            await voice_routes.VoiceTurnSession(ws, service).run()
            await service.wait_recordings()
            assert spy.await_args.kwargs["conversation_id"] == "conv-1"
            assert spy.await_args.kwargs["record"] is True
            sent = _results(ws)[0]
            assert sent["status"] == "identified" and "voice_turn_id" not in sent
            assert await VoiceTurnRepository.count() == 1

            profile = await VoiceProfileRepository.create_profile("Ben")
            service.enrollment.arm(profile["id"], "Ben", 3, 300)
            ws = _make_ws([_start("conv-2"), {"type": "websocket.receive", "bytes": _pcm(2.5)}, _text({"type": "end"})])
            await voice_routes.VoiceTurnSession(ws, service).run()
            assert spy.await_args.kwargs["record"] is False


# ---------------------------------------------------------------------------
# Trace link
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("recording_enabled")
class TestTraceLink:
    async def test_lookup_links_recorded_turn_to_trace(self, db_repository):
        service = _capture_service([[0.98, 0.05, 0.0]])
        voice_service.set_speaker_service(service)
        token = service.store.begin("conv-1")
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config(), conversation_id="conv-1")
        service.store.resolve("conv-1", token, analysis.result)
        turn_id = analysis.result["voice_turn_id"]

        task = make_ingress_task("hi", "conv-1", TaskContext(user_id="ha-owner"))
        with patch.object(voice_service, "load_voice_config", AsyncMock(return_value=_config())):
            # The recording may still be in flight: the link waits for its row.
            await attach_speaker_to_task(task, "conv-1", SpanCollector("trace-42", source="ha"))
        await service.wait_recordings()
        turn = await VoiceTurnRepository.get_turn(turn_id)
        assert turn["trace_id"] == "trace-42"
        assert task.context.speaker is not None and task.context.speaker.name == "Anna"

    async def test_link_failure_is_swallowed(self, db_repository):
        service = _capture_service([])
        with patch.object(VoiceTurnRepository, "set_trace_id", AsyncMock(side_effect=RuntimeError("db"))):
            service.link_recording_trace(new_turn_id(), "trace-1")
            await service.wait_recordings()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetention:
    async def test_cleanup_expires_rows_and_orphans(self, db_repository):
        old = await _record()
        fresh = await _record()
        db = await aiosqlite.connect(str(db_repository))
        try:
            stamp = (datetime.now(UTC) - timedelta(days=40)).isoformat()
            await db.execute("UPDATE voice_turns SET created_at = ? WHERE id = ?", (stamp, old))
            await db.commit()
        finally:
            await shutdown_aiosqlite(db)
        directory = recordings_dir()
        old_orphan = directory / f"{new_turn_id()}.wav"
        new_orphan = directory / f"{new_turn_id()}.wav"
        partial = directory / ".abc.wav.1.part"
        unrelated = directory / "notes.txt"
        for path in (old_orphan, new_orphan, partial, unrelated):
            path.write_bytes(b"x")
        two_hours_ago = time.time() - 7200
        for path in (old_orphan, partial, unrelated):
            os.utime(path, (two_hours_ago, two_hours_ago))

        removed = await cleanup_recordings(30)

        assert removed == {"expired": 1, "orphans": 2}
        assert await VoiceTurnRepository.get_turn(old) is None
        assert not (directory / f"{old}.wav").exists()
        assert (directory / f"{fresh}.wav").exists()
        assert not old_orphan.exists() and not partial.exists()
        # Within the grace period (a recording writes its file before its row) and foreign files stay.
        assert new_orphan.exists() and unrelated.exists()

    async def test_cleanup_without_directory(self, db_repository):
        assert await cleanup_recordings(30) == {"expired": 0, "orphans": 0}

    async def test_retention_loop_reads_setting(self, db_repository):
        await SettingsRepository.set("voice.speaker_id.audio_retention_days", "7", "int", "voice")
        cleanup = AsyncMock(return_value={"expired": 1, "orphans": 0})
        with (
            patch.object(voice_bootstrap, "cleanup_recordings", cleanup),
            patch.object(voice_bootstrap.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])),
            pytest.raises(asyncio.CancelledError),
        ):
            await voice_bootstrap.run_recording_retention()
        cleanup.assert_awaited_once_with(7)

    async def test_retention_loop_survives_failures(self, db_repository):
        cleanup = AsyncMock(side_effect=[RuntimeError("boom"), {"expired": 0, "orphans": 0}])
        sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        with (
            patch.object(voice_bootstrap, "cleanup_recordings", cleanup),
            patch.object(voice_bootstrap.asyncio, "sleep", sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            await voice_bootstrap.run_recording_retention()
        assert cleanup.await_count == 2
        # The first pass waits for the startup delay.
        assert sleep.await_args_list[0].args == (voice_bootstrap.RECORDING_RETENTION_STARTUP_DELAY_S,)

    async def test_startup_delay_keeps_the_first_pass_off_the_database(self, db_repository):
        cleanup = AsyncMock()
        with patch.object(voice_bootstrap, "cleanup_recordings", cleanup):
            task = asyncio.create_task(voice_bootstrap.run_recording_retention())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        cleanup.assert_not_awaited()

    async def test_setup_voice_registers_retention_task_once(self, db_repository):
        app = SimpleNamespace(state=SimpleNamespace())
        release = asyncio.Event()

        async def _blocking_retention() -> None:
            await release.wait()

        with patch.object(voice_bootstrap, "run_recording_retention", _blocking_retention):
            await voice_bootstrap.setup_voice(app)
            task = app.state.voice_retention_task
            await voice_bootstrap.setup_voice(app)
            # A running retention task is never duplicated.
            assert app.state._background_tasks == [task]
            release.set()
            await task


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


@pytest.fixture()
async def admin_client(db_repository):
    service = SpeakerIdService(engine=FakeEngine())
    voice_service.set_speaker_service(service)
    app = build_integration_test_app(override_admin_session=True)
    with patch("app.db.repository.SetupStateRepository.is_complete", new_callable=AsyncMock, return_value=True):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client, service


async def _two_profiles() -> tuple[int, int]:
    anna = await VoiceProfileRepository.create_profile("Anna")
    ben = await VoiceProfileRepository.create_profile("Ben")
    await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0, 0), 3, _MODEL, 3.0, 2.5)
    await VoiceProfileRepository.add_sample(ben["id"], _vec(0, 1, 0), 3, _MODEL, 3.0, 2.5)
    return anna["id"], ben["id"]


class TestVoiceTurnsApi:
    async def test_list_status_and_audio(self, admin_client, db_repository):
        client, _service = admin_client
        empty = (await client.get("/api/admin/voice/turns")).json()
        assert empty["turns"] == [] and empty["total"] == 0 and empty["keep_audio"] is False
        status = (await client.get("/api/admin/voice/status")).json()
        assert status["keep_audio"] is False and status["audio_retention_days"] == 30
        assert status["recorded_turns"] == 0

        anna, _ben = await _two_profiles()
        identified = await _record(status="identified", profile_id=anna, vector=(1.0, 0.0, 0.0))
        short = await _record(status="unknown", speech_s=0.5)
        await VoiceTurnRepository.set_trace_id(identified, "trace-9")
        await _insert_trace(db_repository, "trace-9", "what time is it")

        listed = (await client.get("/api/admin/voice/turns")).json()
        assert listed["total"] == 2 and listed["min_enroll_duration_s"] == 1.5
        by_id = {item["id"]: item for item in listed["turns"]}
        assert by_id[identified]["transcript"] == "what time is it"
        assert by_id[identified]["speaker_name"] == "Anna" and by_id[identified]["assignable"] is True
        assert by_id[short]["assignable"] is False and by_id[short]["transcript"] is None
        assert "embedding" not in by_id[identified]
        unknown_only = (await client.get("/api/admin/voice/turns", params={"status": "unknown"})).json()
        assert [item["id"] for item in unknown_only["turns"]] == [short]
        assert (await client.get("/api/admin/voice/turns", params={"status": "bogus"})).status_code == 422
        assert (await client.get("/api/admin/voice/turns", params={"limit": 0})).status_code == 422
        assert (await client.get("/api/admin/voice/status")).json()["recorded_turns"] == 2

        audio = await client.get(f"/api/admin/voice/turns/{identified}/audio")
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"
        assert audio.content[:4] == b"RIFF"
        assert audio.content == (recordings_dir() / f"{identified}.wav").read_bytes()
        assert (await client.get(f"/api/admin/voice/turns/{new_turn_id()}/audio")).status_code == 404
        # A non-hex id never reaches the database or the file system.
        assert (await client.get("/api/admin/voice/turns/not-a-hex-id..wav/audio")).status_code == 404
        (recordings_dir() / f"{short}.wav").unlink()
        assert (await client.get(f"/api/admin/voice/turns/{short}/audio")).status_code == 404

    async def test_assign_success_keeps_armed_enrollment(self, admin_client):
        client, service = admin_client
        anna, ben = await _two_profiles()
        turn_id = await _record(vector=(0.98, 0.05, 0.0))
        service.enrollment.arm(ben, "Ben", 3, 300)

        assigned = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna})
        assert assigned.status_code == 200
        body = assigned.json()
        assert body["forced"] is False and body["recomputed"] is False and body["guard"]["reason"] is None
        assert await VoiceProfileRepository.count_samples(anna) == 2
        turn = await VoiceTurnRepository.get_turn(turn_id)
        assert turn["assigned_profile_id"] == anna and turn["assigned_sample_id"] == body["sample_id"]
        # The armed enrollment is untouched.
        armed = service.enrollment.current()
        assert armed is not None and armed["profile_id"] == ben and armed["remaining"] == 3

        again = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna})
        assert again.status_code == 409 and again.json()["error"] == "already_assigned"
        listed = (await client.get("/api/admin/voice/turns", params={"assigned": "false"})).json()
        assert listed["total"] == 0

    async def test_guard_rejection_and_force(self, admin_client):
        client, _service = admin_client
        anna, _ben = await _two_profiles()
        turn_id = await _record(vector=(0.0, 1.0, 0.0))

        rejected = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna})
        assert rejected.status_code == 409
        body = rejected.json()
        assert body["error"] == "guard_rejected" and body["reason"] == "rejected_other_speaker"
        assert body["scores"]["best_other_name"] == "Ben"
        assert body["scores"]["own"] == pytest.approx(0.0, abs=1e-4)
        assert body["detail"]
        assert await VoiceProfileRepository.count_samples(anna) == 1

        forced = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna, "force": True})
        assert forced.status_code == 200
        assert forced.json()["forced"] is True and forced.json()["guard"]["reason"] == "rejected_other_speaker"
        assert await VoiceProfileRepository.count_samples(anna) == 2

    async def test_assign_errors(self, admin_client):
        client, _service = admin_client
        anna, _ben = await _two_profiles()
        short = await _record(speech_s=0.5)
        ok_turn = await _record(vector=(1.0, 0.0, 0.0))

        too_short = await client.post(f"/api/admin/voice/turns/{short}/assign", json={"profile_id": anna})
        assert too_short.status_code == 422 and too_short.json()["error"] == "too_short"
        missing_profile = await client.post(f"/api/admin/voice/turns/{ok_turn}/assign", json={"profile_id": 999})
        assert missing_profile.status_code == 404
        missing_turn = await client.post(f"/api/admin/voice/turns/{new_turn_id()}/assign", json={"profile_id": anna})
        assert missing_turn.status_code == 404

    async def test_model_mismatch_recomputes_from_audio(self, admin_client):
        client, service = admin_client
        anna, _ben = await _two_profiles()
        turn_id = await _record(vector=(0.0, 0.0, 1.0), model="old-model")

        service.engine.status = "loading"
        with patch.object(service, "schedule_warm_up") as warm_up:
            unavailable = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna})
        assert unavailable.status_code == 503 and unavailable.json()["error"] == "engine_unavailable"
        warm_up.assert_called_once()

        service.engine.status = "ready"
        service.engine.vectors = [[0.97, 0.1, 0.0]]
        assigned = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna})
        assert assigned.status_code == 200
        assert assigned.json()["recomputed"] is True and service.engine.calls == 1
        samples = await VoiceProfileRepository.list_samples(anna)
        stored = samples[-1]
        assert stored["model"] == _MODEL
        assert np.allclose(np.frombuffer(stored["embedding"], dtype=np.float32), [0.97, 0.1, 0.0])

    async def test_model_mismatch_without_audio(self, admin_client):
        client, _service = admin_client
        anna, _ben = await _two_profiles()
        turn_id = await _record(model="old-model")
        (recordings_dir() / f"{turn_id}.wav").unlink()
        response = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna})
        assert response.status_code == 404 and response.json()["error"] == "audio_missing"

    async def test_delete_one_and_all(self, admin_client):
        client, _service = admin_client
        first = await _record()
        second = await _record()
        third = await _record()

        assert (await client.delete(f"/api/admin/voice/turns/{first}")).status_code == 200
        assert not (recordings_dir() / f"{first}.wav").exists()
        assert (await client.delete(f"/api/admin/voice/turns/{first}")).status_code == 404
        assert (await client.delete("/api/admin/voice/turns/not-an-id")).status_code == 404

        deleted = await client.delete("/api/admin/voice/turns")
        assert deleted.status_code == 200 and deleted.json()["deleted"] == 2
        assert not (recordings_dir() / f"{second}.wav").exists()
        assert not (recordings_dir() / f"{third}.wav").exists()
        assert await VoiceTurnRepository.count() == 0


# ---------------------------------------------------------------------------
# Review follow-ups: unconfirmed turns, zero profiles, store-time check,
# profile deletion, trace fallback, assign races, auth, write connection
# ---------------------------------------------------------------------------


class _ShortVad:
    """VAD stand-in that keeps only ``speech_s`` seconds of speech."""

    state = "ready"

    def __init__(self, speech_s: float) -> None:
        self.speech_s = speech_s

    async def trim(self, pcm: bytes, sample_rate: int) -> SpeechTrim:
        kept = pcm[: int(self.speech_s * sample_rate) * 2]
        return SpeechTrim(pcm=kept, speech_s=self.speech_s, vad=True, embedded_s=self.speech_s)

    def schedule_warm_up(self) -> None:
        pass


def _paced_ws(messages: list[dict]) -> MagicMock:
    """A socket whose frames arrive 20 ms apart, so the analysis finishes between them."""
    ws = _make_ws([])
    pending = list(messages)

    async def receive() -> dict:
        await asyncio.sleep(0.02)
        if pending:
            return pending.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    ws.receive = receive
    return ws


def _audio_frame(seconds: float = 2.5) -> dict:
    return {"type": "websocket.receive", "bytes": _pcm(seconds)}


@pytest.mark.usefixtures("recording_enabled")
class TestUnconfirmedTurns:
    @pytest.mark.parametrize(
        "tail",
        [[_text({"type": "abort"})], []],
        ids=["abort", "disconnect"],
    )
    async def test_unconfirmed_turn_recording_is_deleted(self, db_repository, tail):
        service = _capture_service([[0.98, 0.05, 0.0]])
        with (
            patch.object(voice_routes, "load_voice_config", AsyncMock(return_value=_config())),
            patch.object(service, "discard_recording", wraps=service.discard_recording) as discard,
        ):
            ws = _paced_ws([_start(), _audio_frame(), _text({"type": "audio_end"}), *tail])
            await voice_routes.VoiceTurnSession(ws, service).run()
            await _drain(service)
        discard.assert_called_once()
        assert await VoiceTurnRepository.count() == 0
        assert not list(recordings_dir().glob("*.wav"))

    async def test_confirmed_turn_recording_stays(self, db_repository):
        service = _capture_service([[0.98, 0.05, 0.0]])
        with patch.object(voice_routes, "load_voice_config", AsyncMock(return_value=_config())):
            ws = _paced_ws([_start(), _audio_frame(), _text({"type": "audio_end"}), _text({"type": "end"})])
            await voice_routes.VoiceTurnSession(ws, service).run()
            await _drain(service)
        assert await VoiceTurnRepository.count() == 1
        assert len(list(recordings_dir().glob("*.wav"))) == 1

    async def test_discard_waits_for_the_store_task(self, db_repository):
        service = _capture_service([[0.98, 0.05, 0.0]])
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config())
        # Discarded while the store task is still in flight.
        service.discard_recording(analysis.result["voice_turn_id"])
        await _drain(service)
        assert await VoiceTurnRepository.count() == 0
        assert not list(recordings_dir().glob("*.wav"))


class TestZeroProfilesAndStoreTimeCheck:
    @pytest.mark.usefixtures("recording_enabled")
    async def test_turn_without_profiles_is_recorded(self, db_repository):
        engine = FakeEngine([[0.3, 0.4, 0.5]])
        service = SpeakerIdService(engine=engine, centroids=_FakeCentroids([]))
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config(), conversation_id="conv-0")
        result = analysis.result
        # The identification result is unchanged apart from the recording id.
        assert result["status"] == "no_profiles" and result["profile_id"] is None and result["score"] is None
        assert result["compute_ms"] is None
        await _drain(service)
        turn = await VoiceTurnRepository.get_turn(result["voice_turn_id"])
        assert turn["status"] == "no_profiles" and turn["speech_s"] == pytest.approx(2.5)
        assert engine.calls == 1
        # The recent results never carry the recording id.
        assert "voice_turn_id" not in service.store.recent()[0]

    @pytest.mark.usefixtures("recording_enabled")
    async def test_without_recording_no_profiles_skips_embedding(self, db_repository):
        engine = FakeEngine([[0.3, 0.4, 0.5]])
        service = SpeakerIdService(engine=engine, centroids=_FakeCentroids([]))
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config(keep_audio=False))
        assert analysis.result["status"] == "no_profiles" and "voice_turn_id" not in analysis.result
        assert engine.calls == 0

    @pytest.mark.usefixtures("recording_enabled")
    async def test_without_profiles_too_little_speech_is_not_recorded(self, db_repository):
        engine = FakeEngine([[0.3, 0.4, 0.5]])
        service = SpeakerIdService(engine=engine, centroids=_FakeCentroids([]), vad=_ShortVad(0.4))
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config())
        await _drain(service)
        assert analysis.result["status"] == "no_profiles" and "voice_turn_id" not in analysis.result
        assert engine.calls == 0 and await VoiceTurnRepository.count() == 0

    async def test_recording_turned_off_meanwhile_is_not_stored(self, db_repository):
        # The session read ``keep_audio`` on at ``start``; the setting is off by the time of the write.
        await SettingsRepository.set("voice.speaker_id.enabled", "true", "bool", "voice")
        service = _capture_service([[0.98, 0.05, 0.0]])
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config())
        await _drain(service)
        assert analysis.result["status"] == "identified"
        assert await VoiceTurnRepository.count() == 0
        assert not list(recordings_dir().glob("*.wav"))


async def _insert_trace_at(db_path, trace_id: str, conversation_id: str, user_input: str, at: datetime) -> None:
    db = await aiosqlite.connect(str(db_path))
    try:
        await db.execute(
            "INSERT INTO trace_summary (trace_id, conversation_id, user_input, created_at) VALUES (?, ?, ?, ?)",
            (trace_id, conversation_id, user_input, at.strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)


class TestTraceFallback:
    async def test_unlinked_turn_uses_nearest_trace_of_its_conversation(self, db_repository):
        now = datetime.now(UTC)
        turn_id = await _record(conversation_id="conv-f")
        await _insert_trace_at(db_repository, "tr-early", "conv-f", "too early", now - timedelta(seconds=30))
        await _insert_trace_at(db_repository, "tr-match", "conv-f", "turn on the light", now + timedelta(seconds=2))
        await _insert_trace_at(db_repository, "tr-later", "conv-f", "and the fan", now + timedelta(seconds=10))
        await _insert_trace_at(db_repository, "tr-other", "conv-x", "other conversation", now)

        rows, _ = await VoiceTurnRepository.list_turns()
        assert rows[0]["linked_trace_id"] == "tr-match" and rows[0]["transcript"] == "turn on the light"
        assert rows[0]["trace_id"] is None

        # A trace linked to another recorded turn is never borrowed.
        other = await _record(conversation_id="conv-f")
        assert await VoiceTurnRepository.set_trace_id(other, "tr-match")
        by_id = {row["id"]: row for row in (await VoiceTurnRepository.list_turns())[0]}
        assert by_id[turn_id]["linked_trace_id"] == "tr-later"
        assert by_id[other]["transcript"] == "turn on the light"

    async def test_trace_outside_the_window_is_not_used(self, db_repository):
        await _record(conversation_id="conv-y")
        await _insert_trace_at(
            db_repository, "tr-late", "conv-y", "much later", datetime.now(UTC) + timedelta(seconds=120)
        )
        rows, _ = await VoiceTurnRepository.list_turns()
        assert rows[0]["linked_trace_id"] is None and rows[0]["transcript"] is None

    @pytest.mark.usefixtures("recording_enabled")
    async def test_timed_out_lookup_still_shows_the_transcript(self, db_repository):
        service = _capture_service([[0.98, 0.05, 0.0]])
        voice_service.set_speaker_service(service)
        token = service.store.begin("conv-t")
        task = make_ingress_task("hi", "conv-t", TaskContext(user_id="ha-owner"))
        with patch.object(voice_service, "load_voice_config", AsyncMock(return_value=_config(wait_ms=0))):
            # No result yet: the lookup times out and removes the entry.
            await attach_speaker_to_task(task, "conv-t", SpanCollector("trace-t", source="ha"))
        analysis = await service.analyze_turn(_pcm(2.5), _RATE, _config(), conversation_id="conv-t")
        assert not service.store.resolve("conv-t", token, analysis.result)
        await _drain(service)
        await _insert_trace_at(db_repository, "trace-t", "conv-t", "what time is it", datetime.now(UTC))

        turn = await VoiceTurnRepository.get_turn(analysis.result["voice_turn_id"])
        assert turn["trace_id"] is None
        rows, _ = await VoiceTurnRepository.list_turns()
        assert rows[0]["linked_trace_id"] == "trace-t" and rows[0]["transcript"] == "what time is it"


class TestReviewApi:
    async def test_list_reports_trace_match(self, admin_client, db_repository):
        client, _service = admin_client
        linked = await _record(conversation_id="conv-a")
        await VoiceTurnRepository.set_trace_id(linked, "trace-a")
        unlinked = await _record(conversation_id="conv-b")
        now = datetime.now(UTC)
        await _insert_trace_at(db_repository, "trace-a", "conv-a", "lights on", now)
        await _insert_trace_at(db_repository, "trace-b", "conv-b", "lights off", now + timedelta(seconds=1))
        by_id = {item["id"]: item for item in (await client.get("/api/admin/voice/turns")).json()["turns"]}
        assert by_id[linked]["trace_match"] == "lookup" and by_id[linked]["trace_id"] == "trace-a"
        assert by_id[unlinked]["trace_match"] == "time_window" and by_id[unlinked]["transcript"] == "lights off"

    async def test_no_profiles_status_filter(self, admin_client):
        client, _service = admin_client
        first = await _record(status="no_profiles")
        await _record(status="unknown")
        listed = (await client.get("/api/admin/voice/turns", params={"status": "no_profiles"})).json()
        assert [item["id"] for item in listed["turns"]] == [first]

    async def test_audio_path_outside_the_directory_is_refused(self, admin_client):
        client, _service = admin_client
        turn_id = new_turn_id()
        await VoiceTurnRepository.add_turn(
            turn_id, embedding=_vec(1, 0, 0), dim=3, model=_MODEL, status="unknown", audio_path="../x.wav"
        )
        assert (await client.get(f"/api/admin/voice/turns/{turn_id}/audio")).status_code == 404

    async def test_delete_profile_with_recordings(self, admin_client):
        client, _service = admin_client
        anna, ben = await _two_profiles()
        identified = await _record(status="identified", profile_id=anna)
        assigned = await _record(status="unknown")
        sample_id = await VoiceProfileRepository.add_sample(anna, _vec(1, 0, 0), 3, _MODEL)
        assert await VoiceTurnRepository.mark_assigned(assigned, anna, sample_id)
        unrelated = await _record(status="unknown")
        bens = await _record(status="identified", profile_id=ben)

        response = await client.delete(f"/api/admin/voice/profiles/{anna}", params={"delete_recordings": "true"})
        assert response.status_code == 200 and response.json()["deleted_recordings"] == 2
        assert await VoiceTurnRepository.get_turn(identified) is None
        assert await VoiceTurnRepository.get_turn(assigned) is None
        assert not (recordings_dir() / f"{identified}.wav").exists()
        assert not (recordings_dir() / f"{assigned}.wav").exists()
        assert await VoiceTurnRepository.get_turn(unrelated) is not None

        # Without the option the turns stay, unlinked.
        kept = await client.delete(f"/api/admin/voice/profiles/{ben}")
        assert kept.status_code == 200 and kept.json()["deleted_recordings"] == 0
        turn = await VoiceTurnRepository.get_turn(bens)
        assert turn is not None and turn["speaker_profile_id"] is None
        assert (recordings_dir() / f"{bens}.wav").exists()
        missing = await client.delete("/api/admin/voice/profiles/999", params={"delete_recordings": "true"})
        assert missing.status_code == 404

    async def test_assign_profile_deleted_before_link(self, admin_client):
        client, service = admin_client
        anna, _ben = await _two_profiles()
        turn_id = await _record(vector=(0.98, 0.05, 0.0))
        with (
            patch.object(
                VoiceTurnRepository, "mark_assigned", AsyncMock(side_effect=aiosqlite.IntegrityError("FOREIGN KEY"))
            ),
            patch.object(service, "invalidate_profiles") as invalidate,
        ):
            response = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna})
        assert response.status_code == 404
        assert await VoiceProfileRepository.count_samples(anna) == 1
        invalidate.assert_called()

    async def test_assign_turn_deleted_during_assignment(self, admin_client):
        client, _service = admin_client
        anna, _ben = await _two_profiles()
        turn_id = await _record(vector=(0.98, 0.05, 0.0))

        async def _deleted_meanwhile(turn: str, profile_id: int, sample_id: int) -> bool:
            await VoiceTurnRepository.delete_turn(turn)
            return False

        with patch.object(VoiceTurnRepository, "mark_assigned", _deleted_meanwhile):
            response = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna})
        assert response.status_code == 404 and response.json()["detail"] == "Voice turn not found"
        assert await VoiceProfileRepository.count_samples(anna) == 1

    async def test_assign_add_sample_integrity_error(self, admin_client):
        client, _service = admin_client
        anna, _ben = await _two_profiles()
        turn_id = await _record(vector=(0.98, 0.05, 0.0))
        with patch.object(
            VoiceProfileRepository, "add_sample", AsyncMock(side_effect=aiosqlite.IntegrityError("FOREIGN KEY"))
        ):
            response = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna})
        assert response.status_code == 404

    async def test_audio_deleted_during_re_embedding(self, admin_client):
        client, _service = admin_client
        anna, _ben = await _two_profiles()
        turn_id = await _record(model="old-model")
        with patch.object(voice_admin, "read_wav_pcm", side_effect=FileNotFoundError("gone")):
            response = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": anna})
        assert response.status_code == 404 and response.json()["error"] == "audio_missing"


async def test_turn_routes_require_an_admin_session(db_repository):
    app = build_integration_test_app()
    with patch("app.db.repository.SetupStateRepository.is_complete", new_callable=AsyncMock, return_value=True):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            turn_id = new_turn_id()
            assert (await client.get("/api/admin/voice/turns")).status_code == 401
            assert (await client.get(f"/api/admin/voice/turns/{turn_id}/audio")).status_code == 401
            assigned = await client.post(f"/api/admin/voice/turns/{turn_id}/assign", json={"profile_id": 1})
            assert assigned.status_code in (401, 403)
            assert (await client.delete("/api/admin/voice/turns")).status_code in (401, 403)


async def test_abandoned_write_transaction_is_rolled_back(db_repository):
    import app.db.schema as schema

    async with real_get_db_write() as db:
        await db.execute("SELECT 1")
    # Simulates a write scope cancelled after its BEGIN reached the connection thread.
    await schema._write_conn.execute("BEGIN")
    assert schema._write_conn.in_transaction
    async with real_get_db_write() as db:
        await db.execute("UPDATE settings SET value = value WHERE key = 'voice.speaker_id.enabled'")
    assert schema._write_conn is not None and not schema._write_conn.in_transaction
