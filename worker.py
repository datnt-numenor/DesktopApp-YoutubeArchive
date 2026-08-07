import asyncio

from celery import Celery

from config import settings
from database import engine
from tasks import _update_task, import_playlist_metadata_by_id, sync_playlist_by_id


celery_app = Celery("ytarchive", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
)


async def run_sync_playlist_job(playlist_id: int, owner_id: str, format_: str, task_id: str) -> None:
    try:
        await sync_playlist_by_id(playlist_id, owner_id, format_, task_id)
    finally:
        # Celery tasks call asyncio.run(), which creates a fresh event loop each
        # time. Asyncpg connections are loop-bound, so clear the SQLAlchemy pool
        # before that loop closes.
        await engine.dispose()


async def run_import_playlist_metadata_job(playlist_id: int, owner_id: str, task_id: str) -> None:
    try:
        await import_playlist_metadata_by_id(playlist_id, owner_id, task_id)
    finally:
        await engine.dispose()


@celery_app.task(name="sync_playlist_task", bind=True)
def sync_playlist_task(self, playlist_id: int, owner_id: str, format_: str = "mp3", task_id: str | None = None) -> dict[str, str]:
    resolved_task_id = task_id or self.request.id
    try:
        asyncio.run(run_sync_playlist_job(playlist_id, owner_id, format_, resolved_task_id))
    except Exception as exc:  # noqa: BLE001
        _update_task(resolved_task_id, status="failed", error=str(exc) or exc.__class__.__name__)
        raise
    return {"task_id": resolved_task_id, "status": "done"}


@celery_app.task(name="import_playlist_metadata_task", bind=True)
def import_playlist_metadata_task(self, playlist_id: int, owner_id: str, task_id: str | None = None) -> dict[str, str]:
    resolved_task_id = task_id or self.request.id
    try:
        asyncio.run(run_import_playlist_metadata_job(playlist_id, owner_id, resolved_task_id))
    except Exception as exc:  # noqa: BLE001
        _update_task(resolved_task_id, status="failed", error=str(exc) or exc.__class__.__name__)
        raise
    return {"task_id": resolved_task_id, "status": "done"}
