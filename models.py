import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class VideoStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    DOWNLOADED = "DOWNLOADED"
    DELETED_ON_YT = "DELETED_ON_YT"


class PlaylistSource(str, enum.Enum):
    YOUTUBE = "YOUTUBE"
    CUSTOM = "CUSTOM"


class VideoSource(str, enum.Enum):
    YOUTUBE = "YOUTUBE"
    LOCAL_FILE = "LOCAL_FILE"


class SyncRunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class SyncSource(str, enum.Enum):
    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"
    IN_APP = "IN_APP"


class SyncEventType(str, enum.Enum):
    DOWNLOADED = "DOWNLOADED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    storage_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    playlist_quota: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    storage_quota_bytes: Mapped[int] = mapped_column(BigInteger, default=20 * 1024 * 1024 * 1024, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    playlists: Mapped[list["Playlist"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    auth_tokens: Mapped[list["AuthToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user: Mapped[User] = relationship(back_populates="auth_tokens")


class Playlist(Base):
    __tablename__ = "playlists"
    __table_args__ = (UniqueConstraint("owner_id", "yt_playlist_id", name="uq_owner_playlist"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    yt_playlist_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[PlaylistSource] = mapped_column(
        Enum(PlaylistSource), default=PlaylistSource.YOUTUBE, nullable=False
    )
    auto_sync: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_synced: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped[User] = relationship(back_populates="playlists")
    playlist_videos: Mapped[list["PlaylistVideo"]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistVideo.added_at.desc()",
    )


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    yt_video_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    duration: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[VideoSource] = mapped_column(Enum(VideoSource), default=VideoSource.YOUTUBE, nullable=False)
    status: Mapped[VideoStatus] = mapped_column(Enum(VideoStatus), default=VideoStatus.AVAILABLE, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    playlist_videos: Mapped[list["PlaylistVideo"]] = relationship(back_populates="video")


class PlaylistVideo(Base):
    __tablename__ = "playlist_video"

    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"), primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    local_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    format_saved: Mapped[str | None] = mapped_column(String(16), nullable=True)
    download_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_download_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    youtube_unavailable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_from_youtube_playlist_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    playlist: Mapped[Playlist] = relationship(back_populates="playlist_videos")
    video: Mapped[Video] = relationship(back_populates="playlist_videos")


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    playlist_id: Mapped[int | None] = mapped_column(
        ForeignKey("playlists.id", ondelete="SET NULL"), nullable=True, index=True
    )
    playlist_title: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[SyncSource] = mapped_column(Enum(SyncSource), nullable=False)
    status: Mapped[SyncRunStatus] = mapped_column(Enum(SyncRunStatus), default=SyncRunStatus.RUNNING, nullable=False)
    downloaded_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unavailable_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["SyncEvent"]] = relationship(
        back_populates="sync_run",
        cascade="all, delete-orphan",
        order_by="SyncEvent.created_at.asc()",
    )


class SyncEvent(Base):
    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"), nullable=True)
    yt_video_id: Mapped[str] = mapped_column(String(64), nullable=False)
    video_title: Mapped[str] = mapped_column(String(500), nullable=False)
    event_type: Mapped[SyncEventType] = mapped_column(Enum(SyncEventType), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    sync_run: Mapped[SyncRun] = relationship(back_populates="events")
