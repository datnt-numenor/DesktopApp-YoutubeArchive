from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from config import LOCAL_DATA_DIR


SYNC_LOCK_PATH = LOCAL_DATA_DIR / "sync.lock"


class SyncAlreadyRunning(RuntimeError):
    pass


def _lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - desktop target is Windows
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - desktop target is Windows
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire_file_lock(path: Path) -> BinaryIO:
    """Lock the first byte of path without waiting; raise OSError when another handle holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    try:
        _lock(handle)
    except OSError:
        handle.close()
        raise
    return handle


@contextmanager
def sync_process_lock(path: Path = SYNC_LOCK_PATH) -> Iterator[None]:
    try:
        handle = acquire_file_lock(path)
    except OSError as exc:
        raise SyncAlreadyRunning("Another playlist sync is already running") from exc
    try:
        yield
    finally:
        _unlock(handle)
        handle.close()
