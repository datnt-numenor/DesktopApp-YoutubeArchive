from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import crud
from models import (
    Playlist,
    PlaylistSource,
    PlaylistVideo,
    SyncEventType,
    SyncRun,
    SyncRunStatus,
    User,
    Video,
    VideoStatus,
)


async def create_synced_playlist(session, owner: User, metadata: dict[str, object]) -> Playlist:
    """Create a playlist the way the app does: a pending row, then a metadata sync."""
    pending, _created = await crud.get_or_create_pending_playlist(
        session, owner, yt_playlist_id=metadata["yt_playlist_id"], url=metadata["url"]
    )
    playlist = await crud.get_playlist_for_owner(session, pending.id, owner.id)
    return await crud.sync_playlist_metadata(session, playlist, metadata)


def sample_video(video_id: str, title: str = "Song") -> dict[str, object]:
    return {
        "yt_video_id": video_id,
        "title": title,
        "channel_name": "Channel",
        "duration": 123,
        "thumbnail_url": None,
    }


async def test_custom_playlist_respects_quota_and_is_not_auto_synced(session) -> None:
    owner = User(email="custom-owner@example.com", playlist_quota=1)
    session.add(owner)
    await session.commit()

    playlist = await crud.create_custom_playlist(session, owner, "My offline mix")

    assert playlist.source_type == PlaylistSource.CUSTOM
    assert playlist.auto_sync is False
    assert await crud.list_auto_sync_playlist_ids(session) == []
    with pytest.raises(HTTPException) as quota_error:
        await crud.create_custom_playlist(session, owner, "Too many")
    assert quota_error.value.status_code == 403


async def test_library_song_is_reused_and_archive_ignores_hidden_rows(session) -> None:
    owner = User(email="library-owner@example.com")
    source = Playlist(owner=owner, yt_playlist_id="youtube-list", title="Source", url="https://example.com")
    video = Video(**sample_video("library-song", "Library song"), status=VideoStatus.DOWNLOADED)
    source_link = PlaylistVideo(
        playlist=source,
        video=video,
        local_file_path="downloads/library-song.mp3",
        format_saved="mp3",
    )
    session.add_all([owner, source, video, source_link])
    await session.commit()
    custom = await crud.create_custom_playlist(session, owner, "Custom")

    added = await crud.add_library_videos_to_custom_playlist(session, custom.id, owner.id, [video.id])
    playlists = {item.id: item for item in await crud.list_playlists(session, owner.id)}

    assert added == 1
    assert playlists[custom.id].downloaded_count == 1
    assert playlists[custom.id].archive_percent == 100
    assert (await crud.list_playlist_search_index(session, owner.id))[custom.id][0].title == "Library song"

    await crud.hide_playlist_video(session, custom.id, video.id, owner.id)
    playlists = {item.id: item for item in await crud.list_playlists(session, owner.id)}
    assert playlists[custom.id].video_count == 0
    assert playlists[custom.id].archive_percent == 0
    assert custom.id not in await crud.list_playlist_search_index(session, owner.id)

    await crud.restore_playlist_video(session, custom.id, video.id, owner.id)
    detail = await crud.get_playlist_detail(session, custom.id, owner.id)
    assert detail.videos[0].local_file_path == "downloads/library-song.mp3"


async def test_available_metadata_restores_a_previously_deleted_video(session) -> None:
    owner = User(email="owner@example.com")
    video = Video(**sample_video("deleted123"), status=VideoStatus.DELETED_ON_YT)
    session.add_all([owner, video])
    await session.commit()
    await session.refresh(owner)

    playlist = await create_synced_playlist(
        session,
        owner,
        {
            "yt_playlist_id": "playlist123",
            "title": "Playlist",
            "url": "https://www.youtube.com/playlist?list=playlist123",
            "videos": [sample_video("deleted123")],
        },
    )
    await session.refresh(video)

    targets = await crud.list_available_download_targets(session, playlist.id, "mp3")

    assert video.status == VideoStatus.AVAILABLE
    assert [item.video.yt_video_id for item in targets] == ["deleted123"]


async def test_unavailable_sync_preserves_archived_metadata_and_local_media(session) -> None:
    owner = User(email="archive-owner@example.com")
    session.add(owner)
    await session.commit()
    await session.refresh(owner)
    playlist = await create_synced_playlist(
        session,
        owner,
        {
            "yt_playlist_id": "archive-playlist",
            "title": "Archive",
            "url": "https://example.com/archive",
            "videos": [
                {
                    **sample_video("archived123", "Original title"),
                    "thumbnail_url": "https://example.com/original.jpg",
                }
            ],
        },
    )
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)
    association = playlist.playlist_videos[0]
    association.local_file_path = "downloads/archived123.mp3"
    association.format_saved = "mp3"
    association.video.status = VideoStatus.DOWNLOADED
    video_id = association.video.id
    playlist_id = playlist.id
    await session.commit()

    await crud.sync_playlist_metadata(
        session,
        playlist,
        {
            "title": "Archive",
            "url": "https://example.com/archive",
            "videos": [
                {
                    "yt_video_id": "archived123",
                    "title": "YouTube video archived123",
                    "channel_name": "",
                    "duration": 0,
                    "thumbnail_url": "https://example.com/unavailable.jpg",
                    "is_available_on_youtube": False,
                }
            ],
        },
    )
    refreshed = await crud.get_playlist_for_owner(session, playlist_id, owner.id)
    association = next(item for item in refreshed.playlist_videos if item.video_id == video_id)
    await session.refresh(association.video)
    await session.refresh(association)

    assert association.video.title == "Original title"
    assert association.video.channel_name == "Channel"
    assert association.video.duration == 123
    assert association.video.thumbnail_url == "https://example.com/original.jpg"
    assert association.video.status == VideoStatus.DELETED_ON_YT
    assert association.local_file_path == "downloads/archived123.mp3"


async def test_playlist_detail_prefers_local_thumbnail_route(session) -> None:
    owner = User(email="thumbnail-owner@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="thumb-list", title="Thumbs", url="https://example.com")
    video = Video(
        **sample_video("thumb123"),
        thumbnail_local_path="data/thumbnails/thumb123.jpg",
    )
    session.add_all([owner, playlist, video, PlaylistVideo(playlist=playlist, video=video)])
    await session.commit()

    detail = await crud.get_playlist_detail(session, playlist.id, owner.id)

    assert detail.videos[0].thumbnail_url == f"/thumbnail/{video.id}"


async def test_list_playlists_counts_videos_without_loading_detail_rows(session) -> None:
    owner = User(email="owner@example.com")
    other_owner = User(email="other@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="playlist123", title="Playlist", url="https://example.com")
    other_playlist = Playlist(owner=other_owner, yt_playlist_id="playlist456", title="Other", url="https://example.com")
    first_video = Video(**sample_video("video123"), status=VideoStatus.AVAILABLE)
    second_video = Video(**sample_video("video456"), status=VideoStatus.AVAILABLE)
    session.add_all(
        [
            owner,
            other_owner,
            playlist,
            other_playlist,
            first_video,
            second_video,
            PlaylistVideo(playlist=playlist, video=first_video),
            PlaylistVideo(playlist=playlist, video=second_video),
            PlaylistVideo(playlist=other_playlist, video=second_video),
        ]
    )
    await session.commit()

    playlists = await crud.list_playlists(session, owner.id)

    assert len(playlists) == 1
    assert playlists[0].id == playlist.id
    assert playlists[0].video_count == 2


async def test_soft_hide_keeps_video_data_and_restore_returns_it_to_playlist(session) -> None:
    owner = User(email="hide-owner@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="hide-list", title="Hide list", url="https://example.com")
    video = Video(
        **sample_video("hidden123", "Unavailable song"),
        status=VideoStatus.DELETED_ON_YT,
        thumbnail_local_path="data/thumbnails/hidden123.jpg",
    )
    association = PlaylistVideo(playlist=playlist, video=video)
    session.add_all([owner, playlist, video, association])
    await session.commit()

    hidden = await crud.hide_playlist_video(session, playlist.id, video.id, owner.id)
    visible_detail = await crud.get_playlist_detail(session, playlist.id, owner.id)
    hidden_detail = await crud.get_playlist_detail(session, playlist.id, owner.id, hidden_only=True)
    dashboard_playlists = await crud.list_playlists(session, owner.id)

    assert hidden.hidden_at is not None
    assert hidden.local_file_path is None
    assert hidden.video.title == "Unavailable song"
    assert hidden.video.thumbnail_local_path == "data/thumbnails/hidden123.jpg"
    assert visible_detail.videos == []
    assert visible_detail.hidden_count == 1
    assert hidden_detail.videos[0].is_hidden is True
    assert hidden_detail.videos[0].title == "Unavailable song"
    assert dashboard_playlists[0].video_count == 0

    restored = await crud.restore_playlist_video(session, playlist.id, video.id, owner.id)
    restored_detail = await crud.get_playlist_detail(session, playlist.id, owner.id)

    assert restored.hidden_at is None
    assert restored_detail.hidden_count == 0
    assert [item.yt_video_id for item in restored_detail.videos] == ["hidden123"]


async def test_soft_hide_accepts_available_and_downloaded_videos(session) -> None:
    owner = User(email="protected-owner@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="protected-list", title="Protected", url="https://example.com")
    available = Video(**sample_video("available123"), status=VideoStatus.AVAILABLE)
    downloaded = Video(**sample_video("downloaded-hide123"), status=VideoStatus.DELETED_ON_YT)
    available_link = PlaylistVideo(playlist=playlist, video=available)
    downloaded_link = PlaylistVideo(
        playlist=playlist,
        video=downloaded,
        local_file_path="downloads/downloaded-hide123.mp3",
        format_saved="mp3",
    )
    session.add_all([owner, playlist, available, downloaded, available_link, downloaded_link])
    await session.commit()

    await crud.hide_playlist_video(session, playlist.id, available.id, owner.id)
    await crud.hide_playlist_video(session, playlist.id, downloaded.id, owner.id)

    assert available_link.hidden_at is not None
    assert downloaded_link.hidden_at is not None
    assert downloaded_link.local_file_path == "downloads/downloaded-hide123.mp3"
    assert await crud.list_available_download_targets(session, playlist.id, "mp3") == []


async def test_soft_hide_checks_playlist_ownership_and_survives_sync(session) -> None:
    owner = User(email="sync-hide-owner@example.com")
    other_owner = User(email="other-hide-owner@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="sync-hide-list", title="Sync hidden", url="https://example.com")
    video = Video(**sample_video("sync-hidden123"), status=VideoStatus.DELETED_ON_YT)
    association = PlaylistVideo(playlist=playlist, video=video)
    session.add_all([owner, other_owner, playlist, video, association])
    await session.commit()

    with pytest.raises(HTTPException) as ownership_error:
        await crud.hide_playlist_video(session, playlist.id, video.id, other_owner.id)
    assert ownership_error.value.status_code == 404

    await crud.hide_playlist_video(session, playlist.id, video.id, owner.id)
    video.status = VideoStatus.AVAILABLE
    await session.commit()
    assert await crud.list_available_download_targets(session, playlist.id, "mp3") == []

    await crud.sync_playlist_metadata(
        session,
        playlist,
        {
            "title": playlist.title,
            "url": playlist.url,
            "videos": [{**sample_video("sync-hidden123"), "is_available_on_youtube": False}],
        },
    )
    refreshed = await crud.get_playlist_for_owner(session, playlist.id, owner.id)

    assert len(refreshed.playlist_videos) == 1
    assert refreshed.playlist_videos[0].hidden_at is not None


async def test_count_downloaded_media_counts_only_owner_saved_rows(session) -> None:
    owner = User(email="owner@example.com")
    other_owner = User(email="other@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="playlist123", title="Playlist", url="https://example.com")
    other_playlist = Playlist(owner=other_owner, yt_playlist_id="playlist456", title="Other", url="https://example.com")
    downloaded = Video(**sample_video("downloaded123"), status=VideoStatus.DOWNLOADED)
    pending = Video(**sample_video("pending123"), status=VideoStatus.AVAILABLE)
    other_downloaded = Video(**sample_video("other123"), status=VideoStatus.DOWNLOADED)
    session.add_all(
        [
            owner,
            other_owner,
            playlist,
            other_playlist,
            downloaded,
            pending,
            other_downloaded,
            PlaylistVideo(playlist=playlist, video=downloaded, local_file_path="downloads/downloaded.mp3"),
            PlaylistVideo(playlist=playlist, video=pending),
            PlaylistVideo(playlist=other_playlist, video=other_downloaded, local_file_path="downloads/other.mp3"),
        ]
    )
    await session.commit()

    assert await crud.count_downloaded_media(session, owner.id) == 1


async def test_get_or_create_pending_playlist_returns_quick_placeholder(session) -> None:
    owner = User(email="owner@example.com", playlist_quota=10)
    session.add(owner)
    await session.commit()
    await session.refresh(owner)

    playlist, created = await crud.get_or_create_pending_playlist(
        session,
        owner,
        yt_playlist_id="PL_FAST_ADD",
        url="https://www.youtube.com/playlist?list=PL_FAST_ADD",
    )
    existing, created_again = await crud.get_or_create_pending_playlist(
        session,
        owner,
        yt_playlist_id="PL_FAST_ADD",
        url="https://www.youtube.com/playlist?list=PL_FAST_ADD",
    )

    assert created is True
    assert playlist.id == existing.id
    assert created_again is False
    assert playlist.title == "Importing playlist PL_FAST_ADD"


async def test_set_playlist_auto_sync_updates_owner_playlist(session) -> None:
    owner = User(email="owner@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="playlist123", title="Playlist", url="https://example.com")
    session.add_all([owner, playlist])
    await session.commit()
    await session.refresh(owner)
    await session.refresh(playlist)

    updated = await crud.set_playlist_auto_sync(session, playlist.id, owner.id, False)

    assert updated.auto_sync is False


async def test_list_auto_sync_playlists_excludes_disabled_playlist(session) -> None:
    owner = User(email="owner@example.com")
    enabled = Playlist(owner=owner, yt_playlist_id="playlist-on", title="Enabled", url="https://example.com/on")
    disabled = Playlist(owner=owner, yt_playlist_id="playlist-off", title="Disabled", url="https://example.com/off", auto_sync=False)
    session.add_all([owner, enabled, disabled])
    await session.commit()

    playlists = await crud.list_auto_sync_playlists(session)

    assert [playlist.title for playlist in playlists] == ["Enabled"]


async def test_mark_unavailable_sets_deleted_status_and_error(session) -> None:
    owner = User(email="owner@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="playlist123", title="Playlist", url="https://example.com")
    video = Video(**sample_video("video123"), status=VideoStatus.AVAILABLE)
    association = PlaylistVideo(playlist=playlist, video=video)
    session.add_all([owner, playlist, video, association])
    await session.commit()

    result = await session.execute(
        select(PlaylistVideo)
        .where(PlaylistVideo.video_id == video.id)
        .options(selectinload(PlaylistVideo.video))
    )
    loaded_association = result.scalar_one()

    await crud.mark_video_unavailable_on_youtube(
        session,
        loaded_association,
        "Video unavailable. This video is not available",
    )

    assert loaded_association.video.status == VideoStatus.DELETED_ON_YT
    assert loaded_association.download_error == "Video unavailable. This video is not available"


async def test_mark_downloaded_updates_association_not_global_path(session) -> None:
    owner = User(email="owner@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="playlist123", title="Playlist", url="https://example.com")
    video = Video(**sample_video("video123"), status=VideoStatus.AVAILABLE)
    association = PlaylistVideo(playlist=playlist, video=video, download_error="old error")
    session.add_all([owner, playlist, video, association])
    await session.commit()

    await crud.mark_video_downloaded(
        session,
        association,
        "downloads/video123.mp3",
        "mp3",
        1234,
        owner,
    )

    assert association.local_file_path == "downloads/video123.mp3"
    assert association.format_saved == "mp3"
    assert association.download_error is None
    assert video.status == VideoStatus.DOWNLOADED
    assert owner.storage_used_bytes == 1234


async def test_sync_history_records_and_summarizes_today(session) -> None:
    owner = User(email="history@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="history-list", title="History", url="https://example.com")
    downloaded = Video(**sample_video("downloaded-history"))
    failed = Video(**sample_video("failed-history"))
    unavailable = Video(**sample_video("unavailable-history"))
    session.add_all([owner, playlist, downloaded, failed, unavailable])
    await session.commit()

    run = await crud.create_sync_run(session, playlist, owner.id, "scheduled")
    await crud.record_sync_event(session, run, downloaded, SyncEventType.DOWNLOADED)
    await crud.record_sync_event(session, run, failed, SyncEventType.DOWNLOAD_FAILED, "HTTP 403")
    await crud.record_sync_event(session, run, unavailable, SyncEventType.UNAVAILABLE)
    await crud.finish_sync_run(session, run)

    history = await crud.get_today_sync_history(session, owner.id)

    assert history["downloaded_count"] == 1
    assert history["failed_count"] == 1
    assert history["unavailable_count"] == 1
    assert history["runs"][0]["playlist_title"] == "History"
    assert [event["event_type"] for event in history["runs"][0]["events"]] == [
        "DOWNLOADED",
        "DOWNLOAD_FAILED",
        "UNAVAILABLE",
    ]


async def test_cleanup_sync_history_removes_entries_older_than_30_days(session) -> None:
    owner = User(email="old-history@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="old-list", title="Old", url="https://example.com")
    session.add_all([owner, playlist])
    await session.commit()
    run = await crud.create_sync_run(session, playlist, owner.id, "manual")
    run.started_at = datetime.now(timezone.utc) - timedelta(days=31)
    await session.commit()

    await crud.cleanup_sync_history(session, retention_days=30)

    assert (await session.execute(select(SyncRun))).scalars().all() == []


async def test_cleanup_sync_history_recovers_stale_running_rows_with_sqlite_datetimes(session) -> None:
    owner = User(email="stale-history@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="stale-list", title="Stale", url="https://example.com")
    session.add_all([owner, playlist])
    await session.commit()
    run = await crud.create_sync_run(session, playlist, owner.id, "scheduled")
    run_id = run.id
    run.started_at = datetime.now(timezone.utc) - timedelta(hours=7)
    await session.commit()

    recovered = await crud.cleanup_sync_history(session)

    session.expire_all()
    refreshed = (await session.execute(select(SyncRun).where(SyncRun.id == run_id))).scalar_one()
    assert recovered == 1
    assert refreshed.status == SyncRunStatus.FAILED
    assert refreshed.error == "Sync process stopped before completion"
    assert refreshed.finished_at is not None
    assert await crud.cleanup_sync_history(session) == 0


async def test_scheduled_history_filters_source_owner_status_playlist_and_paginates(session) -> None:
    owner = User(email="scheduled-history@example.com")
    other = User(email="other-scheduled-history@example.com")
    first_playlist = Playlist(owner=owner, yt_playlist_id="scheduled-one", title="Scheduled One", url="https://example.com/1")
    second_playlist = Playlist(owner=owner, yt_playlist_id="scheduled-two", title="Scheduled Two", url="https://example.com/2")
    other_playlist = Playlist(owner=other, yt_playlist_id="scheduled-other", title="Other", url="https://example.com/3")
    downloaded = Video(**sample_video("scheduled-downloaded", "Downloaded song"))
    failed = Video(**sample_video("scheduled-failed", "Failed song"))
    session.add_all([owner, other, first_playlist, second_playlist, other_playlist, downloaded, failed])
    await session.commit()

    completed = await crud.create_sync_run(session, first_playlist, owner.id, "scheduled")
    await crud.record_sync_event(session, completed, downloaded, SyncEventType.DOWNLOADED)
    await crud.finish_sync_run(session, completed)
    partial = await crud.create_sync_run(session, second_playlist, owner.id, "scheduled")
    await crud.record_sync_event(session, partial, failed, SyncEventType.DOWNLOAD_FAILED, "HTTP 403")
    await crud.finish_sync_run(session, partial)
    manual = await crud.create_sync_run(session, first_playlist, owner.id, "manual")
    await crud.record_sync_event(session, manual, downloaded, SyncEventType.DOWNLOADED)
    await crud.finish_sync_run(session, manual)
    old_scheduled = await crud.create_sync_run(session, first_playlist, owner.id, "scheduled")
    old_scheduled.started_at = datetime.now(timezone.utc) - timedelta(days=2)
    await crud.finish_sync_run(session, old_scheduled)
    other_run = await crud.create_sync_run(session, other_playlist, other.id, "scheduled")
    await crud.finish_sync_run(session, other_run)

    today = datetime.now().astimezone().date()
    history = await crud.get_scheduled_sync_history(
        session,
        owner.id,
        date_from=today,
        date_to=today,
        page=1,
        page_size=1,
    )

    # Pages hold whole days: both of today's runs share one group.
    assert history["total_runs"] == 2
    assert history["downloaded_count"] == 1
    assert history["failed_count"] == 1
    assert history["total_pages"] == 1
    assert len(history["groups"]) == 1
    group = history["groups"][0]
    assert (group["run_count"], group["downloaded_count"], group["failed_count"], group["unavailable_count"]) == (2, 1, 1, 0)
    assert 0 <= group["weekday_index"] <= 6
    assert {option["label"] for option in history["playlist_options"]} == {"Scheduled One", "Scheduled Two"}

    two_days = await crud.get_scheduled_sync_history(
        session,
        owner.id,
        date_from=today - timedelta(days=2),
        date_to=today,
        page=2,
        page_size=1,
    )
    assert two_days["total_days"] == 2
    assert two_days["total_pages"] == 2
    assert [len(group["runs"]) for group in two_days["groups"]] == [1]
    assert two_days["groups"][0]["date_key"] == (today - timedelta(days=2)).isoformat()

    partial_history = await crud.get_scheduled_sync_history(
        session,
        owner.id,
        date_from=today - timedelta(days=29),
        date_to=today,
        playlist_filter=str(second_playlist.id),
        status_filter=SyncRunStatus.PARTIAL,
    )
    assert partial_history["total_runs"] == 1
    run = partial_history["groups"][0]["runs"][0]
    assert run["playlist_title"] == "Scheduled Two"
    assert run["status"] == "PARTIAL"
    assert run["events"][0]["video_title"] == "Failed song"
    assert run["events"][0]["message"] == "HTTP 403"


async def test_scheduled_history_keeps_deleted_playlist_snapshot_filter(session) -> None:
    owner = User(email="deleted-history@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="deleted-history", title="Deleted playlist", url="https://example.com")
    session.add_all([owner, playlist])
    await session.commit()
    run = await crud.create_sync_run(session, playlist, owner.id, "scheduled")
    run.playlist_id = None
    await crud.finish_sync_run(session, run)

    today = datetime.now().astimezone().date()
    history = await crud.get_scheduled_sync_history(
        session,
        owner.id,
        date_from=today - timedelta(days=29),
        date_to=today,
        playlist_filter="deleted:Deleted playlist",
    )

    assert history["total_runs"] == 1
    assert history["playlist_options"] == [
        {"value": "deleted:Deleted playlist", "label": "Deleted playlist", "is_deleted": True}
    ]
    assert history["groups"][0]["runs"][0]["playlist_title"] == "Deleted playlist"


async def test_unavailable_transition_is_reported_only_once_per_playlist(session) -> None:
    owner = User(email="transition@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="transition-list", title="Transition", url="https://example.com")
    video = Video(**sample_video("transition-video"), status=VideoStatus.DOWNLOADED)
    association = PlaylistVideo(playlist=playlist, video=video, local_file_path="downloads/transition.mp3")
    session.add_all([owner, playlist, video, association])
    await session.commit()
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)

    first_changes = []
    await crud.sync_playlist_metadata(
        session,
        playlist,
        {"title": "Transition", "url": "https://example.com", "videos": []},
        newly_unavailable=first_changes,
        missing_video_availability={"transition-video": {"state": "unavailable"}},
    )
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)
    second_changes = []
    await crud.sync_playlist_metadata(
        session,
        playlist,
        {"title": "Transition", "url": "https://example.com", "videos": []},
        newly_unavailable=second_changes,
        missing_video_availability={"transition-video": {"state": "unavailable"}},
    )

    assert [item.yt_video_id for item in first_changes] == ["transition-video"]
    assert second_changes == []


async def test_missing_but_available_video_is_marked_removed_from_playlist(session) -> None:
    owner = User(email="removed@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="removed-list", title="Removed", url="https://example.com")
    video = Video(**sample_video("still-public"), status=VideoStatus.AVAILABLE)
    association = PlaylistVideo(playlist=playlist, video=video)
    session.add_all([owner, playlist, video, association])
    await session.commit()
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)
    newly_unavailable = []

    await crud.sync_playlist_metadata(
        session,
        playlist,
        {"title": "Removed", "url": "https://example.com", "videos": []},
        newly_unavailable=newly_unavailable,
        missing_video_availability={
            "still-public": {"state": "available", "metadata": sample_video("still-public", "Current title")}
        },
    )
    detail = await crud.get_playlist_detail(session, playlist.id, owner.id)

    assert newly_unavailable == []
    assert detail.videos[0].status == VideoStatus.AVAILABLE.value
    assert detail.videos[0].is_removed_from_youtube_playlist is True
    assert detail.videos[0].title == "Current title"


async def test_unknown_missing_video_state_does_not_create_false_deletion(session) -> None:
    owner = User(email="unknown-missing@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="unknown-list", title="Unknown", url="https://example.com")
    video = Video(**sample_video("unknown-video"), status=VideoStatus.AVAILABLE)
    association = PlaylistVideo(playlist=playlist, video=video)
    session.add_all([owner, playlist, video, association])
    await session.commit()
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)
    newly_unavailable = []

    await crud.sync_playlist_metadata(
        session,
        playlist,
        {"title": "Unknown", "url": "https://example.com", "videos": []},
        newly_unavailable=newly_unavailable,
        missing_video_availability={"unknown-video": {"state": "unknown", "error": "HTTP 403"}},
    )
    detail = await crud.get_playlist_detail(session, playlist.id, owner.id)

    assert newly_unavailable == []
    assert detail.videos[0].status == VideoStatus.AVAILABLE.value
    assert detail.videos[0].is_removed_from_youtube_playlist is False


async def test_video_readded_to_playlist_clears_removed_marker(session) -> None:
    owner = User(email="readded@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="readded-list", title="Readded", url="https://example.com")
    video = Video(**sample_video("readded-video"), status=VideoStatus.AVAILABLE)
    association = PlaylistVideo(
        playlist=playlist,
        video=video,
        removed_from_youtube_playlist_at=datetime.now(timezone.utc),
    )
    session.add_all([owner, playlist, video, association])
    await session.commit()
    playlist = await crud.get_playlist_for_owner(session, playlist.id, owner.id)

    await crud.sync_playlist_metadata(
        session,
        playlist,
        {"title": "Readded", "url": "https://example.com", "videos": [sample_video("readded-video")]},
    )
    detail = await crud.get_playlist_detail(session, playlist.id, owner.id)

    assert detail.videos[0].is_removed_from_youtube_playlist is False


async def test_app_start_closes_only_interrupted_app_sync_runs(session) -> None:
    owner = User(email="interrupted@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="interrupted", title="Interrupted", url="https://example.com")
    session.add_all([owner, playlist])
    await session.commit()
    manual = await crud.create_sync_run(session, playlist, owner.id, "manual")
    scheduled = await crud.create_sync_run(session, playlist, owner.id, "scheduled")

    closed = await crud.fail_interrupted_app_sync_runs(session)
    await session.refresh(manual)
    await session.refresh(scheduled)

    assert closed == 1
    assert manual.status == SyncRunStatus.FAILED
    assert manual.error == "The app closed before this sync finished"
    assert scheduled.status == SyncRunStatus.RUNNING


async def test_find_orphaned_media_files_keeps_every_referenced_or_in_flight_file(session, tmp_path) -> None:
    downloads = tmp_path / "downloads"
    (downloads / "imported").mkdir(parents=True)
    (downloads / ".uploads").mkdir()
    files = {
        name: downloads / name
        for name in [
            "linked.mp3",
            "legacy.mp3",
            "hidden.mp3",
            "unused.mp3",
            "unused.temp.mp3",
            ".uploads/upload.mp3",
            "imported/unused-import.mp3",
            "notes.txt",
        ]
    }
    for path in files.values():
        path.write_bytes(b"data")

    owner = User(email="orphans@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="orphans", title="Orphans", url="https://example.com")
    stored_paths = {
        "linked": r"downloads\linked.mp3",
        "legacy": "data/downloads/legacy.mp3",
        "hidden": r"downloads\hidden.mp3",
    }
    associations = []
    for video_id, stored_path in stored_paths.items():
        video = Video(yt_video_id=video_id, title=video_id)
        associations.append(
            PlaylistVideo(
                playlist=playlist,
                video=video,
                local_file_path=stored_path,
                hidden_at=datetime.now(timezone.utc) if video_id == "hidden" else None,
            )
        )
    session.add_all([owner, playlist, *associations])
    await session.commit()

    orphans = await crud.find_orphaned_media_files(session, tmp_path)

    assert orphans == sorted([files["unused.mp3"], files["imported/unused-import.mp3"]])


async def test_sync_event_list_returns_only_the_requested_label_for_the_filters(session) -> None:
    owner = User(email="event-list@example.com")
    other = User(email="event-list-other@example.com")
    first = Playlist(owner=owner, yt_playlist_id="events-one", title="Events One", url="https://example.com/1")
    second = Playlist(owner=owner, yt_playlist_id="events-two", title="Events Two", url="https://example.com/2")
    foreign = Playlist(owner=other, yt_playlist_id="events-other", title="Foreign", url="https://example.com/3")
    videos = [Video(**sample_video(f"event-video-{index}", f"Song {index}")) for index in range(4)]
    session.add_all([owner, other, first, second, foreign, *videos])
    await session.commit()

    run_one = await crud.create_sync_run(session, first, owner.id, "scheduled")
    await crud.record_sync_event(session, run_one, videos[0], SyncEventType.DOWNLOAD_FAILED, "HTTP 403")
    await crud.record_sync_event(session, run_one, videos[1], SyncEventType.UNAVAILABLE)
    await crud.record_sync_event(session, run_one, videos[2], SyncEventType.DOWNLOADED)
    await crud.finish_sync_run(session, run_one)
    run_two = await crud.create_sync_run(session, second, owner.id, "scheduled")
    await crud.record_sync_event(session, run_two, videos[3], SyncEventType.DOWNLOAD_FAILED, "Timed out")
    await crud.finish_sync_run(session, run_two)
    manual = await crud.create_sync_run(session, first, owner.id, "manual")
    await crud.record_sync_event(session, manual, videos[0], SyncEventType.DOWNLOAD_FAILED, "manual run")
    await crud.finish_sync_run(session, manual)
    foreign_run = await crud.create_sync_run(session, foreign, other.id, "scheduled")
    await crud.record_sync_event(session, foreign_run, videos[0], SyncEventType.DOWNLOAD_FAILED, "not mine")
    await crud.finish_sync_run(session, foreign_run)

    today = datetime.now().astimezone().date()
    failed = await crud.list_scheduled_sync_events(
        session, owner.id, SyncEventType.DOWNLOAD_FAILED, date_from=today, date_to=today
    )
    assert failed["total"] == 2
    assert {event["video_title"] for event in failed["events"]} == {"Song 0", "Song 3"}
    assert {event["playlist_title"] for event in failed["events"]} == {"Events One", "Events Two"}
    assert failed["events"][0]["date_label"] == today.strftime("%d/%m/%Y")

    removed = await crud.list_scheduled_sync_events(
        session, owner.id, SyncEventType.UNAVAILABLE, date_from=today, date_to=today
    )
    assert [event["video_title"] for event in removed["events"]] == ["Song 1"]

    one_playlist = await crud.list_scheduled_sync_events(
        session,
        owner.id,
        SyncEventType.DOWNLOAD_FAILED,
        date_from=today,
        date_to=today,
        playlist_filter=str(second.id),
    )
    assert [event["message"] for event in one_playlist["events"]] == ["Timed out"]

    limited = await crud.list_scheduled_sync_events(
        session, owner.id, SyncEventType.DOWNLOAD_FAILED, date_from=today, date_to=today, limit=1
    )
    assert limited["total"] == 2 and len(limited["events"]) == 1
