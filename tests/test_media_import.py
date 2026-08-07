from pathlib import Path

import pytest
from fastapi import HTTPException

import media_import


class FakeUploadRequest:
    def __init__(self, filename: str, chunks: list[bytes]) -> None:
        self.headers = {"x-file-name": filename}
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


async def test_mp3_upload_uses_hash_and_deduplicates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(media_import, "_probe_mp3", lambda _path, fallback: (fallback, "Artist", 123))
    downloads = tmp_path / "downloads"

    first = await media_import.receive_mp3_upload(
        FakeUploadRequest("My Song.mp3", [b"same", b" bytes"]), downloads, 1024
    )
    second = await media_import.receive_mp3_upload(
        FakeUploadRequest("Renamed.mp3", [b"same bytes"]), downloads, 1024
    )

    assert first.path == second.path
    assert first.is_new_file is True
    assert second.is_new_file is False
    assert first.title == "My Song"
    assert first.artist == "Artist"
    assert first.duration == 123
    assert first.path.read_bytes() == b"same bytes"


async def test_mp3_upload_rejects_wrong_extension_and_quota(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(HTTPException) as extension_error:
        await media_import.receive_mp3_upload(FakeUploadRequest("song.wav", [b"data"]), tmp_path, 1024)
    assert extension_error.value.status_code == 415

    monkeypatch.setattr(media_import, "_probe_mp3", lambda _path, fallback: (fallback, "", 0))
    with pytest.raises(HTTPException) as quota_error:
        await media_import.receive_mp3_upload(FakeUploadRequest("song.mp3", [b"12345"]), tmp_path, 4)
    assert quota_error.value.status_code == 413
