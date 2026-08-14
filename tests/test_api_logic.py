from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main as app_module
from main import (
    active_tasks,
    add_playlist,
    calculate_storage_level,
    collect_system_status,
    hide_playlist_video,
    restore_playlist_video,
    update_desktop_password,
    update_playlist_auto_sync,
    update_sync_schedule,
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


def test_storage_warning_thresholds_and_mp3_only_request() -> None:
    assert calculate_storage_level(79) == "normal"
    assert calculate_storage_level(80) == "warning"
    assert calculate_storage_level(95) == "danger"
    assert calculate_storage_level(100) == "full"
    assert SyncRequest(format="mp3").format == "mp3"
    with pytest.raises(ValueError):
        SyncRequest(format="mp4")


@pytest.fixture(autouse=True)
def local_task_backend(monkeypatch) -> None:
    monkeypatch.setattr(tasks.settings, "task_backend", "local")
    tasks.task_registry.clear()
    tasks._redis = None


async def test_active_tasks_endpoint_logic_returns_only_current_user_tasks() -> None:
    current = tasks.create_task_status(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    tasks.create_task_status(playlist_id=2, owner_id="owner-2", playlist_title="Other", format_="mp3")

    response = await active_tasks.__wrapped__(request=None, current_user=SimpleNamespace(id="owner-1"))

    assert len(response) == 1
    assert response[0].task_id == current.task_id
    assert response[0].playlist_id == 1


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
        visible_response = TestClient(app_module.app).get("/playlist/1")
        hidden_response = TestClient(app_module.app).get("/playlist/1?hidden=1")
    finally:
        app_module.app.dependency_overrides.clear()

    assert requested_views == [False, True]
    assert "js-hide-video" in visible_response.text
    assert "Bài đã ẩn (1)" in visible_response.text
    assert "js-restore-video" in hidden_response.text
    assert "Đã ẩn" in hidden_response.text


async def test_collect_system_status_reports_counts(session, monkeypatch) -> None:
    monkeypatch.setattr("main.settings.task_backend", "local")
    monkeypatch.setattr("main.settings.storage_backend", "local")
    monkeypatch.setattr("main.settings.registration_invite_code", "invite")
    monkeypatch.setattr("main.settings.auth_cookie_secure", True)

    owner = User(id="owner-1", email="owner@example.com")
    playlist = Playlist(id=1, owner_id=owner.id, yt_playlist_id="PL123", title="Piano", url="https://example.test")
    video = Video(id=1, yt_video_id="abc123", title="Song", channel_name="Channel", status=VideoStatus.DOWNLOADED)
    association = PlaylistVideo(playlist_id=playlist.id, video_id=video.id, local_file_path="downloads/song.mp3")
    session.add_all([owner, playlist, video, association])
    await session.commit()

    status = await collect_system_status(session, include_counts=True)

    assert status["checks"]["database"]["ok"]
    assert status["checks"]["tasks"]["label"] == "local"
    assert status["checks"]["public_auth"]["ok"]
    assert status["counts"] == {
        "users": 1,
        "playlists": 1,
        "videos": 1,
        "saved_media": 1,
    }


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
    monkeypatch.setattr("main.settings.task_backend", "local")

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
        response = TestClient(app_module.app).get("/")
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

    monkeypatch.setattr(app_module.crud, "get_scheduled_sync_history", fake_history)
    monkeypatch.setattr(app_module.crud, "count_downloaded_media", fake_downloaded_count)
    app_module.app.dependency_overrides[app_module.get_optional_user] = fake_user
    app_module.app.dependency_overrides[app_module.get_session] = fake_session
    try:
        response = TestClient(app_module.app).get(
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
        response = TestClient(app_module.app).get("/history", follow_redirects=False)
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
