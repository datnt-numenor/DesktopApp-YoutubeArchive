from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import settings


class Base(DeclarativeBase):
    pass


_is_postgres = settings.database_url.startswith("postgresql")

_engine_kwargs: dict = {"echo": False}
if _is_postgres:
    _engine_kwargs.update(
        pool_size=5,
        pool_recycle=300,
        # Neon serverless PostgreSQL requires statement_cache_size=0 with asyncpg
        # to avoid "prepared statement already exists" errors through their pooler.
        connect_args={"statement_cache_size": 0},
    )

engine = create_async_engine(settings.database_url, **_engine_kwargs)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    import models  # noqa: F401

    if settings.environment == "production":
        return

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            # Compatibility path for local SQLite databases created before Alembic.
            result = await conn.execute(text("PRAGMA table_info(videos)"))
            video_columns = {row[1] for row in result.fetchall()}
            if "thumbnail_local_path" not in video_columns:
                await conn.execute(text("ALTER TABLE videos ADD COLUMN thumbnail_local_path TEXT"))
            if "source_type" not in video_columns:
                await conn.execute(
                    text("ALTER TABLE videos ADD COLUMN source_type VARCHAR(16) NOT NULL DEFAULT 'YOUTUBE'")
                )

            result = await conn.execute(text("PRAGMA table_info(playlists)"))
            playlist_columns = {row[1] for row in result.fetchall()}
            if "source_type" not in playlist_columns:
                await conn.execute(
                    text("ALTER TABLE playlists ADD COLUMN source_type VARCHAR(16) NOT NULL DEFAULT 'YOUTUBE'")
                )
                await conn.execute(
                    text(
                        "UPDATE users SET playlist_quota = 50, "
                        "storage_quota_bytes = 20 * 1024 * 1024 * 1024"
                    )
                )

            result = await conn.execute(text("PRAGMA table_info(playlist_video)"))
            existing_columns = {row[1] for row in result.fetchall()}
            if "download_error" not in existing_columns:
                await conn.execute(text("ALTER TABLE playlist_video ADD COLUMN download_error TEXT"))
            if "last_download_attempt_at" not in existing_columns:
                await conn.execute(text("ALTER TABLE playlist_video ADD COLUMN last_download_attempt_at DATETIME"))
            if "youtube_unavailable_at" not in existing_columns:
                await conn.execute(text("ALTER TABLE playlist_video ADD COLUMN youtube_unavailable_at DATETIME"))
                await conn.execute(
                    text(
                        """
                        UPDATE playlist_video
                        SET youtube_unavailable_at = CURRENT_TIMESTAMP
                        WHERE video_id IN (SELECT id FROM videos WHERE status = 'DELETED_ON_YT')
                        """
                    )
                )
            if "hidden_at" not in existing_columns:
                await conn.execute(text("ALTER TABLE playlist_video ADD COLUMN hidden_at DATETIME"))

            result = await conn.execute(text("PRAGMA table_info(users)"))
            user_columns = {row[1] for row in result.fetchall()}
            if "failed_login_count" not in user_columns:
                await conn.execute(text("ALTER TABLE users ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0"))
            if "locked_until" not in user_columns:
                await conn.execute(text("ALTER TABLE users ADD COLUMN locked_until DATETIME"))
