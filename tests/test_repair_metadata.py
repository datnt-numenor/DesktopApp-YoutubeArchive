import sqlite3
from pathlib import Path

from scripts import repair_archived_metadata


def create_database(path: Path, *, include_playlist_video: bool) -> None:
    db = sqlite3.connect(path)
    db.execute(
        """
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY,
            yt_video_id TEXT NOT NULL,
            title TEXT,
            channel_name TEXT,
            duration INTEGER,
            thumbnail_url TEXT,
            status TEXT NOT NULL
        )
        """
    )
    if include_playlist_video:
        db.execute("CREATE TABLE playlist_video (video_id INTEGER, local_file_path TEXT)")
    db.commit()
    db.close()


def test_thumbnail_backfill_does_not_mark_good_download_as_deleted(tmp_path: Path) -> None:
    target_path = tmp_path / "target.db"
    source_path = tmp_path / "source.db"
    create_database(target_path, include_playlist_video=True)
    create_database(source_path, include_playlist_video=False)

    target = sqlite3.connect(target_path)
    target.execute(
        "INSERT INTO videos VALUES (1, 'public123', 'Good title', 'Channel', 120, NULL, 'DOWNLOADED')"
    )
    target.execute("INSERT INTO playlist_video VALUES (1, 'downloads/public123.mp3')")
    target.commit()
    target.close()

    source = sqlite3.connect(source_path)
    source.execute(
        "INSERT INTO videos VALUES (1, 'public123', 'Good title', 'Channel', 120, NULL, 'DOWNLOADED')"
    )
    source.commit()
    source.close()

    repair_archived_metadata.repair(target_path, source_path)

    target = sqlite3.connect(target_path)
    status = target.execute("SELECT status FROM videos WHERE yt_video_id = 'public123'").fetchone()[0]
    target.close()
    assert status == "DOWNLOADED"
