"""Tests for durable WARNING+ log persistence and restore across restarts."""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from app.bootstrap import _logging as bootstrap_logging
from app.util.log_buffer import LogBuffer, LogBufferHandler, get_log_buffer, set_log_buffer
from app.util.log_persist import PERSIST_FILENAME, LogPersister, _JsonLineFormatter
from tests.conftest import build_integration_test_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    level: int,
    message: str,
    *,
    name: str = "app.test",
    created: float | None = None,
    exc_info=None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="/app/mod.py",
        lineno=7,
        msg=message,
        args=(),
        exc_info=exc_info,
        func="func",
    )
    if created is not None:
        record.created = created
    return record


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture()
def isolated_buffer():
    """Install a fresh global log buffer and reset the bootstrap persister."""
    old_buffer = get_log_buffer()
    buf = LogBuffer(capacity=100)
    set_log_buffer(buf)
    old_persister = bootstrap_logging._log_persister
    bootstrap_logging._log_persister = None
    yield buf
    stray = bootstrap_logging._log_persister
    if stray is not None:
        stray.stop()
    bootstrap_logging._log_persister = old_persister
    set_log_buffer(old_buffer)


async def _restart(tmp_path: Path) -> LogBuffer:
    """Simulate a container restart: stop persistence, fresh buffer, start again."""
    await bootstrap_logging.stop_log_persistence()
    buf = LogBuffer(capacity=100)
    set_log_buffer(buf)
    await bootstrap_logging.start_log_persistence(str(tmp_path))
    return buf


@pytest_asyncio.fixture()
async def admin_client():
    app = build_integration_test_app(setup_complete=True, override_admin_session=True)
    with patch(
        "app.db.repository.SetupStateRepository.is_complete",
        new_callable=AsyncMock,
        return_value=True,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_warning_and_error_persisted_info_not(tmp_path: Path) -> None:
    persister = LogPersister(tmp_path / PERSIST_FILENAME)
    buf = LogBuffer(capacity=100)
    buf.set_persist_sink(persister.submit, persister.min_level)
    persister.start()
    try:
        raise RuntimeError("provider exploded")
    except RuntimeError:
        exc_info = sys.exc_info()
    buf.add(_record(logging.INFO, "info line"))
    buf.add(_record(logging.DEBUG, "debug line"))
    buf.add(_record(logging.WARNING, "warning line"))
    buf.add(_record(logging.ERROR, "LLM call failed for light-agent: boom", exc_info=exc_info))
    persister.stop()

    lines = _read_lines(tmp_path / PERSIST_FILENAME)
    assert [e["message"] for e in lines] == ["warning line", "LLM call failed for light-agent: boom"]
    assert [e["level"] for e in lines] == ["WARNING", "ERROR"]
    error = lines[1]
    assert error["name"] == "app.test"
    assert "RuntimeError: provider exploded" in error["exception"]
    assert "Traceback" in error["exception"]
    assert datetime.fromisoformat(error["timestamp"]).tzinfo is not None


def test_ingested_bridge_entry_persisted_with_trace_fields(tmp_path: Path) -> None:
    persister = LogPersister(tmp_path / PERSIST_FILENAME)
    buf = LogBuffer(capacity=100)
    buf.set_persist_sink(persister.submit, persister.min_level)
    persister.start()
    base = {"timestamp": datetime.now(UTC).isoformat(), "name": "custom_components.ha_agenthub", "source": "ha"}
    buf.add_entry({**base, "level": "INFO", "message": "bridge info"})
    buf.add_entry({**base, "level": "ERROR", "message": "bridge error", "trace_id": "t-1", "conversation_id": "c-1"})
    persister.stop()

    lines = _read_lines(tmp_path / PERSIST_FILENAME)
    assert len(lines) == 1
    assert lines[0]["message"] == "bridge error"
    assert lines[0]["source"] == "ha"
    assert lines[0]["trace_id"] == "t-1"
    assert lines[0]["conversation_id"] == "c-1"


def test_retention_bound_enforced(tmp_path: Path) -> None:
    max_bytes = 2000
    persister = LogPersister(tmp_path / PERSIST_FILENAME, max_bytes=max_bytes, backup_count=2)
    buf = LogBuffer(capacity=1000)
    buf.set_persist_sink(persister.submit, persister.min_level)
    persister.start()
    for i in range(300):
        buf.add(_record(logging.WARNING, f"warning number {i:04d}"))
    persister.stop()

    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == [PERSIST_FILENAME, f"{PERSIST_FILENAME}.1", f"{PERSIST_FILENAME}.2"]
    for name in files:
        assert (tmp_path / name).stat().st_size <= max_bytes
    # The newest entry survives, the oldest was rotated out.
    entries, skipped = persister.load_recent(10_000)
    assert skipped == 0
    messages = [e["message"] for e in entries]
    assert messages[-1] == "warning number 0299"
    assert "warning number 0000" not in messages
    assert messages == sorted(messages)


def test_submit_never_writes_on_calling_thread(tmp_path: Path) -> None:
    """The logging path only enqueues; serialization and file I/O run on the listener thread."""
    persister = LogPersister(tmp_path / PERSIST_FILENAME)
    buf = LogBuffer(capacity=100)
    buf.set_persist_sink(persister.submit, persister.min_level)
    handler = LogBufferHandler(buf)

    # Not started: emitting only enqueues, nothing touches the file system.
    handler.emit(_record(logging.WARNING, "queued only"))
    assert persister.queue.qsize() == 1
    assert not (tmp_path / PERSIST_FILENAME).exists()

    format_threads: list[int] = []
    original_format = _JsonLineFormatter.format

    def _tracking_format(self, record):
        format_threads.append(threading.get_ident())
        return original_format(self, record)

    with patch.object(_JsonLineFormatter, "format", _tracking_format):
        persister.start()
        handler.emit(_record(logging.ERROR, "second"))
        persister.stop()

    # RotatingFileHandler formats once for the size check and once to write.
    assert len(format_threads) >= 2
    assert threading.get_ident() not in format_threads
    assert [e["message"] for e in _read_lines(tmp_path / PERSIST_FILENAME)] == ["queued only", "second"]


def test_full_queue_drops_without_blocking(tmp_path: Path) -> None:
    persister = LogPersister(tmp_path / PERSIST_FILENAME, queue_maxsize=2)
    for i in range(5):
        persister.submit({"timestamp": datetime.now(UTC).isoformat(), "level": "ERROR", "name": "x", "message": str(i)})
    assert persister.queue.qsize() == 2
    assert persister.dropped == 3


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def test_load_recent_skips_corrupt_lines_and_tolerates_missing_file(tmp_path: Path) -> None:
    persister = LogPersister(tmp_path / PERSIST_FILENAME)
    assert persister.load_recent() == ([], 0)

    good = {"timestamp": "2026-10-13T05:48:00+00:00", "level": "ERROR", "name": "app.x", "message": "ok"}
    naive = {**good, "timestamp": "2026-10-13T05:48:00", "message": "naive"}
    (tmp_path / PERSIST_FILENAME).write_text(
        "\n".join(["{not json", json.dumps(good), "[1, 2]", json.dumps(naive), json.dumps({"level": "ERROR"})]) + "\n",
        encoding="utf-8",
    )
    entries, skipped = persister.load_recent()
    assert [e["message"] for e in entries] == ["ok"]
    assert skipped == 4


def test_restore_entries_orders_by_timestamp_and_deduplicates() -> None:
    buf = LogBuffer(capacity=100)
    now = datetime.now(UTC)
    buf.add(_record(logging.INFO, "live", created=now.timestamp()))
    old = [
        {"timestamp": (now - timedelta(minutes=6)).isoformat(), "level": "ERROR", "name": "a", "message": "older"},
        {"timestamp": (now - timedelta(minutes=5)).isoformat(), "level": "WARNING", "name": "a", "message": "newer"},
    ]
    assert buf.restore_entries(old) == 2
    assert buf.restore_entries(old) == 0

    entries = buf.get_entries(limit=10)["entries"]
    assert [e["message"] for e in entries] == ["live", "newer", "older"]
    assert entries[1]["restored"] is True
    assert entries[1]["timestamp"] == old[1]["timestamp"]
    assert "restored" not in entries[0]


async def test_restore_after_restart_visible_in_admin_api_without_duplicates(
    tmp_path: Path, isolated_buffer: LogBuffer, admin_client: httpx.AsyncClient
) -> None:
    await bootstrap_logging.start_log_persistence(str(tmp_path))
    pre_restart_ts = datetime(2026, 10, 13, 5, 48, tzinfo=UTC).timestamp()
    isolated_buffer.add(_record(logging.ERROR, "LLM call failed for light-agent: 503", created=pre_restart_ts))
    isolated_buffer.add(_record(logging.INFO, "not persisted", created=pre_restart_ts + 1))

    await _restart(tmp_path)
    await _restart(tmp_path)

    resp = await admin_client.get("/api/admin/logs", params={"level": "WARNING", "limit": 1000})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    failed = [e for e in entries if e["message"].startswith("LLM call failed")]
    assert len(failed) == 1
    assert failed[0]["timestamp"] == datetime.fromtimestamp(pre_restart_ts, tz=UTC).isoformat()
    assert failed[0]["restored"] is True
    all_messages = [e["message"] for e in get_log_buffer().get_entries(limit=1000)["entries"]]
    assert "not persisted" not in all_messages

    # The file itself never receives restored entries again.
    await bootstrap_logging.stop_log_persistence()
    persisted = _read_lines(tmp_path / PERSIST_FILENAME)
    assert sum(1 for e in persisted if e["message"].startswith("LLM call failed")) == 1


async def test_start_log_persistence_logs_skipped_lines_once(
    tmp_path: Path, isolated_buffer: LogBuffer, caplog: pytest.LogCaptureFixture
) -> None:
    good = {"timestamp": "2026-10-13T05:48:00+00:00", "level": "ERROR", "name": "app.x", "message": "kept"}
    (tmp_path / PERSIST_FILENAME).write_text(f"garbage\n{json.dumps(good)}\n{{broken\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=bootstrap_logging.__name__):
        persister = await bootstrap_logging.start_log_persistence(str(tmp_path))
    assert persister is not None
    skipped_logs = [r for r in caplog.records if "unreadable line" in r.getMessage()]
    assert len(skipped_logs) == 1
    assert "2 unreadable" in skipped_logs[0].getMessage()
    assert [e["message"] for e in isolated_buffer.get_entries(level="ERROR")["entries"]] == ["kept"]
    await bootstrap_logging.stop_log_persistence()


async def test_start_log_persistence_failure_does_not_abort(tmp_path: Path, isolated_buffer: LogBuffer) -> None:
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    persister = await bootstrap_logging.start_log_persistence(str(blocker / "logs"))
    assert persister is None
    assert isolated_buffer._persist_sink is None


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_teardown_failure_still_flushes_log_persistence(tmp_path: Path, isolated_buffer: LogBuffer) -> None:
    from app.bootstrap._shutdown import teardown

    persister = await bootstrap_logging.start_log_persistence(str(tmp_path))
    assert persister is not None

    alarm_monitor = AsyncMock()

    async def _failing_stop() -> None:
        isolated_buffer.add(_record(logging.ERROR, "alarm monitor stop failed"))
        raise RuntimeError("teardown step exploded")

    alarm_monitor.stop.side_effect = _failing_stop
    app = SimpleNamespace(state=SimpleNamespace(alarm_monitor=alarm_monitor))

    with pytest.raises(RuntimeError, match="teardown step exploded"):
        await teardown(app)

    assert bootstrap_logging._log_persister is None
    assert isolated_buffer._persist_sink is None
    messages = [e["message"] for e in _read_lines(tmp_path / PERSIST_FILENAME)]
    assert "alarm monitor stop failed" in messages
