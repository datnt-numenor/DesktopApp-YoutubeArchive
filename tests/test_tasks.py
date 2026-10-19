import asyncio
from contextlib import contextmanager, nullcontext, suppress
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

import tasks
import crud
from downloader import DownloaderError
from models import Playlist, PlaylistVideo, User, Video, VideoStatus


def setup_function() -> None:
    tasks.task_registry.clear()


@pytest_asyncio.fixture(autouse=True)
async def stop_download_worker():
    yield
    worker = tasks._download_worker
    tasks._download_worker = None
    tasks._download_queue = None
    if worker is not None and not worker.done():
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


def test_unavailable_error_detection() -> None:
    assert tasks.is_youtube_unavailable_error("ERROR: Video unavailable. This video is not available")
    assert tasks.is_youtube_unavailable_error("Private video")
    assert not tasks.is_youtube_unavailable_error("HTTP Error 416: Requested range not satisfiable")


async def test_missing_playlist_video_probe_distinguishes_available_unavailable_and_unknown(monkeypatch) -> None:
    owner = User(email="probe@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="probe-list", title="Probe", url="https://example.com")
    videos = [
        Video(yt_video_id="public", title="Public", channel_name="", duration=0),
        Video(yt_video_id="private", title="Private", channel_name="", duration=0),
        Video(yt_video_id="blocked", title="Blocked", channel_name="", duration=0),
    ]
    playlist.playlist_videos = [PlaylistVideo(video=video) for video in videos]

    async def fake_extract(url: str):
        video_id = url.rsplit("=", 1)[-1]
        if video_id == "private":
            raise DownloaderError("ERROR: Video unavailable. This video is private")
        if video_id == "blocked":
            raise DownloaderError("ERROR: Sign in to confirm you're not a bot")
        return {
            "yt_video_id": video_id,
            "title": "Still public",
            "channel_name": "Channel",
            "duration": 120,
            "thumbnail_url": None,
            "is_available_on_youtube": True,
        }

    monkeypatch.setattr(tasks, "extract_video_metadata", fake_extract)

    result = await tasks._probe_missing_playlist_videos(
        playlist,
        {"videos": [], "title": "Probe", "url": playlist.url},
    )

    assert result["public"]["state"] == "available"
    assert result["private"]["state"] == "unavailable"
    assert result["blocked"]["state"] == "unknown"


async def test_missing_playlist_video_probe_skips_already_classified_videos(monkeypatch) -> None:
    owner = User(email="probe-skip@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="probe-skip", title="Probe", url="https://example.com")
    now = datetime.now(timezone.utc)
    playlist.playlist_videos = [
        PlaylistVideo(video=Video(yt_video_id="gone", title="Gone"), youtube_unavailable_at=now),
        PlaylistVideo(video=Video(yt_video_id="moved", title="Moved"), removed_from_youtube_playlist_at=now),
        PlaylistVideo(video=Video(yt_video_id="new-missing", title="New missing")),
    ]
    probed: list[str] = []

    async def fake_extract(url: str):
        probed.append(url.rsplit("=", 1)[-1])
        raise DownloaderError("ERROR: Video unavailable")

    monkeypatch.setattr(tasks, "extract_video_metadata", fake_extract)

    result = await tasks._probe_missing_playlist_videos(playlist, {"videos": []})

    assert probed == ["new-missing"]
    assert set(result) == {"new-missing"}


def test_finished_local_tasks_are_pruned_after_retention() -> None:
    old = tasks.create_task_status(1, "owner", "Old", "mp3")
    tasks._update_task(old.task_id, status="done")
    tasks.task_registry[old.task_id].finished_at = (
        datetime.now(timezone.utc) - tasks.FINISHED_TASK_RETENTION - timedelta(minutes=1)
    ).isoformat()
    running = tasks.create_task_status(2, "owner", "Running", "mp3")

    tasks.create_task_status(3, "owner", "New", "mp3")

    assert old.task_id not in tasks.task_registry
    assert running.task_id in tasks.task_registry


async def test_background_tasks_are_kept_until_they_finish() -> None:
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    tasks._start_background_task(work())
    assert len(tasks._background_tasks) == 1

    release.set()
    await asyncio.gather(*tasks._background_tasks)
    await asyncio.sleep(0)
    assert not tasks._background_tasks


def test_active_task_listing_hides_finished_tasks() -> None:
    active = tasks.create_task_status(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    finished = tasks.create_task_status(playlist_id=2, owner_id="owner-1", playlist_title="Old", format_="mp3")
    tasks._update_task(finished.task_id, status="done", progress=100)

    listed = tasks.list_task_statuses(owner_id="owner-1")

    assert [task.task_id for task in listed] == [active.task_id]
    assert tasks.find_active_task(1, "owner-1", "mp3") == active
    assert tasks.find_active_task(2, "owner-1", "mp3") is None


async def test_enqueue_sync_reuses_existing_active_task(monkeypatch) -> None:
    release = asyncio.Event()
    started = []

    async def fake_sync(playlist_id, owner_id, format_, task_id, **_kwargs):
        started.append(playlist_id)
        await release.wait()

    monkeypatch.setattr(tasks, "sync_playlist_by_id", fake_sync)
    monkeypatch.setattr(tasks, "sync_process_lock", nullcontext)

    first = tasks.enqueue_local_sync(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    second = tasks.enqueue_local_sync(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    await asyncio.sleep(0.01)
    release.set()
    await tasks._download_queue.join()

    assert first is second
    assert started == [1]


async def test_download_jobs_run_one_at_a_time_in_order(monkeypatch) -> None:
    events = []

    async def fake_sync(playlist_id, owner_id, format_, task_id, **kwargs):
        assert kwargs == {"acquire_process_lock": False}
        events.append(("start", playlist_id))
        await asyncio.sleep(0.01)
        events.append(("end", playlist_id))
        tasks._update_task(task_id, status="done")

    async def fake_import(playlist_id, owner_id, urls, task_id):
        events.append(("start", "import"))
        events.append(("end", "import"))
        tasks._update_task(task_id, status="done")

    monkeypatch.setattr(tasks, "sync_playlist_by_id", fake_sync)
    monkeypatch.setattr(tasks, "import_custom_youtube_videos_by_id", fake_import)
    monkeypatch.setattr(tasks, "sync_process_lock", nullcontext)

    first = tasks.enqueue_local_sync(1, "owner-1", "First", "mp3")
    second = tasks.enqueue_local_sync(2, "owner-1", "Second", "mp3")
    imported = tasks.enqueue_custom_youtube_import(3, "owner-1", "Custom", ["https://youtu.be/abc"])

    assert second.status == "queued"
    assert second.current_video == "Waiting in queue"
    await tasks._download_queue.join()

    assert events == [
        ("start", 1),
        ("end", 1),
        ("start", 2),
        ("end", 2),
        ("start", "import"),
        ("end", "import"),
    ]
    assert {first.status, second.status, imported.status} == {"done"}


async def test_failed_download_job_does_not_stop_the_queue(monkeypatch) -> None:
    async def fake_sync(playlist_id, owner_id, format_, task_id, **_kwargs):
        if playlist_id == 1:
            raise RuntimeError("yt-dlp crashed")
        tasks._update_task(task_id, status="done")

    monkeypatch.setattr(tasks, "sync_playlist_by_id", fake_sync)
    monkeypatch.setattr(tasks, "sync_process_lock", nullcontext)

    failed = tasks.enqueue_local_sync(1, "owner-1", "Broken", "mp3")
    succeeded = tasks.enqueue_local_sync(2, "owner-1", "Fine", "mp3")
    await tasks._download_queue.join()

    assert failed.status == "failed"
    assert failed.error == "yt-dlp crashed"
    assert succeeded.status == "done"


async def test_download_job_waits_while_scheduled_sync_holds_the_lock(monkeypatch) -> None:
    attempts = []

    @contextmanager
    def busy_then_free_lock():
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise tasks.SyncAlreadyRunning("busy")
        yield

    async def fake_sync(playlist_id, owner_id, format_, task_id, **_kwargs):
        tasks._update_task(task_id, status="done")

    monkeypatch.setattr(tasks, "sync_playlist_by_id", fake_sync)
    monkeypatch.setattr(tasks, "sync_process_lock", busy_then_free_lock)
    monkeypatch.setattr(tasks, "PROCESS_LOCK_RETRY_SECONDS", 0.001)

    progress = tasks.enqueue_local_sync(1, "owner-1", "Waiting", "mp3")
    await tasks._download_queue.join()

    assert len(attempts) == 3
    assert progress.status == "done"


def test_tasks_from_a_previous_server_loop_are_released() -> None:
    stale = tasks.create_task_status(1, "owner-1", "Stale", "mp3")
    metadata = tasks.create_task_status(2, "owner-1", "Importing", tasks.METADATA_IMPORT_FORMAT)

    tasks._fail_orphaned_download_tasks()

    assert stale.status == "failed"
    assert metadata.status == "queued"
    assert tasks.find_active_task(1, "owner-1", "mp3") is None


def test_enqueue_metadata_import_reuses_existing_active_task(monkeypatch) -> None:
    created_coroutines = []

    def fake_start_background_task(coro):
        created_coroutines.append(coro)
        coro.close()

    monkeypatch.setattr(tasks, "_start_background_task", fake_start_background_task)

    first = tasks.enqueue_local_metadata_import(playlist_id=1, owner_id="owner-1", playlist_title="Importing")
    second = tasks.enqueue_local_metadata_import(playlist_id=1, owner_id="owner-1", playlist_title="Importing")

    assert first is second
    assert first.format == tasks.METADATA_IMPORT_FORMAT
    assert len(created_coroutines) == 1


async def test_sync_playlist_records_successful_download_history(session, monkeypatch, tmp_path) -> None:
    owner = User(email="download-history@example.com", storage_quota_bytes=1024 * 1024)
    playlist = Playlist(owner=owner, yt_playlist_id="download-history", title="Downloads", url="https://example.com")
    video = Video(
        yt_video_id="download-event",
        title="New song",
        channel_name="Channel",
        duration=120,
        status=VideoStatus.AVAILABLE,
    )
    session.add_all([owner, playlist, video, PlaylistVideo(playlist=playlist, video=video)])
    await session.commit()
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)
    media_path = tmp_path / "download-event.mp3"
    media_path.write_bytes(b"mp3")

    async def fake_extract(_url):
        return {
            "title": "Downloads",
            "url": "https://example.com",
            "videos": [
                {
                    "yt_video_id": "download-event",
                    "title": "New song",
                    "channel_name": "Channel",
                    "duration": 120,
                    "thumbnail_url": None,
                    "is_available_on_youtube": True,
                }
            ],
        }

    async def fake_cache(*_args):
        return None

    async def fake_download(*_args):
        return media_path

    async def fake_upload(_path, key):
        return key

    monkeypatch.setattr(tasks, "extract_playlist_metadata", fake_extract)
    monkeypatch.setattr(tasks, "_cache_metadata_thumbnails", fake_cache)
    monkeypatch.setattr(tasks, "download_video", fake_download)
    monkeypatch.setattr(tasks.storage, "upload_file", fake_upload)

    await tasks.sync_playlist(session, playlist, owner, "mp3", history_source="scheduled")
    history = await crud.get_today_sync_history(session, owner.id)

    assert history["downloaded_count"] == 1
    assert history["runs"][0]["events"][0]["video_title"] == "New song"


async def test_sync_playlist_records_download_failure_history(session, monkeypatch) -> None:
    owner = User(email="failure-history@example.com", storage_quota_bytes=1024 * 1024)
    playlist = Playlist(owner=owner, yt_playlist_id="failure-history", title="Failures", url="https://example.com")
    video = Video(
        yt_video_id="failure-event",
        title="Failed song",
        channel_name="Channel",
        duration=120,
        status=VideoStatus.AVAILABLE,
    )
    session.add_all([owner, playlist, video, PlaylistVideo(playlist=playlist, video=video)])
    await session.commit()
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)

    async def fake_extract(_url):
        return {
            "title": "Failures",
            "url": "https://example.com",
            "videos": [
                {
                    "yt_video_id": "failure-event",
                    "title": "Failed song",
                    "channel_name": "Channel",
                    "duration": 120,
                    "thumbnail_url": None,
                    "is_available_on_youtube": True,
                }
            ],
        }

    async def fake_cache(*_args):
        return None

    async def fake_download(*_args):
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(tasks, "extract_playlist_metadata", fake_extract)
    monkeypatch.setattr(tasks, "_cache_metadata_thumbnails", fake_cache)
    monkeypatch.setattr(tasks, "download_video", fake_download)

    await tasks.sync_playlist(session, playlist, owner, "mp3", history_source="manual")
    history = await crud.get_today_sync_history(session, owner.id)

    assert history["failed_count"] == 1
    assert history["runs"][0]["status"] == "PARTIAL"
    assert history["runs"][0]["events"][0]["message"] == "HTTP 403"


async def test_closing_the_app_stops_sync_without_marking_videos_failed(session, monkeypatch) -> None:
    owner = User(email="cancel-history@example.com", storage_quota_bytes=1024 * 1024)
    playlist = Playlist(owner=owner, yt_playlist_id="cancel-history", title="Cancelled", url="https://example.com")
    videos = [
        Video(yt_video_id=f"cancel-{index}", title=f"Song {index}", status=VideoStatus.AVAILABLE)
        for index in range(2)
    ]
    session.add_all([owner, playlist, *videos, *(PlaylistVideo(playlist=playlist, video=video) for video in videos)])
    await session.commit()
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)

    async def fake_extract(_url):
        return {
            "title": "Cancelled",
            "url": "https://example.com",
            "videos": [
                {"yt_video_id": video.yt_video_id, "title": video.title, "is_available_on_youtube": True}
                for video in videos
            ],
        }

    async def fake_cache(*_args):
        return None

    download_attempts = []

    async def fake_download(video_id, *_args):
        download_attempts.append(video_id)
        raise tasks.DownloadCancelled("The app is closing; the download was cancelled")

    monkeypatch.setattr(tasks, "extract_playlist_metadata", fake_extract)
    monkeypatch.setattr(tasks, "_cache_metadata_thumbnails", fake_cache)
    monkeypatch.setattr(tasks, "download_video", fake_download)

    with pytest.raises(tasks.DownloadCancelled):
        await tasks.sync_playlist(session, playlist, owner, "mp3", history_source="manual")
    history = await crud.get_today_sync_history(session, owner.id)
    await session.refresh(playlist, ["playlist_videos"])

    assert len(download_attempts) == 1
    assert history["runs"][0]["status"] == "FAILED"
    assert history["runs"][0]["failed_count"] == 0
    assert all(association.download_error is None for association in playlist.playlist_videos)


def _conflict_once(monkeypatch) -> list[int]:
    real_sync_metadata = crud.sync_playlist_metadata
    calls: list[int] = []

    async def flaky_sync_metadata(*args, **kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise IntegrityError("INSERT INTO videos", {}, Exception("UNIQUE constraint failed: videos.yt_video_id"))
        return await real_sync_metadata(*args, **kwargs)

    monkeypatch.setattr(tasks.crud, "sync_playlist_metadata", flaky_sync_metadata)
    return calls


async def test_metadata_import_retries_after_a_concurrent_insert(session, monkeypatch) -> None:
    owner = User(email="retry-import@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="retry-import", title="Importing", url="https://example.com")
    session.add_all([owner, playlist])
    await session.commit()
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)

    async def fake_extract(_url):
        return {"title": "Imported", "url": "https://example.com", "videos": [{"yt_video_id": "shared", "title": "Shared"}]}

    async def fake_cache(*_args):
        return None

    monkeypatch.setattr(tasks, "extract_playlist_metadata", fake_extract)
    monkeypatch.setattr(tasks, "_cache_metadata_thumbnails", fake_cache)
    calls = _conflict_once(monkeypatch)

    playlist_id, owner_id = playlist.id, owner.id

    await tasks.import_playlist_metadata(session, playlist)
    reloaded = await crud.get_playlist_for_owner(session, playlist_id, owner_id)

    assert calls == [1, 2]
    assert reloaded.title == "Imported"
    assert [association.video.yt_video_id for association in reloaded.playlist_videos] == ["shared"]


async def test_sync_keeps_history_and_owner_usable_after_a_metadata_retry(session, monkeypatch, tmp_path) -> None:
    owner = User(email="retry-sync@example.com", storage_quota_bytes=1024 * 1024)
    playlist = Playlist(owner=owner, yt_playlist_id="retry-sync", title="Sync", url="https://example.com")
    session.add_all([owner, playlist])
    await session.commit()
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)
    downloaded = tmp_path / "shared-sync.mp3"

    async def fake_extract(_url):
        return {"title": "Sync", "url": "https://example.com", "videos": [{"yt_video_id": "shared-sync", "title": "Song"}]}

    async def fake_cache(*_args):
        return None

    async def fake_download(_video_id, _output_dir):
        downloaded.write_bytes(b"mp3")
        return downloaded

    async def fake_upload(_path, _key):
        return "downloads/shared-sync.mp3"

    monkeypatch.setattr(tasks, "extract_playlist_metadata", fake_extract)
    monkeypatch.setattr(tasks, "_cache_metadata_thumbnails", fake_cache)
    monkeypatch.setattr(tasks, "download_video", fake_download)
    monkeypatch.setattr(tasks.storage, "upload_file", fake_upload)
    monkeypatch.setattr(tasks.settings, "downloads_dir", tmp_path)
    calls = _conflict_once(monkeypatch)

    await tasks.sync_playlist(session, playlist, owner, "mp3", history_source="manual")
    history = await crud.get_today_sync_history(session, owner.id)

    assert calls == [1, 2]
    assert history["runs"][0]["status"] == "COMPLETED"
    assert history["downloaded_count"] == 1


async def test_metadata_write_gives_up_after_repeated_conflicts(session, monkeypatch) -> None:
    owner = User(email="retry-give-up@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="retry-give-up", title="Stuck", url="https://example.com")
    session.add_all([owner, playlist])
    await session.commit()
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)
    attempts = []

    async def always_conflicts(*_args, **_kwargs):
        attempts.append(True)
        raise IntegrityError("INSERT INTO videos", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(tasks.crud, "sync_playlist_metadata", always_conflicts)

    with pytest.raises(IntegrityError):
        await tasks._write_playlist_metadata(session, playlist, {"title": "x", "url": "y", "videos": []})

    assert len(attempts) == tasks.METADATA_WRITE_ATTEMPTS
