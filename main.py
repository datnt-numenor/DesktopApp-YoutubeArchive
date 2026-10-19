import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import crud
from auth import (
    authenticate_desktop_user,
    change_desktop_password,
    clear_login_cookie,
    csrf_token_for_request,
    desktop_password_is_configured,
    get_current_user,
    get_optional_user,
    require_csrf,
    set_csrf_cookie,
    set_initial_desktop_password,
    set_login_cookie,
)
from config import BASE_DIR, settings
from database import AsyncSessionLocal, get_session, init_db
from downloader import (
    canonicalize_playlist_url,
    extract_playlist_id_from_url,
    request_download_shutdown,
    reset_download_shutdown,
)
from media_import import receive_mp3_upload
from managed_ytdlp import ManagedYtDlpError, ensure_ytdlp_binary, get_ytdlp_status
from models import PlaylistSource, SyncRunStatus, User
from schemas import (
    BaseResponse,
    CustomPlaylistCreateRequest,
    LibraryVideosAddRequest,
    OrphanedMediaDeleteRequest,
    OrphanedMediaFileSchema,
    OrphanedMediaResponse,
    PasswordChangeRequest,
    PlaylistAutoSyncRequest,
    PlaylistAddRequest,
    PlaylistAddResponse,
    SyncScheduleRequest,
    SyncRequest,
    TaskResponse,
    TaskStatusResponse,
    YtDlpStatusResponse,
    YoutubeVideosAddRequest,
)
from storage import storage
from sync_lock import SyncAlreadyRunning, sync_process_lock
from tasks import (
    enqueue_custom_youtube_import,
    enqueue_metadata_import,
    enqueue_sync,
    get_task_status,
    list_task_statuses,
)
from windows_scheduler import (
    WindowsSchedulerError,
    get_scheduled_sync_info,
    install_scheduled_sync,
    set_scheduled_sync_time,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
ASSET_VERSION = "20261019-redesign"

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_db()
    reset_download_shutdown()
    async with AsyncSessionLocal() as cleanup_session:
        try:
            await crud.fail_interrupted_app_sync_runs(cleanup_session)
            await crud.cleanup_sync_history(cleanup_session)
        except Exception:  # noqa: BLE001 - maintenance must not prevent the desktop app from opening
            await cleanup_session.rollback()
            logger.exception("Could not clean sync history during startup; continuing")
        users = list((await cleanup_session.execute(select(User))).scalars().all())
        for user in users:
            await crud.recalculate_user_storage(cleanup_session, user, settings.downloads_dir.parent)
    # Automatic sync is owned by Windows Task Scheduler, which also runs while the app is closed.
    try:
        yield
    finally:
        # Without this, shutdown waits for the current yt-dlp download (up to an hour) and the
        # process lingers invisibly while still holding the sync lock.
        await asyncio.to_thread(request_download_shutdown)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "00:00"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_bytes(value: int | None) -> str:
    if not value:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def format_gigabytes(value: int | None) -> str:
    size = float(value or 0) / (1024 * 1024 * 1024)
    if size == 0:
        return "0 GB"
    if size < 0.1:
        return f"{size:.2f} GB"
    if size < 10:
        return f"{size:.1f} GB"
    return f"{size:.0f} GB"


VIETNAMESE_WEEKDAYS = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật")


def format_local_datetime(value: object, weekday: bool = False) -> str:
    """Render ISO or Task Scheduler timestamps as dd/mm/yyyy in local time; unknown text passes through."""
    if not value:
        return ""
    if isinstance(value, datetime):
        moment = value
        has_seconds = True
    else:
        text_value = str(value).strip()
        try:
            moment = datetime.fromisoformat(text_value)
        except ValueError:
            return text_value
        has_seconds = text_value.count(":") >= 2
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    label = moment.strftime("%d/%m/%Y %H:%M:%S" if has_seconds else "%d/%m/%Y %H:%M")
    return f"{VIETNAMESE_WEEKDAYS[moment.weekday()]}, {label}" if weekday else label


templates.env.filters["duration"] = format_duration
templates.env.filters["bytes"] = format_bytes
templates.env.filters["local_datetime"] = format_local_datetime
templates.env.globals["weekday_names"] = VIETNAMESE_WEEKDAYS


def calculate_storage_percent(user: User) -> int:
    if not user.storage_quota_bytes:
        return 0
    return min(100, round((user.storage_used_bytes / user.storage_quota_bytes) * 100))


def calculate_storage_level(percent: int) -> str:
    if percent >= 100:
        return "full"
    if percent >= 95:
        return "danger"
    if percent >= 80:
        return "warning"
    return "normal"


async def build_topbar_stats(session: AsyncSession, user: User) -> dict[str, object]:
    storage_percent = calculate_storage_percent(user)
    return {
        "downloaded_count": await crud.count_downloaded_media(session, user.id),
        "storage_percent": storage_percent,
        "storage_level": calculate_storage_level(storage_percent),
        "storage_used_label": format_gigabytes(user.storage_used_bytes),
        "storage_quota_label": format_gigabytes(user.storage_quota_bytes),
    }


def safe_next_path(next_path: str | None) -> str:
    if next_path and next_path.startswith("/") and not next_path.startswith("//"):
        return next_path
    return "/"


def login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/login?next={request.url.path}", status_code=status.HTTP_303_SEE_OTHER)


def render_template(request: Request, template_name: str, context: dict[str, object], status_code: int = 200):
    csrf_token = csrf_token_for_request(request)
    context = {
        **context,
        "csrf_token": csrf_token,
        "csrf_cookie_name": settings.csrf_cookie_name,
        "csrf_header_name": settings.csrf_header_name,
        "asset_version": ASSET_VERSION,
    }
    response = templates.TemplateResponse(request, template_name, context, status_code=status_code)
    set_csrf_cookie(response, csrf_token)
    return response


async def read_form_fields(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


@app.get("/healthz")
async def healthz(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    try:
        await session.execute(text("select 1"))
    except Exception:  # noqa: BLE001
        logger.exception("Database health check failed")
        return JSONResponse({"status": "degraded", "database": False}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse({"status": "ok", "database": True})


@app.get("/login")
async def login_page(
    request: Request,
    next: str | None = None,
    current_user: User | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
):
    if current_user:
        return RedirectResponse(safe_next_path(next), status_code=status.HTTP_303_SEE_OTHER)
    return render_template(
        request,
        "login.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": None,
            "next": safe_next_path(next),
            "error": None,
            "message": None,
            "needs_setup": not await desktop_password_is_configured(session),
        },
    )


@app.post("/login")
@limiter.limit("10/minute")
async def login(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(require_csrf),
):
    form = await read_form_fields(request)
    password = form.get("password", "")
    password_confirm = form.get("password_confirm", "")
    next_path = form.get("next")
    needs_setup = not await desktop_password_is_configured(session)

    if needs_setup:
        if password != password_confirm:
            return render_template(
                request,
                "login.html",
                {
                    "request": request,
                    "app_name": settings.app_name,
                    "user": None,
                    "next": safe_next_path(next_path),
                    "error": "The two passwords do not match.",
                    "message": None,
                    "needs_setup": True,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            user = await set_initial_desktop_password(session, password)
        except HTTPException as exc:
            return render_template(
                request,
                "login.html",
                {
                    "request": request,
                    "app_name": settings.app_name,
                    "user": None,
                    "next": safe_next_path(next_path),
                    "error": str(exc.detail),
                    "message": None,
                    "needs_setup": True,
                },
                status_code=exc.status_code,
            )
    else:
        user = await authenticate_desktop_user(session, password)

    if not user:
        return render_template(
            request,
            "login.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "user": None,
                "next": safe_next_path(next_path),
                "error": "Password is incorrect or temporarily locked.",
                "message": None,
                "needs_setup": False,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    response = RedirectResponse(safe_next_path(next_path), status_code=status.HTTP_303_SEE_OTHER)
    set_login_cookie(response, user)
    return response


@app.post("/logout")
@limiter.limit("30/minute")
async def logout(request: Request, _csrf: None = Depends(require_csrf)) -> RedirectResponse:
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_login_cookie(response)
    return response


@app.get("/")
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User | None = Depends(get_optional_user),
):
    if not current_user:
        return login_redirect(request)
    playlists = await crud.list_playlists(session, current_user.id)
    sync_history = await crud.get_today_sync_history(session, current_user.id)
    topbar_stats = await build_topbar_stats(session, current_user)
    return render_template(
        request,
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "playlists": playlists,
            "sync_history": sync_history,
            "user": current_user,
            "topbar_stats": topbar_stats,
        },
    )


@app.get("/history")
async def scheduled_sync_history_page(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    playlist_id: str | None = None,
    result_status: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    session: AsyncSession = Depends(get_session),
    current_user: User | None = Depends(get_optional_user),
):
    if not current_user:
        return login_redirect(request)

    today = date.today()
    earliest = today - timedelta(days=29)

    def parse_date(value: str | None, fallback: date) -> date:
        try:
            return date.fromisoformat(value) if value else fallback
        except ValueError:
            return fallback

    selected_from = max(earliest, min(parse_date(date_from, earliest), today))
    selected_to = max(earliest, min(parse_date(date_to, today), today))
    if selected_from > selected_to:
        selected_from = selected_to

    allowed_statuses = {
        SyncRunStatus.COMPLETED.value: SyncRunStatus.COMPLETED,
        SyncRunStatus.PARTIAL.value: SyncRunStatus.PARTIAL,
        SyncRunStatus.FAILED.value: SyncRunStatus.FAILED,
    }
    selected_status = result_status if result_status in allowed_statuses else ""
    history = await crud.get_scheduled_sync_history(
        session,
        current_user.id,
        date_from=selected_from,
        date_to=selected_to,
        playlist_filter=playlist_id,
        status_filter=allowed_statuses.get(selected_status),
        page=page,
        page_size=20,
    )
    query_values = {
        "date_from": selected_from.isoformat(),
        "date_to": selected_to.isoformat(),
        "playlist_id": playlist_id or "",
        "status": selected_status,
    }

    def page_url(target_page: int) -> str:
        return f"/history?{urlencode({**query_values, 'page': target_page})}"

    topbar_stats = await build_topbar_stats(session, current_user)
    return render_template(
        request,
        "history.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": current_user,
            "topbar_stats": topbar_stats,
            "history": history,
            "filters": query_values,
            "previous_url": page_url(history["page"] - 1) if history["page"] > 1 else None,
            "next_url": page_url(history["page"] + 1) if history["page"] < history["total_pages"] else None,
        },
    )


@app.get("/settings")
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User | None = Depends(get_optional_user),
):
    if not current_user:
        return login_redirect(request)
    storage_percent = calculate_storage_percent(current_user)
    topbar_stats = await build_topbar_stats(session, current_user)
    scheduled_sync = None
    scheduled_sync_error = None
    try:
        scheduled_sync = await asyncio.to_thread(get_scheduled_sync_info)
    except WindowsSchedulerError as exc:
        scheduled_sync_error = str(exc)
    yt_dlp_status_info = await asyncio.to_thread(get_ytdlp_status)
    return render_template(
        request,
        "settings.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": current_user,
            "scheduled_sync": scheduled_sync,
            "scheduled_sync_error": scheduled_sync_error,
            "yt_dlp_status": yt_dlp_status_info,
            "storage_percent": storage_percent,
            "topbar_stats": topbar_stats,
            "playlist_quota": current_user.playlist_quota,
            "downloads_dir": str(settings.downloads_dir),
        },
    )


@app.get("/api/yt-dlp/status", response_model=YtDlpStatusResponse)
@limiter.limit("60/minute")
async def yt_dlp_status(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> YtDlpStatusResponse:
    del request, current_user
    return YtDlpStatusResponse(**await asyncio.to_thread(get_ytdlp_status))


@app.post("/api/yt-dlp/check", response_model=YtDlpStatusResponse)
@limiter.limit("6/minute")
async def check_yt_dlp_update(
    request: Request,
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> YtDlpStatusResponse:
    del request, current_user
    try:
        await asyncio.to_thread(ensure_ytdlp_binary, force_update=True)
    except ManagedYtDlpError:
        # The failure is recorded in the updater state and shown through last_error.
        logger.warning("Manual yt-dlp update check failed", exc_info=True)
    return YtDlpStatusResponse(**await asyncio.to_thread(get_ytdlp_status))


@app.post("/settings/storage/open-folder", response_model=BaseResponse)
@limiter.limit("20/minute")
async def open_storage_folder(
    request: Request,
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    del request, current_user
    folder = settings.downloads_dir
    if not hasattr(os, "startfile"):
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Opening folders is only supported on Windows")
    try:
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(os.startfile, folder)
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Could not open {folder}") from exc
    return BaseResponse(status="ok", message="Đã mở thư mục lưu trữ")


@app.patch("/settings/password", response_model=BaseResponse)
@limiter.limit("10/hour")
async def update_desktop_password(
    request: Request,
    payload: PasswordChangeRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    await change_desktop_password(session, current_user, payload.current_password, payload.new_password)
    return BaseResponse(status="ok", message="Password changed successfully")


def _orphan_name(path: Path) -> str:
    return path.relative_to(settings.downloads_dir).as_posix()


@app.get("/api/storage/orphans", response_model=OrphanedMediaResponse)
@limiter.limit("30/minute")
async def list_orphaned_media(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> OrphanedMediaResponse:
    orphans = await crud.find_orphaned_media_files(session, settings.downloads_dir.parent)
    files = [OrphanedMediaFileSchema(name=_orphan_name(path), size_bytes=path.stat().st_size) for path in orphans]
    return OrphanedMediaResponse(files=files, count=len(files), total_bytes=sum(item.size_bytes for item in files))


@app.post("/settings/storage/orphans/delete", response_model=BaseResponse)
@limiter.limit("10/minute")
async def delete_orphaned_media(
    request: Request,
    payload: OrphanedMediaDeleteRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    try:
        # Holding the sync lock guarantees no download is about to reference one of these files.
        with sync_process_lock():
            orphans = await crud.find_orphaned_media_files(session, settings.downloads_dir.parent)
            requested = set(payload.files)
            # Only delete files the user previewed that are still unused right now.
            targets = [path for path in orphans if _orphan_name(path) in requested]

            def delete_files() -> tuple[int, int]:
                deleted = freed = 0
                for path in targets:
                    try:
                        size = path.stat().st_size
                        path.unlink()
                    except OSError:
                        logger.warning("Could not delete unused media file %s", path, exc_info=True)
                        continue
                    deleted += 1
                    freed += size
                return deleted, freed

            deleted, freed = await asyncio.to_thread(delete_files)
    except SyncAlreadyRunning as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A sync or download is running; try again when it finishes",
        ) from exc

    await crud.recalculate_user_storage(session, current_user, settings.downloads_dir.parent)
    skipped = len(requested) - deleted
    message = f"Deleted {deleted} unused MP3 file(s), freed {format_bytes(freed)}"
    if skipped:
        message += f"; {skipped} file(s) were skipped because they are in use or already gone"
    return BaseResponse(status="ok", message=message)


@app.get("/playlist/{playlist_id}")
async def playlist_detail(
    playlist_id: int,
    request: Request,
    hidden: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user: User | None = Depends(get_optional_user),
):
    if not current_user:
        return login_redirect(request)
    detail = await crud.get_playlist_detail(session, playlist_id, current_user.id, hidden_only=hidden)
    library_videos = (
        await crud.list_downloaded_library(session, current_user.id)
        if detail.source_type == PlaylistSource.CUSTOM.value and not hidden
        else []
    )
    topbar_stats = await build_topbar_stats(session, current_user)
    return render_template(
        request,
        "detail.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "playlist": detail,
            "library_videos": library_videos,
            "show_hidden": hidden,
            "user": current_user,
            "topbar_stats": topbar_stats,
        },
    )


@app.get("/api/playlists")
@limiter.limit("60/minute")
async def api_playlists(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return await crud.list_playlists(session, current_user.id)


@app.post("/playlist/custom", response_model=PlaylistAddResponse)
@limiter.limit("20/minute")
async def create_custom_playlist(
    request: Request,
    payload: CustomPlaylistCreateRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> PlaylistAddResponse:
    playlist = await crud.create_custom_playlist(session, current_user, payload.title)
    return PlaylistAddResponse(status="ok", message="Custom playlist created", playlist_id=playlist.id)


@app.get("/api/library/downloaded")
@limiter.limit("60/minute")
async def downloaded_library(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return await crud.list_downloaded_library(session, current_user.id)


@app.post("/playlist/{playlist_id}/videos/library", response_model=BaseResponse)
@limiter.limit("30/minute")
async def add_library_videos(
    request: Request,
    playlist_id: int,
    payload: LibraryVideosAddRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    added = await crud.add_library_videos_to_custom_playlist(
        session, playlist_id, current_user.id, payload.video_ids
    )
    return BaseResponse(status="ok", message=f"Added {added} downloaded song(s)")


@app.post("/playlist/{playlist_id}/videos/upload", response_model=BaseResponse)
@limiter.limit("30/minute")
async def upload_custom_mp3(
    request: Request,
    playlist_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    await crud.get_custom_playlist_for_owner(session, playlist_id, current_user.id)
    await crud.recalculate_user_storage(session, current_user, settings.downloads_dir.parent)
    imported = await receive_mp3_upload(request, settings.downloads_dir, current_user.storage_quota_bytes)
    if imported.is_new_file and current_user.storage_used_bytes + imported.size > current_user.storage_quota_bytes:
        imported.path.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="MP3 would exceed 20 GB storage quota")

    object_key = await storage.upload_file(
        imported.path,
        f"users/{current_user.id}/custom/{playlist_id}/{imported.path.name}",
    )
    try:
        await crud.add_local_file_to_custom_playlist(
            session,
            playlist_id,
            current_user,
            digest=imported.digest,
            title=imported.title,
            artist=imported.artist,
            duration=imported.duration,
            object_key=object_key,
            file_size=imported.size,
            count_storage=False,
        )
    except Exception:
        if imported.is_new_file:
            imported.path.unlink(missing_ok=True)
        raise
    await crud.recalculate_user_storage(session, current_user, settings.downloads_dir.parent)
    return BaseResponse(status="ok", message=f"Imported {imported.title}")


@app.post("/playlist/{playlist_id}/videos/youtube", response_model=TaskResponse)
@limiter.limit("10/minute")
async def add_youtube_videos(
    request: Request,
    playlist_id: int,
    payload: YoutubeVideosAddRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> TaskResponse:
    playlist = await crud.get_custom_playlist_for_owner(session, playlist_id, current_user.id)
    urls = list(dict.fromkeys(url.strip() for url in payload.urls if url.strip()))
    if not urls:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one YouTube URL is required")
    task = enqueue_custom_youtube_import(playlist.id, current_user.id, playlist.title, urls)
    return TaskResponse(task_id=task.task_id, status=task.status, message="YouTube MP3 import queued")


@app.post("/playlist/add", response_model=PlaylistAddResponse)
@limiter.limit("10/minute")
async def add_playlist(
    request: Request,
    payload: PlaylistAddRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> PlaylistAddResponse:
    try:
        playlist_url = canonicalize_playlist_url(payload.url)
        yt_playlist_id = extract_playlist_id_from_url(playlist_url)
        playlist, created = await crud.get_or_create_pending_playlist(
            session,
            current_user,
            yt_playlist_id=yt_playlist_id,
            url=playlist_url,
        )
        task = enqueue_metadata_import(playlist.id, current_user.id, playlist.title)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to add playlist")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    message = "Playlist added; importing metadata" if created else "Playlist already exists; refreshing metadata"
    return PlaylistAddResponse(status="ok", message=message, playlist_id=playlist.id, task_id=task.task_id)


@app.post("/playlist/{playlist_id}/sync", response_model=TaskResponse)
@limiter.limit("5/minute")
async def sync_playlist(
    request: Request,
    playlist_id: int,
    payload: SyncRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> TaskResponse:
    playlist = await crud.get_playlist_for_owner(session, playlist_id, current_user.id)
    if playlist.source_type != PlaylistSource.YOUTUBE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Custom playlists do not support sync")
    try:
        task = enqueue_sync(playlist_id, current_user.id, playlist.title, payload.format)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return TaskResponse(task_id=task.task_id, status=task.status, message="Sync task queued")


@app.patch("/playlist/{playlist_id}/auto-sync", response_model=BaseResponse)
@limiter.limit("30/minute")
async def update_playlist_auto_sync(
    request: Request,
    playlist_id: int,
    payload: PlaylistAutoSyncRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    playlist = await crud.set_playlist_auto_sync(session, playlist_id, current_user.id, payload.auto_sync)
    state = "enabled" if playlist.auto_sync else "disabled"
    return BaseResponse(status="ok", message=f"Auto-sync {state} for {playlist.title}")


@app.delete("/playlist/{playlist_id}/video/{video_id}", response_model=BaseResponse)
@limiter.limit("60/minute")
async def hide_playlist_video(
    request: Request,
    playlist_id: int,
    video_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    association = await crud.hide_playlist_video(session, playlist_id, video_id, current_user.id)
    return BaseResponse(status="ok", message=f"Hidden {association.video.title}")


@app.post("/playlist/{playlist_id}/video/{video_id}/restore", response_model=BaseResponse)
@limiter.limit("60/minute")
async def restore_playlist_video(
    request: Request,
    playlist_id: int,
    video_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    association = await crud.restore_playlist_video(session, playlist_id, video_id, current_user.id)
    return BaseResponse(status="ok", message=f"Restored {association.video.title}")


@app.delete("/playlist/{playlist_id}", response_model=BaseResponse)
@limiter.limit("20/minute")
async def delete_playlist(
    request: Request,
    playlist_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    orphaned_paths = await crud.delete_playlist(session, playlist_id, current_user.id)
    message = "Playlist deleted"
    if orphaned_paths:
        message += f"; {len(orphaned_paths)} media file/object path(s) left for manual cleanup"
    return BaseResponse(status="ok", message=message)


@app.get("/stream/{video_id}")
async def stream_video(
    video_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    media_path = await crud.get_download_path_for_video(session, current_user.id, video_id)
    full_path = Path(await storage.get_file_url(media_path.as_posix()))
    if not full_path.is_absolute():
        full_path = BASE_DIR / full_path
    if not full_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found on disk")
    return FileResponse(full_path)


@app.get("/thumbnail/{video_id}")
async def thumbnail_video(
    video_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    thumbnail_path = await crud.get_thumbnail_path_for_video(session, current_user.id, video_id)
    full_path = thumbnail_path if thumbnail_path.is_absolute() else BASE_DIR / thumbnail_path
    if not full_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thumbnail file not found on disk")
    return FileResponse(full_path, headers={"Cache-Control": "private, max-age=86400"})


@app.patch("/settings/sync-schedule", response_model=BaseResponse)
@limiter.limit("20/minute")
async def update_sync_schedule(
    request: Request,
    payload: SyncScheduleRequest,
    _current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    try:
        scheduled_sync = await asyncio.to_thread(set_scheduled_sync_time, payload.time)
    except WindowsSchedulerError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    next_run = f"; next run {scheduled_sync.next_run_time}" if scheduled_sync.next_run_time else ""
    return BaseResponse(status="ok", message=f"Daily MP3 sync updated to {payload.time}{next_run}")


@app.post("/settings/sync-schedule/install", response_model=BaseResponse)
@limiter.limit("10/minute")
async def install_sync_schedule(
    request: Request,
    payload: SyncScheduleRequest,
    _current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    try:
        scheduled_sync = await asyncio.to_thread(install_scheduled_sync, payload.time)
    except WindowsSchedulerError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    next_run = f"; next run {scheduled_sync.next_run_time}" if scheduled_sync.next_run_time else ""
    return BaseResponse(status="ok", message=f"Daily MP3 sync installed at {payload.time}{next_run}")


@app.get("/task/{task_id}/status", response_model=TaskStatusResponse)
@limiter.limit("120/minute")
async def task_status(
    request: Request,
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> TaskStatusResponse:
    task = await asyncio.to_thread(get_task_status, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Task does not belong to this user")
    return TaskStatusResponse(**task.to_dict())


@app.get("/tasks/active", response_model=list[TaskStatusResponse])
@limiter.limit("120/minute")
async def active_tasks(
    request: Request,
    playlist_id: int | None = None,
    current_user: User = Depends(get_current_user),
) -> list[TaskStatusResponse]:
    tasks = await asyncio.to_thread(list_task_statuses, owner_id=current_user.id, playlist_id=playlist_id)
    return [TaskStatusResponse(**task.to_dict()) for task in tasks]


@app.get("/task/{task_id}/stream")
@limiter.limit("30/minute")
async def task_stream(
    request: Request,
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    existing_task = await asyncio.to_thread(get_task_status, task_id)
    if not existing_task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if existing_task.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Task does not belong to this user")

    async def events():
        while True:
            task = await asyncio.to_thread(get_task_status, task_id)
            if not task:
                yield "event: error\ndata: {\"error\":\"Task not found\"}\n\n"
                return
            yield f"data: {json.dumps(task.to_dict())}\n\n"
            if task.status in {"done", "failed"}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")
