from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import scheduled_sync_state
from scripts import scheduled_sync


class FakeSession:
    def __init__(self) -> None:
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def rollback(self) -> None:
        self.rolled_back = True


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def test_state_round_trip_is_atomic_and_notification_throttle_is_scoped(tmp_path: Path) -> None:
    path = tmp_path / "scheduled-sync-state.json"
    state = {
        "status": "failed",
        "last_notification_fingerprint": "failed:network",
        "last_notification_at": "2026-09-06T05:00:00+00:00",
    }

    scheduled_sync_state.write_scheduled_sync_state(state, path)

    assert scheduled_sync_state.read_scheduled_sync_state(path) == state
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []
    assert scheduled_sync_state.notification_is_throttled(
        state,
        "failed:network",
        now=datetime(2026, 9, 6, 10, 59, tzinfo=timezone.utc),
    )
    assert not scheduled_sync_state.notification_is_throttled(
        state,
        "failed:other",
        now=datetime(2026, 9, 6, 10, 59, tzinfo=timezone.utc),
    )


def test_windows_notification_uses_argument_list_and_hidden_process(monkeypatch, tmp_path: Path) -> None:
    script = tmp_path / "show-notification.ps1"
    script.write_text("# test", encoding="utf-8")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(scheduled_sync_state, "NOTIFICATION_SCRIPT", script)
    monkeypatch.setattr(scheduled_sync_state.subprocess, "run", fake_run)

    scheduled_sync_state.show_windows_notification("Title", "Message with spaces", "Warning")

    assert captured["command"][-6:] == [
        "-Title",
        "Title",
        "-Message",
        "Message with spaces",
        "-Level",
        "Warning",
    ]
    assert captured["kwargs"]["timeout"] == 12
    assert captured["kwargs"]["check"] is False


@pytest.mark.asyncio
async def test_cleanup_failure_is_nonfatal_and_playlist_sync_continues(monkeypatch) -> None:
    session = FakeSession()
    engine = FakeEngine()
    states = []
    notifications = []
    synced = []

    async def fake_init_db() -> None:
        return None

    async def fake_cleanup(_session) -> int:
        raise TypeError("maintenance failed")

    async def fake_list(_session):
        return [SimpleNamespace(id=7, owner_id="owner", title="Archive")]

    async def fake_sync(playlist_id, *_args, **_kwargs) -> None:
        synced.append(playlist_id)

    async def no_pending(_playlist_id: int) -> int:
        return 0

    monkeypatch.setattr(scheduled_sync, "read_scheduled_sync_state", lambda: {})
    monkeypatch.setattr(scheduled_sync, "write_scheduled_sync_state", lambda state: states.append(dict(state)))
    monkeypatch.setattr(scheduled_sync, "show_windows_notification", lambda *args: notifications.append(args))
    monkeypatch.setattr(scheduled_sync, "init_db", fake_init_db)
    monkeypatch.setattr(scheduled_sync, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(scheduled_sync.crud, "cleanup_sync_history", fake_cleanup)
    monkeypatch.setattr(scheduled_sync.crud, "list_auto_sync_playlists", fake_list)
    monkeypatch.setattr(scheduled_sync, "sync_playlist_by_id", fake_sync)
    monkeypatch.setattr(scheduled_sync, "count_pending_download_errors", no_pending)
    monkeypatch.setattr(scheduled_sync, "sync_process_lock", nullcontext)
    monkeypatch.setattr(scheduled_sync, "engine", engine)

    result = await scheduled_sync.run_scheduled_sync()

    assert result == 0
    assert synced == [7]
    assert session.rolled_back is True
    assert engine.disposed is True
    assert states[-1]["status"] == "success"
    assert "maintenance failed" in states[-1]["maintenance_warning"]
    assert notifications == []


@pytest.mark.asyncio
async def test_setup_failure_is_persisted_and_notified(monkeypatch) -> None:
    engine = FakeEngine()
    states = []
    notifications = []

    async def broken_init_db() -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(scheduled_sync, "read_scheduled_sync_state", lambda: {})
    monkeypatch.setattr(scheduled_sync, "write_scheduled_sync_state", lambda state: states.append(dict(state)))
    monkeypatch.setattr(scheduled_sync, "show_windows_notification", lambda *args: notifications.append(args))
    monkeypatch.setattr(scheduled_sync, "init_db", broken_init_db)
    monkeypatch.setattr(scheduled_sync, "engine", engine)

    result = await scheduled_sync.run_scheduled_sync()

    assert result == 1
    assert engine.disposed is True
    assert states[-1]["status"] == "failed"
    assert states[-1]["exit_code"] == 1
    assert "database unavailable" in states[-1]["error"]
    assert len(notifications) == 1


@pytest.mark.asyncio
async def test_interrupted_run_is_detected_and_success_sends_recovery(monkeypatch) -> None:
    session = FakeSession()
    engine = FakeEngine()
    states = []
    notifications = []

    async def fake_init_db() -> None:
        return None

    async def fake_cleanup(_session) -> int:
        return 0

    async def no_playlists(_session):
        return []

    monkeypatch.setattr(
        scheduled_sync,
        "read_scheduled_sync_state",
        lambda: {"status": "running", "consecutive_failures": 0},
    )
    monkeypatch.setattr(scheduled_sync, "write_scheduled_sync_state", lambda state: states.append(dict(state)))
    monkeypatch.setattr(scheduled_sync, "show_windows_notification", lambda *args: notifications.append(args))
    monkeypatch.setattr(scheduled_sync, "init_db", fake_init_db)
    monkeypatch.setattr(scheduled_sync, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(scheduled_sync.crud, "cleanup_sync_history", fake_cleanup)
    monkeypatch.setattr(scheduled_sync.crud, "list_auto_sync_playlists", no_playlists)
    monkeypatch.setattr(scheduled_sync, "sync_process_lock", nullcontext)
    monkeypatch.setattr(scheduled_sync, "engine", engine)

    result = await scheduled_sync.run_scheduled_sync()

    assert result == 0
    assert states[-1]["status"] == "success"
    assert states[-1]["recovered_interrupted_count"] == 1
    assert states[-1]["consecutive_failures"] == 0
    assert len(notifications) == 2
    assert notifications[0][0] == "YouTube Archive sync was interrupted"
    assert notifications[1][0] == "YouTube Archive sync recovered"


def test_main_persists_and_notifies_last_resort_startup_failure(monkeypatch) -> None:
    states = []
    notifications = []

    def broken_logging() -> None:
        raise OSError("log directory unavailable")

    monkeypatch.setattr(scheduled_sync, "configure_logging", broken_logging)
    monkeypatch.setattr(scheduled_sync, "read_scheduled_sync_state", lambda: {})
    monkeypatch.setattr(scheduled_sync, "write_scheduled_sync_state", lambda state: states.append(dict(state)))
    monkeypatch.setattr(scheduled_sync, "show_windows_notification", lambda *args: notifications.append(args))
    monkeypatch.setattr("sys.argv", ["scheduled_sync.py"])

    with pytest.raises(SystemExit) as exc_info:
        scheduled_sync.main()

    assert exc_info.value.code == 1
    assert states[-1]["status"] == "failed"
    assert "log directory unavailable" in states[-1]["error"]
    assert len(notifications) == 1
