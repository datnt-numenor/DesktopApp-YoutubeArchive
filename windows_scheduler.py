from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import BASE_DIR
from scheduled_sync_state import read_scheduled_sync_state


TASK_NAME = "Desktop YouTube Archive - Daily MP3 Sync"
SCRIPTS_DIR = BASE_DIR / "scripts"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WindowsSchedulerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScheduledSyncInfo:
    time: str
    state: str
    next_run_time: str | None
    last_run_time: str | None = None
    last_task_result: int | None = None
    last_task_result_hex: str | None = None
    wake_to_run: bool = False
    disallow_start_if_on_batteries: bool = True
    start_when_available: bool = False
    sync_status: str | None = None
    sync_summary: str | None = None
    sync_error: str | None = None
    sync_finished_at: str | None = None


def _run_script(script_name: str, *arguments: str) -> str:
    script_path = SCRIPTS_DIR / script_name
    if not script_path.is_file():
        raise WindowsSchedulerError(f"Scheduler script not found: {script_path}")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WindowsSchedulerError(f"Could not access Windows Task Scheduler: {exc}") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Windows Task Scheduler command failed").strip()
        raise WindowsSchedulerError(message)
    return result.stdout.strip()


def get_scheduled_sync_info() -> ScheduledSyncInfo:
    output = _run_script("get-scheduled-sync.ps1")
    try:
        data = json.loads(output)
        sync_state = read_scheduled_sync_state()
        return ScheduledSyncInfo(
            time=str(data["time"]),
            state=str(data["state"]),
            next_run_time=data.get("next_run_time"),
            last_run_time=data.get("last_run_time"),
            last_task_result=data.get("last_task_result"),
            last_task_result_hex=data.get("last_task_result_hex"),
            wake_to_run=bool(data.get("wake_to_run", False)),
            disallow_start_if_on_batteries=bool(data.get("disallow_start_if_on_batteries", True)),
            start_when_available=bool(data.get("start_when_available", False)),
            sync_status=sync_state.get("status"),
            sync_summary=sync_state.get("summary"),
            sync_error=sync_state.get("error") or sync_state.get("maintenance_warning"),
            sync_finished_at=sync_state.get("finished_at"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WindowsSchedulerError("Windows Task Scheduler returned an invalid response") from exc


def set_scheduled_sync_time(time_value: str) -> ScheduledSyncInfo:
    _run_script("update-scheduled-sync.ps1", "-Time", time_value)
    return get_scheduled_sync_info()
