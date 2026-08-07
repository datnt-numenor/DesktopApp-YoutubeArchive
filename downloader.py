import asyncio
import random
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import yt_dlp

from config import settings


MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024
UNAVAILABLE_AVAILABILITIES = {"private", "premium_only", "subscriber_only", "needs_auth"}
UNAVAILABLE_TITLES = {"[deleted video]", "[private video]", "deleted video", "private video"}


class DownloaderError(RuntimeError):
    pass


class YtDlpLogCollector:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def message(self) -> str:
        if self.errors:
            return self.errors[-1]
        if self.warnings:
            return self.warnings[-1]
        return "yt-dlp failed"


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


def _extract_playlist_sync(url: str) -> dict[str, Any]:
    playlist_url = canonicalize_playlist_url(url)
    options = {
        "extract_flat": True,
        "ignoreerrors": True,
        "quiet": True,
        "skip_download": True,
        "noplaylist": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

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
    options = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
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


def _cleanup_temporary_downloads(video_id: str, output_dir: Path, keep_path: Path | None = None) -> None:
    keep_resolved = keep_path.resolve() if keep_path else None
    temporary_suffixes = {".part", ".ytdl", ".temp", ".tmp"}

    for path in output_dir.glob(f"{video_id}.*"):
        if not path.is_file():
            continue
        if keep_resolved and path.resolve() == keep_resolved:
            continue

        should_delete = path.name.endswith(".part") or path.suffix.lower() in temporary_suffixes
        if keep_path and keep_path.suffix.lower() == ".mp3":
            should_delete = should_delete or path.suffix.lower() in {".webm", ".m4a", ".opus"}

        if should_delete:
            path.unlink(missing_ok=True)


def _download_video_sync(video_id: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_download = _find_downloaded_file(video_id, output_dir)
    if existing_download:
        _cleanup_temporary_downloads(video_id, output_dir, existing_download)
        return existing_download

    outtmpl = str(output_dir / "%(id)s.%(ext)s")
    url = f"https://www.youtube.com/watch?v={video_id}"
    log_collector = YtDlpLogCollector()

    options: dict[str, Any] = {
        "ignoreerrors": True,
        "quiet": False,
        "no_warnings": False,
        "logger": log_collector,
        "outtmpl": outtmpl,
        "restrictfilenames": True,
        "sleep_interval": random.randint(settings.yt_sleep_min_seconds, settings.yt_sleep_max_seconds),
        "max_sleep_interval": settings.yt_sleep_max_seconds,
    }
    options.update(
        {
            "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    )

    before = set(output_dir.glob(f"{video_id}.*"))
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.download([url])

        downloaded_file = _find_downloaded_file(video_id, output_dir)
        if downloaded_file:
            _cleanup_temporary_downloads(video_id, output_dir, downloaded_file)
            return downloaded_file

        if result not in (0, None):
            raise DownloaderError(log_collector.message())

        after = set(output_dir.glob(f"{video_id}.*"))
        created = sorted(
            [
                path
                for path in after - before
                if path.is_file() and not path.name.endswith(".part")
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if created:
            _cleanup_temporary_downloads(video_id, output_dir, created[0])
            return created[0]

        raise DownloaderError(f"No downloaded file found for video {video_id}")
    except Exception:
        _cleanup_temporary_downloads(video_id, output_dir)
        raise


async def download_video(video_id: str, output_dir: Path) -> Path:
    return await asyncio.to_thread(_download_video_sync, video_id, output_dir)
