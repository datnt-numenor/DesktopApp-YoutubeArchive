from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import BASE_DIR, LOCAL_DATA_DIR


STATE_PATH = LOCAL_DATA_DIR / "scheduled-sync-state.json"
NOTIFICATION_SCRIPT = BASE_DIR / "scripts" / "show-notification.ps1"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NOTIFICATION_THROTTLE = timedelta(hours=6)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def read_scheduled_sync_state(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_scheduled_sync_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def notification_is_throttled(
    state: dict[str, Any],
    fingerprint: str,
    *,
    now: datetime | None = None,
) -> bool:
    if state.get("last_notification_fingerprint") != fingerprint:
        return False
    raw_timestamp = state.get("last_notification_at")
    if not raw_timestamp:
        return False
    try:
        timestamp = datetime.fromisoformat(str(raw_timestamp))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (now or utc_now()) - timestamp < NOTIFICATION_THROTTLE


def show_windows_notification(title: str, message: str, level: str = "Info") -> None:
    if not NOTIFICATION_SCRIPT.is_file():
        raise OSError(f"Notification script not found: {NOTIFICATION_SCRIPT}")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(NOTIFICATION_SCRIPT),
            "-Title",
            title,
            "-Message",
            message,
            "-Level",
            level,
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=12,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "notification command failed").strip()
        raise OSError(detail)
