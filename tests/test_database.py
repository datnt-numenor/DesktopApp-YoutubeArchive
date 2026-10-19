from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

import database


async def test_init_db_adds_sync_state_columns_to_legacy_playlist_video(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "legacy.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    legacy_engine = create_async_engine(database_url)

    async with legacy_engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE playlist_video (
                    playlist_id INTEGER NOT NULL,
                    video_id INTEGER NOT NULL,
                    PRIMARY KEY (playlist_id, video_id)
                )
                """
            )
        )

    monkeypatch.setattr(database, "engine", legacy_engine)
    monkeypatch.setattr(database.settings, "database_url", database_url)

    await database.init_db()

    async with legacy_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(playlist_video)"))
        columns = {row[1] for row in result.fetchall()}

    await legacy_engine.dispose()
    assert "hidden_at" in columns
    assert "removed_from_youtube_playlist_at" in columns


async def test_sqlite_foreign_keys_detach_history_from_deleted_playlist(tmp_path: Path, monkeypatch) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'fk.db').as_posix()}"
    test_engine = create_async_engine(database_url)
    event.listen(test_engine.sync_engine, "connect", database.configure_sqlite_connection)
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database.settings, "database_url", database_url)
    await database.init_db()

    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, hashed_password, is_active, is_verified, is_superuser, "
                "failed_login_count, storage_used_bytes, playlist_quota, storage_quota_bytes) "
                "VALUES ('u1', 'fk@example.com', '', 1, 1, 1, 0, 0, 50, 0)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO playlists (id, owner_id, yt_playlist_id, title, url, source_type, auto_sync) "
                "VALUES (1, 'u1', 'A', 'Old', 'x', 'YOUTUBE', 1)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO sync_runs (owner_id, playlist_id, playlist_title, source, status, "
                "downloaded_count, failed_count, unavailable_count) "
                "VALUES ('u1', 1, 'Old', 'SCHEDULED', 'COMPLETED', 0, 0, 0)"
            )
        )
        await conn.execute(text("DELETE FROM playlists WHERE id = 1"))

    async with test_engine.connect() as conn:
        playlist_id = (await conn.execute(text("SELECT playlist_id FROM sync_runs"))).scalar_one()
        journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()

    await test_engine.dispose()
    assert playlist_id is None
    assert journal_mode == "wal"


async def test_init_db_detaches_history_left_by_reused_playlist_ids(tmp_path: Path, monkeypatch) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'legacy-history.db').as_posix()}"
    legacy_engine = create_async_engine(database_url)
    monkeypatch.setattr(database, "engine", legacy_engine)
    monkeypatch.setattr(database.settings, "database_url", database_url)
    await database.init_db()

    # Simulate the old behaviour: no foreign keys, so history kept the id of a deleted playlist
    # and a newer playlist later received the same id.
    async with legacy_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, hashed_password, is_active, is_verified, is_superuser, "
                "failed_login_count, storage_used_bytes, playlist_quota, storage_quota_bytes) "
                "VALUES ('u1', 'legacy@example.com', '', 1, 1, 1, 0, 0, 50, 0)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO sync_runs (owner_id, playlist_id, playlist_title, source, status, "
                "downloaded_count, failed_count, unavailable_count, started_at) VALUES "
                "('u1', 1, 'Deleted', 'SCHEDULED', 'COMPLETED', 0, 0, 0, '2026-01-01 00:00:00'), "
                "('u1', 2, 'Missing', 'SCHEDULED', 'COMPLETED', 0, 0, 0, '2026-01-01 00:00:00'), "
                "('u1', 1, 'New', 'SCHEDULED', 'COMPLETED', 0, 0, 0, '2026-03-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO playlists (id, owner_id, yt_playlist_id, title, url, source_type, auto_sync, created_at) "
                "VALUES (1, 'u1', 'B', 'New', 'y', 'YOUTUBE', 1, '2026-02-01 00:00:00')"
            )
        )

    await database.init_db()

    async with legacy_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT playlist_title, playlist_id FROM sync_runs"))).all()

    await legacy_engine.dispose()
    assert dict(rows) == {"Deleted": None, "Missing": None, "New": 1}
