from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from downloader import _cache_thumbnail_sync  # noqa: E402


def is_placeholder(title: str | None, video_id: str) -> bool:
    normalized = (title or "").strip().lower()
    return not normalized or normalized == f"youtube video {video_id}".lower()


def stored_thumbnail_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def repair(target_path: Path, source_path: Path) -> tuple[Path, int, int]:
    if not target_path.is_file():
        raise FileNotFoundError(f"Target database not found: {target_path}")
    if not source_path.is_file():
        raise FileNotFoundError(f"Source database not found: {source_path}")

    backup_dir = target_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"ytarchive-before-metadata-repair-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(target_path, backup_path)

    target = sqlite3.connect(target_path)
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    target.row_factory = sqlite3.Row
    source.row_factory = sqlite3.Row
    repaired = 0
    cached = 0
    try:
        columns = {row[1] for row in target.execute("PRAGMA table_info(videos)")}
        if "thumbnail_local_path" not in columns:
            target.execute("ALTER TABLE videos ADD COLUMN thumbnail_local_path TEXT")

        rows = target.execute(
            """
            SELECT v.*,
                   EXISTS(
                       SELECT 1 FROM playlist_video pv
                       WHERE pv.video_id = v.id AND pv.local_file_path IS NOT NULL
                   ) AS has_local_file
            FROM videos v
            WHERE v.title = 'YouTube video ' || v.yt_video_id
               OR v.title = ''
               OR v.title IS NULL
               OR (
                   v.thumbnail_local_path IS NULL
                   AND EXISTS(
                       SELECT 1 FROM playlist_video pv
                       WHERE pv.video_id = v.id AND pv.local_file_path IS NOT NULL
                   )
               )
            """
        ).fetchall()

        thumbnail_candidates: list[tuple[int, str, str]] = []

        for current in rows:
            was_placeholder = is_placeholder(current["title"], current["yt_video_id"])
            historical = source.execute(
                "SELECT * FROM videos WHERE yt_video_id = ?",
                (current["yt_video_id"],),
            ).fetchone()
            values = dict(current)
            changed = False
            if historical:
                if is_placeholder(values["title"], values["yt_video_id"]) and not is_placeholder(
                    historical["title"], values["yt_video_id"]
                ):
                    values["title"] = historical["title"]
                    changed = True
                if not values["channel_name"] and historical["channel_name"]:
                    values["channel_name"] = historical["channel_name"]
                    changed = True
                if int(values["duration"] or 0) <= 0 and int(historical["duration"] or 0) > 0:
                    values["duration"] = historical["duration"]
                    changed = True
                if historical["thumbnail_url"]:
                    values["thumbnail_url"] = historical["thumbnail_url"]
                    changed = changed or historical["thumbnail_url"] != current["thumbnail_url"]

            if was_placeholder and values["status"] != "DELETED_ON_YT":
                values["status"] = "DELETED_ON_YT"
                changed = True

            thumbnail_url = values.get("thumbnail_url")
            if thumbnail_url and not values.get("thumbnail_local_path") and values["has_local_file"]:
                thumbnail_candidates.append((values["id"], values["yt_video_id"], thumbnail_url))

            if changed:
                target.execute(
                    """
                    UPDATE videos
                    SET title = ?, channel_name = ?, duration = ?, thumbnail_url = ?,
                        thumbnail_local_path = ?, status = ?
                    WHERE id = ?
                    """,
                    (
                        values["title"],
                        values["channel_name"],
                        values["duration"],
                        values["thumbnail_url"],
                        values.get("thumbnail_local_path"),
                        values["status"],
                        values["id"],
                    ),
                )
                repaired += 1

        def cache_candidate(candidate: tuple[int, str, str]) -> tuple[int, Path | None]:
            video_db_id, video_id, url = candidate
            return video_db_id, _cache_thumbnail_sync(video_id, url, settings.thumbnails_dir)

        with ThreadPoolExecutor(max_workers=8) as executor:
            for video_db_id, local_path in executor.map(cache_candidate, thumbnail_candidates):
                if not local_path:
                    continue
                target.execute(
                    "UPDATE videos SET thumbnail_local_path = ? WHERE id = ?",
                    (stored_thumbnail_path(local_path), video_db_id),
                )
                cached += 1
        target.commit()
    finally:
        source.close()
        target.close()
    return backup_path, repaired, cached


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore archived video metadata without overwriting good values.")
    parser.add_argument("--target-db", type=Path, default=ROOT / "data" / "ytarchive.db")
    parser.add_argument(
        "--source-db",
        type=Path,
        default=ROOT.parent / "Youtube Archive" / "YoutubeArchive-local-data" / "ytarchive.db",
    )
    args = parser.parse_args()
    backup, repaired, cached = repair(args.target_db.resolve(), args.source_db.resolve())
    print(f"Backup: {backup}")
    print(f"Repaired rows: {repaired}")
    print(f"Cached thumbnails: {cached}")


if __name__ == "__main__":
    main()
