import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import (
    Playlist,
    PlaylistSource,
    PlaylistVideo,
    SyncEvent,
    SyncEventType,
    SyncRun,
    SyncRunStatus,
    SyncSource,
    User,
    Video,
    VideoSource,
    VideoStatus,
)
from schemas import (
    LibraryVideoSchema,
    PlaylistDetailResponse,
    PlaylistSchema,
    PlaylistSearchVideoSchema,
    VideoSchema,
)

logger = logging.getLogger(__name__)


async def count_user_playlists(session: AsyncSession, owner_id: str) -> int:
    result = await session.execute(select(func.count(Playlist.id)).where(Playlist.owner_id == owner_id))
    return int(result.scalar_one())


async def count_downloaded_media(session: AsyncSession, owner_id: str) -> int:
    result = await session.execute(
        select(func.count(PlaylistVideo.video_id))
        .join(PlaylistVideo.playlist)
        .where(
            Playlist.owner_id == owner_id,
            PlaylistVideo.local_file_path.is_not(None),
        )
    )
    return int(result.scalar_one())


async def recalculate_user_storage(session: AsyncSession, owner: User, data_dir: Path) -> int:
    total = 0
    downloads_dir = data_dir / "downloads"
    for full_path in downloads_dir.rglob("*") if downloads_dir.exists() else []:
        if ".uploads" in full_path.parts:
            continue
        try:
            if full_path.is_file():
                total += full_path.stat().st_size
        except OSError:
            continue
    owner.storage_used_bytes = total
    await session.commit()
    return total


async def list_playlists(session: AsyncSession, owner_id: str) -> list[PlaylistSchema]:
    result = await session.execute(
        select(
            Playlist,
            func.count(PlaylistVideo.video_id),
            func.sum(case((PlaylistVideo.local_file_path.is_not(None), 1), else_=0)),
            func.max(Video.thumbnail_url),
        )
        .outerjoin(
            PlaylistVideo,
            (PlaylistVideo.playlist_id == Playlist.id) & PlaylistVideo.hidden_at.is_(None),
        )
        .outerjoin(Video, Video.id == PlaylistVideo.video_id)
        .where(Playlist.owner_id == owner_id)
        .group_by(Playlist.id)
        .order_by(Playlist.created_at.desc())
    )
    playlist_rows = result.all()
    search_result = await session.execute(
        select(PlaylistVideo.playlist_id, Video.id, Video.title, Video.channel_name, Video.yt_video_id)
        .join(PlaylistVideo.video)
        .join(PlaylistVideo.playlist)
        .where(Playlist.owner_id == owner_id, PlaylistVideo.hidden_at.is_(None))
        .order_by(PlaylistVideo.added_at.desc())
    )
    search_videos: dict[int, list[PlaylistSearchVideoSchema]] = {}
    for playlist_id, video_id, title, channel_name, source_id in search_result.all():
        search_videos.setdefault(playlist_id, []).append(
            PlaylistSearchVideoSchema(
                id=video_id,
                title=title,
                channel_name=channel_name,
                source_id=source_id,
            )
        )

    playlists = []
    for playlist, video_count, downloaded_count, thumbnail_url in playlist_rows:
        total = int(video_count or 0)
        downloaded = int(downloaded_count or 0)
        playlists.append(
            PlaylistSchema(
                id=playlist.id,
                title=playlist.title,
                url=playlist.url,
                auto_sync=playlist.auto_sync,
                created_at=playlist.created_at,
                last_synced=playlist.last_synced,
                video_count=total,
                downloaded_count=downloaded,
                archive_percent=round(downloaded / total * 100) if total else 0,
                thumbnail_url=thumbnail_url,
                source_type=playlist.source_type.value,
                search_videos=search_videos.get(playlist.id, []),
            )
        )
    return playlists


async def list_admin_users(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(User, func.count(Playlist.id))
        .outerjoin(Playlist, Playlist.owner_id == User.id)
        .group_by(User.id)
        .order_by(User.created_at.desc())
    )
    return [
        {
            "user": user,
            "playlist_count": int(playlist_count or 0),
        }
        for user, playlist_count in result.all()
    ]


async def update_user_admin_settings(
    session: AsyncSession,
    user_id: str,
    *,
    is_active: bool,
    playlist_quota: int,
    storage_quota_bytes: int,
) -> User:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.is_active = is_active
    user.playlist_quota = playlist_quota
    user.storage_quota_bytes = storage_quota_bytes
    await session.commit()
    await session.refresh(user)
    return user


async def get_playlist_for_owner(session: AsyncSession, playlist_id: int, owner_id: str) -> Playlist:
    result = await session.execute(
        select(Playlist)
        .where(Playlist.id == playlist_id, Playlist.owner_id == owner_id)
        .options(selectinload(Playlist.playlist_videos).selectinload(PlaylistVideo.video))
        .execution_options(populate_existing=True)
    )
    playlist = result.scalar_one_or_none()
    if not playlist:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Playlist does not belong to this user")
    return playlist


async def get_custom_playlist_for_owner(session: AsyncSession, playlist_id: int, owner_id: str) -> Playlist:
    playlist = await get_playlist_for_owner(session, playlist_id, owner_id)
    if playlist.source_type != PlaylistSource.CUSTOM:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This action requires a custom playlist")
    return playlist


async def create_custom_playlist(session: AsyncSession, owner: User, title: str) -> Playlist:
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Playlist title is required")
    if await count_user_playlists(session, owner.id) >= owner.playlist_quota:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Playlist quota exceeded")

    custom_id = f"custom-{uuid.uuid4().hex}"
    playlist = Playlist(
        owner_id=owner.id,
        yt_playlist_id=custom_id,
        title=clean_title[:500],
        url=f"custom://{custom_id}",
        source_type=PlaylistSource.CUSTOM,
        auto_sync=False,
    )
    session.add(playlist)
    await session.commit()
    await session.refresh(playlist)
    return playlist


async def list_downloaded_library(session: AsyncSession, owner_id: str) -> list[LibraryVideoSchema]:
    result = await session.execute(
        select(Video, PlaylistVideo.local_file_path)
        .join(PlaylistVideo, PlaylistVideo.video_id == Video.id)
        .join(Playlist, Playlist.id == PlaylistVideo.playlist_id)
        .where(Playlist.owner_id == owner_id, PlaylistVideo.local_file_path.is_not(None))
        .order_by(Video.title.asc())
    )
    videos: dict[int, LibraryVideoSchema] = {}
    for video, _local_file_path in result.all():
        videos.setdefault(
            video.id,
            LibraryVideoSchema(
                id=video.id,
                title=video.title,
                channel_name=video.channel_name,
                duration=video.duration,
                thumbnail_url=f"/thumbnail/{video.id}" if video.thumbnail_local_path else video.thumbnail_url,
            ),
        )
    return list(videos.values())


async def add_library_videos_to_custom_playlist(
    session: AsyncSession,
    playlist_id: int,
    owner_id: str,
    video_ids: list[int],
) -> int:
    playlist = await get_custom_playlist_for_owner(session, playlist_id, owner_id)
    result = await session.execute(
        select(PlaylistVideo)
        .join(PlaylistVideo.playlist)
        .where(
            Playlist.owner_id == owner_id,
            PlaylistVideo.video_id.in_(set(video_ids)),
            PlaylistVideo.local_file_path.is_not(None),
        )
        .options(selectinload(PlaylistVideo.video))
    )
    sources: dict[int, PlaylistVideo] = {}
    for association in result.scalars().all():
        sources.setdefault(association.video_id, association)
    if not sources:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No downloaded songs were selected")

    added = 0
    for video_id, source in sources.items():
        target = await _get_playlist_video(session, playlist.id, video_id)
        if target:
            target.hidden_at = None
            if not target.local_file_path:
                target.local_file_path = source.local_file_path
                target.format_saved = source.format_saved or "mp3"
            continue
        session.add(
            PlaylistVideo(
                playlist_id=playlist.id,
                video_id=video_id,
                local_file_path=source.local_file_path,
                format_saved=source.format_saved or "mp3",
            )
        )
        added += 1
    await session.commit()
    return added


async def add_youtube_metadata_to_custom_playlist(
    session: AsyncSession,
    playlist_id: int,
    owner_id: str,
    metadata: dict[str, Any],
) -> PlaylistVideo:
    playlist = await get_custom_playlist_for_owner(session, playlist_id, owner_id)
    video = await _get_video_by_youtube_id(session, metadata["yt_video_id"])
    if video is None:
        video = _new_video_from_metadata(metadata)
        video.source_type = VideoSource.YOUTUBE
        session.add(video)
        await session.flush()
    else:
        _apply_video_metadata(video, metadata)

    association = await _get_playlist_video(session, playlist.id, video.id)
    if association is None:
        association = PlaylistVideo(playlist_id=playlist.id, video_id=video.id)
        session.add(association)
    else:
        association.hidden_at = None
    if not association.local_file_path:
        downloaded_result = await session.execute(
            select(PlaylistVideo)
            .join(PlaylistVideo.playlist)
            .where(
                Playlist.owner_id == owner_id,
                PlaylistVideo.video_id == video.id,
                PlaylistVideo.local_file_path.is_not(None),
            )
            .limit(1)
        )
        downloaded = downloaded_result.scalar_one_or_none()
        if downloaded:
            association.local_file_path = downloaded.local_file_path
            association.format_saved = downloaded.format_saved or "mp3"
    await session.commit()
    await session.refresh(association)
    association.video = video
    return association


async def add_local_file_to_custom_playlist(
    session: AsyncSession,
    playlist_id: int,
    owner: User,
    *,
    digest: str,
    title: str,
    artist: str,
    duration: int,
    object_key: str,
    file_size: int,
    count_storage: bool,
) -> PlaylistVideo:
    playlist = await get_custom_playlist_for_owner(session, playlist_id, owner.id)
    local_id = f"local_{digest[:56]}"
    video = await _get_video_by_youtube_id(session, local_id)
    if video is None:
        video = Video(
            yt_video_id=local_id,
            title=title[:500],
            channel_name=artist[:255],
            duration=max(0, duration),
            source_type=VideoSource.LOCAL_FILE,
            status=VideoStatus.DOWNLOADED,
        )
        session.add(video)
        await session.flush()

    association = await _get_playlist_video(session, playlist.id, video.id)
    if association is None:
        association = PlaylistVideo(
            playlist_id=playlist.id,
            video_id=video.id,
            local_file_path=object_key,
            format_saved="mp3",
        )
        session.add(association)
    else:
        association.hidden_at = None
        association.local_file_path = object_key
        association.format_saved = "mp3"
    if count_storage:
        owner.storage_used_bytes += file_size
    await session.commit()
    await session.refresh(association)
    association.video = video
    return association


async def _get_video_by_youtube_id(session: AsyncSession, yt_video_id: str) -> Video | None:
    result = await session.execute(select(Video).where(Video.yt_video_id == yt_video_id))
    return result.scalar_one_or_none()


async def _get_playlist_video(session: AsyncSession, playlist_id: int, video_id: int) -> PlaylistVideo | None:
    result = await session.execute(
        select(PlaylistVideo).where(
            PlaylistVideo.playlist_id == playlist_id,
            PlaylistVideo.video_id == video_id,
        )
    )
    return result.scalar_one_or_none()


def _apply_video_metadata(video: Video, metadata: dict[str, Any]) -> None:
    if not metadata.get("is_available_on_youtube", True):
        return

    title = str(metadata.get("title") or "").strip()
    placeholder = f"YouTube video {video.yt_video_id}".lower()
    if title and title.lower() != placeholder:
        video.title = title

    channel_name = str(metadata.get("channel_name") or "").strip()
    if channel_name:
        video.channel_name = channel_name

    duration = int(metadata.get("duration") or 0)
    if duration > 0:
        video.duration = duration

    thumbnail_url = metadata.get("thumbnail_url")
    if thumbnail_url:
        video.thumbnail_url = thumbnail_url


def _new_video_from_metadata(metadata: dict[str, Any]) -> Video:
    return Video(
        yt_video_id=metadata["yt_video_id"],
        title=metadata["title"],
        channel_name=metadata.get("channel_name") or "",
        duration=int(metadata.get("duration") or 0),
        thumbnail_url=metadata.get("thumbnail_url"),
        status=(
            VideoStatus.AVAILABLE
            if metadata.get("is_available_on_youtube", True)
            else VideoStatus.DELETED_ON_YT
        ),
    )


async def upsert_playlist_from_metadata(session: AsyncSession, owner: User, metadata: dict[str, Any]) -> Playlist:
    result = await session.execute(
        select(Playlist).where(
            Playlist.owner_id == owner.id,
            Playlist.yt_playlist_id == metadata["yt_playlist_id"],
        )
    )
    playlist = result.scalar_one_or_none()
    if not playlist:
        total = await count_user_playlists(session, owner.id)
        if total >= owner.playlist_quota:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Playlist quota exceeded")
        playlist = Playlist(
            owner_id=owner.id,
            yt_playlist_id=metadata["yt_playlist_id"],
            title=metadata["title"],
            url=metadata["url"],
        )
        session.add(playlist)
        await session.flush()
    else:
        playlist.title = metadata["title"]
        playlist.url = metadata["url"]

    for video_data in metadata["videos"]:
        video = await _get_video_by_youtube_id(session, video_data["yt_video_id"])
        if not video:
            video = _new_video_from_metadata(video_data)
            session.add(video)
            await session.flush()
        else:
            _apply_video_metadata(video, video_data)
            if not video_data.get("is_available_on_youtube", True):
                video.status = VideoStatus.DELETED_ON_YT
            elif video.status == VideoStatus.DELETED_ON_YT:
                video.status = VideoStatus.AVAILABLE

        association = await _get_playlist_video(session, playlist.id, video.id)
        if not association:
            session.add(PlaylistVideo(playlist_id=playlist.id, video_id=video.id))

    await session.commit()
    await session.refresh(playlist)
    return playlist


async def get_or_create_pending_playlist(
    session: AsyncSession,
    owner: User,
    *,
    yt_playlist_id: str,
    url: str,
) -> tuple[Playlist, bool]:
    result = await session.execute(
        select(Playlist).where(
            Playlist.owner_id == owner.id,
            Playlist.yt_playlist_id == yt_playlist_id,
        )
    )
    playlist = result.scalar_one_or_none()
    if playlist:
        if playlist.url != url:
            playlist.url = url
            await session.commit()
            await session.refresh(playlist)
        return playlist, False

    total = await count_user_playlists(session, owner.id)
    if total >= owner.playlist_quota:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Playlist quota exceeded")

    short_id = yt_playlist_id[:12] if len(yt_playlist_id) > 12 else yt_playlist_id
    playlist = Playlist(
        owner_id=owner.id,
        yt_playlist_id=yt_playlist_id,
        title=f"Importing playlist {short_id}",
        url=url,
    )
    session.add(playlist)
    await session.commit()
    await session.refresh(playlist)
    return playlist, True


async def sync_playlist_metadata(
    session: AsyncSession,
    playlist: Playlist,
    metadata: dict[str, Any],
    newly_unavailable: list[Video] | None = None,
    missing_video_availability: dict[str, dict[str, Any]] | None = None,
) -> Playlist:
    current_yt_ids = {video["yt_video_id"] for video in metadata["videos"]}
    missing_video_availability = missing_video_availability or {}
    playlist.title = metadata["title"]
    playlist.url = metadata["url"]
    playlist.last_synced = datetime.now(timezone.utc)

    for association in playlist.playlist_videos:
        yt_video_id = association.video.yt_video_id
        if yt_video_id not in current_yt_ids:
            availability = missing_video_availability.get(yt_video_id, {})
            state = availability.get("state", "unknown")
            if state == "available":
                video_metadata = availability.get("metadata")
                if video_metadata:
                    _apply_video_metadata(association.video, video_metadata)
                if association.removed_from_youtube_playlist_at is None:
                    association.removed_from_youtube_playlist_at = datetime.now(timezone.utc)
                association.youtube_unavailable_at = None
                if association.video.status == VideoStatus.DELETED_ON_YT:
                    association.video.status = (
                        VideoStatus.DOWNLOADED if association.local_file_path else VideoStatus.AVAILABLE
                    )
            elif state == "unavailable":
                association.removed_from_youtube_playlist_at = None
                association.video.status = VideoStatus.DELETED_ON_YT
                if association.youtube_unavailable_at is None:
                    association.youtube_unavailable_at = datetime.now(timezone.utc)
                    if newly_unavailable is not None:
                        newly_unavailable.append(association.video)

    for video_data in metadata["videos"]:
        video = await _get_video_by_youtube_id(session, video_data["yt_video_id"])
        if not video:
            video = _new_video_from_metadata(video_data)
            session.add(video)
            await session.flush()
        else:
            _apply_video_metadata(video, video_data)

        association = await _get_playlist_video(session, playlist.id, video.id)
        if not association:
            association = PlaylistVideo(playlist_id=playlist.id, video_id=video.id)
            session.add(association)

        association.removed_from_youtube_playlist_at = None
        if not video_data.get("is_available_on_youtube", True):
            video.status = VideoStatus.DELETED_ON_YT
            if association.youtube_unavailable_at is None:
                association.youtube_unavailable_at = datetime.now(timezone.utc)
                if newly_unavailable is not None:
                    newly_unavailable.append(video)
        else:
            association.youtube_unavailable_at = None
            if video.status == VideoStatus.DELETED_ON_YT:
                video.status = VideoStatus.DOWNLOADED if association.local_file_path else VideoStatus.AVAILABLE

    await session.commit()
    await session.refresh(playlist)
    return playlist


async def get_playlist_detail(
    session: AsyncSession,
    playlist_id: int,
    owner_id: str,
    *,
    hidden_only: bool = False,
) -> PlaylistDetailResponse:
    playlist = await get_playlist_for_owner(session, playlist_id, owner_id)
    hidden_count = sum(association.hidden_at is not None for association in playlist.playlist_videos)
    associations = [
        association
        for association in playlist.playlist_videos
        if (association.hidden_at is not None) == hidden_only
    ]
    videos = [
        VideoSchema(
            id=association.video.id,
            yt_video_id=association.video.yt_video_id,
            title=association.video.title,
            channel_name=association.video.channel_name,
            duration=association.video.duration,
            thumbnail_url=(
                f"/thumbnail/{association.video.id}"
                if association.video.thumbnail_local_path
                else association.video.thumbnail_url
            ),
            status=association.video.status.value,
            local_file_path=association.local_file_path,
            format_saved=association.format_saved,
            download_error=association.download_error,
            last_download_attempt_at=association.last_download_attempt_at,
            is_hidden=association.hidden_at is not None,
            is_removed_from_youtube_playlist=association.removed_from_youtube_playlist_at is not None,
            source_type=association.video.source_type.value,
        )
        for association in associations
    ]
    return PlaylistDetailResponse(
        id=playlist.id,
        title=playlist.title,
        url=playlist.url,
        auto_sync=playlist.auto_sync,
        last_synced=playlist.last_synced,
        videos=videos,
        hidden_count=hidden_count,
        source_type=playlist.source_type.value,
    )


async def get_playlist_video_for_owner(
    session: AsyncSession,
    playlist_id: int,
    video_id: int,
    owner_id: str,
) -> PlaylistVideo:
    result = await session.execute(
        select(PlaylistVideo)
        .join(PlaylistVideo.playlist)
        .where(
            PlaylistVideo.playlist_id == playlist_id,
            PlaylistVideo.video_id == video_id,
            Playlist.owner_id == owner_id,
        )
        .options(selectinload(PlaylistVideo.video))
    )
    association = result.scalar_one_or_none()
    if not association:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found in this playlist")
    return association


async def hide_playlist_video(
    session: AsyncSession,
    playlist_id: int,
    video_id: int,
    owner_id: str,
) -> PlaylistVideo:
    association = await get_playlist_video_for_owner(session, playlist_id, video_id, owner_id)
    if association.hidden_at is not None:
        return association
    association.hidden_at = datetime.now(timezone.utc)
    await session.commit()
    return association


async def restore_playlist_video(
    session: AsyncSession,
    playlist_id: int,
    video_id: int,
    owner_id: str,
) -> PlaylistVideo:
    association = await get_playlist_video_for_owner(session, playlist_id, video_id, owner_id)
    if association.hidden_at is None:
        return association

    association.hidden_at = None
    await session.commit()
    return association


async def list_available_download_targets(session: AsyncSession, playlist_id: int, format_saved: str) -> list[PlaylistVideo]:
    result = await session.execute(
        select(PlaylistVideo)
        .where(PlaylistVideo.playlist_id == playlist_id)
        .where(PlaylistVideo.hidden_at.is_(None))
        .join(PlaylistVideo.video)
        .where(Video.status != VideoStatus.DELETED_ON_YT)
        .where(
            (PlaylistVideo.local_file_path.is_(None))
            | (PlaylistVideo.format_saved.is_(None))
            | (PlaylistVideo.format_saved != format_saved)
        )
        .options(selectinload(PlaylistVideo.video))
        .order_by(PlaylistVideo.added_at.desc())
    )
    return list(result.scalars().unique().all())


async def mark_video_downloaded(
    session: AsyncSession,
    association: PlaylistVideo,
    object_key: str,
    format_saved: str,
    file_size: int,
    owner: User,
) -> None:
    association.local_file_path = object_key
    association.format_saved = format_saved
    association.download_error = None
    association.last_download_attempt_at = datetime.now(timezone.utc)
    association.video.status = VideoStatus.DOWNLOADED
    owner.storage_used_bytes += file_size
    await session.commit()


async def mark_video_download_started(session: AsyncSession, association: PlaylistVideo) -> None:
    association.download_error = None
    association.last_download_attempt_at = datetime.now(timezone.utc)
    await session.commit()


async def mark_video_download_failed(session: AsyncSession, association: PlaylistVideo, error: str) -> None:
    association.download_error = error[:2000]
    association.last_download_attempt_at = datetime.now(timezone.utc)
    await session.commit()


async def mark_video_unavailable_on_youtube(session: AsyncSession, association: PlaylistVideo, error: str) -> None:
    association.download_error = error[:2000]
    association.last_download_attempt_at = datetime.now(timezone.utc)
    association.video.status = VideoStatus.DELETED_ON_YT
    if association.youtube_unavailable_at is None:
        association.youtube_unavailable_at = datetime.now(timezone.utc)
    await session.commit()


async def delete_playlist(session: AsyncSession, playlist_id: int, owner_id: str) -> list[str]:
    playlist = await get_playlist_for_owner(session, playlist_id, owner_id)
    orphaned_paths = [
        association.local_file_path
        for association in playlist.playlist_videos
        if association.local_file_path
    ]
    for path in orphaned_paths:
        logger.warning("Playlist deletion leaves media file/object for manual cleanup: %s", path)

    await session.delete(playlist)
    await session.commit()
    return orphaned_paths


async def set_playlist_auto_sync(session: AsyncSession, playlist_id: int, owner_id: str, auto_sync: bool) -> Playlist:
    playlist = await get_playlist_for_owner(session, playlist_id, owner_id)
    if playlist.source_type != PlaylistSource.YOUTUBE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Custom playlists do not support auto-sync")
    playlist.auto_sync = auto_sync
    await session.commit()
    await session.refresh(playlist)
    return playlist


async def list_auto_sync_playlist_ids(session: AsyncSession) -> list[int]:
    result = await session.execute(
        select(Playlist.id).where(
            Playlist.auto_sync.is_(True),
            Playlist.source_type == PlaylistSource.YOUTUBE,
        )
    )
    return list(result.scalars().all())


async def list_auto_sync_playlists(session: AsyncSession) -> list[Playlist]:
    result = await session.execute(
        select(Playlist)
        .where(Playlist.auto_sync.is_(True), Playlist.source_type == PlaylistSource.YOUTUBE)
        .order_by(Playlist.last_synced.isnot(None), Playlist.last_synced.asc(), Playlist.created_at.asc())
    )
    return list(result.scalars().all())


async def get_download_path_for_video(session: AsyncSession, owner_id: str, video_id: int) -> Path:
    result = await session.execute(
        select(PlaylistVideo)
        .join(PlaylistVideo.playlist)
        .where(
            Playlist.owner_id == owner_id,
            PlaylistVideo.video_id == video_id,
            PlaylistVideo.local_file_path.is_not(None),
        )
        .limit(1)
    )
    association = result.scalar_one_or_none()
    if not association or not association.local_file_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Downloaded media not found")
    return Path(association.local_file_path)


async def set_video_thumbnail_local_path(
    session: AsyncSession,
    yt_video_id: str,
    thumbnail_local_path: str,
) -> None:
    video = await _get_video_by_youtube_id(session, yt_video_id)
    if not video or video.thumbnail_local_path:
        return
    video.thumbnail_local_path = thumbnail_local_path
    await session.commit()


async def get_thumbnail_path_for_video(session: AsyncSession, owner_id: str, video_id: int) -> Path:
    result = await session.execute(
        select(Video)
        .join(PlaylistVideo, PlaylistVideo.video_id == Video.id)
        .join(Playlist, Playlist.id == PlaylistVideo.playlist_id)
        .where(
            Playlist.owner_id == owner_id,
            Video.id == video_id,
            Video.thumbnail_local_path.is_not(None),
        )
        .limit(1)
    )
    video = result.scalar_one_or_none()
    if not video or not video.thumbnail_local_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thumbnail not found")
    return Path(video.thumbnail_local_path)


async def create_sync_run(
    session: AsyncSession,
    playlist: Playlist,
    owner_id: str,
    source: str,
) -> SyncRun:
    run = SyncRun(
        owner_id=owner_id,
        playlist_id=playlist.id,
        playlist_title=playlist.title,
        source=SyncSource(source.upper()),
        status=SyncRunStatus.RUNNING,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def record_sync_event(
    session: AsyncSession,
    run: SyncRun,
    video: Video,
    event_type: SyncEventType,
    message: str | None = None,
) -> None:
    await record_sync_event_snapshot(
        session,
        run,
        yt_video_id=video.yt_video_id,
        video_title=video.title,
        event_type=event_type,
        message=message,
        video_id=video.id,
    )


async def record_sync_event_snapshot(
    session: AsyncSession,
    run: SyncRun,
    *,
    yt_video_id: str,
    video_title: str,
    event_type: SyncEventType,
    message: str | None = None,
    video_id: int | None = None,
) -> None:
    session.add(
        SyncEvent(
            sync_run_id=run.id,
            video_id=video_id,
            yt_video_id=(yt_video_id or "unknown")[:64],
            video_title=(video_title or "Unknown item")[:500],
            event_type=event_type,
            message=message[:2000] if message else None,
        )
    )
    if event_type == SyncEventType.DOWNLOADED:
        run.downloaded_count += 1
    elif event_type == SyncEventType.DOWNLOAD_FAILED:
        run.failed_count += 1
    elif event_type == SyncEventType.UNAVAILABLE:
        run.unavailable_count += 1
    await session.commit()


async def finish_sync_run(
    session: AsyncSession,
    run: SyncRun,
    *,
    error: str | None = None,
) -> None:
    if error:
        run.status = SyncRunStatus.FAILED
        run.error = error[:2000]
    elif run.failed_count:
        run.status = SyncRunStatus.PARTIAL
    else:
        run.status = SyncRunStatus.COMPLETED
    run.finished_at = datetime.now(timezone.utc)
    await session.commit()


def _as_local_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone()


async def get_today_sync_history(session: AsyncSession, owner_id: str) -> dict[str, Any]:
    local_now = datetime.now().astimezone()
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(timezone.utc)
    utc_end = local_end.astimezone(timezone.utc)
    result = await session.execute(
        select(SyncRun)
        .where(
            SyncRun.owner_id == owner_id,
            SyncRun.started_at >= utc_start,
            SyncRun.started_at < utc_end,
        )
        .options(selectinload(SyncRun.events))
        .order_by(SyncRun.started_at.desc())
    )
    runs = list(result.scalars().unique())
    return {
        "date_label": local_start.strftime("%d/%m/%Y"),
        "downloaded_count": sum(run.downloaded_count for run in runs),
        "failed_count": sum(run.failed_count for run in runs),
        "unavailable_count": sum(run.unavailable_count for run in runs),
        "runs": [
            {
                "id": run.id,
                "playlist_id": run.playlist_id,
                "playlist_title": run.playlist_title,
                "source": run.source.value,
                "status": run.status.value,
                "downloaded_count": run.downloaded_count,
                "failed_count": run.failed_count,
                "unavailable_count": run.unavailable_count,
                "error": run.error,
                "time_label": _as_local_time(run.started_at).strftime("%H:%M"),
                "events": [
                    {
                        "yt_video_id": event.yt_video_id,
                        "video_title": event.video_title,
                        "event_type": event.event_type.value,
                        "message": event.message,
                    }
                    for event in run.events
                ],
            }
            for run in runs
        ],
    }


async def get_scheduled_sync_history(
    session: AsyncSession,
    owner_id: str,
    *,
    date_from: date,
    date_to: date,
    playlist_filter: str | None = None,
    status_filter: SyncRunStatus | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    utc_start = datetime.combine(date_from, time.min, tzinfo=local_tz).astimezone(timezone.utc)
    utc_end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=local_tz).astimezone(timezone.utc)
    filters = [
        SyncRun.owner_id == owner_id,
        SyncRun.source == SyncSource.SCHEDULED,
        SyncRun.started_at >= utc_start,
        SyncRun.started_at < utc_end,
    ]
    if playlist_filter:
        if playlist_filter.startswith("deleted:"):
            filters.extend(
                [
                    SyncRun.playlist_id.is_(None),
                    SyncRun.playlist_title == playlist_filter.removeprefix("deleted:"),
                ]
            )
        elif playlist_filter.isdigit():
            filters.append(SyncRun.playlist_id == int(playlist_filter))
    if status_filter:
        filters.append(SyncRun.status == status_filter)

    summary_result = await session.execute(
        select(
            func.count(SyncRun.id),
            func.coalesce(func.sum(SyncRun.downloaded_count), 0),
            func.coalesce(func.sum(SyncRun.failed_count), 0),
            func.coalesce(func.sum(SyncRun.unavailable_count), 0),
        ).where(*filters)
    )
    total_runs, downloaded_count, failed_count, unavailable_count = summary_result.one()
    total_runs = int(total_runs or 0)
    page_size = max(1, min(page_size, 100))
    total_pages = max(1, (total_runs + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    runs_result = await session.execute(
        select(SyncRun)
        .where(*filters)
        .options(selectinload(SyncRun.events))
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    runs = list(runs_result.scalars().unique())

    history_local_start = datetime.combine(
        datetime.now().astimezone().date() - timedelta(days=29),
        time.min,
        tzinfo=local_tz,
    ).astimezone(timezone.utc)
    history_local_end = datetime.combine(
        datetime.now().astimezone().date() + timedelta(days=1),
        time.min,
        tzinfo=local_tz,
    ).astimezone(timezone.utc)
    playlist_result = await session.execute(
        select(SyncRun.playlist_id, SyncRun.playlist_title)
        .where(
            SyncRun.owner_id == owner_id,
            SyncRun.source == SyncSource.SCHEDULED,
            SyncRun.started_at >= history_local_start,
            SyncRun.started_at < history_local_end,
        )
        .distinct()
        .order_by(SyncRun.playlist_title.asc())
    )
    playlist_options = [
        {
            "value": str(playlist_id) if playlist_id is not None else f"deleted:{playlist_title}",
            "label": playlist_title,
            "is_deleted": playlist_id is None,
        }
        for playlist_id, playlist_title in playlist_result.all()
    ]

    groups: list[dict[str, Any]] = []
    groups_by_date: dict[str, dict[str, Any]] = {}
    for run in runs:
        local_started = _as_local_time(run.started_at)
        local_finished = _as_local_time(run.finished_at) if run.finished_at else None
        date_key = local_started.date().isoformat()
        group = groups_by_date.get(date_key)
        if group is None:
            group = {
                "date_key": date_key,
                "date_label": local_started.strftime("%d/%m/%Y"),
                "runs": [],
            }
            groups_by_date[date_key] = group
            groups.append(group)
        duration_seconds = None
        if run.finished_at:
            started = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
            finished = run.finished_at.replace(tzinfo=timezone.utc) if run.finished_at.tzinfo is None else run.finished_at
            duration_seconds = max(0, round((finished - started).total_seconds()))
        group["runs"].append(
            {
                "id": run.id,
                "playlist_id": run.playlist_id,
                "playlist_title": run.playlist_title,
                "status": run.status.value,
                "downloaded_count": run.downloaded_count,
                "failed_count": run.failed_count,
                "unavailable_count": run.unavailable_count,
                "error": run.error,
                "started_label": local_started.strftime("%H:%M"),
                "finished_label": local_finished.strftime("%H:%M") if local_finished else None,
                "duration_seconds": duration_seconds,
                "events": [
                    {
                        "yt_video_id": event.yt_video_id,
                        "video_title": event.video_title,
                        "event_type": event.event_type.value,
                        "message": event.message,
                    }
                    for event in run.events
                ],
            }
        )

    return {
        "total_runs": total_runs,
        "downloaded_count": int(downloaded_count or 0),
        "failed_count": int(failed_count or 0),
        "unavailable_count": int(unavailable_count or 0),
        "groups": groups,
        "playlist_options": playlist_options,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


async def cleanup_sync_history(session: AsyncSession, retention_days: int = 30) -> int:
    now = datetime.now(timezone.utc)
    stale_running_cutoff = now - timedelta(hours=6)
    stale_result = await session.execute(
        update(SyncRun)
        .where(
            SyncRun.status == SyncRunStatus.RUNNING,
            SyncRun.started_at < stale_running_cutoff,
        )
        .values(
            status=SyncRunStatus.FAILED,
            error=func.coalesce(SyncRun.error, "Sync process stopped before completion"),
            finished_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    recovered_count = int(stale_result.rowcount or 0)

    cutoff = now - timedelta(days=retention_days)
    old_run_ids = select(SyncRun.id).where(SyncRun.started_at < cutoff)
    await session.execute(
        delete(SyncEvent)
        .where(SyncEvent.sync_run_id.in_(old_run_ids))
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        delete(SyncRun)
        .where(SyncRun.started_at < cutoff)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return recovered_count
