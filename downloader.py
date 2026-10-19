import asyncio
import json
import logging
import random
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from config import settings
from managed_ytdlp import CREATE_NO_WINDOW, ytdlp_base_command


MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024
UNAVAILABLE_AVAILABILITIES = {"private", "premium_only", "subscriber_only", "needs_auth"}
UNAVAILABLE_TITLES = {"[deleted video]", "[private video]", "deleted video", "private video"}
DOWNLOAD_RETRY_DELAYS_SECONDS = (10, 30, 90)
RETRYABLE_DOWNLOAD_MARKERS = (
    "http error 403",
    "http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "remote end closed connection",
    "temporary failure",
)
logger = logging.getLogger(__name__)


class DownloaderError(RuntimeError):
    pass


def canonicalize_playlist_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    playlist_ids = query.get("list")
    if not playlist_ids:
        return url

    playlist_id = playlist_ids[0]
    if parsed.netloc.endswith("youtube.com") or parsed.netloc.endswith("youtu.be"):
        return urlunparse(("https", "www.youtube.com", "/playlist", "", urlencode({"list": playlist_id}), ""))
    return url


def _require_web_url(url: str) -> str:
    """Reject anything yt-dlp could interpret as an option or a local file."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DownloaderError("Only http(s) YouTube URLs are supported.")
    return url.strip()


def extract_playlist_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    playlist_ids = parse_qs(parsed.query).get("list")
    if not playlist_ids or not playlist_ids[0].strip():
        raise DownloaderError("Use a public YouTube playlist URL that includes a list= playlist id.")
    return playlist_ids[0].strip()


def _best_thumbnail(entry: dict[str, Any]) -> str | None:
    thumbnails = entry.get("thumbnails") or []
    if thumbnails:
        return thumbnails[-1].get("url")
    return entry.get("thumbnail")


def _normalize_video(entry: dict[str, Any]) -> dict[str, Any] | None:
    video_id = entry.get("id") or entry.get("url")
    if not video_id:
        return None
    raw_title = str(entry.get("title") or "").strip()
    availability = str(entry.get("availability") or "").strip().lower()
    normalized_title = raw_title.lower()
    is_available = (
        bool(raw_title)
        and normalized_title not in UNAVAILABLE_TITLES
        and "video unavailable" not in normalized_title
        and "video is unavailable" not in normalized_title
    )
    if availability in UNAVAILABLE_AVAILABILITIES:
        is_available = False
    return {
        "yt_video_id": str(video_id),
        "title": raw_title or f"YouTube video {video_id}",
        "channel_name": entry.get("channel") or entry.get("uploader") or "",
        "duration": int(entry.get("duration") or 0),
        "thumbnail_url": _best_thumbnail(entry),
        "is_available_on_youtube": is_available,
    }


def _cache_thumbnail_sync(video_id: str, thumbnail_url: str, output_dir: Path) -> Path | None:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = next((path for path in output_dir.glob(f"{video_id}.*") if path.is_file()), None)
    if existing:
        return existing

    request = Request(thumbnail_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=15) as response:
            content_type = (response.headers.get_content_type() or "").lower()
            if not content_type.startswith("image/"):
                return None
            payload = response.read(MAX_THUMBNAIL_BYTES + 1)
    except (OSError, ValueError):
        return None

    if not payload or len(payload) > MAX_THUMBNAIL_BYTES:
        return None
    extension = {"image/png": ".png", "image/webp": ".webp"}.get(content_type, ".jpg")
    destination = output_dir / f"{video_id}{extension}"
    temporary = output_dir / f".{video_id}{extension}.tmp"
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return destination


async def cache_thumbnail(video_id: str, thumbnail_url: str, output_dir: Path) -> Path | None:
    return await asyncio.to_thread(_cache_thumbnail_sync, video_id, thumbnail_url, output_dir)


def _command_error(result: subprocess.CompletedProcess[str]) -> str:
    lines = [line.strip() for line in f"{result.stderr}\n{result.stdout}".splitlines() if line.strip()]
    errors = [line for line in lines if line.lower().startswith("error:")]
    return (errors[-1] if errors else lines[-1] if lines else "yt-dlp failed")[:2000]


def _run_ytdlp(arguments: list[str], *, force_update: bool = False, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    command = [
        *ytdlp_base_command(force_update=force_update),
        "--no-config",
        "--encoding",
        "utf-8",
        *arguments,
    ]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloaderError(f"yt-dlp timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise DownloaderError(f"Could not start yt-dlp: {exc}") from exc


def _run_ytdlp_json(arguments: list[str]) -> dict[str, Any]:
    result = _run_ytdlp(["--quiet", "--skip-download", "--dump-single-json", *arguments], timeout=600)
    if result.returncode != 0:
        raise DownloaderError(_command_error(result))
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DownloaderError("yt-dlp returned invalid metadata") from exc
    if not isinstance(payload, dict):
        raise DownloaderError("yt-dlp did not return metadata")
    return payload


def _extract_playlist_sync(url: str) -> dict[str, Any]:
    playlist_url = _require_web_url(canonicalize_playlist_url(url))
    info = _run_ytdlp_json(["--flat-playlist", "--ignore-errors", "--yes-playlist", "--", playlist_url])

    if not info:
        raise DownloaderError("yt-dlp did not return playlist metadata")

    entries = [_normalize_video(entry) for entry in info.get("entries", []) if entry]
    videos = [entry for entry in entries if entry]
    if not videos:
        raise DownloaderError("No videos found. Use a public YouTube playlist URL, not a private or unavailable playlist.")

    return {
        "yt_playlist_id": str(info.get("id") or url),
        "title": info.get("title") or "Untitled playlist",
        "url": info.get("webpage_url") or playlist_url,
        "videos": videos,
    }


async def extract_playlist_metadata(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_extract_playlist_sync, url)


def _extract_video_sync(url: str) -> dict[str, Any]:
    info = _run_ytdlp_json(["--no-playlist", "--", _require_web_url(url)])
    if not info:
        raise DownloaderError("yt-dlp did not return video metadata")
    metadata = _normalize_video(info)
    if not metadata or not metadata.get("is_available_on_youtube", False):
        raise DownloaderError("Video is unavailable or private on YouTube")
    return metadata


async def extract_video_metadata(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_extract_video_sync, url)


def _find_downloaded_file(video_id: str, output_dir: Path) -> Path | None:
    preferred_ext = ".mp3"
    preferred = output_dir / f"{video_id}{preferred_ext}"
    if preferred.exists():
        return preferred

    candidates = [
        path
        for path in output_dir.glob(f"{video_id}.*")
        if path.is_file() and not path.name.endswith(".part") and path.suffix.lower() == preferred_ext
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def _cleanup_temporary_downloads(
    video_id: str,
    output_dir: Path,
    keep_path: Path | None = None,
    *,
    remove_source_audio: bool = False,
) -> None:
    keep_resolved = keep_path.resolve() if keep_path else None
    temporary_suffixes = {".part", ".ytdl", ".temp", ".tmp"}

    for path in output_dir.glob(f"{video_id}.*"):
        if not path.is_file():
            continue
        if keep_resolved and path.resolve() == keep_resolved:
            continue

        should_delete = path.name.endswith(".part") or path.suffix.lower() in temporary_suffixes
        if (keep_path and keep_path.suffix.lower() == ".mp3") or remove_source_audio:
            should_delete = should_delete or path.suffix.lower() in {".webm", ".m4a", ".opus"}

        if should_delete:
            path.unlink(missing_ok=True)


def _download_once(video_id: str, output_dir: Path, *, force_update: bool = False) -> Path:
    outtmpl = str(output_dir / "%(id)s.%(ext)s")
    url = f"https://www.youtube.com/watch?v={video_id}"
    sleep_interval = random.randint(settings.yt_sleep_min_seconds, settings.yt_sleep_max_seconds)
    result = _run_ytdlp(
        [
            "--no-playlist",
            "--no-progress",
            "--newline",
            "--restrict-filenames",
            "--retries",
            "2",
            "--fragment-retries",
            "2",
            "--sleep-interval",
            str(sleep_interval),
            "--max-sleep-interval",
            str(settings.yt_sleep_max_seconds),
            "--format",
            "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "192K",
            "--output",
            outtmpl,
            "--",
            url,
        ],
        force_update=force_update,
    )
    downloaded_file = _find_downloaded_file(video_id, output_dir)
    if downloaded_file:
        _cleanup_temporary_downloads(video_id, output_dir, downloaded_file)
        return downloaded_file
    raise DownloaderError(_command_error(result) if result.returncode else f"No downloaded file found for video {video_id}")


def _is_retryable_download_error(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in RETRYABLE_DOWNLOAD_MARKERS)


def _download_video_sync(video_id: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_download = _find_downloaded_file(video_id, output_dir)
    if existing_download:
        _cleanup_temporary_downloads(video_id, output_dir, existing_download)
        return existing_download

    attempts = len(DOWNLOAD_RETRY_DELAYS_SECONDS) + 1
    last_error: DownloaderError | None = None
    for attempt in range(attempts):
        try:
            return _download_once(video_id, output_dir, force_update=attempt == 1)
        except DownloaderError as exc:
            last_error = exc
            _cleanup_temporary_downloads(video_id, output_dir, remove_source_audio=True)
            if attempt >= attempts - 1 or not _is_retryable_download_error(str(exc)):
                raise
            delay = DOWNLOAD_RETRY_DELAYS_SECONDS[attempt]
            jittered_delay = delay * random.uniform(0.9, 1.1)
            logger.warning(
                "Retrying YouTube download %s after attempt %d/%d failed: %s",
                video_id,
                attempt + 1,
                attempts,
                exc,
            )
            time.sleep(jittered_delay)

    raise last_error or DownloaderError(f"No downloaded file found for video {video_id}")


async def download_video(video_id: str, output_dir: Path) -> Path:
    return await asyncio.to_thread(_download_video_sync, video_id, output_dir)
