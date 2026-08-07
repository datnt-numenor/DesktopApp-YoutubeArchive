from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import windows_scheduler
from schemas import SyncScheduleRequest


def test_sync_schedule_request_validates_24_hour_time() -> None:
    assert SyncScheduleRequest(time="00:00").time == "00:00"
    assert SyncScheduleRequest(time="23:59").time == "23:59"
    with pytest.raises(ValidationError):
        SyncScheduleRequest(time="24:00")
    with pytest.raises(ValidationError):
        SyncScheduleRequest(time="12:60")


def test_get_scheduled_sync_info_parses_powershell_json(monkeypatch) -> None:
    monkeypatch.setattr(
        windows_scheduler,
        "_run_script",
        lambda *_args: '{"time":"12:00","state":"Ready","next_run_time":"2026-08-06 12:00"}',
    )

    info = windows_scheduler.get_scheduled_sync_info()

    assert info.time == "12:00"
    assert info.state == "Ready"
    assert info.next_run_time == "2026-08-06 12:00"


def test_set_scheduled_sync_time_updates_then_reads_task(monkeypatch) -> None:
    calls = []

    def fake_run(script_name: str, *args: str) -> str:
        calls.append((script_name, args))
        if script_name == "update-scheduled-sync.ps1":
            return "Updated"
        return '{"time":"20:30","state":"Ready","next_run_time":null}'

    monkeypatch.setattr(windows_scheduler, "_run_script", fake_run)

    info = windows_scheduler.set_scheduled_sync_time("20:30")

    assert info.time == "20:30"
    assert calls == [
        ("update-scheduled-sync.ps1", ("-Time", "20:30")),
        ("get-scheduled-sync.ps1", ()),
    ]


def test_run_script_turns_powershell_failure_into_scheduler_error(monkeypatch) -> None:
    monkeypatch.setattr(
        windows_scheduler.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr="Task missing", stdout=""),
    )

    with pytest.raises(windows_scheduler.WindowsSchedulerError, match="Task missing"):
        windows_scheduler._run_script("get-scheduled-sync.ps1")
