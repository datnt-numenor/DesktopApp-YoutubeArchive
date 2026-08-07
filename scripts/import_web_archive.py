"""Import a local YoutubeArchive SQLite database and media into DesktopApp."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path, PureWindowsPath


MEDIA_SUFFIXES = {".mp3", ".mp4", ".m4a", ".webm", ".opus"}


def connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def media_for_association(source_downloads: Path, stored_path: str, youtube_id: str) -> Path:
    stored_name = PureWindowsPath(stored_path).name
    direct = source_downloads / stored_name
    if direct.is_file():
        return direct

    preferred = [source_downloads / f"{youtube_id}.mp3", source_downloads / f"{youtube_id}.mp4"]
    preferred.extend(
        candidate
        for candidate in source_downloads.glob(f"{youtube_id}.*")
        if candidate.suffix.lower() in MEDIA_SUFFIXES and not candidate.name.endswith(".part")
    )
    for candidate in preferred:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"No completed media file found for {youtube_id} (stored path: {stored_path})")


def copy_media(source_downloads: Path, target_downloads: Path) -> tuple[int, int]:
    target_downloads.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    for source_file in sorted(source_downloads.iterdir()):
        if not source_file.is_file() or source_file.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        target_file = target_downloads / source_file.name
        if target_file.exists():
            if target_file.stat().st_size != source_file.stat().st_size:
                raise RuntimeError(f"Refusing to overwrite a different existing file: {target_file}")
            skipped += 1
            continue
        shutil.copy2(source_file, target_file)
        copied += 1
    return copied, skipped


def import_archive(source_db: Path, source_downloads: Path, target_db: Path, target_downloads: Path) -> dict[str, object]:
    for required in (source_db, source_downloads, target_db):
        if not required.exists():
            raise FileNotFoundError(required)

    backup_dir = target_db.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"ytarchive-before-import-{timestamp}.db"
    shutil.copy2(target_db, backup_path)

    copied_files, skipped_files = copy_media(source_downloads, target_downloads)

    source = connect_read_only(source_db)
    target = sqlite3.connect(target_db)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys=ON")

    try:
        target_users = target.execute("SELECT id FROM users ORDER BY created_at, id").fetchall()
        if len(target_users) != 1:
            raise RuntimeError(f"Desktop database must contain exactly one local user; found {len(target_users)}")
        owner_id = target_users[0]["id"]

        source_playlists = source.execute("SELECT * FROM playlists ORDER BY id").fetchall()
        source_videos = source.execute("SELECT * FROM videos ORDER BY id").fetchall()
        source_links = source.execute(
            """
            SELECT pv.*, v.yt_video_id
            FROM playlist_video AS pv
            JOIN videos AS v ON v.id = pv.video_id
            ORDER BY pv.playlist_id, pv.video_id
            """
        ).fetchall()

        target.execute("BEGIN IMMEDIATE")
        playlist_map: dict[int, int] = {}
        inserted_playlists = 0
        merged_playlists = 0
        for playlist in source_playlists:
            existing = target.execute(
                "SELECT id FROM playlists WHERE owner_id = ? AND yt_playlist_id = ?",
                (owner_id, playlist["yt_playlist_id"]),
            ).fetchone()
            if existing:
                target_playlist_id = int(existing["id"])
                merged_playlists += 1
            else:
                cursor = target.execute(
                    """
                    INSERT INTO playlists (owner_id, yt_playlist_id, title, url, auto_sync, created_at, last_synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        playlist["yt_playlist_id"],
                        playlist["title"],
                        playlist["url"],
                        playlist["auto_sync"],
                        playlist["created_at"],
                        playlist["last_synced"],
                    ),
                )
                target_playlist_id = int(cursor.lastrowid)
                inserted_playlists += 1
            playlist_map[int(playlist["id"])] = target_playlist_id

        video_map: dict[int, int] = {}
        inserted_videos = 0
        for video in source_videos:
            existing = target.execute(
                "SELECT id FROM videos WHERE yt_video_id = ?",
                (video["yt_video_id"],),
            ).fetchone()
            if existing:
                target_video_id = int(existing["id"])
            else:
                cursor = target.execute(
                    """
                    INSERT INTO videos
                        (yt_video_id, title, channel_name, duration, thumbnail_url, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        video["yt_video_id"],
                        video["title"],
                        video["channel_name"],
                        video["duration"],
                        video["thumbnail_url"],
                        video["status"],
                        video["updated_at"],
                    ),
                )
                target_video_id = int(cursor.lastrowid)
                inserted_videos += 1
            video_map[int(video["id"])] = target_video_id

        inserted_links = 0
        merged_links = 0
        repaired_paths = 0
        for link in source_links:
            target_playlist_id = playlist_map[int(link["playlist_id"])]
            target_video_id = video_map[int(link["video_id"])]
            local_file_path = None
            format_saved = link["format_saved"]
            if link["local_file_path"]:
                media_file = media_for_association(source_downloads, link["local_file_path"], link["yt_video_id"])
                stored_name = PureWindowsPath(link["local_file_path"]).name
                repaired_paths += int(media_file.name != stored_name)
                local_file_path = (Path("downloads") / media_file.name).as_posix()
                format_saved = media_file.suffix.lower().lstrip(".")

            existing = target.execute(
                "SELECT local_file_path FROM playlist_video WHERE playlist_id = ? AND video_id = ?",
                (target_playlist_id, target_video_id),
            ).fetchone()
            if existing:
                merged_links += 1
                if not existing["local_file_path"] and local_file_path:
                    target.execute(
                        """
                        UPDATE playlist_video
                        SET local_file_path = ?, format_saved = ?, download_error = NULL,
                            last_download_attempt_at = ?
                        WHERE playlist_id = ? AND video_id = ?
                        """,
                        (
                            local_file_path,
                            format_saved,
                            link["last_download_attempt_at"],
                            target_playlist_id,
                            target_video_id,
                        ),
                    )
                continue

            target.execute(
                """
                INSERT INTO playlist_video
                    (playlist_id, video_id, added_at, local_file_path, format_saved, download_error, last_download_attempt_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_playlist_id,
                    target_video_id,
                    link["added_at"],
                    local_file_path,
                    format_saved,
                    link["download_error"],
                    link["last_download_attempt_at"],
                ),
            )
            inserted_links += 1

        storage_bytes = sum(path.stat().st_size for path in target_downloads.iterdir() if path.is_file())
        playlist_count = int(
            target.execute("SELECT count(1) FROM playlists WHERE owner_id = ?", (owner_id,)).fetchone()[0]
        )
        target.execute(
            """
            UPDATE users
            SET storage_used_bytes = ?, playlist_quota = max(playlist_quota, ?)
            WHERE id = ?
            """,
            (storage_bytes, playlist_count, owner_id),
        )
        target.commit()

        return {
            "backup": str(backup_path),
            "source_playlist_rows": len(source_playlists),
            "inserted_playlists": inserted_playlists,
            "merged_duplicate_playlists": merged_playlists,
            "inserted_videos": inserted_videos,
            "inserted_links": inserted_links,
            "merged_duplicate_links": merged_links,
            "repaired_media_paths": repaired_paths,
            "copied_media_files": copied_files,
            "skipped_existing_files": skipped_files,
            "storage_bytes": storage_bytes,
        }
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--source-downloads", type=Path, required=True)
    parser.add_argument("--target-db", type=Path, required=True)
    parser.add_argument("--target-downloads", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = import_archive(
        args.source_db.resolve(),
        args.source_downloads.resolve(),
        args.target_db.resolve(),
        args.target_downloads.resolve(),
    )
    for key, value in result.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
