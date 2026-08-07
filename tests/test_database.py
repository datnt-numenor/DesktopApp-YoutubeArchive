from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import database


async def test_init_db_adds_hidden_at_to_legacy_playlist_video(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr(database.settings, "environment", "development")

    await database.init_db()

    async with legacy_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(playlist_video)"))
        columns = {row[1] for row in result.fetchall()}

    await legacy_engine.dispose()
    assert "hidden_at" in columns
