"""Migration safety: pre-migration snapshots and the schema downgrade guard."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite
import pytest

from app.config import Settings
from app.db.schema import close_db, init_db
from app.db.schema._migrations import (
    LATEST_SCHEMA_VERSION,
    SNAPSHOT_DIR_NAME,
    SNAPSHOT_RETENTION,
    MigrationBackupError,
    SchemaDowngradeError,
    _prepare_migrations,
    _run_migrations,
)
from tests.helpers import shutdown_aiosqlite

_OLD_VERSION = LATEST_SCHEMA_VERSION - 2


async def _init(db_path: Path, *, backup: bool = True) -> None:
    """Run the real ``init_db`` against ``db_path`` and close the shared connection."""
    fake_settings = SimpleNamespace(sqlite_db_path=str(db_path), migration_backup=backup)
    try:
        with patch("app.db.schema.settings", fake_settings):
            await init_db()
    finally:
        await close_db()


def _schema_versions(db_path: Path) -> list[int]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY version")]
    finally:
        conn.close()


def _dump(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def _rewind_schema(db_path: Path, version: int) -> None:
    """Pretend the database was last migrated to ``version``."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM schema_version WHERE version > ?", (version,))
        conn.commit()
    finally:
        conn.close()


def _snapshots(db_path: Path) -> list[Path]:
    backup_dir = db_path.parent / SNAPSHOT_DIR_NAME
    if not backup_dir.is_dir():
        return []
    return sorted(p for p in backup_dir.iterdir() if p.name.startswith("pre-migration-"))


@pytest.fixture()
def db_file(tmp_path: Path) -> Path:
    return tmp_path / "data" / "agent_assist.db"


@pytest.fixture()
async def existing_db(db_file: Path) -> Path:
    """An existing database last migrated to ``_OLD_VERSION`` (migrations pending)."""
    await _init(db_file)
    _rewind_schema(db_file, _OLD_VERSION)
    return db_file


async def test_fresh_install_takes_no_snapshot(db_file: Path) -> None:
    await _init(db_file)

    assert max(_schema_versions(db_file)) == LATEST_SCHEMA_VERSION
    assert not (db_file.parent / SNAPSHOT_DIR_NAME).exists()


async def test_up_to_date_db_takes_no_snapshot(db_file: Path) -> None:
    await _init(db_file)
    await _init(db_file)

    assert _snapshots(db_file) == []


async def test_pending_migrations_snapshot_is_restorable(existing_db: Path) -> None:
    await _init(existing_db)

    snapshots = _snapshots(existing_db)
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.name.startswith(f"pre-migration-v{_OLD_VERSION}-to-v{LATEST_SCHEMA_VERSION}-")
    assert snapshot.name.endswith("Z.db")
    assert snapshot.stat().st_size > 0
    # The snapshot holds the pre-migration state; the live DB is migrated.
    assert max(_schema_versions(snapshot)) == _OLD_VERSION
    assert max(_schema_versions(existing_db)) == LATEST_SCHEMA_VERSION
    conn = sqlite3.connect(str(snapshot))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert not list(snapshot.parent.glob("*.partial"))


async def test_retention_keeps_newest_snapshots(existing_db: Path) -> None:
    backup_dir = existing_db.parent / SNAPSHOT_DIR_NAME
    backup_dir.mkdir()
    old = [backup_dir / f"pre-migration-v40-to-v41-2020010{day}T000000Z.db" for day in range(1, 5)]
    for path in old:
        path.write_bytes(b"old")
    unrelated = backup_dir / "manual-backup.db"
    unrelated.write_bytes(b"keep me")

    await _init(existing_db)

    remaining = _snapshots(existing_db)
    assert len(remaining) == SNAPSHOT_RETENTION
    assert old[-1] in remaining and old[-2] in remaining
    assert not any(path.exists() for path in old[:2])
    assert any(p.name.startswith(f"pre-migration-v{_OLD_VERSION}-") for p in remaining)
    assert unrelated.exists()


async def test_snapshot_failure_aborts_and_leaves_db_unchanged(existing_db: Path) -> None:
    # A regular file where the backups directory should go makes the snapshot fail.
    (existing_db.parent / SNAPSHOT_DIR_NAME).write_text("not a directory")
    before = _dump(existing_db)

    with pytest.raises(MigrationBackupError):
        await _init(existing_db)

    assert _dump(existing_db) == before
    assert max(_schema_versions(existing_db)) == _OLD_VERSION


async def test_vacuum_failure_aborts_and_removes_partial_file(existing_db: Path) -> None:
    before = _dump(existing_db)
    conn = await aiosqlite.connect(str(existing_db), isolation_level=None)
    real_execute = conn.execute

    def failing_execute(sql, *args, **kwargs):
        if sql.startswith("VACUUM INTO"):
            raise sqlite3.OperationalError("database or disk is full")
        return real_execute(sql, *args, **kwargs)

    try:
        with (
            patch.object(conn, "execute", side_effect=failing_execute),
            pytest.raises(MigrationBackupError, match="disk is full"),
        ):
            await _prepare_migrations(conn, existing_db, backup_enabled=True)
    finally:
        await shutdown_aiosqlite(conn)

    assert _dump(existing_db) == before
    assert _snapshots(existing_db) == []
    assert not list((existing_db.parent / SNAPSHOT_DIR_NAME).glob("*.partial"))


async def test_migration_backup_disabled_skips_snapshot(existing_db: Path) -> None:
    await _init(existing_db, backup=False)

    assert max(_schema_versions(existing_db)) == LATEST_SCHEMA_VERSION
    assert _snapshots(existing_db) == []


async def test_downgrade_guard_refuses_newer_database(db_file: Path) -> None:
    await _init(db_file)
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (LATEST_SCHEMA_VERSION + 1,))
        conn.commit()
    finally:
        conn.close()
    before = _dump(db_file)

    with pytest.raises(SchemaDowngradeError) as excinfo:
        await _init(db_file)

    message = str(excinfo.value)
    assert f"schema version {LATEST_SCHEMA_VERSION + 1} is newer" in message
    assert f"latest schema version {LATEST_SCHEMA_VERSION}" in message
    assert "newer HA-AgentHub version" in message
    assert "pre-migration snapshot" in message
    assert _dump(db_file) == before
    assert _snapshots(db_file) == []


async def test_run_migrations_also_rejects_newer_database(db_file: Path) -> None:
    await _init(db_file)
    conn = await aiosqlite.connect(str(db_file))
    try:
        await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (LATEST_SCHEMA_VERSION + 5,))
        with pytest.raises(SchemaDowngradeError):
            await _run_migrations(conn)
    finally:
        await shutdown_aiosqlite(conn)


def test_migration_backup_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIGRATION_BACKUP", raising=False)
    assert Settings(_env_file=None).migration_backup is True
    monkeypatch.setenv("MIGRATION_BACKUP", "false")
    assert Settings(_env_file=None).migration_backup is False
