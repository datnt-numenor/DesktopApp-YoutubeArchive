from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import BASE_DIR


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
        return ScheduledSyncInfo(
            time=str(data["time"]),
            state=str(data["state"]),
            next_run_time=data.get("next_run_time"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WindowsSchedulerError("Windows Task Scheduler returned an invalid response") from exc


def set_scheduled_sync_time(time_value: str) -> ScheduledSyncInfo:
    _run_script("update-scheduled-sync.ps1", "-Time", time_value)
    return get_scheduled_sync_info()
