from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.request import Request, urlopen

from config import LOCAL_DATA_DIR


TOOLS_DIR = LOCAL_DATA_DIR / "tools"
YTDLP_PATH = TOOLS_DIR / "yt-dlp.exe"
YTDLP_PREVIOUS_PATH = TOOLS_DIR / "yt-dlp.previous.exe"
YTDLP_STATE_PATH = TOOLS_DIR / "yt-dlp-state.json"
YTDLP_UPDATE_LOCK_PATH = TOOLS_DIR / "yt-dlp-update.lock"
LATEST_RELEASE_URL = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
SUCCESSFUL_CHECK_INTERVAL = timedelta(days=7)
FAILED_CHECK_INTERVAL = timedelta(hours=6)
DOWNLOAD_TIMEOUT_SECONDS = 180
UPDATE_LOCK_TIMEOUT_SECONDS = 180
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
logger = logging.getLogger(__name__)


class ManagedYtDlpError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(YTDLP_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict[str, Any]) -> None:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    temporary = YTDLP_STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(YTDLP_STATE_PATH)


def _request_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "DesktopApp-YoutubeArchive",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        return response.read()


def _latest_release() -> tuple[str, str, str]:
    try:
        release = json.loads(_request_bytes(LATEST_RELEASE_URL))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ManagedYtDlpError(f"Could not check the latest yt-dlp release: {exc}") from exc

    version = str(release.get("tag_name") or "").strip()
    assets = {
        str(asset.get("name")): str(asset.get("browser_download_url"))
        for asset in release.get("assets", [])
        if asset.get("name") and asset.get("browser_download_url")
    }
    executable_url = assets.get("yt-dlp.exe")
    checksums_url = assets.get("SHA2-256SUMS")
    if not version or not executable_url or not checksums_url:
        raise ManagedYtDlpError("The latest yt-dlp release is missing the Windows executable or checksum file")
    return version, executable_url, checksums_url


def _expected_checksum(checksums: bytes, filename: str) -> str:
    for raw_line in checksums.decode("utf-8", errors="replace").splitlines():
        fields = raw_line.strip().split()
        if len(fields) >= 2 and fields[-1].lstrip("*") == filename:
            return fields[0].lower()
    raise ManagedYtDlpError(f"No SHA-256 checksum was published for {filename}")


def _executable_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = result.stdout.strip().splitlines()
    return version[-1].strip() if result.returncode == 0 and version else None


def _install_release(version: str, executable_url: str, checksums_url: str) -> str:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    payload = _request_bytes(executable_url)
    expected = _expected_checksum(_request_bytes(checksums_url), "yt-dlp.exe")
    actual = hashlib.sha256(payload).hexdigest().lower()
    if actual != expected:
        raise ManagedYtDlpError("The downloaded yt-dlp.exe failed SHA-256 verification")

    temporary = TOOLS_DIR / "yt-dlp.download.exe"
    temporary.write_bytes(payload)
    downloaded_version = _executable_version(temporary)
    if downloaded_version != version:
        temporary.unlink(missing_ok=True)
        raise ManagedYtDlpError(
            f"Downloaded yt-dlp.exe reported version {downloaded_version or 'unknown'}, expected {version}"
        )

    if YTDLP_PATH.is_file():
        shutil.copy2(YTDLP_PATH, YTDLP_PREVIOUS_PATH)
    try:
        temporary.replace(YTDLP_PATH)
        installed_version = _executable_version(YTDLP_PATH)
        if installed_version != version:
            raise ManagedYtDlpError("The updated yt-dlp.exe did not pass its startup check")
    except Exception:
        if YTDLP_PREVIOUS_PATH.is_file():
            shutil.copy2(YTDLP_PREVIOUS_PATH, YTDLP_PATH)
        else:
            YTDLP_PATH.unlink(missing_ok=True)
        raise
    return version


def _try_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - desktop target is Windows
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - desktop target is Windows
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _update_lock() -> Iterator[None]:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    handle = YTDLP_UPDATE_LOCK_PATH.open("a+b")
    if YTDLP_UPDATE_LOCK_PATH.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + UPDATE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            _try_lock(handle)
            break
        except OSError as exc:
            if time.monotonic() >= deadline:
                handle.close()
                raise ManagedYtDlpError("Timed out waiting for another yt-dlp update") from exc
            time.sleep(0.25)
    try:
        yield
    finally:
        _unlock(handle)
        handle.close()


def _check_is_due(state: dict[str, Any], *, force_update: bool, executable_exists: bool) -> bool:
    if force_update or not executable_exists:
        return True
    last_success = _parse_datetime(state.get("last_successful_check_at"))
    if last_success and _utc_now() - last_success < SUCCESSFUL_CHECK_INTERVAL:
        return False
    last_attempt = _parse_datetime(state.get("last_attempt_at"))
    if state.get("last_error") and last_attempt and _utc_now() - last_attempt < FAILED_CHECK_INTERVAL:
        return False
    return True


def ensure_ytdlp_binary(*, force_update: bool = False) -> Path:
    """Return a verified official yt-dlp executable, updating it when due."""
    with _update_lock():
        state = _read_state()
        installed_version = _executable_version(YTDLP_PATH)
        if not _check_is_due(state, force_update=force_update, executable_exists=bool(installed_version)):
            return YTDLP_PATH

        attempted_at = _utc_now().isoformat()
        try:
            latest_version, executable_url, checksums_url = _latest_release()
            if installed_version != latest_version:
                installed_version = _install_release(latest_version, executable_url, checksums_url)
                logger.info("Installed yt-dlp %s", installed_version)
            state.update(
                {
                    "installed_version": installed_version,
                    "last_attempt_at": attempted_at,
                    "last_successful_check_at": _utc_now().isoformat(),
                    "last_error": None,
                }
            )
            _write_state(state)
            return YTDLP_PATH
        except Exception as exc:
            state.update(
                {
                    "installed_version": installed_version,
                    "last_attempt_at": attempted_at,
                    "last_error": str(exc),
                }
            )
            _write_state(state)
            if installed_version:
                logger.warning("yt-dlp update failed; continuing with %s: %s", installed_version, exc)
                return YTDLP_PATH
            raise ManagedYtDlpError(str(exc)) from exc


def ytdlp_base_command(*, force_update: bool = False) -> list[str]:
    executable = ensure_ytdlp_binary(force_update=force_update)
    command = [str(executable)]
    node_path = shutil.which("node")
    if node_path:
        command.extend(["--js-runtimes", f"node:{node_path}"])
    else:
        logger.warning("Node 22+ was not found; full YouTube JavaScript challenge support is unavailable")
    return command
