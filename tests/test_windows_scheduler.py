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
        "read_scheduled_sync_state",
        lambda: {
            "status": "partial",
            "summary": "1 succeeded, 1 failed",
            "error": "HTTP 403",
            "finished_at": "2026-09-06T05:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        windows_scheduler,
        "_run_script",
        lambda *_args: (
            '{"time":"12:00","state":"Ready","next_run_time":"2026-08-06 12:00",'
            '"last_run_time":"2026-08-05 12:00:01","last_task_result":1,'
            '"last_task_result_hex":"0x00000001","wake_to_run":true,'
            '"disallow_start_if_on_batteries":true,"start_when_available":true}'
        ),
    )

    info = windows_scheduler.get_scheduled_sync_info()

    assert info.time == "12:00"
    assert info.state == "Ready"
    assert info.next_run_time == "2026-08-06 12:00"
    assert info.last_run_time == "2026-08-05 12:00:01"
    assert info.last_task_result_hex == "0x00000001"
    assert info.wake_to_run is True
    assert info.disallow_start_if_on_batteries is True
    assert info.start_when_available is True
    assert info.sync_status == "partial"
    assert info.sync_error == "HTTP 403"


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


def test_install_scheduled_sync_registers_then_reads_task(monkeypatch) -> None:
    calls = []

    def fake_run(script_name: str, *args: str) -> str:
        calls.append((script_name, args))
        if script_name == "install-scheduled-sync.ps1":
            return "Installed task"
        return '{"time":"07:15","state":"Ready","next_run_time":"2026-10-20 07:15"}'

    monkeypatch.setattr(windows_scheduler, "_run_script", fake_run)

    info = windows_scheduler.install_scheduled_sync("07:15")

    assert info.time == "07:15"
    assert calls == [
        ("install-scheduled-sync.ps1", ("-Time", "07:15")),
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
