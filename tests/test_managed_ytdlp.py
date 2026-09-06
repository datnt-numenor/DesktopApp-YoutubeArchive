from __future__ import annotations

import hashlib
from pathlib import Path

import managed_ytdlp
import pytest


def configure_tool_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(managed_ytdlp, "TOOLS_DIR", tmp_path)
    monkeypatch.setattr(managed_ytdlp, "YTDLP_PATH", tmp_path / "yt-dlp.exe")
    monkeypatch.setattr(managed_ytdlp, "YTDLP_PREVIOUS_PATH", tmp_path / "yt-dlp.previous.exe")
    monkeypatch.setattr(managed_ytdlp, "YTDLP_STATE_PATH", tmp_path / "yt-dlp-state.json")
    monkeypatch.setattr(managed_ytdlp, "YTDLP_UPDATE_LOCK_PATH", tmp_path / "yt-dlp-update.lock")


def test_managed_binary_is_downloaded_and_checksum_verified(monkeypatch, tmp_path: Path) -> None:
    configure_tool_paths(monkeypatch, tmp_path)
    payload = b"official yt-dlp executable"
    checksum = hashlib.sha256(payload).hexdigest().encode()
    monkeypatch.setattr(
        managed_ytdlp,
        "_latest_release",
        lambda: ("2026.08.19", "https://example.test/yt-dlp.exe", "https://example.test/SHA2-256SUMS"),
    )
    monkeypatch.setattr(
        managed_ytdlp,
        "_request_bytes",
        lambda url: payload if url.endswith("yt-dlp.exe") else checksum + b"  yt-dlp.exe\n",
    )
    monkeypatch.setattr(
        managed_ytdlp,
        "_executable_version",
        lambda path: "2026.08.19" if path.is_file() else None,
    )

    result = managed_ytdlp.ensure_ytdlp_binary()

    assert result == tmp_path / "yt-dlp.exe"
    assert result.read_bytes() == payload
    state = managed_ytdlp._read_state()
    assert state["installed_version"] == "2026.08.19"
    assert state["last_updated_at"]
    assert state["previous_version"] is None
    assert state["last_error"] is None


def test_successful_update_records_previous_version_and_time(monkeypatch, tmp_path: Path) -> None:
    configure_tool_paths(monkeypatch, tmp_path)
    managed_ytdlp.YTDLP_PATH.write_bytes(b"old executable")
    monkeypatch.setattr(managed_ytdlp, "_executable_version", lambda _path: "2026.07.04")
    monkeypatch.setattr(
        managed_ytdlp,
        "_latest_release",
        lambda: ("2026.08.19", "https://example.test/yt-dlp.exe", "https://example.test/SHA2-256SUMS"),
    )
    monkeypatch.setattr(managed_ytdlp, "_install_release", lambda *_args: "2026.08.19")

    managed_ytdlp.ensure_ytdlp_binary(force_update=True)

    state = managed_ytdlp._read_state()
    assert state["installed_version"] == "2026.08.19"
    assert state["previous_version"] == "2026.07.04"
    assert state["last_updated_at"]


def test_status_reads_recorded_updater_details_without_network(monkeypatch, tmp_path: Path) -> None:
    configure_tool_paths(monkeypatch, tmp_path)
    managed_ytdlp._write_state(
        {
            "installed_version": "2026.08.19",
            "previous_version": "2026.07.04",
            "last_successful_check_at": "2026-08-27T15:19:52+00:00",
            "last_updated_at": "2026-08-27T15:19:50+00:00",
            "last_error": None,
        }
    )

    status = managed_ytdlp.get_ytdlp_status()

    assert status["installed_version"] == "2026.08.19"
    assert status["previous_version"] == "2026.07.04"
    assert status["last_updated_at"] == "2026-08-27T15:19:50+00:00"
    assert status["last_error"] is None


def test_failed_update_keeps_existing_binary(monkeypatch, tmp_path: Path) -> None:
    configure_tool_paths(monkeypatch, tmp_path)
    managed_ytdlp.YTDLP_PATH.write_bytes(b"working version")
    monkeypatch.setattr(
        managed_ytdlp,
        "_executable_version",
        lambda path: "2026.07.04" if path == managed_ytdlp.YTDLP_PATH and path.is_file() else None,
    )
    monkeypatch.setattr(
        managed_ytdlp,
        "_latest_release",
        lambda: (_ for _ in ()).throw(managed_ytdlp.ManagedYtDlpError("GitHub unavailable")),
    )

    result = managed_ytdlp.ensure_ytdlp_binary(force_update=True)

    assert result == managed_ytdlp.YTDLP_PATH
    assert result.read_bytes() == b"working version"
    assert "GitHub unavailable" in managed_ytdlp._read_state()["last_error"]


def test_invalid_replacement_rolls_back_to_previous_binary(monkeypatch, tmp_path: Path) -> None:
    configure_tool_paths(monkeypatch, tmp_path)
    managed_ytdlp.YTDLP_PATH.write_bytes(b"old executable")
    payload = b"new executable"
    checksum = hashlib.sha256(payload).hexdigest().encode()
    monkeypatch.setattr(
        managed_ytdlp,
        "_request_bytes",
        lambda url: payload if url.endswith("yt-dlp.exe") else checksum + b"  yt-dlp.exe\n",
    )

    def fake_version(path: Path) -> str | None:
        if not path.is_file():
            return None
        if path.name == "yt-dlp.download.exe":
            return "2026.08.19"
        if path == managed_ytdlp.YTDLP_PATH and path.read_bytes() == b"new executable":
            return None
        return "2026.07.04"

    monkeypatch.setattr(managed_ytdlp, "_executable_version", fake_version)

    with pytest.raises(managed_ytdlp.ManagedYtDlpError, match="startup check"):
        managed_ytdlp._install_release(
            "2026.08.19",
            "https://example.test/yt-dlp.exe",
            "https://example.test/SHA2-256SUMS",
        )

    assert managed_ytdlp.YTDLP_PATH.read_bytes() == b"old executable"


def test_command_enables_existing_node_runtime(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "yt-dlp.exe"
    executable.write_bytes(b"exe")
    monkeypatch.setattr(managed_ytdlp, "ensure_ytdlp_binary", lambda **_kwargs: executable)
    monkeypatch.setattr(managed_ytdlp.shutil, "which", lambda name: r"C:\Program Files\nodejs\node.exe")

    command = managed_ytdlp.ytdlp_base_command()

    assert command == [
        str(executable),
        "--js-runtimes",
        r"node:C:\Program Files\nodejs\node.exe",
    ]
