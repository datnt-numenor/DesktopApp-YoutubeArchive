import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import crud
from config import BASE_DIR, settings
from database import AsyncSessionLocal
from downloader import DownloaderError, cache_thumbnail, download_video, extract_playlist_metadata, extract_video_metadata
from models import Playlist, PlaylistSource, PlaylistVideo, SyncEventType, User
from storage import storage
from sync_lock import SyncAlreadyRunning, sync_process_lock

logger = logging.getLogger(__name__)

TaskState = Literal["queued", "running", "done", "failed"]
ACTIVE_TASK_STATES = {"queued", "running"}
METADATA_IMPORT_FORMAT = "metadata"
FINISHED_TASK_RETENTION = timedelta(hours=1)
PROCESS_LOCK_RETRY_SECONDS = 15.0
PROCESS_LOCK_MAX_WAIT_SECONDS = 6 * 60 * 60.0


@dataclass
class TaskVideoError:
    video_id: int
    yt_video_id: str
    title: str
    message: str


@dataclass
class TaskProgress:
    task_id: str
    status: TaskState
    playlist_id: int | None = None
    playlist_title: str | None = None
    owner_id: str | None = None
    format: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    progress: int | None = None
    error: str | None = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    current_video: str | None = None
    errors: list[TaskVideoError] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


task_registry: dict[str, TaskProgress] = {}
# The event loop only keeps weak references to tasks; hold them until they finish.
_background_tasks: set[asyncio.Task[None]] = set()


UNAVAILABLE_ERROR_MARKERS = (
    "video unavailable",
    "this video is not available",
    "private video",
    "video has been removed",
    "this video has been removed",
)


def build_media_object_key(owner_id: str, playlist_id: int, filename: str) -> str:
    safe_filename = filename.replace("\\", "/").split("/")[-1]
    return f"users/{owner_id}/playlists/{playlist_id}/{safe_filename}"


def is_youtube_unavailable_error(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in UNAVAILABLE_ERROR_MARKERS)


async def _probe_missing_playlist_videos(
    playlist: Playlist,
    metadata: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    current_yt_ids = {item["yt_video_id"] for item in metadata["videos"]}
    results: dict[str, dict[str, Any]] = {}
    for association in playlist.playlist_videos:
        video = association.video
        if video.yt_video_id in current_yt_ids:
            continue
        if association.youtube_unavailable_at is not None or association.removed_from_youtube_playlist_at is not None:
            # Already classified by an earlier sync; probing it again only slows the sync down
            # and adds YouTube requests. Missing results are treated as "unknown" (no change).
            continue
        url = f"https://www.youtube.com/watch?v={video.yt_video_id}"
        try:
            video_metadata = await extract_video_metadata(url)
        except DownloaderError as exc:
            message = str(exc) or exc.__class__.__name__
            if is_youtube_unavailable_error(message):
                results[video.yt_video_id] = {"state": "unavailable", "error": message}
            else:
                results[video.yt_video_id] = {"state": "unknown", "error": message}
                logger.warning(
                    "Could not verify whether missing playlist video %s is still available: %s",
                    video.yt_video_id,
                    message,
                )
        else:
            results[video.yt_video_id] = {"state": "available", "metadata": video_metadata}
    return results


def _save_task(task: TaskProgress) -> None:
    task_registry[task.task_id] = task


def _prune_finished_local_tasks() -> None:
    cutoff = datetime.now(timezone.utc) - FINISHED_TASK_RETENTION
    for task_id, task in list(task_registry.items()):
        if task.finished_at and datetime.fromisoformat(task.finished_at) < cutoff:
            task_registry.pop(task_id, None)


def create_task_status(playlist_id: int, owner_id: str, playlist_title: str, format_: str) -> TaskProgress:
    _prune_finished_local_tasks()
    task_id = str(uuid.uuid4())
    progress = TaskProgress(
        task_id=task_id,
        status="queued",
        playlist_id=playlist_id,
        playlist_title=playlist_title,
        owner_id=owner_id,
        format=format_,
        progress=0,
    )
    _save_task(progress)
    return progress


def get_task_status(task_id: str) -> TaskProgress | None:
    return task_registry.get(task_id)


def list_task_statuses(owner_id: str | None = None, playlist_id: int | None = None) -> list[TaskProgress]:
    tasks = list(task_registry.values())
    if owner_id is not None:
        tasks = [task for task in tasks if task.owner_id == owner_id]
    if playlist_id is not None:
        tasks = [task for task in tasks if task.playlist_id == playlist_id]
    tasks = [task for task in tasks if task.status in ACTIVE_TASK_STATES]
    return sorted(tasks, key=lambda task: task.created_at, reverse=True)[:10]


def find_active_task(playlist_id: int, owner_id: str, format_: str) -> TaskProgress | None:
    for task in task_registry.values():
        if (
            task.playlist_id == playlist_id
            and task.owner_id == owner_id
            and task.format == format_
            and task.status in ACTIVE_TASK_STATES
        ):
            return task
    return None


def _update_task(
    task_id: str,
    *,
    status: TaskState | None = None,
    progress: int | None = None,
    error: str | None = None,
    total: int | None = None,
    completed: int | None = None,
    failed: int | None = None,
    current_video: str | None = None,
    append_error: TaskVideoError | None = None,
) -> None:
    task = get_task_status(task_id)
    if not task:
        task = TaskProgress(task_id=task_id, status="queued", progress=0)

    task.updated_at = datetime.now(timezone.utc).isoformat()
    if status is not None:
        task.status = status
        if status in {"done", "failed"} and task.finished_at is None:
            task.finished_at = task.updated_at
    if progress is not None:
        task.progress = progress
    if error is not None:
        task.error = error
    if total is not None:
        task.total = total
    if completed is not None:
        task.completed = completed
    if failed is not None:
        task.failed = failed
    if current_video is not None:
        task.current_video = current_video
    if append_error is not None:
        task.errors.append(append_error)

    _save_task(task)


async def sync_playlist(
    session: AsyncSession,
    playlist: Playlist,
    owner: User,
    format_: str,
    task_id: str | None = None,
    history_source: str = "manual",
) -> None:
    if format_ != "mp3":
        raise ValueError("Only MP3 sync is supported")
    if playlist.source_type != PlaylistSource.YOUTUBE:
        raise ValueError("Custom playlists do not support source synchronization")
    history_run = await crud.create_sync_run(session, playlist, owner.id, history_source)
    if task_id:
        _update_task(task_id, status="running", progress=3, current_video="Reading playlist metadata")

    try:
        metadata = await extract_playlist_metadata(playlist.url)
        missing_video_availability = await _probe_missing_playlist_videos(playlist, metadata)
        newly_unavailable = []
        playlist = await crud.sync_playlist_metadata(
            session,
            playlist,
            metadata,
            newly_unavailable=newly_unavailable,
            missing_video_availability=missing_video_availability,
        )
        for video in newly_unavailable:
            await crud.record_sync_event(session, history_run, video, SyncEventType.UNAVAILABLE)
        await _cache_metadata_thumbnails(session, metadata)
        targets = await crud.list_available_download_targets(session, playlist.id, format_)
    except Exception as exc:
        await session.rollback()
        await session.refresh(history_run)
        await crud.finish_sync_run(session, history_run, error=str(exc) or exc.__class__.__name__)
        raise

    if not targets:
        if task_id:
            _update_task(task_id, status="done", progress=100, total=0, current_video="Nothing new to download")
        await crud.finish_sync_run(session, history_run)
        return

    try:
        total = len(targets)
        completed = 0
        failed = 0
        if task_id:
            _update_task(task_id, status="running", progress=5, total=total, completed=0, failed=0)

        for index, association in enumerate(targets, start=1):
            video_title = association.video.title
            try:
                if owner.storage_used_bytes >= owner.storage_quota_bytes:
                    message = "Storage quota reached"
                    logger.warning("%s for user %s", message, owner.email)
                    await crud.mark_video_download_failed(session, association, message)
                    await crud.record_sync_event(
                        session, history_run, association.video, SyncEventType.DOWNLOAD_FAILED, message
                    )
                    failed += 1
                    if task_id:
                        _update_task(
                            task_id,
                            failed=failed,
                            append_error=TaskVideoError(
                                video_id=association.video.id,
                                yt_video_id=association.video.yt_video_id,
                                title=video_title,
                                message=message,
                            ),
                        )
                    break

                if task_id:
                    _update_task(task_id, current_video=video_title)

                await crud.mark_video_download_started(session, association)
                expected_path = settings.downloads_dir / f"{association.video.yt_video_id}.mp3"
                existed_before = expected_path.exists()
                local_path = await download_video(
                    association.video.yt_video_id,
                    settings.downloads_dir,
                )
                file_size = local_path.stat().st_size
                storage_increment = 0 if existed_before else file_size
                if owner.storage_used_bytes + storage_increment > owner.storage_quota_bytes:
                    message = "This file would exceed storage quota"
                    logger.warning("Skipping %s because it would exceed user storage quota", local_path)
                    if not existed_before:
                        local_path.unlink(missing_ok=True)
                    await crud.mark_video_download_failed(session, association, message)
                    await crud.record_sync_event(
                        session, history_run, association.video, SyncEventType.DOWNLOAD_FAILED, message
                    )
                    failed += 1
                    if task_id:
                        _update_task(
                            task_id,
                            failed=failed,
                            append_error=TaskVideoError(
                                video_id=association.video.id,
                                yt_video_id=association.video.yt_video_id,
                                title=video_title,
                                message=message,
                            ),
                        )
                    continue

                object_key = await storage.upload_file(
                    local_path,
                    build_media_object_key(owner.id, association.playlist_id, local_path.name),
                )
                await crud.mark_video_downloaded(session, association, object_key, "mp3", storage_increment, owner)
                await crud.record_sync_event(session, history_run, association.video, SyncEventType.DOWNLOADED)
                completed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                message = str(exc) or exc.__class__.__name__
                logger.exception("Failed to download video %s", association.video.yt_video_id)
                if is_youtube_unavailable_error(message):
                    await crud.mark_video_unavailable_on_youtube(session, association, message)
                    await crud.record_sync_event(
                        session, history_run, association.video, SyncEventType.UNAVAILABLE, message
                    )
                else:
                    await crud.mark_video_download_failed(session, association, message)
                    await crud.record_sync_event(
                        session, history_run, association.video, SyncEventType.DOWNLOAD_FAILED, message
                    )
                if task_id:
                    _update_task(
                        task_id,
                        failed=failed,
                        append_error=TaskVideoError(
                            video_id=association.video.id,
                            yt_video_id=association.video.yt_video_id,
                            title=video_title,
                            message=message,
                        ),
                    )

            if task_id:
                _update_task(
                    task_id,
                    status="running",
                    progress=5 + int(index / total * 90),
                    completed=completed,
                    failed=failed,
                )

    except Exception as exc:
        # A database error inside the per-video error handling must not leave the run RUNNING.
        await session.rollback()
        await session.refresh(history_run)
        await crud.finish_sync_run(session, history_run, error=str(exc) or exc.__class__.__name__)
        raise

    if task_id:
        _update_task(task_id, status="done", progress=100, completed=completed, failed=failed, current_video="Finished")
    await crud.finish_sync_run(session, history_run)


async def import_custom_youtube_videos_by_id(
    playlist_id: int,
    owner_id: str,
    urls: list[str],
    task_id: str | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        playlist = await crud.get_custom_playlist_for_owner(session, playlist_id, owner_id)
        owner = await session.get(User, owner_id)
        if not owner:
            raise ValueError(f"Owner {owner_id} does not exist")
        history_run = await crud.create_sync_run(session, playlist, owner.id, "manual")
        total = len(urls)
        completed = 0
        failed = 0
        if task_id:
            _update_task(task_id, status="running", progress=3, total=total)

        for index, url in enumerate(urls, start=1):
            association: PlaylistVideo | None = None
            title = url
            try:
                metadata = await extract_video_metadata(url)
                title = metadata["title"]
                association = await crud.add_youtube_metadata_to_custom_playlist(
                    session, playlist.id, owner.id, metadata
                )
                await _cache_metadata_thumbnails(session, {"videos": [metadata]})
                if association.local_file_path:
                    completed += 1
                    continue
                if owner.storage_used_bytes >= owner.storage_quota_bytes:
                    raise RuntimeError("Storage quota reached")

                if task_id:
                    _update_task(task_id, current_video=title)
                await crud.mark_video_download_started(session, association)
                expected_path = settings.downloads_dir / f"{association.video.yt_video_id}.mp3"
                existed_before = expected_path.exists()
                local_path = await download_video(association.video.yt_video_id, settings.downloads_dir)
                file_size = local_path.stat().st_size
                storage_increment = 0 if existed_before else file_size
                if owner.storage_used_bytes + storage_increment > owner.storage_quota_bytes:
                    if not existed_before:
                        local_path.unlink(missing_ok=True)
                    raise RuntimeError("This file would exceed storage quota")
                object_key = await storage.upload_file(
                    local_path,
                    build_media_object_key(owner.id, association.playlist_id, local_path.name),
                )
                await crud.mark_video_downloaded(session, association, object_key, "mp3", storage_increment, owner)
                await crud.record_sync_event(session, history_run, association.video, SyncEventType.DOWNLOADED)
                completed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                message = str(exc) or exc.__class__.__name__
                logger.exception("Failed to import custom YouTube URL %s", url)
                if association:
                    await crud.mark_video_download_failed(session, association, message)
                    await crud.record_sync_event(
                        session, history_run, association.video, SyncEventType.DOWNLOAD_FAILED, message
                    )
                else:
                    await crud.record_sync_event_snapshot(
                        session,
                        history_run,
                        yt_video_id=url,
                        video_title=title,
                        event_type=SyncEventType.DOWNLOAD_FAILED,
                        message=message,
                    )
                if task_id:
                    _update_task(
                        task_id,
                        failed=failed,
                        append_error=TaskVideoError(
                            video_id=association.video_id if association else 0,
                            yt_video_id=association.video.yt_video_id if association else url,
                            title=title,
                            message=message,
                        ),
                    )
            finally:
                if task_id:
                    _update_task(
                        task_id,
                        status="running",
                        progress=3 + int(index / total * 94),
                        completed=completed,
                        failed=failed,
                    )

        await crud.finish_sync_run(
            session,
            history_run,
            error="All YouTube imports failed" if failed and not completed else None,
        )
        if task_id:
            _update_task(
                task_id,
                status="done" if completed or not failed else "failed",
                progress=100,
                completed=completed,
                failed=failed,
                current_video="Finished",
            )


async def import_playlist_metadata(session: AsyncSession, playlist: Playlist, task_id: str | None = None) -> None:
    if task_id:
        _update_task(task_id, status="running", progress=5, current_video="Reading playlist metadata")

    metadata = await extract_playlist_metadata(playlist.url)
    await crud.sync_playlist_metadata(session, playlist, metadata)
    await _cache_metadata_thumbnails(session, metadata)
    video_count = len(metadata.get("videos") or [])
    if task_id:
        _update_task(
            task_id,
            status="done",
            progress=100,
            total=video_count,
            completed=video_count,
            failed=0,
            current_video="Metadata imported",
        )


async def import_playlist_metadata_by_id(playlist_id: int, owner_id: str, task_id: str | None = None) -> None:
    async with AsyncSessionLocal() as session:
        playlist = await crud.get_playlist_for_owner(session, playlist_id, owner_id)
        await import_playlist_metadata(session, playlist, task_id)


async def sync_playlist_by_id(
    playlist_id: int,
    owner_id: str | None = None,
    format_: str = "mp3",
    task_id: str | None = None,
    *,
    acquire_process_lock: bool = True,
    history_source: str = "manual",
) -> None:
    if acquire_process_lock:
        with sync_process_lock():
            await _sync_playlist_by_id_unlocked(playlist_id, owner_id, format_, task_id, history_source)
        return
    await _sync_playlist_by_id_unlocked(playlist_id, owner_id, format_, task_id, history_source)


async def _sync_playlist_by_id_unlocked(
    playlist_id: int,
    owner_id: str | None,
    format_: str,
    task_id: str | None,
    history_source: str,
) -> None:
    async with AsyncSessionLocal() as session:
        if owner_id:
            playlist = await crud.get_playlist_for_owner(session, playlist_id, owner_id)
        else:
            result = await session.execute(
                select(Playlist)
                .where(Playlist.id == playlist_id)
                .options(selectinload(Playlist.playlist_videos).selectinload(PlaylistVideo.video))
            )
            playlist = result.scalar_one_or_none()
            if not playlist:
                raise ValueError(f"Playlist {playlist_id} does not exist")
        owner = await session.get(User, playlist.owner_id)
        if not owner:
            raise ValueError(f"Owner {playlist.owner_id} does not exist")
        await sync_playlist(session, playlist, owner, format_, task_id, history_source)


def _start_background_task(coroutine: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@dataclass
class DownloadJob:
    task_id: str
    description: str
    run: Callable[[], Awaitable[None]]


_download_queue: asyncio.Queue[DownloadJob] | None = None
_download_worker: asyncio.Task[None] | None = None


def _fail_orphaned_download_tasks() -> None:
    """Tasks queued on a previous event loop can never run; release them for new requests."""
    for task in task_registry.values():
        if task.format != METADATA_IMPORT_FORMAT and task.status in ACTIVE_TASK_STATES:
            _update_task(task.task_id, status="failed", error="The local server restarted before this task finished")


def _download_queue_for_current_loop() -> asyncio.Queue[DownloadJob]:
    global _download_queue, _download_worker
    loop = asyncio.get_running_loop()
    if _download_worker is None or _download_worker.done() or _download_worker.get_loop() is not loop:
        if _download_worker is not None:
            _fail_orphaned_download_tasks()
        _download_queue = asyncio.Queue()
        _download_worker = loop.create_task(_run_download_worker(_download_queue), name="ytarchive-download-queue")
    assert _download_queue is not None
    return _download_queue


async def _run_with_process_lock(job: DownloadJob) -> None:
    """Run one job while holding the cross-process lock shared with the scheduled sync."""
    waited_seconds = 0.0
    while True:
        lock = ExitStack()
        try:
            lock.enter_context(sync_process_lock())
        except SyncAlreadyRunning:
            lock.close()
            if waited_seconds >= PROCESS_LOCK_MAX_WAIT_SECONDS:
                raise
            _update_task(job.task_id, current_video="Waiting for the scheduled sync to finish")
            await asyncio.sleep(PROCESS_LOCK_RETRY_SECONDS)
            waited_seconds += PROCESS_LOCK_RETRY_SECONDS
            continue
        with lock:
            await job.run()
        return


async def _run_download_worker(queue: asyncio.Queue[DownloadJob]) -> None:
    while True:
        job = await queue.get()
        try:
            await _run_with_process_lock(job)
        except Exception as exc:  # noqa: BLE001 - one failed job must not stop the queue
            logger.exception("%s failed", job.description)
            _update_task(job.task_id, status="failed", error=str(exc) or exc.__class__.__name__)
        finally:
            queue.task_done()


def _enqueue_download_job(
    queue: asyncio.Queue[DownloadJob],
    progress: TaskProgress,
    description: str,
    run: Callable[[], Awaitable[None]],
) -> TaskProgress:
    _update_task(progress.task_id, current_video="Waiting in queue")
    queue.put_nowait(DownloadJob(task_id=progress.task_id, description=description, run=run))
    return progress


def enqueue_local_sync(playlist_id: int, owner_id: str, playlist_title: str, format_: str) -> TaskProgress:
    # Resolve the queue first so tasks orphaned by a server restart are released before deduplication.
    queue = _download_queue_for_current_loop()
    existing = find_active_task(playlist_id, owner_id, format_)
    if existing:
        return existing

    progress = create_task_status(playlist_id, owner_id, playlist_title, format_)
    return _enqueue_download_job(
        queue,
        progress,
        "Sync task",
        lambda: sync_playlist_by_id(playlist_id, owner_id, format_, progress.task_id, acquire_process_lock=False),
    )


def enqueue_custom_youtube_import(
    playlist_id: int,
    owner_id: str,
    playlist_title: str,
    urls: list[str],
) -> TaskProgress:
    queue = _download_queue_for_current_loop()
    progress = create_task_status(playlist_id, owner_id, playlist_title, "mp3")
    return _enqueue_download_job(
        queue,
        progress,
        "Custom YouTube import task",
        lambda: import_custom_youtube_videos_by_id(playlist_id, owner_id, urls, progress.task_id),
    )


def enqueue_local_metadata_import(playlist_id: int, owner_id: str, playlist_title: str) -> TaskProgress:
    existing = find_active_task(playlist_id, owner_id, METADATA_IMPORT_FORMAT)
    if existing:
        return existing

    progress = create_task_status(playlist_id, owner_id, playlist_title, METADATA_IMPORT_FORMAT)

    async def runner() -> None:
        try:
            await import_playlist_metadata_by_id(playlist_id, owner_id, progress.task_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Metadata import task failed")
            _update_task(progress.task_id, status="failed", error=str(exc))

    _start_background_task(runner())
    return progress


def enqueue_sync(playlist_id: int, owner_id: str, playlist_title: str, format_: str) -> TaskProgress:
    if format_ != "mp3":
        raise ValueError("Only MP3 sync is supported")
    return enqueue_local_sync(playlist_id, owner_id, playlist_title, format_)


def enqueue_metadata_import(playlist_id: int, owner_id: str, playlist_title: str) -> TaskProgress:
    return enqueue_local_metadata_import(playlist_id, owner_id, playlist_title)


async def _cache_metadata_thumbnails(session: AsyncSession, metadata: dict[str, Any]) -> None:
    semaphore = asyncio.Semaphore(6)

    async def cache_one(video_data: dict[str, Any]) -> tuple[str, str] | None:
        thumbnail_url = video_data.get("thumbnail_url")
        if not video_data.get("is_available_on_youtube", True) or not thumbnail_url:
            return None
        try:
            async with semaphore:
                local_path = await cache_thumbnail(
                    video_data["yt_video_id"],
                    thumbnail_url,
                    settings.thumbnails_dir,
                )
            if not local_path:
                return None
            try:
                stored_path = local_path.relative_to(BASE_DIR).as_posix()
            except ValueError:
                stored_path = local_path.as_posix()
            return video_data["yt_video_id"], stored_path
        except Exception:  # noqa: BLE001 - thumbnail failure must not fail playlist sync
            logger.warning("Could not cache thumbnail for %s", video_data.get("yt_video_id"), exc_info=True)
            return None

    results = await asyncio.gather(*(cache_one(video) for video in metadata.get("videos") or []))
    for result in results:
        if result:
            await crud.set_video_thumbnail_local_path(session, *result)
