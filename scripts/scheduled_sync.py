from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crud  # noqa: E402
from config import LOCAL_DATA_DIR  # noqa: E402
from database import AsyncSessionLocal, engine, init_db  # noqa: E402
from models import PlaylistVideo, Video, VideoStatus  # noqa: E402
from scheduled_sync_state import (  # noqa: E402
    notification_is_throttled,
    read_scheduled_sync_state,
    show_windows_notification,
    utc_now_iso,
    write_scheduled_sync_state,
)
from sync_lock import SyncAlreadyRunning, sync_process_lock  # noqa: E402
from tasks import sync_playlist_by_id  # noqa: E402


LOG_DIR = LOCAL_DATA_DIR / "logs"
LOG_PATH = LOG_DIR / "scheduled-sync.log"
logger = logging.getLogger("scheduled_sync")


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)


def _write_state(state: dict[str, Any]) -> None:
    try:
        write_scheduled_sync_state(state)
    except OSError:
        logger.exception("Could not persist scheduled sync state")


def _notify(
    state: dict[str, Any],
    *,
    title: str,
    message: str,
    level: str,
    fingerprint: str,
    force: bool = False,
) -> None:
    if not force and notification_is_throttled(state, fingerprint):
        logger.info("Duplicate Windows notification suppressed: %s", fingerprint)
        return
    try:
        show_windows_notification(title, message, level)
    except Exception:  # noqa: BLE001 - notification failure must never fail synchronization
        logger.exception("Could not show Windows notification")
        return
    state["last_notification_at"] = utc_now_iso()
    state["last_notification_fingerprint"] = fingerprint
    _write_state(state)


def _start_state(previous: dict[str, Any], *, dry_run: bool) -> tuple[dict[str, Any], bool]:
    interrupted = previous.get("status") == "running"
    consecutive_failures = int(previous.get("consecutive_failures") or 0)
    if interrupted:
        consecutive_failures += 1
    state: dict[str, Any] = {
        "version": 1,
        "status": "running",
        "started_at": utc_now_iso(),
        "finished_at": None,
        "exit_code": None,
        "summary": "Dry run started" if dry_run else "Scheduled sync started",
        "error": None,
        "failures": 0,
        "playlists": [],
        "maintenance_warning": None,
        "consecutive_failures": consecutive_failures,
        "recovered_interrupted_count": 1 if interrupted else 0,
        "last_notification_at": previous.get("last_notification_at"),
        "last_notification_fingerprint": previous.get("last_notification_fingerprint"),
    }
    _write_state(state)
    return state, interrupted


def _finish_state(
    state: dict[str, Any],
    *,
    status: str,
    exit_code: int,
    summary: str,
    error: str | None = None,
    failures: int = 0,
    playlists: list[dict[str, Any]] | None = None,
) -> int:
    had_previous_failure = int(state.get("consecutive_failures") or 0) > 0
    state.update(
        {
            "status": status,
            "finished_at": utc_now_iso(),
            "exit_code": exit_code,
            "summary": summary,
            "error": error,
            "failures": failures,
            "playlists": playlists or [],
        }
    )
    if status in {"failed", "partial"}:
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    elif status == "success":
        state["consecutive_failures"] = 0
    _write_state(state)

    if status in {"failed", "partial"}:
        fingerprint = f"{status}:{error or summary}"[:500]
        _notify(
            state,
            title="YouTube Archive sync needs attention",
            message=f"{summary}. Open Settings or scheduled-sync.log for details.",
            level="Error" if status == "failed" else "Warning",
            fingerprint=fingerprint,
        )
    elif status == "success" and had_previous_failure:
        _notify(
            state,
            title="YouTube Archive sync recovered",
            message=summary,
            level="Info",
            fingerprint=f"recovered:{state['finished_at']}",
            force=True,
        )
    return exit_code


async def count_pending_download_errors(playlist_id: int) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.count(PlaylistVideo.video_id))
            .join(PlaylistVideo.video)
            .where(
                PlaylistVideo.playlist_id == playlist_id,
                PlaylistVideo.hidden_at.is_(None),
                PlaylistVideo.local_file_path.is_(None),
                PlaylistVideo.download_error.is_not(None),
                Video.status != VideoStatus.DELETED_ON_YT,
            )
        )
        return int(result.scalar_one())


async def run_scheduled_sync(*, dry_run: bool = False) -> int:
    previous = read_scheduled_sync_state()
    state, interrupted = _start_state(previous, dry_run=dry_run)
    if interrupted:
        logger.warning("The previous scheduled sync stopped before recording a final result")
        _notify(
            state,
            title="YouTube Archive sync was interrupted",
            message="The previous run did not finish. Recovery is running now.",
            level="Warning",
            fingerprint="interrupted",
        )

    playlist_results: list[dict[str, Any]] = []
    try:
        await init_db()
        async with AsyncSessionLocal() as session:
            try:
                recovered = await crud.cleanup_sync_history(session)
                state["recovered_interrupted_count"] += recovered
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort maintenance
                await session.rollback()
                state["maintenance_warning"] = f"{type(exc).__name__}: {exc}"
                logger.exception("Sync history cleanup failed; continuing with scheduled sync")
            playlists = await crud.list_auto_sync_playlists(session)

        logger.info("Scheduled MP3 sync started; %d playlist(s) enabled", len(playlists))
        if dry_run:
            for playlist in playlists:
                logger.info("Dry run: %s (%s)", playlist.title, playlist.id)
            return _finish_state(
                state,
                status="skipped",
                exit_code=0,
                summary=f"Dry run completed; {len(playlists)} playlist(s) eligible",
            )

        failures = 0
        try:
            with sync_process_lock():
                for playlist in playlists:
                    result: dict[str, Any] = {
                        "id": playlist.id,
                        "title": playlist.title,
                        "status": "success",
                        "pending_errors": 0,
                        "error": None,
                    }
                    try:
                        logger.info("Syncing playlist: %s (%s)", playlist.title, playlist.id)
                        await sync_playlist_by_id(
                            playlist.id,
                            playlist.owner_id,
                            "mp3",
                            acquire_process_lock=False,
                            history_source="scheduled",
                        )
                        pending_errors = await count_pending_download_errors(playlist.id)
                        result["pending_errors"] = pending_errors
                        if pending_errors:
                            failures += 1
                            result["status"] = "partial"
                            logger.warning(
                                "Playlist completed with %d pending download error(s): %s",
                                pending_errors,
                                playlist.title,
                            )
                        else:
                            logger.info("Playlist completed: %s", playlist.title)
                    except Exception as exc:  # noqa: BLE001 - continue with remaining playlists
                        failures += 1
                        result["status"] = "failed"
                        result["error"] = f"{type(exc).__name__}: {exc}"
                        logger.exception("Playlist failed: %s", playlist.title)
                    playlist_results.append(result)
        except SyncAlreadyRunning:
            logger.warning("Scheduled sync skipped because another sync is already running")
            return _finish_state(
                state,
                status="skipped",
                exit_code=2,
                summary="Another sync is already running; Task Scheduler may retry later",
                playlists=playlist_results,
            )

        status = "success"
        if failures:
            status = "failed" if failures == len(playlists) else "partial"
        summary = f"Scheduled MP3 sync finished; {len(playlists) - failures} succeeded, {failures} failed"
        logger.info(summary)
        return _finish_state(
            state,
            status=status,
            exit_code=1 if failures else 0,
            summary=summary,
            failures=failures,
            playlists=playlist_results,
        )
    except Exception as exc:  # noqa: BLE001 - pythonw has no visible stderr
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("Scheduled sync failed before completion")
        return _finish_state(
            state,
            status="failed",
            exit_code=1,
            summary="Scheduled sync failed before playlists could complete",
            error=error,
            playlists=playlist_results,
        )
    finally:
        try:
            await engine.dispose()
        except Exception:  # noqa: BLE001 - state and original result remain authoritative
            logger.exception("Could not dispose the scheduled sync database engine")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync all auto-sync playlists and download new MP3 files.")
    parser.add_argument("--dry-run", action="store_true", help="List eligible playlists without contacting YouTube.")
    args = parser.parse_args()
    try:
        configure_logging()
        exit_code = asyncio.run(run_scheduled_sync(dry_run=args.dry_run))
    except Exception as exc:  # noqa: BLE001 - last-resort handling for pythonw
        logging.basicConfig(level=logging.ERROR)
        logger.exception("Unhandled scheduled sync runner failure")
        state, _interrupted = _start_state(read_scheduled_sync_state(), dry_run=args.dry_run)
        exit_code = _finish_state(
            state,
            status="failed",
            exit_code=1,
            summary="Scheduled sync runner could not start",
            error=f"{type(exc).__name__}: {exc}",
        )
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
