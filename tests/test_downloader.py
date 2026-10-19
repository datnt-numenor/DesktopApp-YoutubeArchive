from pathlib import Path
import subprocess

import pytest

import downloader


def test_canonicalize_watch_playlist_url() -> None:
    url = "https://www.youtube.com/watch?v=abc123&list=PL_TEST_123&index=2"

    assert downloader.canonicalize_playlist_url(url) == "https://www.youtube.com/playlist?list=PL_TEST_123"


def test_extract_playlist_id_from_url() -> None:
    url = "https://www.youtube.com/watch?v=abc123&list=PL_TEST_123&index=2"

    assert downloader.extract_playlist_id_from_url(url) == "PL_TEST_123"


def test_normalize_unavailable_video_keeps_id_but_marks_it_unavailable() -> None:
    result = downloader._normalize_video(
        {
            "id": "deleted123",
            "title": None,
            "duration": None,
            "thumbnail": "https://example.com/deleted.jpg",
        }
    )

    assert result == {
        "yt_video_id": "deleted123",
        "title": "YouTube video deleted123",
        "channel_name": "",
        "duration": 0,
        "thumbnail_url": "https://example.com/deleted.jpg",
        "is_available_on_youtube": False,
    }


def test_normalize_private_video_marks_it_unavailable() -> None:
    result = downloader._normalize_video(
        {"id": "private123", "title": "[Private video]", "availability": "private"}
    )

    assert result is not None
    assert result["is_available_on_youtube"] is False


def test_existing_mp3_is_reused_and_temporary_files_are_cleaned(tmp_path: Path) -> None:
    mp3 = tmp_path / "video123.mp3"
    mp3.write_bytes(b"mp3")
    stale_files = [
        tmp_path / "video123.webm",
        tmp_path / "video123.m4a",
        tmp_path / "video123.webm.part",
        tmp_path / "video123.ytdl",
    ]
    for path in stale_files:
        path.write_bytes(b"stale")

    result = downloader._download_video_sync("video123", tmp_path)

    assert result == mp3
    assert mp3.exists()
    assert all(not path.exists() for path in stale_files)


def test_mp3_download_prefers_m4a_source(monkeypatch, tmp_path: Path) -> None:
    captured_arguments: list[str] = []

    def fake_run(arguments: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        captured_arguments.extend(arguments)
        (tmp_path / "video456.mp3").write_bytes(b"mp3")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(downloader, "_run_ytdlp", fake_run)

    result = downloader._download_video_sync("video456", tmp_path)

    assert result == tmp_path / "video456.mp3"
    format_index = captured_arguments.index("--format")
    assert captured_arguments[format_index + 1].startswith("bestaudio[ext=m4a]")
    assert "--audio-format" in captured_arguments
    assert captured_arguments[captured_arguments.index("--audio-format") + 1] == "mp3"


def test_transient_403_is_retried_with_forced_update(monkeypatch, tmp_path: Path) -> None:
    force_update_values: list[bool] = []

    def fake_download(_video_id: str, output_dir: Path, *, force_update: bool = False) -> Path:
        force_update_values.append(force_update)
        if len(force_update_values) == 1:
            raise downloader.DownloaderError("ERROR: unable to download video data: HTTP Error 403: Forbidden")
        target = output_dir / "video789.mp3"
        target.write_bytes(b"mp3")
        return target

    monkeypatch.setattr(downloader, "_download_once", fake_download)
    monkeypatch.setattr(downloader.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(downloader.random, "uniform", lambda _low, _high: 1.0)

    result = downloader._download_video_sync("video789", tmp_path)

    assert result == tmp_path / "video789.mp3"
    assert force_update_values == [False, True]


def test_unavailable_video_is_not_retried(monkeypatch, tmp_path: Path) -> None:
    attempts = 0

    def fake_download(_video_id: str, _output_dir: Path, *, force_update: bool = False) -> Path:
        nonlocal attempts
        attempts += 1
        raise downloader.DownloaderError("ERROR: Video unavailable. This video is private")

    monkeypatch.setattr(downloader, "_download_once", fake_download)

    with pytest.raises(downloader.DownloaderError, match="Video unavailable"):
        downloader._download_video_sync("private123", tmp_path)

    assert attempts == 1


@pytest.mark.parametrize("url", ["--exec=calc", "file:///C:/Windows/win.ini", "youtube.com/watch?v=abc"])
def test_metadata_extraction_rejects_non_web_urls_before_running_ytdlp(monkeypatch, url: str) -> None:
    def fail_if_called(_arguments):
        raise AssertionError("yt-dlp must not run for an invalid URL")

    monkeypatch.setattr(downloader, "_run_ytdlp_json", fail_if_called)

    with pytest.raises(downloader.DownloaderError):
        downloader._extract_video_sync(url)


def test_metadata_extraction_separates_url_from_ytdlp_options(monkeypatch) -> None:
    captured = {}

    def fake_run(arguments):
        captured["arguments"] = arguments
        return {"id": "abc", "title": "Song"}

    monkeypatch.setattr(downloader, "_run_ytdlp_json", fake_run)

    downloader._extract_video_sync("https://www.youtube.com/watch?v=abc")

    assert captured["arguments"][-2:] == ["--", "https://www.youtube.com/watch?v=abc"]
