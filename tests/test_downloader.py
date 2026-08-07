from pathlib import Path

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
    captured_options: dict[str, object] = {}

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            captured_options.update(options)

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def download(self, urls: list[str]) -> int:
            (tmp_path / "video456.mp3").write_bytes(b"mp3")
            return 0

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    result = downloader._download_video_sync("video456", tmp_path)

    assert result == tmp_path / "video456.mp3"
    assert str(captured_options["format"]).startswith("bestaudio[ext=m4a]")
