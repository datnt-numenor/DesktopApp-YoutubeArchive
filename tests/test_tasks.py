import asyncio
from datetime import datetime, timedelta, timezone

import tasks
import crud
from downloader import DownloaderError
from models import Playlist, PlaylistVideo, User, Video, VideoStatus


def setup_function() -> None:
    tasks.task_registry.clear()


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


def test_enqueue_sync_reuses_existing_active_task(monkeypatch) -> None:
    created_coroutines = []

    def fake_start_background_task(coro):
        created_coroutines.append(coro)
        coro.close()

    monkeypatch.setattr(tasks, "_start_background_task", fake_start_background_task)

    first = tasks.enqueue_local_sync(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    second = tasks.enqueue_local_sync(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")

    assert first is second
    assert len(created_coroutines) == 1


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
