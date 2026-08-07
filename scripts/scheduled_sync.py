from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crud  # noqa: E402
from config import LOCAL_DATA_DIR  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import PlaylistVideo, Video, VideoStatus  # noqa: E402
from sync_lock import SyncAlreadyRunning, sync_process_lock  # noqa: E402
from tasks import sync_playlist_by_id  # noqa: E402


LOG_DIR = LOCAL_DATA_DIR / "logs"
LOG_PATH = LOG_DIR / "scheduled-sync.log"
logger = logging.getLogger("scheduled_sync")


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)


async def count_pending_download_errors(playlist_id: int) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.count(PlaylistVideo.video_id))
            .join(PlaylistVideo.video)
            .where(
                PlaylistVideo.playlist_id == playlist_id,
                PlaylistVideo.hidden_at.is_(None),
                PlaylistVideo.local_file_path.is_(None),
                PlaylistVideo.download_error.is_not(None),
                Video.status != VideoStatus.DELETED_ON_YT,
            )
        )
        return int(result.scalar_one())


async def run_scheduled_sync(*, dry_run: bool = False) -> int:
    await init_db()
    async with AsyncSessionLocal() as session:
        await crud.cleanup_sync_history(session)
        playlists = await crud.list_auto_sync_playlists(session)

    logger.info("Scheduled MP3 sync started; %d playlist(s) enabled", len(playlists))
    if dry_run:
        for playlist in playlists:
            logger.info("Dry run: %s (%s)", playlist.title, playlist.id)
        await engine.dispose()
        return 0

    failures = 0
    try:
        with sync_process_lock():
            for playlist in playlists:
                try:
                    logger.info("Syncing playlist: %s (%s)", playlist.title, playlist.id)
                    await sync_playlist_by_id(
                        playlist.id,
                        playlist.owner_id,
                        "mp3",
                        acquire_process_lock=False,
                        history_source="scheduled",
                    )
                    pending_errors = await count_pending_download_errors(playlist.id)
                    if pending_errors:
                        failures += 1
                        logger.warning(
                            "Playlist completed with %d pending download error(s): %s",
                            pending_errors,
                            playlist.title,
                        )
                    else:
                        logger.info("Playlist completed: %s", playlist.title)
                except Exception:  # noqa: BLE001 - continue with the remaining playlists
                    failures += 1
                    logger.exception("Playlist failed: %s", playlist.title)
    except SyncAlreadyRunning:
        logger.warning("Scheduled sync skipped because another sync is already running")
        return 2
    finally:
        await engine.dispose()

    logger.info("Scheduled MP3 sync finished; failures=%d", failures)
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync all auto-sync playlists and download new MP3 files.")
    parser.add_argument("--dry-run", action="store_true", help="List eligible playlists without contacting YouTube.")
    args = parser.parse_args()
    configure_logging()
    raise SystemExit(asyncio.run(run_scheduled_sync(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
