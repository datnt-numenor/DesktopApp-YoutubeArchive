import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import crud
from config import settings
from database import AsyncSessionLocal
from tasks import sync_playlist_by_id

logger = logging.getLogger(__name__)

_sync_lock = asyncio.Lock()
scheduler = AsyncIOScheduler()


async def auto_sync_job() -> None:
    if _sync_lock.locked():
        logger.info("Sync job is already running; skipping this schedule tick.")
        return

    async with _sync_lock:
        async with AsyncSessionLocal() as session:
            playlists = await crud.list_auto_sync_playlists(session)

        for playlist in playlists:
            try:
                await sync_playlist_by_id(playlist.id, format_="mp3", history_source="in_app")
            except Exception:
                logger.exception("Scheduled sync failed for playlist %s", playlist.id)


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        auto_sync_job,
        "interval",
        hours=settings.sync_interval_hours,
        id="auto_sync_playlists",
        replace_existing=True,
    )
    scheduler.start()


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
