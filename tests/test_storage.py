from pathlib import Path

import pytest
from fastapi import HTTPException

import storage as storage_module
import tasks


def test_build_media_object_key_scopes_file_by_owner_and_playlist() -> None:
    assert (
        tasks.build_media_object_key("owner-1", 42, r"nested\video123.mp3")
        == "users/owner-1/playlists/42/video123.mp3"
    )


async def test_local_storage_round_trips_paths_relative_to_data_dir(monkeypatch, tmp_path: Path) -> None:
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    media_file = downloads_dir / "song.mp3"
    media_file.write_bytes(b"mp3")
    monkeypatch.setattr(storage_module.settings, "downloads_dir", downloads_dir)
    storage = storage_module.LocalStorage()

    stored_path = await storage.upload_file(media_file, "users/owner/playlists/1/song.mp3")

    assert Path(stored_path) == Path("downloads") / "song.mp3"
    assert Path(await storage.get_file_url(stored_path)) == media_file


async def test_local_storage_reports_missing_media(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(storage_module.settings, "downloads_dir", tmp_path / "downloads")

    with pytest.raises(HTTPException) as error:
        await storage_module.LocalStorage().get_file_url("downloads/missing.mp3")

    assert error.value.status_code == 404
