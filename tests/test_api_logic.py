import json
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main as app_module
from main import (
    active_tasks,
    add_playlist,
    calculate_storage_level,
    healthz,
    hide_playlist_video,
    install_sync_schedule,
    restore_playlist_video,
    update_desktop_password,
    update_playlist_auto_sync,
    update_sync_schedule,
    yt_dlp_status,
)
from models import Playlist, PlaylistVideo, User, Video, VideoStatus
from schemas import (
    PasswordChangeRequest,
    PlaylistAddRequest,
    PlaylistAutoSyncRequest,
    PlaylistDetailResponse,
    PlaylistSchema,
    SyncScheduleRequest,
    SyncRequest,
    VideoSchema,
)
import auth
import tasks

LOCAL_BASE_URL = "http://127.0.0.1:8765"


def test_storage_warning_thresholds_and_mp3_only_request() -> None:
    assert calculate_storage_level(79) == "normal"
    assert calculate_storage_level(80) == "warning"
    assert calculate_storage_level(95) == "danger"
    assert calculate_storage_level(100) == "full"
    assert SyncRequest(format="mp3").format == "mp3"
    with pytest.raises(ValueError):
        SyncRequest(format="mp4")


@pytest.fixture(autouse=True)
def clear_task_registry() -> None:
    tasks.task_registry.clear()


async def test_active_tasks_endpoint_logic_returns_only_current_user_tasks() -> None:
    current = tasks.create_task_status(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    tasks.create_task_status(playlist_id=2, owner_id="owner-2", playlist_title="Other", format_="mp3")

    response = await active_tasks.__wrapped__(request=None, current_user=SimpleNamespace(id="owner-1"))

    assert len(response) == 1
    assert response[0].task_id == current.task_id
    assert response[0].playlist_id == 1


async def test_ytdlp_status_endpoint_returns_local_updater_state(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        "get_ytdlp_status",
        lambda: {
            "installed_version": "2026.08.19",
            "previous_version": "2026.07.04",
            "last_attempt_at": "2026-08-27T15:19:47+00:00",
            "last_successful_check_at": "2026-08-27T15:19:52+00:00",
            "last_updated_at": "2026-08-27T15:19:50+00:00",
            "last_error": None,
        },
    )

    response = await yt_dlp_status.__wrapped__(request=None, current_user=SimpleNamespace(id="owner-1"))

    assert response.installed_version == "2026.08.19"
    assert response.previous_version == "2026.07.04"
    assert response.last_error is None


async def test_soft_hide_and_restore_endpoints_keep_association(session) -> None:
    owner = User(id="soft-hide-owner", email="soft-hide@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="soft-hide-list", title="Soft hide", url="https://example.test")
    video = Video(
        yt_video_id="soft-hide-video",
        title="Removed song",
        channel_name="Channel",
        duration=123,
        status=VideoStatus.DELETED_ON_YT,
    )
    association = PlaylistVideo(playlist=playlist, video=video)
    session.add_all([owner, playlist, video, association])
    await session.commit()

    hidden_response = await hide_playlist_video.__wrapped__(
        request=None,
        playlist_id=playlist.id,
        video_id=video.id,
        session=session,
        current_user=owner,
        _csrf=None,
    )
    restored_response = await restore_playlist_video.__wrapped__(
        request=None,
        playlist_id=playlist.id,
        video_id=video.id,
        session=session,
        current_user=owner,
        _csrf=None,
    )

    assert hidden_response.status == "ok"
    assert restored_response.status == "ok"
    assert association.hidden_at is None


def test_playlist_detail_template_exposes_hide_and_restore_controls() -> None:
    template = (Path(__file__).resolve().parents[1] / "templates" / "detail.html").read_text(encoding="utf-8")

    assert "Bài đã ẩn" in template
    assert "js-hide-video" in template
    assert "js-restore-video" in template
    assert "Sync MP4" not in template


def test_playlist_detail_switches_between_visible_and_hidden_views(monkeypatch) -> None:
    owner = User(
        id="detail-owner",
        email="detail@example.com",
        storage_used_bytes=0,
        storage_quota_bytes=1024 * 1024 * 1024,
    )
    requested_views = []

    async def fake_user():
        return owner

    async def fake_session():
        yield None

    async def fake_detail(_session, playlist_id: int, owner_id: str, *, hidden_only: bool = False):
        requested_views.append(hidden_only)
        return PlaylistDetailResponse(
            id=playlist_id,
            title="Archived playlist",
            url="https://example.test",
            auto_sync=True,
            last_synced=None,
            hidden_count=1,
            videos=[
                VideoSchema(
                    id=9,
                    yt_video_id="removed123",
                    title="Removed song",
                    channel_name="Channel",
                    duration=120,
                    thumbnail_url=None,
                    status="DELETED_ON_YT",
                    local_file_path=None,
                    format_saved=None,
                    is_hidden=hidden_only,
                )
            ],
        )

    async def fake_count_downloaded(_session, _owner_id: str) -> int:
        return 0

    monkeypatch.setattr(app_module.crud, "get_playlist_detail", fake_detail)
    monkeypatch.setattr(app_module.crud, "count_downloaded_media", fake_count_downloaded)
    app_module.app.dependency_overrides[app_module.get_optional_user] = fake_user
    app_module.app.dependency_overrides[app_module.get_session] = fake_session
    try:
        visible_response = TestClient(app_module.app, base_url=LOCAL_BASE_URL).get("/playlist/1")
        hidden_response = TestClient(app_module.app, base_url=LOCAL_BASE_URL).get("/playlist/1?hidden=1")
    finally:
        app_module.app.dependency_overrides.clear()

    assert requested_views == [False, True]
    assert "js-hide-video" in visible_response.text
    assert "Bài đã ẩn (1)" in visible_response.text
    assert "js-restore-video" in hidden_response.text
    assert "Đã ẩn" in hidden_response.text


async def test_healthz_reports_database_status(session) -> None:
    response = await healthz(session)

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ok", "database": True}


async def test_add_playlist_returns_quick_pending_playlist(session, monkeypatch) -> None:
    owner = User(id="owner-1", email="owner@example.com", playlist_quota=10)
    session.add(owner)
    await session.commit()
    await session.refresh(owner)
    enqueued = []

    def fake_enqueue(playlist_id: int, owner_id: str, playlist_title: str):
        enqueued.append((playlist_id, owner_id, playlist_title))
        return SimpleNamespace(task_id="task-1")

    monkeypatch.setattr("main.enqueue_metadata_import", fake_enqueue)

    response = await add_playlist.__wrapped__(
        request=None,
        payload=PlaylistAddRequest(url="https://www.youtube.com/watch?v=abc123&list=PL_FAST_ADD"),
        session=session,
        current_user=owner,
        _csrf=None,
    )

    assert response.playlist_id is not None
    assert response.task_id == "task-1"
    assert response.message == "Playlist added; importing metadata"
    assert enqueued == [(response.playlist_id, owner.id, "Importing playlist PL_FAST_ADD")]


async def test_update_playlist_auto_sync_endpoint_updates_owner_playlist(session) -> None:
    owner = User(id="owner-1", email="owner@example.com")
    playlist = Playlist(id=1, owner_id=owner.id, yt_playlist_id="PL123", title="Piano", url="https://example.test")
    session.add_all([owner, playlist])
    await session.commit()

    response = await update_playlist_auto_sync.__wrapped__(
        request=None,
        playlist_id=playlist.id,
        payload=PlaylistAutoSyncRequest(auto_sync=False),
        session=session,
        current_user=owner,
        _csrf=None,
    )

    await session.refresh(playlist)
    assert playlist.auto_sync is False
    assert response.status == "ok"
    assert response.message == "Auto-sync disabled for Piano"


async def test_update_sync_schedule_updates_windows_task(monkeypatch) -> None:
    monkeypatch.setattr(
        "main.set_scheduled_sync_time",
        lambda value: SimpleNamespace(time=value, state="Ready", next_run_time="2026-08-07 12:00"),
    )

    response = await update_sync_schedule.__wrapped__(
        request=None,
        payload=SyncScheduleRequest(time="12:00"),
        _current_user=SimpleNamespace(id="owner-1"),
        _csrf=None,
    )

    assert response.status == "ok"
    assert response.message == "Daily MP3 sync updated to 12:00; next run 2026-08-07 12:00"


async def test_install_sync_schedule_registers_windows_task(monkeypatch) -> None:
    installed = []

    def fake_install(value: str):
        installed.append(value)
        return SimpleNamespace(time=value, state="Ready", next_run_time="2026-10-20 07:15")

    monkeypatch.setattr("main.install_scheduled_sync", fake_install)

    response = await install_sync_schedule.__wrapped__(
        request=None,
        payload=SyncScheduleRequest(time="07:15"),
        _current_user=SimpleNamespace(id="owner-1"),
        _csrf=None,
    )

    assert installed == ["07:15"]
    assert response.message == "Daily MP3 sync installed at 07:15; next run 2026-10-20 07:15"


async def test_install_sync_schedule_reports_scheduler_errors(monkeypatch) -> None:
    def failing_install(_value: str):
        raise app_module.WindowsSchedulerError("Access is denied")

    monkeypatch.setattr("main.install_scheduled_sync", failing_install)

    with pytest.raises(app_module.HTTPException) as error:
        await install_sync_schedule.__wrapped__(
            request=None,
            payload=SyncScheduleRequest(time="07:15"),
            _current_user=SimpleNamespace(id="owner-1"),
            _csrf=None,
        )

    assert error.value.status_code == 503
    assert error.value.detail == "Access is denied"


def test_settings_offers_install_button_when_task_is_missing() -> None:
    template = (Path(__file__).resolve().parents[1] / "templates" / "settings.html").read_text(encoding="utf-8")
    script = (Path(__file__).resolve().parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'id="syncScheduleForm"' in template and "data-installed=" in template
    assert "Cài đặt đồng bộ hằng ngày" in template
    assert '"/settings/sync-schedule/install"' in script


async def test_update_desktop_password_endpoint_changes_real_password(session) -> None:
    owner = await auth.set_initial_desktop_password(session, "password123")

    response = await update_desktop_password.__wrapped__(
        request=None,
        payload=PasswordChangeRequest(current_password="password123", new_password="new-password123"),
        session=session,
        current_user=owner,
        _csrf=None,
    )

    assert response.status == "ok"
    assert await auth.authenticate_desktop_user(session, "new-password123") is not None


def test_dashboard_renders_horizontal_layout(monkeypatch) -> None:
    owner = User(
        id="owner-1",
        email="owner@example.com",
        playlist_quota=10,
        storage_quota_bytes=1024 * 1024 * 1024,
        storage_used_bytes=512 * 1024 * 1024,
        is_superuser=False,
    )

    async def fake_user():
        return owner

    async def fake_session():
        yield None

    async def fake_list_playlists(_session, _owner_id: str):
        return [
            PlaylistSchema(
                id=1,
                title="Lo-Fi Beats for Study",
                url="https://www.youtube.com/playlist?list=PL_TEST",
                auto_sync=True,
                created_at=datetime.now(timezone.utc),
                last_synced=datetime.now(timezone.utc),
                video_count=185,
                thumbnail_url=None,
            )
        ]

    async def fake_count_downloaded_media(_session, _owner_id: str) -> int:
        return 7

    async def fake_sync_history(_session, _owner_id: str):
        return {
            "date_label": "06/08/2026",
            "downloaded_count": 0,
            "failed_count": 0,
            "unavailable_count": 0,
            "runs": [],
        }

    monkeypatch.setattr(app_module.crud, "list_playlists", fake_list_playlists)
    monkeypatch.setattr(app_module.crud, "count_downloaded_media", fake_count_downloaded_media)
    monkeypatch.setattr(app_module.crud, "get_today_sync_history", fake_sync_history)
    app_module.app.dependency_overrides[app_module.get_optional_user] = fake_user
    app_module.app.dependency_overrides[app_module.get_session] = fake_session
    try:
        response = TestClient(app_module.app, base_url=LOCAL_BASE_URL).get("/")
    finally:
        app_module.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "dashboard-add-form" in response.text
    assert "dashboard-card-grid" in response.text
    assert "Cập nhật hôm nay" in response.text
    assert 'class="today-sync-head js-today-sync-toggle"' in response.text
    assert 'aria-expanded="false"' in response.text
    assert 'aria-controls="todaySyncRuns"' in response.text
    assert 'id="todaySyncRuns" aria-hidden="true" inert' in response.text
    assert "dashboard-playlist-card" in response.text
    assert 'id="createCustomPlaylistForm"' in response.text
    assert "js-sync" not in response.text
    assert "topbar-storage" in response.text
    assert "--storage-percent: 50%;" in response.text
    assert "<strong>7</strong>" in response.text
    assert "0.5 GB" in response.text


@pytest.mark.parametrize(("total_pages", "expected_pagination_count"), [(1, 0), (4, 2)])
def test_scheduled_history_page_renders_filters_summary_and_sidebar(
    monkeypatch,
    total_pages: int,
    expected_pagination_count: int,
) -> None:
    owner = User(
        id="history-page-owner",
        email="history-page@example.com",
        storage_quota_bytes=20 * 1024 * 1024 * 1024,
        storage_used_bytes=0,
    )
    captured = {}

    async def fake_user():
        return owner

    async def fake_session():
        yield None

    expected_page = 2 if total_pages > 1 else 1

    async def fake_history(_session, owner_id: str, **kwargs):
        captured.update({"owner_id": owner_id, **kwargs})
        return {
            "total_runs": 1,
            "downloaded_count": 2,
            "failed_count": 1,
            "unavailable_count": 0,
            "page": expected_page,
            "page_size": 20,
            "total_pages": total_pages,
            "playlist_options": [{"value": "7", "label": "Night music", "is_deleted": False}],
            "groups": [
                {
                    "date_key": "2026-08-13",
                    "date_label": "13/08/2026",
                    "runs": [
                        {
                            "id": 1,
                            "playlist_id": 7,
                            "playlist_title": "Night music",
                            "status": "PARTIAL",
                            "downloaded_count": 2,
                            "failed_count": 1,
                            "unavailable_count": 0,
                            "error": None,
                            "started_label": "12:00",
                            "finished_label": "12:02",
                            "duration_seconds": 120,
                            "events": [],
                        }
                    ],
                }
            ],
        }

    async def fake_downloaded_count(_session, _owner_id: str):
        return 0

    class FixedDate(date):
        # The route clamps filters to the last 30 days, so pin "today" to keep the dates below valid.
        @classmethod
        def today(cls):
            return cls(2026, 8, 13)

    monkeypatch.setattr(app_module, "date", FixedDate)
    monkeypatch.setattr(app_module.crud, "get_scheduled_sync_history", fake_history)
    monkeypatch.setattr(app_module.crud, "count_downloaded_media", fake_downloaded_count)
    app_module.app.dependency_overrides[app_module.get_optional_user] = fake_user
    app_module.app.dependency_overrides[app_module.get_session] = fake_session
    try:
        response = TestClient(app_module.app, base_url=LOCAL_BASE_URL).get(
            f"/history?date_from=2026-08-01&date_to=2026-08-13&playlist_id=7&status=PARTIAL&page={expected_page}"
        )
    finally:
        app_module.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Lịch sử tự đồng bộ" in response.text
    assert 'data-nav="history"' in response.text
    assert "Night music" in response.text
    assert "Không có bài mới trong lần tự đồng bộ này" in response.text
    assert captured["owner_id"] == owner.id
    assert captured["playlist_filter"] == "7"
    assert captured["status_filter"].value == "PARTIAL"
    assert captured["page_size"] == 20
    assert captured["page"] == expected_page
    assert response.text.count('class="history-pagination history-pagination-') == expected_pagination_count
    assert response.text.count('class="history-page-jump js-history-page-jump"') == expected_pagination_count
    if total_pages > 1:
        assert response.text.count(f'min="1" max="{total_pages}" value="{expected_page}"') == 2
        assert response.text.count('name="date_from" value="2026-08-01"') >= 2
        assert response.text.count('name="playlist_id" value="7"') >= 2
        assert response.text.count('name="status" value="PARTIAL"') >= 2


def test_scheduled_history_page_requires_login() -> None:
    async def no_user():
        return None

    async def fake_session():
        yield None

    app_module.app.dependency_overrides[app_module.get_optional_user] = no_user
    app_module.app.dependency_overrides[app_module.get_session] = fake_session
    try:
        response = TestClient(app_module.app, base_url=LOCAL_BASE_URL).get("/history", follow_redirects=False)
    finally:
        app_module.app.dependency_overrides.clear()

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/history"


def test_history_page_jump_clamps_page_and_uses_soft_navigation() -> None:
    script = (Path(__file__).resolve().parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert '.matches(".js-history-page-jump")' in script
    assert "Math.min(totalPages, Math.max(1, requestedPage))" in script
    assert "new URLSearchParams(new FormData(form))" in script
    assert "await navigateTo(targetUrl.href, { useCache: false })" in script


def test_toast_renders_messages_as_text_not_html() -> None:
    script = (Path(__file__).resolve().parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")
    toast_function = script.split("function showToast", 1)[1].split("\nfunction ", 1)[0]

    assert '<div class="toast-body"></div>' in toast_function
    assert '.querySelector(".toast-body").textContent = String(message ?? "")' in toast_function
    assert "${message}" not in toast_function


async def _orphan_fixture(session, tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    kept = downloads / "kept.mp3"
    previewed = downloads / "previewed.mp3"
    appeared_later = downloads / "appeared-later.mp3"
    for path in (kept, previewed, appeared_later):
        path.write_bytes(b"12345")
    owner = User(id="orphan-owner", email="orphan-owner@example.com", storage_quota_bytes=10_000)
    playlist = Playlist(owner=owner, yt_playlist_id="kept", title="Kept", url="https://example.com")
    video = Video(yt_video_id="kept", title="Kept")
    session.add_all([owner, playlist, PlaylistVideo(playlist=playlist, video=video, local_file_path=r"downloads\kept.mp3")])
    await session.commit()
    monkeypatch.setattr(app_module.settings, "downloads_dir", downloads)
    return owner, kept, previewed, appeared_later


async def test_orphan_cleanup_lists_then_deletes_only_previewed_unused_files(session, tmp_path, monkeypatch) -> None:
    owner, kept, previewed, appeared_later = await _orphan_fixture(session, tmp_path, monkeypatch)
    monkeypatch.setattr(app_module, "sync_process_lock", nullcontext)

    listing = await app_module.list_orphaned_media.__wrapped__(request=None, session=session, _current_user=owner)
    response = await app_module.delete_orphaned_media.__wrapped__(
        request=None,
        payload=app_module.OrphanedMediaDeleteRequest(files=["previewed.mp3", "kept.mp3"]),
        session=session,
        current_user=owner,
        _csrf=None,
    )

    assert {item.name for item in listing.files} == {"previewed.mp3", "appeared-later.mp3"}
    assert listing.total_bytes == 10
    assert not previewed.exists()
    assert kept.exists()
    assert appeared_later.exists()
    assert response.message.startswith("Deleted 1 unused MP3 file(s)")
    assert "1 file(s) were skipped" in response.message


async def test_orphan_cleanup_refuses_while_a_sync_holds_the_lock(session, tmp_path, monkeypatch) -> None:
    owner, _kept, previewed, _appeared_later = await _orphan_fixture(session, tmp_path, monkeypatch)

    @contextmanager
    def busy_lock():
        raise app_module.SyncAlreadyRunning("busy")
        yield

    monkeypatch.setattr(app_module, "sync_process_lock", busy_lock)

    with pytest.raises(app_module.HTTPException) as error:
        await app_module.delete_orphaned_media.__wrapped__(
            request=None,
            payload=app_module.OrphanedMediaDeleteRequest(files=["previewed.mp3"]),
            session=session,
            current_user=owner,
            _csrf=None,
        )

    assert error.value.status_code == 409
    assert previewed.exists()


def test_requests_with_a_foreign_host_header_are_rejected() -> None:
    client = TestClient(app_module.app, base_url=LOCAL_BASE_URL)

    rebinding = client.get("/healthz", headers={"host": "evil.example:8765"})
    local = client.get("/static/css/app.css", headers={"host": "localhost:8765"})

    assert rebinding.status_code == 400
    assert local.status_code == 200


@pytest.mark.parametrize(
    ("next_path", "expected"),
    [
        ("/playlist/3?hidden=1", "/playlist/3?hidden=1"),
        ("/settings", "/settings"),
        (None, "/"),
        ("", "/"),
        ("https://evil.example/login", "/"),
        ("//evil.example", "/"),
        (r"/\evil.example", "/"),
        (r"/\/evil.example", "/"),
        ("/\t/evil.example", "/"),
        ("/\n/evil.example", "/"),
        ("settings", "/"),
    ],
)
def test_login_next_path_only_allows_same_site_paths(next_path, expected) -> None:
    assert app_module.safe_next_path(next_path) == expected


async def _upload_owner(session, used_bytes: int, quota_bytes: int) -> tuple[User, Playlist]:
    owner = User(id="upload-owner", email="upload@example.com", storage_used_bytes=used_bytes, storage_quota_bytes=quota_bytes)
    playlist = Playlist(
        owner=owner,
        yt_playlist_id="custom-upload",
        title="Uploads",
        url="custom://upload",
        source_type=app_module.PlaylistSource.CUSTOM,
    )
    session.add_all([owner, playlist])
    await session.commit()
    return owner, playlist


async def test_upload_is_limited_to_the_remaining_storage(session, monkeypatch) -> None:
    owner, playlist = await _upload_owner(session, used_bytes=0, quota_bytes=1000)
    captured = {}

    async def fake_recalculate(_session, user, _data_dir):
        user.storage_used_bytes = 900
        return 900

    async def fake_receive(_request, _downloads_dir, max_file_bytes):
        captured["max_file_bytes"] = max_file_bytes
        raise app_module.HTTPException(status_code=413, detail="MP3 exceeds storage quota")

    monkeypatch.setattr(app_module.crud, "recalculate_user_storage", fake_recalculate)
    monkeypatch.setattr(app_module, "receive_mp3_upload", fake_receive)

    with pytest.raises(app_module.HTTPException):
        await app_module.upload_custom_mp3.__wrapped__(
            request=None, playlist_id=playlist.id, session=session, current_user=owner, _csrf=None
        )

    assert captured["max_file_bytes"] == 100


async def test_upload_is_refused_without_reading_when_storage_is_full(session, monkeypatch) -> None:
    owner, playlist = await _upload_owner(session, used_bytes=1000, quota_bytes=1000)

    async def keep_usage(_session, user, _data_dir):
        return user.storage_used_bytes

    async def must_not_receive(*_args):
        raise AssertionError("a full quota must reject the upload before reading it")

    monkeypatch.setattr(app_module.crud, "recalculate_user_storage", keep_usage)
    monkeypatch.setattr(app_module, "receive_mp3_upload", must_not_receive)

    with pytest.raises(app_module.HTTPException) as error:
        await app_module.upload_custom_mp3.__wrapped__(
            request=None, playlist_id=playlist.id, session=session, current_user=owner, _csrf=None
        )

    assert error.value.status_code == 413
    assert error.value.detail == "Storage quota is full"
