"""Speaker identification persistence: repository, migration 49, settings, service, admin API."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import numpy as np
import pytest
from fastapi import HTTPException

from app.api.routes.admin._settings import _validate_setting_value
from app.db.repositories.voice import VoiceProfileNameConflictError
from app.db.repository import SettingsRepository, VoiceProfileRepository
from app.db.schema import _create_indexes, _create_tables, _seed_defaults
from app.db.schema._migrations import _run_migrations
from app.voice import service as voice_service
from app.voice.service import SpeakerIdService, VoiceConfig, load_voice_config
from app.voice.speaker_engine import SpeakerEngineUnavailableError
from tests.conftest import build_integration_test_app
from tests.helpers import shutdown_aiosqlite

_MODEL = "test-model"


class FakeEngine:
    """Stands in for SpeakerEngine: returns queued vectors, no sherpa-onnx needed."""

    def __init__(self, vectors: list[list[float]] | None = None, *, unavailable: bool = False) -> None:
        self.model_id = _MODEL
        self.status = "ready"
        self.last_error = None
        self.vectors = list(vectors or [])
        self.unavailable = unavailable
        self.calls = 0

    async def ensure_ready(self, num_threads: int = 2) -> bool:
        return not self.unavailable

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
        "min_duration_s": 1.5,
        "min_enroll_duration_s": 2.0,
        "wait_ms": 500,
        "num_threads": 2,
    }
    values.update(overrides)
    return VoiceConfig(**values)


def _pcm(seconds: float) -> bytes:
    return b"\x01\x00" * int(16000 * seconds)


def _vec(*values: float) -> bytes:
    return np.array(values, dtype=np.float32).tobytes()


@pytest.fixture(autouse=True)
def _reset_voice_service():
    voice_service.set_speaker_service(None)
    yield
    voice_service.set_speaker_service(None)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class TestVoiceProfileRepository:
    async def test_crud_and_sample_counts(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("  Anna  ", " user-1 ")
        assert anna["name"] == "Anna" and anna["ha_user_id"] == "user-1"
        ben = await VoiceProfileRepository.create_profile("Ben")
        assert ben["ha_user_id"] is None

        with pytest.raises(VoiceProfileNameConflictError):
            await VoiceProfileRepository.create_profile("Anna")
        with pytest.raises(ValueError):
            await VoiceProfileRepository.create_profile("   ")
        with pytest.raises(ValueError):
            await VoiceProfileRepository.create_profile("x" * 65)

        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0), 2, _MODEL, 2.5)
        await VoiceProfileRepository.add_sample(anna["id"], _vec(0, 1), 2, "other-model", 2.5)
        profiles = {p["name"]: p for p in await VoiceProfileRepository.list_profiles(_MODEL)}
        assert profiles["Anna"]["sample_count"] == 1
        assert profiles["Ben"]["sample_count"] == 0
        assert await VoiceProfileRepository.count_samples(anna["id"]) == 2

        renamed = await VoiceProfileRepository.rename_profile(anna["id"], "Anna B")
        assert renamed is not None and renamed["name"] == "Anna B"
        with pytest.raises(VoiceProfileNameConflictError):
            await VoiceProfileRepository.rename_profile(ben["id"], "Anna B")
        mapped = await VoiceProfileRepository.set_ha_user(ben["id"], "user-2")
        assert mapped is not None and mapped["ha_user_id"] == "user-2"
        unmapped = await VoiceProfileRepository.set_ha_user(ben["id"], "")
        assert unmapped is not None and unmapped["ha_user_id"] is None
        assert await VoiceProfileRepository.rename_profile(999, "Nobody") is None

        rows = await VoiceProfileRepository.list_samples_for_model(_MODEL)
        assert [(r["profile_id"], r["name"], r["dim"]) for r in rows] == [(anna["id"], "Anna B", 2)]

        assert await VoiceProfileRepository.delete_samples(anna["id"]) == 2
        assert await VoiceProfileRepository.count_samples(anna["id"]) == 0

    async def test_delete_profile_removes_samples(self, db_repository):
        profile = await VoiceProfileRepository.create_profile("Anna")
        await VoiceProfileRepository.add_sample(profile["id"], _vec(1, 0), 2, _MODEL, 2.0)
        assert await VoiceProfileRepository.delete_profile(profile["id"])
        assert not await VoiceProfileRepository.delete_profile(profile["id"])
        db = await aiosqlite.connect(str(db_repository))
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM voice_samples")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await shutdown_aiosqlite(db)

    async def test_foreign_key_cascade(self, db_repository):
        profile = await VoiceProfileRepository.create_profile("Anna")
        await VoiceProfileRepository.add_sample(profile["id"], _vec(1, 0), 2, _MODEL, 2.0)
        db = await aiosqlite.connect(str(db_repository))
        try:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("DELETE FROM voice_profiles WHERE id = ?", (profile["id"],))
            await db.commit()
            cursor = await db.execute("SELECT COUNT(*) FROM voice_samples")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await shutdown_aiosqlite(db)


# ---------------------------------------------------------------------------
# Migration 49
# ---------------------------------------------------------------------------


async def _tables(path: Path) -> set[str]:
    db = await aiosqlite.connect(str(path))
    try:
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in await cursor.fetchall()}
    finally:
        await shutdown_aiosqlite(db)


async def test_migration_49_creates_voice_tables_on_existing_db(tmp_path):
    path = tmp_path / "upgrade.db"
    db = await aiosqlite.connect(str(path))
    try:
        await _create_tables(db)
        await _create_indexes(db)
        await _seed_defaults(db)
        await _run_migrations(db)
        await db.commit()
        # Simulate an install at schema 48 that predates the voice tables.
        await db.execute("DROP TABLE voice_samples")
        await db.execute("DROP TABLE voice_profiles")
        await db.execute("DELETE FROM schema_version WHERE version >= 49")
        await db.commit()
    finally:
        await shutdown_aiosqlite(db)
    assert "voice_profiles" not in await _tables(path)

    db = await aiosqlite.connect(str(path))
    try:
        await _run_migrations(db)
        await db.commit()
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        assert (await cursor.fetchone())[0] >= 49
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE name = 'idx_voice_samples_profile_model'")
        assert await cursor.fetchone() is not None
    finally:
        await shutdown_aiosqlite(db)
    assert {"voice_profiles", "voice_samples"} <= await _tables(path)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestVoiceSettings:
    async def test_seeded_defaults(self, db_repository):
        rows = {row["key"]: row for row in await SettingsRepository.get_by_category("voice")}
        assert rows["voice.speaker_id.enabled"]["value"] == "false"
        assert rows["voice.speaker_id.threshold"]["value"] == "0.55"
        assert rows["voice.speaker_id.margin"]["value"] == "0.05"
        assert rows["voice.speaker_id.min_duration_s"]["value"] == "1.5"
        assert rows["voice.speaker_id.min_enroll_duration_s"]["value"] == "2.0"
        assert rows["voice.speaker_id.wait_ms"]["value"] == "500"
        assert rows["voice.speaker_id.num_threads"]["value"] == "2"
        config = await load_voice_config()
        assert config == _config(enabled=False)

    @pytest.mark.parametrize(
        ("key", "value", "value_type"),
        [
            ("voice.speaker_id.threshold", "1.5", "float"),
            ("voice.speaker_id.margin", "-0.1", "float"),
            ("voice.speaker_id.wait_ms", "3001", "int"),
            ("voice.speaker_id.num_threads", "0", "int"),
            ("voice.speaker_id.num_threads", "9", "int"),
            ("voice.speaker_id.enabled", "maybe", "bool"),
            ("voice.speaker_id.threshold", "nan", "float"),
        ],
    )
    def test_invalid_values_rejected(self, key, value, value_type):
        with pytest.raises(HTTPException) as exc:
            _validate_setting_value(key, value, value_type)
        assert exc.value.status_code == 400

    @pytest.mark.parametrize(
        ("key", "value", "value_type"),
        [
            ("voice.speaker_id.threshold", "0.7", "float"),
            ("voice.speaker_id.margin", "0", "float"),
            ("voice.speaker_id.wait_ms", "3000", "int"),
            ("voice.speaker_id.num_threads", "8", "int"),
            ("voice.speaker_id.enabled", "true", "bool"),
        ],
    )
    def test_valid_values_accepted(self, key, value, value_type):
        _validate_setting_value(key, value, value_type)

    async def test_out_of_range_db_values_are_clamped(self, db_repository):
        await SettingsRepository.set("voice.speaker_id.wait_ms", "99999", "int", "voice")
        await SettingsRepository.set("voice.speaker_id.threshold", "garbage", "float", "voice")
        config = await load_voice_config()
        assert config.wait_ms == 3000
        assert config.threshold == 0.55


# ---------------------------------------------------------------------------
# Service: identification and enrollment
# ---------------------------------------------------------------------------


class TestSpeakerIdServiceProcessTurn:
    async def test_no_profiles_skips_embedding(self, db_repository):
        engine = FakeEngine()
        service = SpeakerIdService(engine=engine)
        result = await service.process_turn(_pcm(2.0), 16000, _config())
        assert result["status"] == "no_profiles"
        assert engine.calls == 0

    async def test_too_short(self, db_repository):
        service = SpeakerIdService(engine=FakeEngine())
        result = await service.process_turn(_pcm(1.0), 16000, _config())
        assert result["status"] == "too_short"
        assert result["duration_s"] == pytest.approx(1.0)

    async def test_identified_and_unknown(self, db_repository):
        anna = await VoiceProfileRepository.create_profile("Anna", "user-anna")
        ben = await VoiceProfileRepository.create_profile("Ben")
        await VoiceProfileRepository.add_sample(anna["id"], _vec(1, 0, 0), 3, _MODEL, 2.0)
        await VoiceProfileRepository.add_sample(ben["id"], _vec(0, 1, 0), 3, _MODEL, 2.0)
        # A sample from another model must never join a centroid.
        await VoiceProfileRepository.add_sample(ben["id"], _vec(1, 0, 0), 3, "old-model", 2.0)
        engine = FakeEngine([[0.95, 0.05, 0.0], [0.6, 0.6, 0.5]])
        service = SpeakerIdService(engine=engine)

        identified = await service.process_turn(_pcm(2.0), 16000, _config())
        assert identified["status"] == "identified"
        assert identified["profile_id"] == anna["id"]
        assert identified["name"] == "Anna" and identified["ha_user_id"] == "user-anna"
        assert identified["score"] > 0.9 and identified["margin"] > 0.5

        unknown = await service.process_turn(_pcm(2.0), 16000, _config())
        assert unknown["status"] == "unknown"
        assert unknown["profile_id"] is None and unknown["name"] is None
        assert unknown["score"] is not None

        recent = service.store.recent()
        assert [r["status"] for r in recent] == ["unknown", "identified"]
        # Calibration aid: nearest candidate shown for unknown turns.
        assert recent[0]["name"] in ("Anna", "Ben")

    async def test_engine_unavailable(self, db_repository):
        profile = await VoiceProfileRepository.create_profile("Anna")
        await VoiceProfileRepository.add_sample(profile["id"], _vec(1, 0), 2, _MODEL, 2.0)
        service = SpeakerIdService(engine=FakeEngine(unavailable=True))
        result = await service.process_turn(_pcm(2.0), 16000, _config())
        assert result["status"] == "unavailable"

    @pytest.mark.parametrize("engine_status", ["unavailable", "loading"])
    async def test_engine_not_ready_schedules_warm_up_without_waiting(self, db_repository, engine_status):
        profile = await VoiceProfileRepository.create_profile("Anna")
        await VoiceProfileRepository.add_sample(profile["id"], _vec(1, 0), 2, _MODEL, 2.0)
        engine = FakeEngine([[1, 0]])
        engine.status = engine_status
        service = SpeakerIdService(engine=engine)
        with patch.object(service, "schedule_warm_up") as warm_up:
            result = await service.process_turn(_pcm(2.0), 16000, _config())
        assert result["status"] == "unavailable" and result["reason"] == "engine_not_ready"
        warm_up.assert_called_once()
        assert engine.calls == 0

    async def test_engine_not_ready_during_enrollment_stores_nothing(self, db_repository):
        profile = await VoiceProfileRepository.create_profile("Anna")
        engine = FakeEngine([[1, 0]])
        engine.status = "unavailable"
        service = SpeakerIdService(engine=engine)
        service.enrollment.arm(profile["id"], "Anna", samples=2, timeout_s=60)
        with patch.object(service, "schedule_warm_up") as warm_up:
            analysis = await service.analyze_turn(_pcm(2.5), 16000, _config())
        assert analysis.pending is None
        assert analysis.result["status"] == "unavailable" and analysis.result["profile_id"] == profile["id"]
        warm_up.assert_called_once()
        assert engine.calls == 0
        assert service.enrollment.current()["remaining"] == 2

    async def test_schedule_warm_up_skips_ready_or_loading_engine(self, db_repository):
        engine = FakeEngine()
        service = SpeakerIdService(engine=engine)
        with patch.object(voice_service, "spawn") as spawn:
            service.schedule_warm_up()
            engine.status = "loading"
            service.schedule_warm_up()
            spawn.assert_not_called()
            engine.status = "unavailable"
            service.schedule_warm_up()
            spawn.assert_called_once()
            spawn.call_args.args[0].close()

    async def test_analyze_enrollment_defers_storage_until_commit(self, db_repository):
        profile = await VoiceProfileRepository.create_profile("Anna")
        service = SpeakerIdService(engine=FakeEngine([[1, 0]]))
        service.enrollment.arm(profile["id"], "Anna", samples=2, timeout_s=60)
        analysis = await service.analyze_turn(_pcm(2.5), 16000, _config())
        assert analysis.pending is not None
        assert await VoiceProfileRepository.count_samples(profile["id"], _MODEL) == 0
        assert service.enrollment.current()["remaining"] == 2
        committed = await service.commit_enrollment(analysis.pending)
        assert committed["status"] == "enrolled_sample"
        assert await VoiceProfileRepository.count_samples(profile["id"], _MODEL) == 1
        assert service.enrollment.current()["remaining"] == 1

    async def test_enrollment_collects_and_auto_disarms(self, db_repository):
        profile = await VoiceProfileRepository.create_profile("Anna")
        engine = FakeEngine([[1, 0], [0.9, 0.1]])
        service = SpeakerIdService(engine=engine)
        service.enrollment.arm(profile["id"], "Anna", samples=2, timeout_s=60)

        short = await service.process_turn(_pcm(1.0), 16000, _config())
        assert short["status"] == "too_short" and short["profile_id"] == profile["id"]
        assert engine.calls == 0

        first = await service.process_turn(_pcm(2.5), 16000, _config())
        assert first["status"] == "enrolled_sample" and first["profile_id"] == profile["id"]
        assert service.enrollment.current()["remaining"] == 1
        second = await service.process_turn(_pcm(2.5), 16000, _config())
        assert second["status"] == "enrolled_sample"
        assert service.enrollment.current() is None
        assert await VoiceProfileRepository.count_samples(profile["id"], _MODEL) == 2

        # Disarmed: the next turn is identified against the fresh centroid.
        engine.vectors.append([1, 0])
        identified = await service.process_turn(_pcm(2.0), 16000, _config())
        assert identified["status"] == "identified" and identified["name"] == "Anna"

    async def test_enrollment_for_deleted_profile_errors(self, db_repository):
        profile = await VoiceProfileRepository.create_profile("Anna")
        service = SpeakerIdService(engine=FakeEngine([[1, 0]]))
        service.enrollment.arm(profile["id"], "Anna", samples=2, timeout_s=60)
        await VoiceProfileRepository.delete_profile(profile["id"])
        with patch.object(VoiceProfileRepository, "add_sample", AsyncMock(side_effect=aiosqlite.IntegrityError())):
            result = await service.process_turn(_pcm(2.5), 16000, _config())
        assert result["status"] == "error" and result["reason"] == "sample_not_stored"
        assert service.enrollment.current() is None


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


class TestVoiceAdminApi:
    async def test_profile_lifecycle(self, admin_client):
        client, _service = admin_client
        created = await client.post("/api/admin/voice/profiles", json={"name": " Anna ", "ha_user_id": "u1"})
        assert created.status_code == 201
        body = created.json()
        assert body["name"] == "Anna" and body["ha_user_id"] == "u1" and body["sample_count"] == 0
        pid = body["id"]

        dup = await client.post("/api/admin/voice/profiles", json={"name": "Anna"})
        assert dup.status_code == 409
        blank = await client.post("/api/admin/voice/profiles", json={"name": "  "})
        assert blank.status_code == 400

        await VoiceProfileRepository.add_sample(pid, _vec(1, 0), 2, _MODEL, 2.0)
        listed = await client.get("/api/admin/voice/profiles")
        assert listed.status_code == 200
        assert listed.json()[0]["sample_count"] == 1

        patched = await client.patch(f"/api/admin/voice/profiles/{pid}", json={"name": "Anna B", "ha_user_id": None})
        assert patched.status_code == 200
        assert patched.json()["name"] == "Anna B" and patched.json()["ha_user_id"] is None
        assert patched.json()["sample_count"] == 1
        empty = await client.patch(f"/api/admin/voice/profiles/{pid}", json={})
        assert empty.status_code == 400
        missing = await client.patch("/api/admin/voice/profiles/999", json={"name": "X"})
        assert missing.status_code == 404

        reset = await client.delete(f"/api/admin/voice/profiles/{pid}/samples")
        assert reset.status_code == 200 and reset.json()["deleted"] == 1
        assert (await client.delete("/api/admin/voice/profiles/999/samples")).status_code == 404

        deleted = await client.delete(f"/api/admin/voice/profiles/{pid}")
        assert deleted.status_code == 200
        assert (await client.delete(f"/api/admin/voice/profiles/{pid}")).status_code == 404

    async def test_enrollment_and_status(self, admin_client):
        client, service = admin_client
        pid = (await client.post("/api/admin/voice/profiles", json={"name": "Anna"})).json()["id"]

        bad = await client.post(f"/api/admin/voice/profiles/{pid}/enroll", json={"samples": 0})
        assert bad.status_code == 422
        bad_timeout = await client.post(f"/api/admin/voice/profiles/{pid}/enroll", json={"timeout_s": 5})
        assert bad_timeout.status_code == 422
        assert (await client.post("/api/admin/voice/profiles/999/enroll", json={})).status_code == 404

        # Disabled feature: enrollment could never collect samples, so it is rejected.
        rejected = await client.post(f"/api/admin/voice/profiles/{pid}/enroll", json={"samples": 3})
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "Speaker identification is disabled"
        assert service.enrollment.current() is None
        assert (await client.get("/api/admin/voice/status")).json()["enabled"] is False

        await SettingsRepository.set("voice.speaker_id.enabled", "true")
        armed = await client.post(f"/api/admin/voice/profiles/{pid}/enroll", json={"samples": 3, "timeout_s": 120})
        assert armed.status_code == 200
        assert armed.json()["enrollment"]["remaining"] == 3

        service.store.record_recent({"status": "unknown", "score": 0.3, "margin": 0.1, "duration_s": 2.0})
        status = (await client.get("/api/admin/voice/status")).json()
        assert status["enabled"] is True
        assert status["engine"] == "ready"
        assert status["model"] == _MODEL
        assert status["enrollment"]["profile_id"] == pid and status["enrollment"]["name"] == "Anna"
        assert status["recent"][0]["status"] == "unknown"

        # Deleting the armed profile disarms enrollment.
        await client.delete(f"/api/admin/voice/profiles/{pid}")
        assert (await client.get("/api/admin/voice/status")).json()["enrollment"] is None

        pid2 = (await client.post("/api/admin/voice/profiles", json={"name": "Ben"})).json()["id"]
        await client.post(f"/api/admin/voice/profiles/{pid2}/enroll")
        assert service.enrollment.current()["remaining"] == 5
        cancelled = await client.delete("/api/admin/voice/enroll")
        assert cancelled.json() == {"ok": True, "cancelled": True}

    async def test_settings_api_accepts_voice_keys(self, admin_client):
        client, _ = admin_client
        ok = await client.put(
            "/api/admin/settings",
            json={"items": {"voice.speaker_id.enabled": "true", "voice.speaker_id.threshold": "0.6"}},
        )
        assert ok.status_code == 200
        assert await SettingsRepository.get_value("voice.speaker_id.enabled") == "true"
        bad = await client.put("/api/admin/settings", json={"items": {"voice.speaker_id.wait_ms": "5000"}})
        assert bad.status_code == 400
        grouped = (await client.get("/api/admin/settings")).json()["settings"]
        assert len(grouped["voice"]) == 7

    async def test_enabling_via_settings_api_schedules_warm_up(self, admin_client):
        client, service = admin_client
        service.engine.status = "unavailable"
        with patch.object(service, "schedule_warm_up") as warm_up:
            await client.put("/api/admin/settings", json={"items": {"voice.speaker_id.threshold": "0.6"}})
            await client.put("/api/admin/settings", json={"items": {"voice.speaker_id.enabled": "false"}})
            warm_up.assert_not_called()
            ok = await client.put("/api/admin/settings", json={"items": {"voice.speaker_id.enabled": "true"}})
            assert ok.status_code == 200
            warm_up.assert_called_once()
