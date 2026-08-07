import pytest

import worker


class FakeEngine:
    def __init__(self) -> None:
        self.dispose_count = 0

    async def dispose(self) -> None:
        self.dispose_count += 1


async def test_worker_job_disposes_engine_after_success(monkeypatch) -> None:
    calls = []
    fake_engine = FakeEngine()

    async def fake_sync(playlist_id: int, owner_id: str, format_: str, task_id: str) -> None:
        calls.append((playlist_id, owner_id, format_, task_id))

    monkeypatch.setattr(worker, "sync_playlist_by_id", fake_sync)
    monkeypatch.setattr(worker, "engine", fake_engine)

    await worker.run_sync_playlist_job(5, "owner-1", "mp3", "task-1")

    assert calls == [(5, "owner-1", "mp3", "task-1")]
    assert fake_engine.dispose_count == 1


async def test_worker_job_disposes_engine_after_failure(monkeypatch) -> None:
    fake_engine = FakeEngine()

    async def fake_sync(*_args) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "sync_playlist_by_id", fake_sync)
    monkeypatch.setattr(worker, "engine", fake_engine)

    with pytest.raises(RuntimeError, match="boom"):
        await worker.run_sync_playlist_job(5, "owner-1", "mp3", "task-1")

    assert fake_engine.dispose_count == 1


async def test_metadata_import_worker_job_disposes_engine_after_success(monkeypatch) -> None:
    calls = []
    fake_engine = FakeEngine()

    async def fake_import(playlist_id: int, owner_id: str, task_id: str) -> None:
        calls.append((playlist_id, owner_id, task_id))

    monkeypatch.setattr(worker, "import_playlist_metadata_by_id", fake_import)
    monkeypatch.setattr(worker, "engine", fake_engine)

    await worker.run_import_playlist_metadata_job(5, "owner-1", "task-1")

    assert calls == [(5, "owner-1", "task-1")]
    assert fake_engine.dispose_count == 1
