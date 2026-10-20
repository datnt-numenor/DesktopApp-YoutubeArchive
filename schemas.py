from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PlaylistAddRequest(BaseModel):
    url: str = Field(min_length=8)


class CustomPlaylistCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class LibraryVideosAddRequest(BaseModel):
    video_ids: list[int] = Field(min_length=1, max_length=900)


class YoutubeVideosAddRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=50)


class SyncRequest(BaseModel):
    format: Literal["mp3"] = "mp3"


class SyncScheduleRequest(BaseModel):
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class PlaylistAutoSyncRequest(BaseModel):
    auto_sync: bool


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class OrphanedMediaDeleteRequest(BaseModel):
    # Names exactly as returned by GET /api/storage/orphans (relative to downloads/).
    files: list[str] = Field(min_length=1, max_length=20000)


class OrphanedMediaFileSchema(BaseModel):
    name: str
    size_bytes: int


class OrphanedMediaResponse(BaseModel):
    files: list[OrphanedMediaFileSchema]
    count: int
    total_bytes: int


class BaseResponse(BaseModel):
    status: Literal["ok", "error"]
    message: str


class PlaylistAddResponse(BaseResponse):
    playlist_id: int | None = None
    task_id: str | None = None


class TaskResponse(BaseModel):
    task_id: str
    status: Literal["queued", "running", "done", "failed"]
    message: str


class TaskErrorSchema(BaseModel):
    video_id: int
    yt_video_id: str
    title: str
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: Literal["queued", "running", "done", "failed"]
    playlist_id: int | None = None
    playlist_title: str | None = None
    owner_id: str | None = None
    format: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    progress: int | None = None
    error: str | None = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    current_video: str | None = None
    errors: list[TaskErrorSchema] = Field(default_factory=list)


class YtDlpStatusResponse(BaseModel):
    installed_version: str | None = None
    previous_version: str | None = None
    last_attempt_at: str | None = None
    last_successful_check_at: str | None = None
    last_updated_at: str | None = None
    last_error: str | None = None


class VideoSchema(BaseModel):
    id: int
    yt_video_id: str
    title: str
    channel_name: str
    duration: int
    thumbnail_url: str | None
    status: str
    local_file_path: str | None
    format_saved: str | None
    download_error: str | None = None
    last_download_attempt_at: datetime | None = None
    is_hidden: bool = False
    is_removed_from_youtube_playlist: bool = False
    source_type: str = "YOUTUBE"


class PlaylistSearchVideoSchema(BaseModel):
    id: int
    title: str
    channel_name: str
    source_id: str


class LibraryVideoSchema(BaseModel):
    id: int
    title: str
    channel_name: str
    duration: int
    thumbnail_url: str | None


class PlaylistSchema(BaseModel):
    id: int
    title: str
    url: str
    auto_sync: bool
    created_at: datetime
    last_synced: datetime | None
    video_count: int = 0
    downloaded_count: int = 0
    archive_percent: int = 0
    thumbnail_url: str | None = None
    source_type: str = "YOUTUBE"


class PlaylistDetailResponse(BaseModel):
    id: int
    title: str
    url: str
    auto_sync: bool
    last_synced: datetime | None
    videos: list[VideoSchema]
    hidden_count: int = 0
    source_type: str = "YOUTUBE"
