from pathlib import Path

import pytest

from sync_lock import SyncAlreadyRunning, sync_process_lock


def test_sync_process_lock_rejects_second_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "sync.lock"

    with sync_process_lock(lock_path):
        with pytest.raises(SyncAlreadyRunning):
            with sync_process_lock(lock_path):
                pass


def test_sync_process_lock_can_be_reacquired_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "sync.lock"

    with sync_process_lock(lock_path):
        pass
    with sync_process_lock(lock_path):
        pass
