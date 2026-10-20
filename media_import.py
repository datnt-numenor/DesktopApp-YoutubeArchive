from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from fastapi import HTTPException, Request, status


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class ImportedMp3:
    path: Path
    digest: str
    size: int
    title: str
    artist: str
    duration: int
    is_new_file: bool


def _safe_stem(filename: str) -> str:
    stem = Path(filename).stem.strip()
    cleaned = re.sub(r"[\x00-\x1f]+", " ", stem).strip()
    return cleaned[:500] or "Imported song"


def _probe_mp3(path: Path, fallback_title: str) -> tuple[str, str, int]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration:format_tags=title,artist",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ffprobe was not found; install FFmpeg and add it to PATH",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Could not read the MP3 file") from exc
    if result.returncode != 0:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not a valid MP3")
    try:
        payload = json.loads(result.stdout or "{}")
        format_data = payload.get("format") or {}
        format_name = str(format_data.get("format_name") or "").lower()
        if "mp3" not in format_name:
            raise ValueError("not mp3")
        tags = {str(key).lower(): value for key, value in (format_data.get("tags") or {}).items()}
        title = str(tags.get("title") or fallback_title).strip() or fallback_title
        artist = str(tags.get("artist") or "").strip()
        duration = max(0, round(float(format_data.get("duration") or 0)))
        return title[:500], artist[:255], duration
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not a valid MP3") from exc


async def receive_mp3_upload(request: Request, downloads_dir: Path, max_file_bytes: int) -> ImportedMp3:
    encoded_name = request.headers.get("x-file-name") or "imported.mp3"
    filename = unquote(encoded_name)
    if Path(filename).suffix.lower() != ".mp3":
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Only .mp3 files are supported")

    temporary_dir = downloads_dir / ".uploads"
    temporary_dir.mkdir(parents=True, exist_ok=True)
    temporary_path = temporary_dir / f"{uuid.uuid4().hex}.upload"
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary_path.open("wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_file_bytes:
                    raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="MP3 exceeds storage quota")
                digest.update(chunk)
                output.write(chunk)
        if size == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MP3 file is empty")

        title, artist, duration = await asyncio.to_thread(_probe_mp3, temporary_path, _safe_stem(filename))
        digest_hex = digest.hexdigest()
        imported_dir = downloads_dir / "imported"
        imported_dir.mkdir(parents=True, exist_ok=True)
        destination = imported_dir / f"{digest_hex}.mp3"
        is_new_file = not destination.exists()
        if is_new_file:
            temporary_path.replace(destination)
        else:
            temporary_path.unlink(missing_ok=True)
        return ImportedMp3(
            path=destination,
            digest=digest_hex,
            size=destination.stat().st_size,
            title=title,
            artist=artist,
            duration=duration,
            is_new_file=is_new_file,
        )
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
