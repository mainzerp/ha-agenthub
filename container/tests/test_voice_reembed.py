"""Speaker-model switch: model constants, migration 57 and re-embedding recorded samples."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import numpy as np
import pytest

from app.bootstrap import _voice as voice_bootstrap
from app.db.repository import SettingsRepository, VoiceProfileRepository, VoiceTurnRepository
from app.db.schema import _create_indexes, _create_tables, _seed_defaults
from app.db.schema._migrations import _VOICE_MODEL_SWITCH_57, _migrate_to_57, _run_migrations
from app.defaults import VOICE_DEFAULTS
from app.voice import model as voice_model
from app.voice import service as voice_service
from app.voice.recordings import TURN_STATUS_ENROLLED, TurnRecording, new_turn_id, recordings_dir, store_recording
from app.voice.reembed import ReembedAlreadyRunningError
from app.voice.service import SpeakerIdService
from app.voice.speaker_engine import SpeakerEngine, SpeakerEngineUnavailableError
from tests.conftest import build_integration_test_app
from tests.helpers import shutdown_aiosqlite

_OLD = "old-model"
_NEW = "new-model"
_RATE = 16000


class FakeEngine:
    """Stands in for SpeakerEngine: returns queued vectors, optionally blocking until released."""

    def __init__(self, vectors: list[list[float]] | None = None, *, status: str = "ready") -> None:
        self.model_id = _NEW
        self.status = status
        self.last_error = None
        self.vectors = list(vectors or [])
        self.calls = 0
        self.unavailable = False
        self.gate: asyncio.Event | None = None
        self.pcm_lengths: list[int] = []

    async def ensure_ready(self, num_threads: int = 2) -> bool:
        return self.status == "ready"

    def schedule_reload(self, num_threads: int) -> bool:
        return False

    async def embed(self, pcm: bytes, sample_rate: int, *, num_threads: int = 2) -> np.ndarray:
        self.calls += 1
        self.pcm_lengths.append(len(pcm))
        if self.gate is not None:
            await self.gate.wait()
        if self.unavailable:
            raise SpeakerEngineUnavailableError("model missing")
        return np.array(self.vectors.pop(0), dtype=np.float32)


def _vec(*values: float) -> bytes:
    return np.array(values, dtype=np.float32).tobytes()


@pytest.fixture(autouse=True)
def _reset_voice_service():
    voice_service.set_speaker_service(None)
    yield
    voice_service.set_speaker_service(None)


async def _record(
    *,
    status: str,
    model: str = _OLD,
    seconds: float = 2.0,
    assigned_profile_id: int | None = None,
    assigned_sample_id: int | None = None,
    speaker_profile_id: int | None = None,
) -> str:
    turn_id = new_turn_id()
    stored = await store_recording(
        TurnRecording(
            turn_id=turn_id,
            pcm=b"\x10\x00" * int(_RATE * seconds),
            sample_rate=_RATE,
            vector=np.array([0.5, 0.5, 0.0], dtype=np.float32),
            model_id=model,
            status=status,
            conversation_id="conv-1",
            duration_s=seconds + 0.5,
            speech_s=seconds,
            profile_id=speaker_profile_id,
            assigned_profile_id=assigned_profile_id,
            assigned_sample_id=assigned_sample_id,
        )
    )
    assert stored
    return turn_id


async def _household() -> dict[str, object]:
    """Two profiles with old-model samples: one enrolled recording each, one assigned turn, one unlinked turn."""
    anna = (await VoiceProfileRepository.create_profile("Anna"))["id"]
    ben = (await VoiceProfileRepository.create_profile("Ben"))["id"]
    anna_old = await VoiceProfileRepository.add_sample(anna, _vec(1, 0, 0), 3, _OLD, 2.5, 2.0)
    ben_old = await VoiceProfileRepository.add_sample(ben, _vec(0, 1, 0), 3, _OLD, 3.5, 3.0)
    assigned_old = await VoiceProfileRepository.add_sample(ben, _vec(0, 0.9, 0.1), 3, _OLD, 2.5, 2.0)
    enrolled = await _record(
        status=TURN_STATUS_ENROLLED, assigned_profile_id=anna, assigned_sample_id=anna_old, speaker_profile_id=anna
    )
    enrolled_ben = await _record(
        status=TURN_STATUS_ENROLLED,
        seconds=3.0,
        assigned_profile_id=ben,
        assigned_sample_id=ben_old,
        speaker_profile_id=ben,
    )
    assigned = await _record(status="unknown")
    assert await VoiceTurnRepository.mark_assigned(assigned, ben, assigned_old)
    unlinked = await _record(status="identified", speaker_profile_id=anna)
    return {
        "anna": anna,
        "ben": ben,
        "enrolled": enrolled,
        "enrolled_ben": enrolled_ben,
        "assigned": assigned,
        "unlinked": unlinked,
        "old_samples": {anna_old, ben_old, assigned_old},
    }


def _service(engine: FakeEngine) -> SpeakerIdService:
    service = SpeakerIdService(engine=engine)
    voice_service.set_speaker_service(service)
    return service


# ---------------------------------------------------------------------------
# Model constants
# ---------------------------------------------------------------------------


class TestModelConstants:
    def test_eres2net_model(self):
        assert voice_model.MODEL_ID == "3dspeaker_speech_eres2net_sv_en_voxceleb_16k"
        assert voice_model.MODEL_FILENAME == "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
        # The release tag is misspelled upstream.
        assert voice_model.MODEL_URL == (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
            "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
        )
        assert voice_model.MODEL_SHA256 == "c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5"
        assert voice_model.MODEL_SIZE_BYTES == 26_485_263
        assert SpeakerEngine().model_id == voice_model.MODEL_ID

    def test_dockerfile_ships_only_the_current_model(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        assert f"ARG SPEAKER_MODEL_URL={voice_model.MODEL_URL}" in dockerfile
        assert f"ARG SPEAKER_MODEL_SHA256={voice_model.MODEL_SHA256}" in dockerfile
        assert f"/opt/models/speaker/{voice_model.MODEL_FILENAME}" in dockerfile
        assert "campplus" not in dockerfile


# ---------------------------------------------------------------------------
# Migration 57
# ---------------------------------------------------------------------------


async def _fresh_install(path: Path) -> None:
    db = await aiosqlite.connect(str(path))
    try:
        await _create_tables(db)
        await _create_indexes(db)
        await _seed_defaults(db)
        await _run_migrations(db)
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)


async def _voice_values(path: Path) -> dict[str, str]:
    db = await aiosqlite.connect(str(path))
    try:
        cursor = await db.execute("SELECT key, value FROM settings WHERE key LIKE 'voice.speaker_id.%'")
        return {row[0]: row[1] for row in await cursor.fetchall()}
    finally:
        await shutdown_aiosqlite(db)


class TestMigration57:
    def test_values_match_fresh_install_defaults(self):
        assert dict(_VOICE_MODEL_SWITCH_57) == {
            "voice.speaker_id.threshold": "0.45",
            "voice.speaker_id.margin": "0.15",
            "voice.speaker_id.enroll_min_similarity": "0.5",
        }
        for key, value in _VOICE_MODEL_SWITCH_57:
            assert float(VOICE_DEFAULTS[key]) == float(value)  # type: ignore[arg-type]

    async def test_fresh_install_uses_new_defaults(self, tmp_path: Path):
        path = tmp_path / "fresh.db"
        await _fresh_install(path)
        values = await _voice_values(path)
        assert float(values["voice.speaker_id.threshold"]) == 0.45
        assert float(values["voice.speaker_id.margin"]) == 0.15
        assert float(values["voice.speaker_id.enroll_min_similarity"]) == 0.5

    async def test_existing_values_are_replaced_unconditionally(self, tmp_path: Path, caplog):
        path = tmp_path / "live.db"
        await _fresh_install(path)
        db = await aiosqlite.connect(str(path))
        try:
            # Admin-tuned values for the old model (live instance), plus one other voice setting.
            for key, value in (
                ("voice.speaker_id.threshold", "0.55"),
                ("voice.speaker_id.margin", "0.15"),
                ("voice.speaker_id.enroll_min_similarity", "0.60"),
                ("voice.speaker_id.min_duration_s", "2.0"),
            ):
                await db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
            await db.execute("DELETE FROM schema_version WHERE version > 56")
            await db.commit()
            with caplog.at_level(logging.INFO, logger="app.db.schema._migrations"):
                await _run_migrations(db)
            await db.commit()
            cursor = await db.execute("SELECT 1 FROM schema_version WHERE version = 57")
            assert await cursor.fetchone() is not None
        finally:
            await shutdown_aiosqlite(db)
        values = await _voice_values(path)
        assert values["voice.speaker_id.threshold"] == "0.45"
        assert values["voice.speaker_id.margin"] == "0.15"
        assert values["voice.speaker_id.enroll_min_similarity"] == "0.5"
        # Other voice settings are untouched.
        assert values["voice.speaker_id.min_duration_s"] == "2.0"
        changed = [record.getMessage() for record in caplog.records if "Migration 57" in record.getMessage()]
        assert len(changed) == 2
        assert any("voice.speaker_id.threshold changed from 0.55 to 0.45" in line for line in changed)
        assert any("voice.speaker_id.enroll_min_similarity changed from 0.60 to 0.5" in line for line in changed)

    async def test_rerun_and_missing_rows(self, tmp_path: Path, caplog):
        path = tmp_path / "rerun.db"
        await _fresh_install(path)
        db = await aiosqlite.connect(str(path))
        try:
            await db.execute("DELETE FROM settings WHERE key = 'voice.speaker_id.margin'")
            with caplog.at_level(logging.INFO, logger="app.db.schema._migrations"):
                await _migrate_to_57(db)
                await _migrate_to_57(db)
            await db.commit()
        finally:
            await shutdown_aiosqlite(db)
        values = await _voice_values(path)
        assert "voice.speaker_id.margin" not in values
        assert not [record for record in caplog.records if "Migration 57" in record.getMessage()]


# ---------------------------------------------------------------------------
# Re-embedding
# ---------------------------------------------------------------------------


class TestReembed:
    async def test_enrolled_and_assigned_recordings_get_current_model_samples(self, db_repository):
        data = await _household()
        engine = FakeEngine([[1.0, 0.1, 0.0], [0.1, 1.0, 0.0], [0.0, 1.0, 0.2]])
        service = _service(engine)
        assert await service.centroids.get(_NEW) == []

        run = await service.reembed_recordings("manual")

        assert (run.reembedded, run.skipped, run.failed) == (3, 0, 0)
        assert run.model == _NEW and run.trigger == "manual"
        assert engine.calls == 3
        # The stored PCM is embedded as is (no second trim): 2 s and 3 s at 16 kHz, 16 bit.
        assert sorted(engine.pcm_lengths) == [64000, 64000, 96000]
        for key, profile in (("enrolled", "anna"), ("enrolled_ben", "ben"), ("assigned", "ben")):
            turn = await VoiceTurnRepository.get_turn(data[key])
            assert turn["model"] == _NEW and turn["dim"] == 3
            assert turn["assigned_profile_id"] == data[profile]
            assert turn["assigned_sample_id"] not in data["old_samples"]
            samples = {row["id"]: row for row in await VoiceProfileRepository.list_samples(data[profile])}
            new_sample = samples[turn["assigned_sample_id"]]
            assert new_sample["model"] == _NEW
            assert new_sample["embedding"] == turn["embedding"]
            assert new_sample["speech_s"] == turn["speech_s"] and new_sample["duration_s"] == turn["duration_s"]
        stored = set()
        for key in ("enrolled", "enrolled_ben", "assigned"):
            turn = await VoiceTurnRepository.get_turn(data[key])
            stored.add(tuple(round(float(value), 3) for value in np.frombuffer(turn["embedding"], dtype=np.float32)))
        assert stored == {(1.0, 0.1, 0.0), (0.1, 1.0, 0.0), (0.0, 1.0, 0.2)}
        # Old-model samples stay; the unlinked turn keeps its embedding.
        assert await VoiceProfileRepository.count_samples(data["anna"], _OLD) == 1
        assert await VoiceProfileRepository.count_samples(data["ben"], _OLD) == 2
        assert await VoiceProfileRepository.count_samples(data["ben"], _NEW) == 2
        unlinked = await VoiceTurnRepository.get_turn(data["unlinked"])
        assert unlinked["model"] == _OLD and unlinked["assigned_sample_id"] is None
        # The centroid cache was invalidated: both profiles have current-model centroids now.
        assert {centroid.name for centroid in await service.centroids.get(_NEW)} == {"Anna", "Ben"}
        assert service.reembedder.last_run == run

    async def test_second_run_is_idempotent(self, db_repository):
        await _household()
        engine = FakeEngine([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 1.0, 0.1]])
        service = _service(engine)
        await service.reembed_recordings("startup")
        again = await service.reembed_recordings("manual")
        assert (again.reembedded, again.skipped, again.failed) == (0, 3, 0)
        assert engine.calls == 3

    async def test_cleared_link_is_not_a_source(self, db_repository):
        data = await _household()
        # Deleting a sample (admin, reset, outlier review) clears the link: never re-created.
        enrolled = await VoiceTurnRepository.get_turn(data["enrolled"])
        assert await VoiceProfileRepository.delete_sample(data["anna"], enrolled["assigned_sample_id"])
        engine = FakeEngine([[0.0, 1.0, 0.0], [0.0, 1.0, 0.1]])
        run = await _service(engine).reembed_recordings("manual")
        assert (run.reembedded, run.skipped, run.failed) == (2, 0, 0)
        assert await VoiceProfileRepository.count_samples(data["anna"]) == 0
        assert (await VoiceTurnRepository.get_turn(data["enrolled"]))["model"] == _OLD

    async def test_missing_audio_is_counted_and_the_run_continues(self, db_repository):
        data = await _household()
        (recordings_dir() / f"{data['enrolled']}.wav").unlink()
        engine = FakeEngine([[0.0, 1.0, 0.0], [0.0, 1.0, 0.1]])
        run = await _service(engine).reembed_recordings("manual")
        assert (run.reembedded, run.skipped, run.failed) == (2, 0, 1)
        turn = await VoiceTurnRepository.get_turn(data["enrolled"])
        assert turn["model"] == _OLD and turn["assigned_sample_id"] in data["old_samples"]
        assert await VoiceProfileRepository.count_samples(data["anna"], _NEW) == 0

    async def test_linked_sample_missing_is_recreated(self, db_repository):
        data = await _household()
        enrolled = await VoiceTurnRepository.get_turn(data["enrolled"])
        # A sample removed without clearing the link (foreign keys off) still has its recording.
        db = await aiosqlite.connect(str(db_repository))
        try:
            await db.execute("DELETE FROM voice_samples WHERE id = ?", (enrolled["assigned_sample_id"],))
            await db.commit()
        finally:
            await shutdown_aiosqlite(db)
        links = {row["id"]: row for row in await VoiceTurnRepository.list_sample_links()}
        assert links[data["enrolled"]]["sample_model"] is None
        engine = FakeEngine([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 1.0, 0.1]])
        run = await _service(engine).reembed_recordings("manual")
        assert run.reembedded == 3
        assert await VoiceProfileRepository.count_samples(data["anna"], _NEW) == 1

    async def test_engine_loss_stops_embedding(self, db_repository):
        await _household()
        engine = FakeEngine()
        engine.unavailable = True
        run = await _service(engine).reembed_recordings("manual")
        assert (run.reembedded, run.skipped, run.failed) == (0, 0, 3)
        assert engine.calls == 1

    async def test_changed_link_is_never_overwritten(self, db_repository):
        data = await _household()
        turn = await VoiceTurnRepository.get_turn(data["assigned"])
        stale = await VoiceTurnRepository.replace_linked_sample(
            data["assigned"],
            profile_id=data["ben"],
            expected_sample_id=turn["assigned_sample_id"] + 1000,
            embedding=_vec(1, 0, 0),
            dim=3,
            model=_NEW,
            duration_s=1.0,
            speech_s=1.0,
        )
        assert stale is None
        assert await VoiceProfileRepository.count_samples(data["ben"], _NEW) == 0
        assert (await VoiceTurnRepository.get_turn(data["assigned"]))["model"] == _OLD

    async def test_concurrent_run_is_refused(self, db_repository):
        await _household()
        engine = FakeEngine([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 1.0, 0.1]])
        engine.gate = asyncio.Event()
        service = _service(engine)
        first = asyncio.create_task(service.reembed_recordings("startup"))
        for _ in range(50):
            if engine.calls:
                break
            await asyncio.sleep(0.01)
        assert service.reembedder.running
        with pytest.raises(ReembedAlreadyRunningError):
            await service.reembed_recordings("manual")
        engine.gate.set()
        run = await first
        assert run.reembedded == 3 and not service.reembedder.running
        assert engine.calls == 3


# ---------------------------------------------------------------------------
# Admin API and startup trigger
# ---------------------------------------------------------------------------


@pytest.fixture()
async def admin_client(db_repository):
    engine = FakeEngine()
    service = _service(engine)
    app = build_integration_test_app(override_admin_session=True)
    with patch("app.db.repository.SetupStateRepository.is_complete", new_callable=AsyncMock, return_value=True):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client, service


class TestReembedApi:
    async def test_endpoint_returns_counts_and_status_reports_the_last_run(self, admin_client):
        client, service = admin_client
        before = (await client.get("/api/admin/voice/status")).json()
        assert before["reembed"] == {"running": False, "last_run": None}
        await _household()
        service.engine.vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 1.0, 0.1]]

        response = await client.post("/api/admin/voice/reembed")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert (body["reembedded"], body["skipped"], body["failed"]) == (3, 0, 0)
        assert body["trigger"] == "manual" and body["model"] == _NEW
        assert body["started_at"] and body["finished_at"]
        status = (await client.get("/api/admin/voice/status")).json()
        assert status["reembed"]["running"] is False
        last = status["reembed"]["last_run"]
        assert (last["reembedded"], last["skipped"], last["failed"], last["trigger"]) == (3, 0, 0, "manual")
        again = (await client.post("/api/admin/voice/reembed")).json()
        assert (again["reembedded"], again["skipped"], again["failed"]) == (0, 3, 0)

    async def test_engine_not_ready(self, admin_client):
        client, service = admin_client
        service.engine.status = "loading"
        with patch.object(service, "schedule_warm_up") as warm_up:
            response = await client.post("/api/admin/voice/reembed")
        assert response.status_code == 503 and response.json()["error"] == "engine_unavailable"
        warm_up.assert_called_once()
        assert service.reembedder.last_run is None

    async def test_running_run_is_refused(self, admin_client):
        client, service = admin_client
        await _household()
        service.engine.vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 1.0, 0.1]]
        service.engine.gate = asyncio.Event()
        running = asyncio.create_task(service.reembed_recordings("startup"))
        for _ in range(50):
            if service.engine.calls:
                break
            await asyncio.sleep(0.01)
        response = await client.post("/api/admin/voice/reembed")
        assert response.status_code == 409 and response.json()["error"] == "reembed_running"
        assert (await client.get("/api/admin/voice/status")).json()["reembed"]["running"] is True
        service.engine.gate.set()
        assert (await running).reembedded == 3


class TestStartupTrigger:
    async def test_runs_after_the_engine_is_ready(self, db_repository):
        await _household()
        engine = FakeEngine([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 1.0, 0.1]])
        service = _service(engine)
        with patch.object(service, "warm_up", AsyncMock(return_value=True)):
            await voice_bootstrap.warm_up_and_reembed()
        last = service.reembedder.last_run
        assert last is not None and last.trigger == "startup" and last.reembedded == 3

    async def test_unavailable_engine_skips_the_run(self, db_repository):
        await _household()
        engine = FakeEngine(status="unavailable")
        service = _service(engine)
        with patch.object(service, "warm_up", AsyncMock(return_value=False)):
            await voice_bootstrap.warm_up_and_reembed()
        assert service.reembedder.last_run is None and engine.calls == 0

    async def test_failure_is_logged_not_raised(self, db_repository):
        service = _service(FakeEngine())
        with (
            patch.object(service, "warm_up", AsyncMock(return_value=True)),
            patch.object(service, "reembed_recordings", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            await voice_bootstrap.warm_up_and_reembed()

    async def test_setup_voice_schedules_one_startup_run_when_enabled(self, db_repository):
        await SettingsRepository.set("voice.speaker_id.enabled", "true", "bool", "voice")
        app = SimpleNamespace(state=SimpleNamespace())
        release = asyncio.Event()

        async def _blocking() -> None:
            await release.wait()

        with (
            patch.object(voice_bootstrap, "run_recording_retention", _blocking),
            patch.object(voice_bootstrap, "warm_up_and_reembed", _blocking),
        ):
            await voice_bootstrap.setup_voice(app)
            startup = app.state.voice_reembed_task
            await voice_bootstrap.setup_voice(app)
            assert app.state._background_tasks == [app.state.voice_retention_task, startup]
            release.set()
            await asyncio.gather(*app.state._background_tasks)

    async def test_setup_voice_disabled_schedules_no_run(self, db_repository):
        app = SimpleNamespace(state=SimpleNamespace())
        release = asyncio.Event()

        async def _blocking() -> None:
            await release.wait()

        with patch.object(voice_bootstrap, "run_recording_retention", _blocking):
            await voice_bootstrap.setup_voice(app)
            assert getattr(app.state, "voice_reembed_task", None) is None
            release.set()
            await asyncio.gather(*app.state._background_tasks)
