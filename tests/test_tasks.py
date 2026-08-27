import tasks
import crud
from downloader import DownloaderError
from models import Playlist, PlaylistVideo, User, Video, VideoStatus
from redis.exceptions import RedisError


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def sadd(self, key: str, value: str) -> None:
        self.sets.setdefault(key, set()).add(value)

    def expire(self, _key: str, _ttl: int) -> None:
        return None

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def scan_iter(self, pattern: str):
        prefix = pattern.removesuffix("*")
        for key in self.values:
            if key.startswith(prefix):
                yield key

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class FailingRedis:
    def smembers(self, _key: str):
        raise RedisError("redis unavailable")


def setup_function() -> None:
    tasks.settings.task_backend = "local"
    tasks.task_registry.clear()
    tasks._redis = None


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

    def fake_create_task(coro):
        created_coroutines.append(coro)
        coro.close()
        return object()

    monkeypatch.setattr(tasks.asyncio, "create_task", fake_create_task)

    first = tasks.enqueue_local_sync(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    second = tasks.enqueue_local_sync(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")

    assert first is second
    assert len(created_coroutines) == 1


def test_enqueue_metadata_import_reuses_existing_active_task(monkeypatch) -> None:
    created_coroutines = []

    def fake_create_task(coro):
        created_coroutines.append(coro)
        coro.close()
        return object()

    monkeypatch.setattr(tasks.asyncio, "create_task", fake_create_task)

    first = tasks.enqueue_local_metadata_import(playlist_id=1, owner_id="owner-1", playlist_title="Importing")
    second = tasks.enqueue_local_metadata_import(playlist_id=1, owner_id="owner-1", playlist_title="Importing")

    assert first is second
    assert first.format == tasks.METADATA_IMPORT_FORMAT
    assert len(created_coroutines) == 1


def test_redis_task_store_tracks_active_and_finished_tasks(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(tasks.settings, "task_backend", "celery")
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)

    progress = tasks.create_task_status(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    tasks._update_task(
        progress.task_id,
        status="running",
        total=1,
        append_error=tasks.TaskVideoError(video_id=10, yt_video_id="yt10", title="Song", message="failed once"),
    )

    loaded = tasks.get_task_status(progress.task_id)
    listed = tasks.list_task_statuses(owner_id="owner-1")

    assert loaded is not None
    assert loaded.status == "running"
    assert loaded.errors[0].message == "failed once"
    assert [task.task_id for task in listed] == [progress.task_id]
    assert tasks.find_active_task(1, "owner-1", "mp3").task_id == progress.task_id

    tasks._update_task(progress.task_id, status="done", progress=100)

    assert tasks.find_active_task(1, "owner-1", "mp3") is None
    assert tasks.list_task_statuses(owner_id="owner-1") == []


def test_redis_client_uses_short_timeouts(monkeypatch) -> None:
    captured_kwargs = {}

    def fake_from_url(*_args, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeRedis()

    monkeypatch.setattr(tasks.settings, "task_backend", "celery")
    monkeypatch.setattr(tasks.settings, "redis_socket_timeout_seconds", 1.25)
    monkeypatch.setattr(tasks.settings, "redis_socket_connect_timeout_seconds", 0.75)
    monkeypatch.setattr(tasks.Redis, "from_url", fake_from_url)

    assert isinstance(tasks._redis_client(), FakeRedis)
    assert captured_kwargs["socket_timeout"] == 1.25
    assert captured_kwargs["socket_connect_timeout"] == 0.75
    assert captured_kwargs["health_check_interval"] == 30
    assert captured_kwargs["retry_on_timeout"] is False


def test_redis_task_listing_fails_fast_to_empty(monkeypatch) -> None:
    monkeypatch.setattr(tasks.settings, "task_backend", "celery")
    monkeypatch.setattr(tasks, "_redis_client", lambda: FailingRedis())

    assert tasks.list_task_statuses(owner_id="owner-1") == []


def test_enqueue_sync_uses_celery_backend_and_reuses_active_task(monkeypatch) -> None:
    fake_redis = FakeRedis()
    sent_tasks = []

    def fake_send(playlist_id: int, owner_id: str, format_: str, task_id: str) -> None:
        sent_tasks.append((playlist_id, owner_id, format_, task_id))

    monkeypatch.setattr(tasks.settings, "task_backend", "celery")
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)
    monkeypatch.setattr(tasks, "_send_celery_sync_task", fake_send)

    first = tasks.enqueue_sync(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    second = tasks.enqueue_sync(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")

    assert first.task_id == second.task_id
    assert sent_tasks == [(1, "owner-1", "mp3", first.task_id)]


def test_enqueue_metadata_import_uses_celery_backend_and_reuses_active_task(monkeypatch) -> None:
    fake_redis = FakeRedis()
    sent_tasks = []

    def fake_send(playlist_id: int, owner_id: str, task_id: str) -> None:
        sent_tasks.append((playlist_id, owner_id, task_id))

    monkeypatch.setattr(tasks.settings, "task_backend", "celery")
    monkeypatch.setattr(tasks, "_redis_client", lambda: fake_redis)
    monkeypatch.setattr(tasks, "_send_celery_metadata_import_task", fake_send)

    first = tasks.enqueue_metadata_import(playlist_id=1, owner_id="owner-1", playlist_title="Importing")
    second = tasks.enqueue_metadata_import(playlist_id=1, owner_id="owner-1", playlist_title="Importing")

    assert first.task_id == second.task_id
    assert first.format == tasks.METADATA_IMPORT_FORMAT
    assert sent_tasks == [(1, "owner-1", first.task_id)]


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
